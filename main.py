"""
コモディティニュース自動通知Bot
- Google News RSSから最新ニュースを取得
- Claude Haikuで要約・重要度判定
- 重要度3以上のニュースをDiscordに通知
"""
import os
import json
import hashlib
import feedparser
import requests
from anthropic import Anthropic

# --- 設定 ---
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
SEEN_FILE = "seen.json"
IMPORTANCE_THRESHOLD = 3  # この重要度以上だけ通知（1-5）
MAX_ARTICLES_PER_FEED = 10  # 1フィードあたりの最大チェック記事数

# 監視対象のRSSフィード（Google News）
FEEDS = {
    "小麦": "https://news.google.com/rss/search?q=wheat+price+OR+%E5%B0%8F%E9%BA%A6&hl=ja&gl=JP&ceid=JP:ja",
    "金": "https://news.google.com/rss/search?q=gold+price+OR+%E9%87%91%E7%9B%B8%E5%A0%B4&hl=ja&gl=JP&ceid=JP:ja",
    "原油": "https://news.google.com/rss/search?q=crude+oil+OR+%E5%8E%9F%E6%B2%B9%E4%BE%A1%E6%A0%BC&hl=ja&gl=JP&ceid=JP:ja",
    "トウモロコシ": "https://news.google.com/rss/search?q=corn+futures+OR+%E3%83%88%E3%82%A6%E3%83%A2%E3%83%AD%E3%82%B3%E3%82%B7&hl=ja&gl=JP&ceid=JP:ja",
    "大豆": "https://news.google.com/rss/search?q=soybean+futures+OR+%E5%A4%A7%E8%B1%86%E5%85%88%E7%89%A9&hl=ja&gl=JP&ceid=JP:ja",
    "銅": "https://news.google.com/rss/search?q=copper+price+OR+%E9%8A%85%E7%9B%B8%E5%A0%B4&hl=ja&gl=JP&ceid=JP:ja",
    "コモディティ全般": "https://news.google.com/rss/search?q=commodity+market+OR+%E5%95%86%E5%93%81%E5%B8%82%E5%A0%B4&hl=ja&gl=JP&ceid=JP:ja",
}


def load_seen():
    """既読記事IDのロード"""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    """既読記事IDの保存（最新1000件のみ保持）"""
    seen_list = list(seen)[-1000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen_list, f, ensure_ascii=False)


def article_id(entry):
    """記事リンクからユニークIDを生成"""
    return hashlib.md5(entry.link.encode()).hexdigest()


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
        # コードフェンスがあれば除去
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except (json.JSONDecodeError, Exception) as e:
        print(f"  ⚠️ 分析エラー: {e}")
        return None


def send_to_discord(category, entry, analysis):
    """Discord Webhookで通知送信"""
    impact_emoji = {
        "上昇": "📈",
        "下落": "📉",
        "中立": "➡️",
    }.get(analysis["impact"], "❓")

    importance_stars = "⭐" * analysis["importance"]

    content = (
        f"**🌾 {category}ニュース速報** {importance_stars}\n\n"
        f"📰 **{entry.title}**\n"
        f"{impact_emoji} 影響: {analysis['impact']}\n\n"
        f"📝 {analysis['summary_jp']}\n"
        f"💡 {analysis['reason']}\n\n"
        f"🔗 {entry.link}"
    )

    # Discord制限対応（2000文字）
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
    notified_count = 0

    for category, url in FEEDS.items():
        print(f"📡 {category} をチェック中...")
        feed = feedparser.parse(url)

        for entry in feed.entries[:MAX_ARTICLES_PER_FEED]:
            aid = article_id(entry)
            if aid in seen:
                continue

            seen.add(aid)
            new_count += 1

            summary = entry.get("summary", "")[:500]
            analysis = analyze_with_claude(client, category, entry.title, summary)

            if analysis is None:
                continue

            importance = analysis.get("importance", 0)
            print(f"  [{importance}/5] {entry.title[:50]}...")

            if importance >= IMPORTANCE_THRESHOLD:
                send_to_discord(category, entry, analysis)
                notified_count += 1

    save_seen(seen)
    print(f"\n✅ 完了: {new_count}件チェック、{notified_count}件通知")


if __name__ == "__main__":
    main()
