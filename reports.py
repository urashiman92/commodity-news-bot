"""
業界レポート取得モジュール (v3: マルチチャンネル対応)
- 業界レポート専用チャンネルに送信
- 重要度⭐⭐⭐⭐⭐は最重要チャンネルにも複製通知
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
WEBHOOK_REPORTS = os.environ.get("WEBHOOK_REPORTS", "")
WEBHOOK_CRITICAL = os.environ.get("WEBHOOK_CRITICAL", "")
SEEN_FILE = "seen_reports.json"
MAX_AGE_DAYS = 7
CRITICAL_THRESHOLD = 5

# 業界レポート用フィード
REPORT_FEEDS = {
    "USDA NASS公式": {
        "url": "https://www.nass.usda.gov/rss/reports.xml",
        "emoji": "🏛",
        "default_categories": ["小麦", "トウモロコシ", "大豆"],
    },
    "FRB金融政策": {
        "url": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "emoji": "🏦",
        "default_categories": ["金", "原油", "コモディティ全般"],
    },
    "FRB全プレスリリース": {
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
        "emoji": "🏦",
        "default_categories": ["金", "コモディティ全般"],
    },
    "FRB Powell議長発言": {
        "url": "https://www.federalreserve.gov/feeds/s_t_powell.xml",
        "emoji": "🎤",
        "default_categories": ["金", "原油", "コモディティ全般"],
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
    "OPEC関連": {
        "url": "https://news.google.com/rss/search?q=(OPEC+production+OR+OPEC+meeting+OR+OPEC+quota+OR+OPEC+output)+when:7d&hl=en-US&gl=US&ceid=US:en",
        "emoji": "🛢️",
        "default_categories": ["原油"],
    },
    "Reuters商品ニュース": {
        "url": "https://news.google.com/rss/search?q=site:reuters.com+(commodity+OR+oil+OR+gold+OR+wheat+OR+copper)+when:3d&hl=en-US&gl=US&ceid=US:en",
        "emoji": "📰",
        "default_categories": ["コモディティ全般"],
    },
    "Bloomberg商品ニュース": {
        "url": "https://news.google.com/rss/search?q=site:bloomberg.com+(commodity+OR+oil+OR+gold+OR+wheat+OR+copper)+when:3d&hl=en-US&gl=US&ceid=US:en",
        "emoji": "📰",
        "default_categories": ["コモディティ全般"],
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


def _entry_iso(entry):
    """記事の published を UTC tz-aware ISO8601 文字列で返す。無ければ現在UTC。"""
    published = entry.get("published_parsed")
    if published:
        try:
            return datetime.fromtimestamp(mktime(published), tz=timezone.utc).isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


def save_news_state(items, path="reports_state.json", keep_hours=72):
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
        # レポートは1記事が複数カテゴリに展開されるため commodity もキーに含める
        key = f"{it.get('title', '')}|{it.get('timestamp', '')}|{it.get('commodity', '')}"
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


def is_fresh(entry, max_age_days=MAX_AGE_DAYS):
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


def is_relevant_report(title, summary, source):
    text = (title + " " + summary).lower()

    if "FRB" in source:
        fed_keywords = [
            "monetary policy", "fomc", "rate", "interest", "inflation",
            "powell", "federal funds", "balance sheet", "qe", "qt",
            "tapering", "easing", "tightening", "economic outlook",
            "金利", "金融政策", "利上げ", "利下げ",
        ]
        return any(kw in text for kw in fed_keywords)

    important = [
        "wasde", "crop production", "crop progress", "acreage",
        "grain stocks", "supply and demand", "world agricultural",
        "petroleum status", "weekly petroleum", "ethanol",
        "lme inventory", "copper stockpile", "gold reserves",
        "opec", "output cut", "production quota", "saudi", "russia",
        "estimate", "forecast", "report", "production", "yield",
        "harvest", "stocks", "inventory", "supply", "demand",
        "供給見通し", "在庫", "生産", "需給",
    ]
    return any(kw in text for kw in important)


def analyze_report_with_claude(client, source, title, summary):
    prompt = f"""以下はコモディティ関連の業界レポートまたは公式発表です。
コモディティ投資家の視点で詳細に分析してください。

ソース: {source}
タイトル: {title}
内容: {summary}

以下のJSON形式のみで返答してください（前後に余計な文字を入れないこと）：
{{
  "categories": ["影響を受けるコモディティ"のリスト],
  "impact": "上昇" または "下落" または "中立" または "混合",
  "importance": 1-5の整数,
  "event_type": "scheduled" または "breaking" または "commentary",
  "surprise": "high" または "inline" または "low" または "unknown",
  "summary_jp": "日本語で3-4行の要約。具体的な数値があれば必ず含める",
  "key_data": "もっとも重要な数字やデータポイントを1行で",
  "trade_implication": "投資家への示唆を1-2行で"
}}

カテゴリの選択肢: 小麦, 金, 原油, トウモロコシ, 大豆, 銅, コモディティ全般

特記事項：
- FRBの金利・金融政策はすべてのコモディティに影響、特に金は強く反応
- OPECの減産・増産発表は原油価格に直撃
- 米国の利上げ＝金利上昇＝金（無利子資産）下落圧力

業界レポートの重要度判定基準:
1 = 通常の状況更新、市場予想範囲内
2 = 小さなサプライズ、注目程度
3 = 注目すべきデータ、ポジション再考
4 = 大きなサプライズ、市場が大きく動く可能性
5 = 歴史的な数字、市場大変動確実

event_type の判定基準（ニュースの種類）:
- scheduled = WASDE/EIA週間在庫/FOMC/雇用統計/Crop Progress など、予定された定期発表・公式統計
- breaking  = OPEC緊急会合・地政学イベント・事故・突発の供給ショックなど、予定外の速報
- commentary = 解説・観測・アナリスト見解など、既に方向が織り込まれているもの
※業界レポート・公式統計は基本 scheduled が多い。日付の決まった定期発表なら scheduled とする。

surprise の判定基準（予想比サプライズ度。価格への「予想外度」を厳しく判定すること）:
- "high": 統計が市場予想を大幅に外した場合のみ（作付面積・在庫・生産量が事前予想から大きく乖離）。
    または地政学的ショックや±5%超の価格急変を伴う/伴いそうな事象。
  ※「予想の範囲内」「既報の続報」「2-3%程度の変動」は high にしない
- "inline": 統計がほぼ事前予想どおり（予想の範囲内）→ 値動きは限定的
- "low": 通常の状況更新、定例の進捗報告（±3%未満の通常変動）
- "unknown": 予想比が判断できない、サプライズ度が不明
迷う場合や予想比が不明な場合は "unknown" を選ぶこと。high は安易に選ばない。"""

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


def build_message(source, source_emoji, entry, analysis, price_infos):
    impact_emoji = {
        "上昇": "📈",
        "下落": "📉",
        "中立": "➡️",
        "混合": "🔀",
    }.get(analysis.get("impact", "中立"), "❓")

    importance_stars = "⭐" * analysis.get("importance", 1)
    age_str = format_age(entry)

    categories = analysis.get("categories", [])
    cat_emojis = "".join(CATEGORY_EMOJI.get(c, "") for c in categories)

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
    return content


def post_to_webhook(webhook_url, content, label=""):
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


def send_to_discord(source, source_emoji, entry, analysis, price_infos):
    """業界レポートチャンネルに送信。⭐⭐⭐⭐⭐は最重要チャンネルにも複製"""
    content = build_message(source, source_emoji, entry, analysis, price_infos)

    # 業界レポート専用チャンネルへ送信
    sent = post_to_webhook(WEBHOOK_REPORTS, content, label="業界レポート")

    # 重要度5なら最重要チャンネルにも送信
    importance = analysis.get("importance", 0)
    if importance >= CRITICAL_THRESHOLD and WEBHOOK_CRITICAL:
        critical_content = "🚨 **最重要レポート** 🚨\n\n" + content
        if len(critical_content) > 1900:
            critical_content = critical_content[:1900] + "..."
        post_to_webhook(WEBHOOK_CRITICAL, critical_content, label="最重要")
        print(f"  🚨 最重要チャンネルにも通知")

    return sent


def fetch_feed_safely(url, source_name):
    try:
        feed = feedparser.parse(url)
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
    news_state = []
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

            if not is_relevant_report(entry.title, summary, source):
                print(f"  ⏭ スキップ（無関係）: {entry.title[:40]}...")
                continue

            new_count += 1
            analysis = analyze_report_with_claude(client, source, entry.title, summary)

            if analysis is None:
                continue

            # analyzer連携: 通知閾値判定の前に記録。レポートは複数カテゴリに効くので
            # カテゴリごとに1レコードへ展開（WASDE/FOMC等が複数銘柄に届くように）。
            ts_iso = _entry_iso(entry)
            for cat in analysis.get("categories", []):
                news_state.append({
                    "title": entry.title,
                    "timestamp": ts_iso,
                    "importance": analysis.get("importance", 1),
                    "direction": analysis.get("impact", "中立"),
                    "event_type": analysis.get("event_type", "commentary"),
                    "surprise": analysis.get("surprise", "unknown"),
                    "source": source,
                    "commodity": cat,
                    "summary": analysis.get("summary_jp", ""),
                })

            importance = analysis.get("importance", 0)
            age_str = format_age(entry)
            print(f"  [{importance}/5] ({age_str}) {entry.title[:50]}...")

            if importance >= 2:
                categories = analysis.get("categories", config["default_categories"])
                price_infos = {}
                for cat in categories:
                    if cat not in price_cache:
                        price_cache[cat] = get_price_for_category(cat)
                    price_infos[cat] = price_cache[cat]

                if send_to_discord(source, source_emoji, entry, analysis, price_infos):
                    notified_count += 1

    save_seen(seen)
    save_news_state(news_state)
    print(f"\n✅ 完了: {new_count}件チェック、{notified_count}件通知")


if __name__ == "__main__":
    main()
