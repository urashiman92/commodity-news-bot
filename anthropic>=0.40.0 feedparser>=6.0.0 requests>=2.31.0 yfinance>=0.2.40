"""
コモディティ定期価格レポート
朝9時(日本時間)と夜9時(日本時間)に全コモディティ価格をDiscordに送信
"""
import os
from datetime import datetime, timezone, timedelta
import requests
from prices import get_price, format_price_line, COMMODITY_TICKERS

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]


def get_jst_now():
    """日本時間の現在時刻を取得"""
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst)


def build_digest():
    """全コモディティの価格レポートを構築"""
    jst_now = get_jst_now()
    hour = jst_now.hour

    # 朝刊 or 夕刊判定
    if 5 <= hour < 15:
        title = "☀️ コモディティ朝刊"
    else:
        title = "🌙 コモディティ夕刊"

    date_str = jst_now.strftime("%Y-%m-%d %H:%M JST")
    header = f"**{title}** - {date_str}\n"
    header += "━━━━━━━━━━━━━━━━━━━━\n"

    lines = []
    notable = []  # 大きく動いた銘柄

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

            # 大きく動いた銘柄をハイライト（絶対値1%以上）
            if abs(info["change_pct"]) >= 1.0:
                direction = "急騰" if info["change_pct"] > 0 else "急落"
                notable.append(f"{category}: {info['change_pct']:+.2f}% ({direction})")

    body = "\n".join(lines)

    # 注目銘柄があれば下部にコメント
    footer = ""
    if notable:
        footer = "\n\n📌 **本日の注目**\n" + "\n".join(f"・{n}" for n in notable)

    return header + body + footer


def send_digest(content):
    """Discordに送信"""
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
