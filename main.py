"""
コモディティニュース自動通知Bot (v3: 鮮度フィルタ付き)
- Google News RSSから最新ニュースを取得
- 24時間以内の記事のみを対象にする（鮮度フィルタ）
- Claude Haikuで要約・重要度判定
- yfinanceで現在価格を取得
- 重要度3以上のニュースをDiscordに通知（価格情報付き）
"""
import os
import json
import hashlib
import feedparser
import requests
from datetime import datetime, timezone, timedelta
from time import mktime
from anthropic import Anthropic
from prices import get_price_for_category, format_price_line, CATEGORY_EMOJI

# --- 設定 ---
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
SEEN_FILE = "seen.json"
IMPORTANCE_THRESHOLD = 3  # この重要度以上だけ通知（1-5）
MAX_ARTICLES_PER_FEED = 10
MAX_AGE_HOURS = 24  # 何時間以内の記事を「速報」として扱うか

# Google News RSS に when:1d を付けて24時間以内に絞る
# "when:1d" = 1日以内, "when:1h" = 1時間以内, "when:7d" = 1週間以内
FRESHNESS_FILTER = "when:1d"

FEEDS = {
    "小麦": f"https://news.google.com/rss/search?q=(wheat+price+OR+%E5%B0%8F%E9%BA%A6)+{FRESHNESS_FILTER}&hl=ja&gl=JP&ceid=JP:ja",
    "金": f"https://news.google.com/rss/search?q=(gold+price+OR+%E9%87%91%E7%9B%B8%E5%A0%B4)+{FRESHNESS_FILTER}&hl=ja&gl=JP&ceid=JP:ja",
    "原油": f"https://news.google.com/rss/search?q=(crude+oil+OR+%E5%8E%9F%E6%B2%B9%E4%BE%A1%E6%A0%BC)+{FRESHNESS_FILTER}&hl=ja&gl=JP&ceid=JP:ja",
    "トウモロコシ": f"https://news.google.com/rss/search?q=(corn+futures+OR+%E3%83%88%E3%82%A6%E3%83%A2%E3%83%AD%E3%82%B3%E3%82%B7)+{FRESHNESS_FILTER}&hl=ja&gl=JP&ceid=JP:ja",
    "大豆": f"https://news.google.com/rss/search?q=(soybean+futures+OR+%E5%A4%A7%E8%B1%86%E5%85%88%E7%89%A9)+{FRESHNESS_FILTER}&hl=ja&gl=JP&ceid=JP:ja",
    "銅": f"https://news.google.com/rss/search?q=(copper+price+OR+%E9%8A%85%E7%9B%B8%E5%A0%B4)+{FRESHNESS_FILTER}&hl=ja&gl=JP&ceid=JP:ja",
    "コモディティ全般": f"https://news.google.com/rss/search?q=(commodity+market+OR+%E5%95%86%E5%93%81%E5%B8%82%E5%A0%B4)+{FRESHNESS_FILTER}&hl=ja&gl=JP&ceid=JP:ja",
}


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    seen_list = list(seen)[-1000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen_list, f, ensure_ascii=False)


def article_id(entry):
    return hashlib.md5(entry.link.encode()).hexdigest()


def is_fresh(entry, max_age_hours=MAX_AGE_HOURS):
    """
    記事が指定時間以内のものかチェック。
    published_parsed が取得できない場合は True（安全側）を返す。
    """
    published = entry.get("published_parsed")
    if not published:
        return True  # 日付不明なら一応通す

    try:
        # published_parsed は time.struct_time（UTC）
        article_time = datetime.fromtimestamp(mktime(published), tz=timezone.utc)
        now = datetime.now(timezone.utc)
        age = now - article_time
        return age <= timedelta(hours=max_age_hours)
    except Exception:
        return True  # パースエラー時も一応通す


def format_age(entry):
    """記事の経過時間を人間向けフォーマット"""
    published = entry.get("published_parsed")
    if not published:
        return "?"
    try:
        article_time = datetime.fromtimestamp(mktime(published), tz=timezone.utc)
        now = datetime.now(timezone.utc)
        age = now - article_time
        hours = age.total_seconds() / 3600
        if hours < 1:
            return f"{int(age.total_seconds() / 60)}分前"
        elif hours < 24:
            return f"{int(hours)}時間前"
        else:
            return f"{int(hours / 24)}日前"
    except Exception:
        return "?"


def analyze_with_claude(client, category, title, summary):
    """Claude Haikuでニュースを分析"""
    prompt = f"""以下のニュースをコモディティ投資家の視点で分析してください。

カテゴリ: {category}
タイトル: {title}
概要: {summary}

以下のJSON形式のみで返答してください（前後に余計な文字を入れないこと）：
{{
  "impact": "上昇" または "下落" または "中立",
  "importance": 1-5の整数,
  "summary_jp": "日本語で3行以内の要約",
  "reason": "価格への影響理由を1行で"
}}

重要度の基準:
1 = 市場に影響なし・雑ニュース
2 = 軽微な話題
3 = 注目すべきニュース
4 = 重要、ポジション検討レベル
5 = 緊急・市場大変動の可能性"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except (json.JSONDecodeError, Exception) as e:
        print(f"  ⚠️ 分析エラー: {e}")
        return None


def send_to_discord(category, entry, analysis, price_info=None):
    """Discord Webhookで通知送信（価格情報付き）"""
    impact_emoji = {
        "上昇": "📈",
        "下落": "📉",
        "中立": "➡️",
    }.get(analysis["impact"], "❓")

    importance_stars = "⭐" * analysis["importance"]
    cat_emoji = CATEGORY_EMOJI.get(category, "🌾")
    age_str = format_age(entry)

    # 価格情報ブロック
    price_block = ""
    if price_info:
        price_line = format_price_line(category, price_info)
        if price_line:
            price_block = f"\n💰 **現在価格**\n{price_line}\n"
            hi = price_info["high_5d"]
            lo = price_info["low_5d"]
            if hi >= 1000:
                price_block += f"   5日レンジ: {lo:,.2f} 〜 {hi:,.2f}\n"
            else:
                price_block += f"   5日レンジ: {lo:.2f} 〜 {hi:.2f}\n"

    content = (
        f"{cat_emoji} **{category}ニュース速報** {importance_stars}\n"
        f"🕐 {age_str}の記事\n\n"
        f"📰 **{entry.title}**\n"
        f"{impact_emoji} 影響: {analysis['impact']}\n\n"
        f"📝 {analysis['summary_jp']}\n"
        f"💡 {analysis['reason']}\n"
        f"{price_block}\n"
        f"🔗 {entry.link}"
    )

    if len(content) > 1900:
        content = content[:1900] + "..."

    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ⚠️ Discord送信エラー: {e}")


def main():
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    seen = load_seen()
    new_count = 0
    skipped_old = 0
    notified_count = 0

    # カテゴリごとに価格情報をキャッシュ（APIコール削減）
    price_cache = {}

    for category, url in FEEDS.items():
        print(f"📡 {category} をチェック中...")
        feed = feedparser.parse(url)

        for entry in feed.entries[:MAX_ARTICLES_PER_FEED]:
            aid = article_id(entry)
            if aid in seen:
                continue

            seen.add(aid)

            # ★新機能：鮮度フィルタ
            if not is_fresh(entry):
                skipped_old += 1
                print(f"  ⏭ スキップ（古い記事 {format_age(entry)}）: {entry.title[:40]}...")
                continue

            new_count += 1
            summary = entry.get("summary", "")[:500]
            analysis = analyze_with_claude(client, category, entry.title, summary)

            if analysis is None:
                continue

            importance = analysis.get("importance", 0)
            age_str = format_age(entry)
            print(f"  [{importance}/5] ({age_str}) {entry.title[:45]}...")

            if importance >= IMPORTANCE_THRESHOLD:
                # 価格情報を取得（キャッシュ利用）
                if category not in price_cache:
                    print(f"  💰 {category} の価格を取得中...")
                    price_cache[category] = get_price_for_category(category)

                send_to_discord(category, entry, analysis, price_cache[category])
                notified_count += 1

    save_seen(seen)
    print(f"\n✅ 完了: {new_count}件チェック、{skipped_old}件スキップ（古い）、{notified_count}件通知")


if __name__ == "__main__":
    main()
