"""
コモディティ定期価格レポート (v2: 専用チャンネル対応)
朝9時(日本時間)と夜9時(日本時間)に全コモディティ価格を日次レポートチャンネルに送信
"""
import os
from datetime import datetime, timezone, timedelta
import requests
from prices import get_price, format_price_line, COMMODITY_TICKERS

# 日次レポート専用チャンネルへ送信
DISCORD_WEBHOOK_URL = os.environ["WEBHOOK_DAILY"]


def get_jst_now():
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst)


def build_digest():
    jst_now = get_jst_now()
    hour = jst_now.hour

    if 5 <= hour < 15:
        title = "☀️ コモディティ朝刊"
    else:
        title = "🌙 コモディティ夕刊"

    date_str = jst_now.strftime("%Y-%m-%d %H:%M JST")
    header = f"**{title}** - {date_str}\n"
    header += "━━━━━━━━━━━━━━━━━━━━\n"

    lines = []
    notable = []

    for category, (ticker, unit) in COMMODITY_TICKERS.items():
        if ticker is None:
            continue

        print(f"💰 {category} ({ticker}) を取得中...")
        info = get_price(ticker)

        if info is None:
            lines.append(f"{category}: データ取得失敗")
            continue

        line = format_price_line(category, info)
        if line:
            lines.append(line)

            if abs(info["change_pct"]) >= 1.0:
                direction = "急騰" if info["change_pct"] > 0 else "急落"
                notable.append(f"{category}: {info['change_pct']:+.2f}% ({direction})")

    body = "\n".join(lines)

    footer = ""
    if notable:
        footer = "\n\n📌 **本日の注目**\n" + "\n".join(f"・{n}" for n in notable)

    return header + body + footer


def send_digest(content):
    if len(content) > 1900:
        content = content[:1900] + "..."

    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=15)
        resp.raise_for_status()
        print("✅ Discord送信成功")
    except requests.RequestException as e:
        print(f"❌ Discord送信エラー: {e}")


def main():
    print("📊 定期レポート生成開始...")
    digest = build_digest()
    print("\n" + digest + "\n")
    send_digest(digest)
    print("✅ 完了")


if __name__ == "__main__":
    main()
