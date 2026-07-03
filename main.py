"""
コモディティニュース自動通知Bot (v7: 検索精度向上版)
- 検索クエリをダブルクォートで完全一致化
- 関連性の低いニュースを排除
- カテゴリ別Discordチャンネルに振り分けて通知
"""
import os
import json
import hashlib
import sys
import feedparser
import requests
from urllib.parse import quote
from datetime import datetime, timezone, timedelta
from time import mktime
from anthropic import Anthropic
from prices import get_price_for_category, format_price_line, CATEGORY_EMOJI

# --- 設定 ---
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SEEN_FILE = "seen.json"
IMPORTANCE_THRESHOLD = 3
CRITICAL_THRESHOLD = 5
MAX_ARTICLES_PER_FEED = 10
MAX_AGE_HOURS = 24

# カテゴリ → Webhook URL のマッピング
WEBHOOK_URLS = {
    "小麦": os.environ.get("WEBHOOK_WHEAT", ""),
    "金": os.environ.get("WEBHOOK_GOLD", ""),
    "原油": os.environ.get("WEBHOOK_OIL", ""),
    "トウモロコシ": os.environ.get("WEBHOOK_CORN", ""),
    "大豆": os.environ.get("WEBHOOK_SOYBEAN", ""),
    "銅": os.environ.get("WEBHOOK_COPPER", ""),
    "コモディティ全般": os.environ.get("WEBHOOK_OTHER", ""),
}
WEBHOOK_CRITICAL = os.environ.get("WEBHOOK_CRITICAL", "")


def make_url(query):
    """検索クエリからGoogle News RSS URLを生成"""
    encoded = quote(query)
    return f"https://news.google.com/rss/search?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"


# 検索クエリ（精度UP版）：ダブルクォートで完全一致、複数キーワードで網羅性UP
FEEDS = {
    "小麦": make_url('("wheat futures" OR "wheat price" OR 小麦先物 OR 小麦価格 OR 小麦相場) when:1d'),
    "金": make_url('("gold price" OR "gold futures" OR "spot gold" OR 金相場 OR 金先物 OR 金価格) when:1d'),
    "原油": make_url('("crude oil" OR "WTI crude" OR "Brent crude" OR 原油先物 OR 原油価格 OR WTI原油 OR 原油相場) when:1d'),
    "トウモロコシ": make_url('("corn futures" OR "corn price" OR トウモロコシ先物 OR トウモロコシ価格 OR コーン先物) when:1d'),
    "大豆": make_url('("soybean futures" OR "soybean price" OR 大豆先物 OR 大豆価格 OR 大豆相場) when:1d'),
    "銅": make_url('("copper price" OR "copper futures" OR "LME copper" OR LME銅 OR 銅相場 OR 銅価格) when:1d'),
    "コモディティ全般": make_url('("commodity market" OR "commodities market" OR コモディティ市場 OR 商品先物市場 OR 国際商品市況) when:1d'),
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


def _entry_iso(entry):
    """記事の published を UTC tz-aware ISO8601 文字列で返す。無ければ現在UTC。"""
    published = entry.get("published_parsed")
    if published:
        try:
            return datetime.fromtimestamp(mktime(published), tz=timezone.utc).isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


def save_news_state(items, path="news_state.json", keep_hours=72):
    """analyzer向けニュース状態を保存。既存JSONとマージし、title|timestampで重複排除、
    現在UTC - keep_hours より新しいレコードだけ残して書き戻す。"""
    existing = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    seen_keys = set()
    deduped = []
    for it in existing + items:
        key = f"{it.get('title', '')}|{it.get('timestamp', '')}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(it)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=keep_hours)
    kept = []
    for it in deduped:
        try:
            ts = datetime.fromisoformat(it["timestamp"])
        except Exception:
            continue
        if ts >= cutoff:
            kept.append(it)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=2)


def article_id(entry):
    return hashlib.md5(entry.link.encode()).hexdigest()


def is_fresh(entry, max_age_hours=MAX_AGE_HOURS):
    published = entry.get("published_parsed")
    if not published:
        return True
    try:
        article_time = datetime.fromtimestamp(mktime(published), tz=timezone.utc)
        now = datetime.now(timezone.utc)
        age = now - article_time
        return age <= timedelta(hours=max_age_hours)
    except Exception:
        return True


def format_age(entry):
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


def is_relevant(category, title, summary):
    """カテゴリと関連性のチェック（無関係なニュースを除外）"""
    text = (title + " " + summary).lower()

    # 各カテゴリのキーワード（これがあれば関連）
    relevance_keywords = {
        "小麦": ["wheat", "小麦", "grain", "穀物", "agriculture", "農産物", "harvest"],
        "金": ["gold", "金", "bullion", "precious metal", "貴金属", "fed", "frb",
                "interest rate", "金利", "inflation", "インフレ", "dollar", "ドル"],
        "原油": ["oil", "crude", "petroleum", "原油", "wti", "brent", "opec", "石油",
                 "barrel", "バレル", "energy", "エネルギー"],
        "トウモロコシ": ["corn", "トウモロコシ", "コーン", "maize", "ethanol", "農産物",
                          "harvest", "feed grain", "穀物"],
        "大豆": ["soybean", "soy", "大豆", "農産物", "soya", "harvest", "穀物",
                 "feed grain"],
        "銅": ["copper", "銅", "metal", "lme", "金属", "trump", "tariff", "関税",
               "industrial metal"],
        "コモディティ全般": ["commodity", "commodities", "コモディティ", "商品",
                              "資源", "raw material", "原材料"],
    }

    keywords = relevance_keywords.get(category, [])
    if not keywords:
        return True

    # 除外キーワード（明らかに無関係なもの）
    exclude_keywords = [
        "ladies", "ladeies", "tournament", "ゴルフ", "golf", "サッカー", "野球",
        "選手権", "大会結果", "予選通過",
    ]

    for kw in exclude_keywords:
        if kw in text:
            return False

    # 関連キーワードが含まれているか
    return any(kw in text for kw in keywords)


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
  "event_type": "scheduled" または "breaking" または "commentary",
  "surprise": "high" または "inline" または "low" または "unknown",
  "summary_jp": "日本語で3行以内の要約",
  "reason": "価格への影響理由を1行で"
}}

重要度の基準:
1 = 市場に影響なし・雑ニュース
2 = 軽微な話題
3 = 注目すべきニュース
4 = 重要、ポジション検討レベル
5 = 緊急・市場大変動の可能性

event_type の判定基準（ニュースの種類）:
- scheduled = WASDE/EIA週間在庫/FOMC/雇用統計など、予定された定期発表・経済指標
- breaking  = OPEC緊急会合・地政学イベント・事故・突発の供給ショックなど、予定外の速報
- commentary = 解説・観測・アナリスト見解・相場の振り返りなど、既に方向が織り込まれているもの

surprise の判定基準（予想比サプライズ度。価格への「予想外度」を厳しく判定すること）:
- "high": 明確に予想外で、市場が大きく動く/動いた場合のみ。具体的には
    ・価格の急変（目安±5%超）を伴う、または伴いそうな事象
    ・地政学的ショック（紛争勃発・主要供給国の突発的な供給途絶・海峡封鎖等）
    ・市場予想を大幅に外れた公式統計
  ※「2-3%程度の変動」「既に報じられた事象の続報」「予想の範囲内の動き」は high にしない
- "low": 軽微・既定路線。通常のボラティリティ範囲（±3%未満）の値動き、想定内のニュース
- "inline": 予想とほぼ一致（予定イベントが事前予想どおりだった等、明確に「予想通り」と判断できる場合）
- "unknown": 予想比が判断できない、サプライズ度が不明
迷う場合や予想比が不明な場合は "unknown" を選ぶこと。high は安易に選ばない。"""

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


def build_message(category, entry, analysis, price_info=None):
    """Discord通知メッセージを構築"""
    impact_emoji = {
        "上昇": "📈",
        "下落": "📉",
        "中立": "➡️",
    }.get(analysis["impact"], "❓")

    importance_stars = "⭐" * analysis["importance"]
    cat_emoji = CATEGORY_EMOJI.get(category, "🌾")
    age_str = format_age(entry)

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
    return content


def post_to_webhook(webhook_url, content, label=""):
    """Webhook URLにメッセージを送信"""
    if not webhook_url:
        print(f"  ⚠️ {label} のWebhook URLが未設定")
        return False
    try:
        resp = requests.post(webhook_url, json={"content": content}, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"  ⚠️ Discord送信エラー ({label}): {e}")
        return False


def send_to_discord(category, entry, analysis, price_info=None):
    """カテゴリ別チャンネルに通知。⭐⭐⭐⭐⭐は最重要チャンネルにも複製"""
    content = build_message(category, entry, analysis, price_info)

    target_url = WEBHOOK_URLS.get(category, WEBHOOK_URLS.get("コモディティ全般"))
    sent_main = post_to_webhook(target_url, content, label=category)

    importance = analysis.get("importance", 0)
    if importance >= CRITICAL_THRESHOLD and WEBHOOK_CRITICAL:
        critical_content = "🚨 **最重要アラート** 🚨\n\n" + content
        if len(critical_content) > 1900:
            critical_content = critical_content[:1900] + "..."
        post_to_webhook(WEBHOOK_CRITICAL, critical_content, label="最重要")
        print(f"  🚨 最重要チャンネルにも通知")

    return sent_main


def main():
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    seen = load_seen()
    news_state = []
    new_count = 0
    analyzed_ok = 0
    skipped_old = 0
    skipped_irrelevant = 0
    notified_count = 0
    price_cache = {}

    for category, url in FEEDS.items():
        print(f"📡 {category} をチェック中...")
        feed = feedparser.parse(url)

        for entry in feed.entries[:MAX_ARTICLES_PER_FEED]:
            aid = article_id(entry)
            if aid in seen:
                continue

            seen.add(aid)

            if not is_fresh(entry):
                skipped_old += 1
                print(f"  ⏭ スキップ（古い記事 {format_age(entry)}）: {entry.title[:40]}...")
                continue

            summary = entry.get("summary", "")[:500]

            # ★新機能：関連性フィルタ
            if not is_relevant(category, entry.title, summary):
                skipped_irrelevant += 1
                print(f"  ⏭ スキップ（無関係）: {entry.title[:40]}...")
                continue

            new_count += 1
            analysis = analyze_with_claude(client, category, entry.title, summary)

            if analysis is None:
                continue
            analyzed_ok += 1

            # analyzer連携: 通知閾値判定の前に全分析結果を記録（追加API呼び出しなし）
            news_state.append({
                "title": entry.title,
                "timestamp": _entry_iso(entry),
                "importance": analysis.get("importance", 1),
                "direction": analysis.get("impact", "中立"),
                "event_type": analysis.get("event_type", "commentary"),
                "surprise": analysis.get("surprise", "unknown"),
                "source": "Google News",
                "commodity": category,
                "summary": analysis.get("summary_jp", ""),
            })

            importance = analysis.get("importance", 0)
            age_str = format_age(entry)
            print(f"  [{importance}/5] ({age_str}) {entry.title[:45]}...")

            if importance >= IMPORTANCE_THRESHOLD:
                if category not in price_cache:
                    print(f"  💰 {category} の価格を取得中...")
                    price_cache[category] = get_price_for_category(category)

                if send_to_discord(category, entry, analysis, price_cache[category]):
                    notified_count += 1

    save_seen(seen)
    save_news_state(news_state)
    print(f"\n✅ 完了: {new_count}件チェック、{skipped_old}件スキップ（古い）、"
          f"{skipped_irrelevant}件スキップ（無関係）、{notified_count}件通知")

    # fail-loud: チェック対象があるのに分析成功0件 = キー失効/クレジット枯渇などの全滅障害。
    # exit 1 で run を赤くして気づけるようにする（部分成功では落とさない）。
    # このとき後続の commit ステップは走らず seen も永続化されないため、
    # 復旧後の run が同じ記事を再分析できる（障害中の記事を取りこぼさない）。
    if new_count >= 1 and analyzed_ok == 0:
        print(f"❌ 分析が全滅（チェック{new_count}件・成功0件）。"
              f"ANTHROPIC_API_KEY / クレジット残高を確認してください。")
        sys.exit(1)


if __name__ == "__main__":
    main()
