"""
業界レポート取得モジュール (v1)
USDA, EIA等の公式・準公式レポートを監視

データソース:
1. USDA NASS 公式RSS（最優先）
2. Google News経由のレポート関連ニュース（補完用）
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
SEEN_FILE = "seen_reports.json"  # 通常ニュースとは別管理
MAX_AGE_DAYS = 7  # 1週間以内のレポートを対象

# 業界レポート用フィード
REPORT_FEEDS = {
    "USDA NASS公式": {
        "url": "https://www.nass.usda.gov/rss/reports.xml",
        "emoji": "🏛",
        "default_categories": ["小麦", "トウモロコシ", "大豆"],
    },
    "WASDE関連": {
        "url": "https://news.google.com/rss/search?q=(WASDE+report+OR+USDA+supply+demand+estimate)+when:7d&hl=en-US&gl=US&ceid=US:en",
        "emoji": "📊",
        "default_categories": ["小麦", "トウモロコシ", "大豆"],
    },
    "EIA原油在庫": {
        "url": "https://news.google.com/rss/search?q=(EIA+weekly+petroleum+OR+crude+oil+inventory+OR+EIA+crude+stocks)+when:7d&hl=en-US&gl=US&ceid=US:en",
        "emoji": "🛢️",
        "default_categories": ["原油"],
    },
    "Crop Progress/Production": {
        "url": "https://news.google.com/rss/search?q=(USDA+crop+progress+OR+crop+production+OR+grain+stocks)+when:7d&hl=en-US&gl=US&ceid=US:en",
        "emoji": "🌾",
        "default_categories": ["小麦", "トウモロコシ", "大豆"],
    },
    "鉱業・金属レポート": {
        "url": "https://news.google.com/rss/search?q=(LME+inventory+OR+copper+stockpile+OR+gold+reserves+report)+when:7d&hl=en-US&gl=US&ceid=US:en",
        "emoji": "⛏️",
        "default_categories": ["銅", "金"],
    },
}


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    seen_list = list(seen)[-500:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen_list, f, ensure_ascii=False)


def article_id(entry):
    return hashlib.md5(entry.link.encode()).hexdigest()


def is_fresh(entry, max_age_days=MAX_AGE_DAYS):
    """1週間以内なら新着"""
    published = entry.get("published_parsed")
    if not published:
        return True
    try:
        article_time = datetime.fromtimestamp(mktime(published), tz=timezone.utc)
        now = datetime.now(timezone.utc)
        age = now - article_time
        return age <= timedelta(days=max_age_days)
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


def is_relevant_report(title, summary):
    """業界レポートとしての関連性チェック"""
    text = (title + " " + summary).lower()

    # 重要キーワード
    important = [
        "wasde", "crop production", "crop progress", "acreage",
        "grain stocks", "supply and demand", "world agricultural",
        "petroleum status", "weekly petroleum", "ethanol",
        "lme inventory", "copper stockpile", "gold reserves",
        "estimate", "forecast", "report", "production", "yield",
        "harvest", "stocks", "inventory", "供給見通し", "在庫",
    ]

    return any(kw in text for kw in important)


def analyze_report_with_claude(client, source, title, summary):
    """業界レポートを投資家視点で分析"""
    prompt = f"""以下はコモディティ関連の業界レポートまたは公式発表に関する情報です。
コモディティ投資家の視点で詳細に分析してください。

ソース: {source}
タイトル: {title}
内容: {summary}

以下のJSON形式のみで返答してください（前後に余計な文字を入れないこと）：
{{
  "categories": ["影響を受けるコモディティ"のリスト],
  "impact": "上昇" または "下落" または "中立" または "混合",
  "importance": 1-5の整数,
  "summary_jp": "日本語で3-4行の要約。具体的な数値があれば必ず含める",
  "key_data": "もっとも重要な数字やデータポイントを1行で",
  "trade_implication": "投資家への示唆を1-2行で"
}}

カテゴリの選択肢: 小麦, 金, 原油, トウモロコシ, 大豆, 銅, コモディティ全般
（複数該当する場合は全て含める）

業界レポートの重要度判定基準:
1 = 通常の状況更新、市場予想範囲内
2 = 小さなサプライズ、注目程度
3 = 注目すべきデータ、ポジション再考
4 = 大きなサプライズ、市場が大きく動く可能性
5 = 歴史的な数字、市場大変動確実"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"  ⚠️ 分析エラー: {e}")
        return None


def send_to_discord(source, source_emoji, entry, analysis, price_infos):
    """Discord通知（複数カテゴリ対応・価格情報付き）"""
    impact_emoji = {
        "上昇": "📈",
        "下落": "📉",
        "中立": "➡️",
        "混合": "🔀",
    }.get(analysis.get("impact", "中立"), "❓")

    importance_stars = "⭐" * analysis.get("importance", 1)
    age_str = format_age(entry)

    # カテゴリの絵文字
    categories = analysis.get("categories", [])
    cat_emojis = "".join(CATEGORY_EMOJI.get(c, "") for c in categories)

    # 価格情報ブロック（複数カテゴリ対応）
    price_block = ""
    if price_infos:
        valid_prices = {c: i for c, i in price_infos.items() if i}
        if valid_prices:
            price_block = "\n💰 **関連コモディティ価格**\n"
            for cat, info in valid_prices.items():
                line = format_price_line(cat, info)
                if line:
                    price_block += f"{line}\n"

    content = (
        f"📋 **業界レポート速報** {source_emoji} {cat_emojis} {importance_stars}\n"
        f"🏛 ソース: {source}\n"
        f"🕐 {age_str}の発表\n\n"
        f"📰 **{entry.title}**\n"
        f"{impact_emoji} 市場影響: {analysis.get('impact', '?')}\n\n"
        f"📝 {analysis.get('summary_jp', '')}\n\n"
        f"📊 **重要データ**\n{analysis.get('key_data', '')}\n\n"
        f"💡 **投資への示唆**\n{analysis.get('trade_implication', '')}\n"
        f"{price_block}\n"
        f"🔗 {entry.link}"
    )

    if len(content) > 1900:
        content = content[:1900] + "..."

    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"  ⚠️ Discord送信エラー: {e}")
        return False


def fetch_feed_safely(url, source_name):
    """RSSフィードの取得（エラーハンドリング付き）"""
    try:
        feed = feedparser.parse(url)
        if feed.bozo and feed.bozo_exception:
            print(f"  ⚠️ {source_name} のRSS解析警告: {feed.bozo_exception}")
        if not feed.entries:
            print(f"  ⚠️ {source_name}: エントリなし")
            return []
        return feed.entries
    except Exception as e:
        print(f"  ❌ {source_name} の取得失敗: {e}")
        return []


def main():
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    seen = load_seen()
    new_count = 0
    notified_count = 0

    price_cache = {}

    for source, config in REPORT_FEEDS.items():
        url = config["url"]
        source_emoji = config["emoji"]
        print(f"\n📡 {source} をチェック中...")

        entries = fetch_feed_safely(url, source)

        for entry in entries[:15]:
            aid = article_id(entry)
            if aid in seen:
                continue

            seen.add(aid)

            if not is_fresh(entry):
                continue

            summary = entry.get("summary", "")[:600]

            # 関連性チェック
            if not is_relevant_report(entry.title, summary):
                print(f"  ⏭ スキップ（無関係）: {entry.title[:40]}...")
                continue

            new_count += 1
            analysis = analyze_report_with_claude(client, source, entry.title, summary)

            if analysis is None:
                continue

            importance = analysis.get("importance", 0)
            age_str = format_age(entry)
            print(f"  [{importance}/5] ({age_str}) {entry.title[:50]}...")

            # 業界レポートは閾値2以上で通知（重要度高いため緩め）
            if importance >= 2:
                # 関連カテゴリ全部の価格を取得
                categories = analysis.get("categories", config["default_categories"])
                price_infos = {}
                for cat in categories:
                    if cat not in price_cache:
                        price_cache[cat] = get_price_for_category(cat)
                    price_infos[cat] = price_cache[cat]

                if send_to_discord(source, source_emoji, entry, analysis, price_infos):
                    notified_count += 1

    save_seen(seen)
    print(f"\n✅ 完了: {new_count}件チェック、{notified_count}件通知")


if __name__ == "__main__":
    main()
