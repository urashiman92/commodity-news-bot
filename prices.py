"""
価格データ取得モジュール
Yahoo Finance (yfinance) を使用してコモディティ先物の価格を取得
"""
import yfinance as yf

# カテゴリ → Yahoo Financeティッカーのマッピング
COMMODITY_TICKERS = {
    "小麦": ("ZW=F", "¢/bu"),      # Wheat Futures
    "金": ("GC=F", "$/oz"),        # Gold Futures
    "原油": ("CL=F", "$/bbl"),      # WTI Crude Oil
    "トウモロコシ": ("ZC=F", "¢/bu"),  # Corn Futures
    "大豆": ("ZS=F", "¢/bu"),       # Soybean Futures
    "銅": ("HG=F", "$/lb"),        # Copper Futures
    "コモディティ全般": (None, None),  # 該当なし
}

# 絵文字マッピング
CATEGORY_EMOJI = {
    "小麦": "🌾",
    "金": "🥇",
    "原油": "🛢️",
    "トウモロコシ": "🌽",
    "大豆": "🫘",
    "銅": "🥉",
    "コモディティ全般": "📊",
}


def get_price(ticker, period="5d"):
    """
    指定ティッカーの現在価格と前日比を取得

    Returns:
        dict: {
            "price": 現在価格,
            "prev_close": 前日終値,
            "change_pct": 変化率(%),
            "high_5d": 5日高値,
            "low_5d": 5日安値,
            "sparkline": テキストスパークライン,
            "closes": 終値リスト(5日分),
        }
        失敗時は None
    """
    if ticker is None:
        return None

    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=period)

        if hist.empty or len(hist) < 2:
            return None

        closes = hist["Close"].tolist()
        current_price = closes[-1]
        prev_close = closes[-2]
        change_pct = ((current_price - prev_close) / prev_close) * 100

        return {
            "price": current_price,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "high_5d": max(hist["High"].tolist()),
            "low_5d": min(hist["Low"].tolist()),
            "sparkline": make_sparkline(closes),
            "closes": closes,
        }
    except Exception as e:
        print(f"  ⚠️ 価格取得エラー ({ticker}): {e}")
        return None


def make_sparkline(values):
    """
    数値リストからUnicodeスパークラインを生成
    例: [100, 102, 98, 105, 110] → "▃▅▁▆█"
    """
    if not values or len(values) < 2:
        return ""

    bars = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)

    if hi == lo:
        return bars[3] * len(values)

    result = []
    for v in values:
        # 0-7の8段階に正規化
        idx = int((v - lo) / (hi - lo) * (len(bars) - 1))
        result.append(bars[idx])
    return "".join(result)


def format_price_line(category, ticker_info):
    """
    1カテゴリ分の価格情報を1行にフォーマット
    例: "🥇 金: $2,385.40 /oz (+0.8% 📈) ▁▃▅▆█"
    """
    if ticker_info is None:
        return None

    ticker, unit = COMMODITY_TICKERS.get(category, (None, None))
    if ticker is None:
        return None

    emoji = CATEGORY_EMOJI.get(category, "")
    price = ticker_info["price"]
    change = ticker_info["change_pct"]
    spark = ticker_info["sparkline"]

    # 変化方向の絵文字
    if change > 0.5:
        arrow = "📈"
    elif change < -0.5:
        arrow = "📉"
    else:
        arrow = "➡️"

    # 価格フォーマット（桁数調整）
    if price >= 1000:
        price_str = f"{price:,.2f}"
    else:
        price_str = f"{price:.2f}"

    return f"{emoji} **{category}**: {price_str} {unit} ({change:+.2f}% {arrow}) {spark}"


def get_price_for_category(category):
    """カテゴリから価格情報を取得するショートカット"""
    ticker, _ = COMMODITY_TICKERS.get(category, (None, None))
    if ticker is None:
        return None
    return get_price(ticker)
