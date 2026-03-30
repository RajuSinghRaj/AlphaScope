from __future__ import annotations

import os
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
import time

import requests
from flask import Flask, jsonify, render_template, request


BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "database"
DB_PATH = DB_DIR / "db.sqlite3"
OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_SYMBOL = "AAPL"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}"

CRYPTO_ALIASES = {
    "BITCOIN": "BTC-USD",
    "BTC": "BTC-USD",
    "ETHEREUM": "ETH-USD",
    "ETH": "ETH-USD",
    "SUI": "SUI-USD",
    "SEI": "SEI-USD",
    "ARB": "ARB-USD",
    "OP": "OP-USD",
    "TON": "TON11419-USD",
    "SOLANA": "SOL-USD",
    "SOL": "SOL-USD",
    "DOGECOIN": "DOGE-USD",
    "DOGE": "DOGE-USD",
}

FOREX_ALIASES = {
    "USDINR": "USDINR=X",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDINR=X": "USDINR=X",
}

MARKET_PRESETS = {
    "crypto": ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD"],
    "india": ["TCS.NS", "RELIANCE.NS", "INFY.NS", "HDFCBANK.NS"],
    "us": ["AAPL", "MSFT", "NVDA", "AMZN"],
    "forex": ["USDINR=X", "EURUSD=X", "GBPUSD=X", "USDJPY=X"],
}

MARKET_DEFAULTS = {
    "crypto": "BTC-USD",
    "india": "TCS.NS",
    "us": "AAPL",
    "forex": "USDINR=X",
}

INDIA_PLAIN_SYMBOLS = {"TCS", "RELIANCE", "INFY", "HDFCBANK"}
US_PLAIN_SYMBOLS = {"AAPL", "MSFT", "NVDA", "AMZN"}

DISPLAY_NAMES = {
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
    "SOL-USD": "Solana",
    "DOGE-USD": "Dogecoin",
    "TCS.NS": "Tata Consultancy Services",
    "RELIANCE.NS": "Reliance Industries",
    "INFY.NS": "Infosys",
    "HDFCBANK.NS": "HDFC Bank",
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "AMZN": "Amazon",
    "USDINR=X": "USD / INR",
    "EURUSD=X": "EUR / USD",
    "GBPUSD=X": "GBP / USD",
    "USDJPY=X": "USD / JPY",
}

RANGE_CONFIG = {
    "hour": {"period": "1d", "interval": "5m", "gap_ms": 30 * 60 * 1000},
    "day": {"period": "1d", "interval": "5m", "gap_ms": 30 * 60 * 1000},
    "week": {"period": "5d", "interval": "30m", "gap_ms": 3 * 60 * 60 * 1000},
    "month": {"period": "6mo", "interval": "1d", "gap_ms": 2 * 24 * 60 * 60 * 1000},
    "year": {"period": "1y", "interval": "1wk", "gap_ms": 14 * 24 * 60 * 60 * 1000},
}

POSITIVE_WORDS = {"surge", "gain", "up", "beats", "growth", "rally", "strong", "positive", "record"}
NEGATIVE_WORDS = {"fall", "down", "miss", "drop", "weak", "risk", "cut", "loss", "negative"}

app = Flask(__name__)
ASSET_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
OVERVIEW_CACHE: tuple[float, dict[str, list[dict[str, Any]]]] | None = None
NEWS_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}


def disable_invalid_proxy_env() -> None:
    blocked_values = {"http://127.0.0.1:9", "https://127.0.0.1:9"}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        if os.environ.get(key, "").strip().lower() in blocked_values:
            os.environ.pop(key, None)


disable_invalid_proxy_env()


def create_http_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    return session


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                quantity REAL NOT NULL CHECK(quantity > 0),
                purchase_price REAL NOT NULL CHECK(purchase_price >= 0),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(portfolio)").fetchall()}
        if "purchase_price" not in columns:
            connection.execute("ALTER TABLE portfolio ADD COLUMN purchase_price REAL NOT NULL DEFAULT 0")
        connection.commit()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_symbol(raw_symbol: str, market: str | None = None) -> list[str]:
    cleaned = raw_symbol.strip().upper()
    market_key = (market or "").strip().lower()

    if not cleaned:
        return [MARKET_DEFAULTS.get(market_key, DEFAULT_SYMBOL)]

    if cleaned in CRYPTO_ALIASES:
        alias = CRYPTO_ALIASES[cleaned]
        if cleaned == "SUI":
            return [alias, "SUI20947-USD"]
        if cleaned == "TON":
            return [alias, "TONCOIN-USD"]
        return [alias]

    if cleaned in FOREX_ALIASES:
        return [FOREX_ALIASES[cleaned]]

    if cleaned.endswith(("-USD", ".NS", ".BO", "=X")):
        return [cleaned]

    if market_key == "crypto":
        if cleaned == "SUI":
            return ["SUI-USD", "SUI20947-USD"]
        if cleaned == "TON":
            return ["TON11419-USD", "TONCOIN-USD"]
        return [f"{cleaned}-USD"]
    if market_key == "forex":
        return [f"{cleaned}=X"]
    if market_key == "india":
        return [f"{cleaned}.NS", f"{cleaned}.BO", cleaned]
    if market_key == "us":
        return [cleaned]

    if cleaned in {"XRP", "ADA", "BNB", "MATIC", "LTC", "AVAX"}:
        return [f"{cleaned}-USD"]
    if cleaned in INDIA_PLAIN_SYMBOLS:
        return [f"{cleaned}.NS", f"{cleaned}.BO", cleaned]
    if cleaned in US_PLAIN_SYMBOLS:
        return [cleaned]
    if len(cleaned) <= 10 and cleaned.replace("/", "").isalpha():
        return [cleaned, f"{cleaned}.NS", f"{cleaned}=X"]
    return [cleaned]


def classify_market(symbol: str) -> str:
    if symbol.endswith("-USD"):
        return "Crypto"
    if symbol.endswith(("=X",)):
        return "Forex"
    if symbol.endswith((".NS", ".BO")):
        return "India"
    return "US"


def determine_risk(change_percent: float, trend: list[float]) -> str:
    if len(trend) < 2:
        return "Medium"
    spread = max(trend) - min(trend)
    baseline = max(abs(trend[-1]), 1)
    volatility = abs(change_percent) + (spread / baseline * 100)
    if volatility < 2:
        return "Low"
    if volatility < 6:
        return "Medium"
    return "High"


def detect_market_pattern(trend: list[float], change_percent: float) -> str:
    if len(trend) < 5:
        return "trend"

    recent = trend[-5:]
    high = max(recent)
    low = min(recent)
    spread = high - low
    baseline = max(abs(recent[-1]), 1)
    spread_ratio = spread / baseline * 100

    first_half = recent[:2]
    second_half = recent[-2:]
    first_avg = sum(first_half) / len(first_half)
    second_avg = sum(second_half) / len(second_half)

    if spread_ratio < 0.8:
        return "consolidation"
    if change_percent > 1.2 and second_avg > first_avg:
        return "breakout"
    if change_percent < -1.2 and second_avg < first_avg:
        return "breakdown"
    if change_percent > 0 and recent[-1] > recent[-2] < recent[-3]:
        return "recovery"
    if change_percent < 0 and recent[-1] < recent[-2] > recent[-3]:
        return "pullback"
    return "trend"


def build_beginner_tip(
    market: str,
    news_bias: str,
    risk_level: str,
    pattern: str,
    trend_up: bool,
    volatility_ratio: float,
) -> str:
    tips: list[str] = []

    market_specific = {
        "Crypto": [
            "Crypto can move sharply without warning, so confirmation matters more than speed.",
            "In crypto, avoid chasing fast candles and wait for the move to prove it can hold.",
        ],
        "Forex": [
            "Forex moves often follow macro themes, so steady direction usually matters more than one spike.",
            "For currency pairs, focus on whether the move stays consistent rather than reacting to every small swing.",
        ],
        "India": [
            "For Indian stocks, it helps to compare the move with the broader market before treating it as a strong signal.",
            "In Indian equities, follow-through after the opening move is often more important than the first reaction.",
        ],
        "US": [
            "For US stocks, check whether the move is market-wide or specific to that company before acting.",
            "In US equities, waiting for confirmation after the first move can reduce rushed decisions.",
        ],
    }
    tips.extend(market_specific.get(market, market_specific["US"]))

    pattern_specific = {
        "breakout": "A breakout is stronger when price holds above the old range instead of slipping back quickly.",
        "breakdown": "A breakdown deserves caution until buyers show they can rebuild support.",
        "consolidation": "Consolidation usually means the market is deciding direction, so patience can be more useful than guessing.",
        "recovery": "A recovery looks healthier when each bounce creates a slightly higher floor.",
        "pullback": "A pullback is easier to trust if sellers slow down near a clear support area.",
        "trend": "A trend becomes easier to trust when the market keeps respecting the same direction over multiple candles.",
    }
    tips.append(pattern_specific.get(pattern, pattern_specific["trend"]))

    if news_bias == "Positive":
        tips.append("When headlines and price both lean positive, the move is usually easier to trust.")
    elif news_bias == "Negative":
        tips.append("If headlines stay negative, avoid assuming every dip is a quick rebound.")
    else:
        tips.append("When headlines are neutral, price action itself becomes the better guide.")

    if volatility_ratio > 6:
        tips.append("This move is volatile, so focus on risk first and avoid chasing candles.")
    elif volatility_ratio < 2:
        tips.append("This trend looks steadier, so consistency may matter more than reacting quickly.")

    if trend_up:
        tips.append("A healthy rise usually keeps making higher lows, even after short pauses.")
    else:
        tips.append("A weak trend often keeps making lower highs, so watch whether selling pressure is fading.")

    if risk_level == "High":
        tips.append("High-risk setups are often better for observation than aggressive entries when you are still learning.")
    elif risk_level == "Low":
        tips.append("Lower-risk setups still need discipline, but they usually give beginners more time to think clearly.")

    return tips[int((volatility_ratio + len(pattern) + len(market)) % len(tips))]


def derive_sentiment(text: str) -> str:
    lower_text = text.lower()
    positive_score = sum(word in lower_text for word in POSITIVE_WORDS)
    negative_score = sum(word in lower_text for word in NEGATIVE_WORDS)
    if positive_score > negative_score:
        return "Positive"
    if negative_score > positive_score:
        return "Negative"
    return "Neutral"


def fetch_news(query: str, market: str) -> list[dict[str, str]]:
    cache_key = f"{market}:{query}".lower()
    now = time.time()
    cached = NEWS_CACHE.get(cache_key)
    if cached and now - cached[0] < 900:
        return cached[1]

    try:
        session = create_http_session()
        response = session.get(
            GOOGLE_NEWS_RSS.format(query=quote_plus(f"{query} {market} market")),
            timeout=15,
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
        items = root.findall(".//item")[:4]
        news_items: list[dict[str, str]] = []

        for item in items:
            title = (item.findtext("title") or "").strip()
            description = (item.findtext("description") or "").strip()
            if not title:
                continue
            sentiment = derive_sentiment(f"{title} {description}")
            news_items.append(
                {
                    "headline": title,
                    "sentiment": sentiment,
                    "summary": description[:180] or f"{market} headlines are shaping the latest tone around this asset.",
                }
            )

        NEWS_CACHE[cache_key] = (now, news_items)
        return news_items
    except Exception:
        fallback = [
            {
                "headline": f"{query} market coverage is limited right now.",
                "sentiment": "Neutral",
                "summary": f"Live headlines were unavailable, so AlphaScope is using price action and trend data for the current {market.lower()} insight.",
            }
        ]
        NEWS_CACHE[cache_key] = (now, fallback)
        return fallback


def generate_rule_based_insight(
    price: float,
    change: float,
    trend: list[float],
    market: str,
    news_context: list[dict[str, str]],
) -> dict[str, str]:
    risk_level = determine_risk(change, trend)
    news_bias = news_context[0]["sentiment"] if news_context else "Neutral"
    spread = max(trend) - min(trend) if trend else 0
    baseline = max(abs(trend[-1]), 1) if trend else 1
    volatility_ratio = spread / baseline * 100
    trend_up = len(trend) >= 2 and trend[-1] >= trend[0]
    pattern = detect_market_pattern(trend, change)

    if pattern == "breakout":
        summary = (
            f"{market} price is pushing through a recent range near {price:.2f}, which suggests a possible breakout. "
            f"Headline tone is {news_bias.lower()}, so confidence may be helping traders extend the move."
        )
    elif pattern == "breakdown":
        summary = (
            f"{market} price is slipping below a recent range near {price:.2f}, which points to a possible breakdown. "
            f"Headline tone is {news_bias.lower()}, so caution may still be weighing on sentiment."
        )
    elif pattern == "consolidation":
        summary = (
            f"{market} price is moving in a relatively tight band near {price:.2f}, so the market looks to be consolidating. "
            f"Headline tone is {news_bias.lower()}, and traders may be waiting for a clearer direction."
        )
    elif change >= 0:
        summary = (
            f"{market} buyers are in control near {price:.2f}, and the latest move still has upward momentum. "
            f"Headline tone is {news_bias.lower()}, which helps explain why confidence may be supporting price."
        )
    else:
        summary = (
            f"{market} sellers are controlling the move near {price:.2f}, so short-term momentum is weaker. "
            f"Headline tone is {news_bias.lower()}, which may be reinforcing trader caution."
        )
    beginner_tip = build_beginner_tip(market, news_bias, risk_level, pattern, trend_up, volatility_ratio)

    return {
        "summary": summary,
        "risk_level": risk_level,
        "beginner_tip": beginner_tip,
        "source": "fallback",
    }


def parse_ollama_response(text: str, fallback: dict[str, str]) -> dict[str, str]:
    cleaned = [line.strip("- ").strip() for line in text.splitlines() if line.strip()]
    if not cleaned:
        return fallback

    summary = fallback["summary"]
    risk_level = fallback["risk_level"]
    beginner_tip = fallback["beginner_tip"]

    for line in cleaned:
        lower_line = line.lower()
        if lower_line.startswith("summary"):
            summary = line.partition(":")[2].strip() or summary
        elif lower_line.startswith("risk"):
            candidate = line.partition(":")[2].strip().title()
            if candidate in {"Low", "Medium", "High"}:
                risk_level = candidate
        elif lower_line.startswith("tip"):
            beginner_tip = line.partition(":")[2].strip() or beginner_tip

    return {
        "summary": summary,
        "risk_level": risk_level,
        "beginner_tip": beginner_tip,
        "source": "ollama",
    }


def generate_ai_insight(
    price: float,
    change: float,
    trend: list[float],
    market: str,
    symbol: str,
    news_context: list[dict[str, str]],
) -> dict[str, str]:
    fallback = generate_rule_based_insight(price, change, trend, market, news_context)
    headlines = "; ".join(item["headline"] for item in news_context[:2]) or "No major headlines available."
    prompt = (
        "You explain financial movement to beginners in plain English.\n"
        f"Asset: {symbol}\n"
        f"Market: {market}\n"
        f"Price: {price:.4f}\n"
        f"Change: {change:+.2f}%\n"
        f"Recent trend points: {', '.join(f'{point:.2f}' for point in trend[-8:])}\n"
        f"Recent headlines: {headlines}\n"
        "Explain what is happening and why in 2 short sentences.\n"
        "Then provide risk level as Low, Medium, or High and one beginner tip.\n"
        "Format:\nSummary: ...\nRisk: ...\nTip: ..."
    )

    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": "llama3", "prompt": prompt, "stream": False},
            timeout=12,
        )
        response.raise_for_status()
        return parse_ollama_response(response.json().get("response", ""), fallback)
    except requests.RequestException:
        return fallback


def build_trend_status(change_percent: float, closes: list[float]) -> dict[str, str]:
    acceleration = closes[-1] - closes[-3] if len(closes) >= 3 else 0
    momentum = "Accelerating" if acceleration > 0 else "Cooling"
    if abs(change_percent) < 0.35:
        momentum = "Balanced"
    return {
        "momentum": momentum,
        "signal": "Buyers leading" if change_percent >= 0 else "Sellers leading",
        "health": "Healthy trend" if abs(change_percent) < 3 else "High volatility",
    }


def fetch_yahoo_chart(symbol: str, range_key: str) -> dict[str, Any]:
    config = RANGE_CONFIG.get(range_key, RANGE_CONFIG["day"])
    session = create_http_session()
    response = session.get(
        YAHOO_CHART_URL.format(symbol=symbol),
        params={
            "range": config["period"],
            "interval": config["interval"],
            "includePrePost": "false",
            "events": "div,splits,capitalGains",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    chart = payload.get("chart", {})
    if chart.get("error"):
        error = chart["error"]
        raise ValueError(error.get("description") or error.get("code") or "Yahoo chart error")
    result = (chart.get("result") or [None])[0]
    if not result:
        raise ValueError(f"No market data found for {symbol}")
    return result


def parse_chart_result(symbol: str, range_key: str, result: dict[str, Any]) -> dict[str, Any]:
    meta = result.get("meta", {})
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []

    points: list[dict[str, Any]] = []
    candles: list[dict[str, Any]] = []
    closes_clean: list[float] = []
    gap_ms = RANGE_CONFIG.get(range_key, RANGE_CONFIG["day"])["gap_ms"]
    previous_ts_ms: int | None = None

    for idx, timestamp in enumerate(timestamps):
        close_value = closes[idx] if idx < len(closes) else None
        open_value = opens[idx] if idx < len(opens) else close_value
        high_value = highs[idx] if idx < len(highs) else close_value
        low_value = lows[idx] if idx < len(lows) else close_value
        if None in {open_value, high_value, low_value, close_value}:
            continue
        ts_ms = int(timestamp) * 1000
        if previous_ts_ms is not None and ts_ms - previous_ts_ms > gap_ms:
            # Break the line between separate trading sessions so the chart
            # does not draw a misleading diagonal across market-closed periods.
            points.append({"x": previous_ts_ms + 1, "y": None})
        close_number = safe_float(close_value)
        points.append({"x": ts_ms, "y": round(close_number, 4)})
        candles.append(
            {
                "x": ts_ms,
                "o": round(safe_float(open_value), 4),
                "h": round(safe_float(high_value), 4),
                "l": round(safe_float(low_value), 4),
                "c": round(close_number, 4),
            }
        )
        closes_clean.append(close_number)
        previous_ts_ms = ts_ms

    if not closes_clean:
        raise ValueError(f"No close data available for {symbol}")

    market = classify_market(symbol)
    current_price = safe_float(meta.get("regularMarketPrice"), closes_clean[-1])
    previous_close = safe_float(meta.get("chartPreviousClose"), closes_clean[0])
    change_percent = ((current_price - previous_close) / previous_close) * 100 if previous_close else 0
    name = meta.get("shortName") or DISPLAY_NAMES.get(symbol.upper(), symbol.upper())
    currency = meta.get("currency") or ("INR" if market == "India" else "USD")
    exchange = meta.get("fullExchangeName") or meta.get("exchangeName") or market
    news = fetch_news(name, market)
    insight = generate_ai_insight(current_price, change_percent, closes_clean, market, symbol, news)

    return {
        "symbol": symbol.upper(),
        "name": name,
        "market": market,
        "currency": currency,
        "exchange": exchange,
        "price": round(current_price, 4 if market == "Forex" else 2),
        "change_percent": round(change_percent, 2),
        "range": range_key,
        "history": {
            "points": points,
            "candles": candles,
        },
        "trend_status": build_trend_status(change_percent, closes_clean),
        "insight": insight,
        "news": news,
        "mode": "live",
    }


def fetch_market_data(symbol: str, market: str | None = None, range_key: str = "day") -> dict[str, Any]:
    cache_key = ((market or "").lower(), symbol.strip().upper(), range_key)
    now = time.time()
    cached = ASSET_CACHE.get(cache_key)
    if cached and now - cached[0] < 20:
        return cached[1]

    candidates = normalize_symbol(symbol, market)
    final_error = None
    for candidate in candidates:
        try:
            result = fetch_yahoo_chart(candidate, range_key)
            parsed = parse_chart_result(candidate, range_key, result)
            ASSET_CACHE[cache_key] = (now, parsed)
            return parsed
        except Exception as error:
            final_error = error
    raise ValueError(str(final_error) if final_error else f"No market data found for {symbol}")


def build_market_overview() -> dict[str, list[dict[str, Any]]]:
    global OVERVIEW_CACHE
    now = time.time()
    if OVERVIEW_CACHE and now - OVERVIEW_CACHE[0] < 45:
        return OVERVIEW_CACHE[1]

    overview: dict[str, list[dict[str, Any]]] = {}
    for market_name, symbols in MARKET_PRESETS.items():
        cards: list[dict[str, Any]] = []
        for symbol in symbols:
            try:
                data = fetch_market_data(symbol, market_name, "day")
                cards.append(
                    {
                        "symbol": data["symbol"],
                        "name": data["name"],
                        "price": data["price"],
                        "currency": data["currency"],
                        "change_percent": data["change_percent"],
                        "market": data["market"],
                    }
                )
            except Exception:
                continue
        overview[market_name] = cards
    OVERVIEW_CACHE = (now, overview)
    return overview


def calculate_portfolio_metrics(rows: list[sqlite3.Row]) -> dict[str, Any]:
    holdings: list[dict[str, Any]] = []
    total_investment = 0.0
    current_value = 0.0

    for row in rows:
        asset = fetch_market_data(row["symbol"], None, "day")
        latest_price = safe_float(asset["price"])
        quantity = safe_float(row["quantity"])
        invested_amount = safe_float(row["purchase_price"]) * quantity
        market_value = latest_price * quantity
        total_investment += invested_amount
        current_value += market_value
        holdings.append(
            {
                "id": row["id"],
                "symbol": asset["symbol"],
                "market": asset["market"],
                "currency": asset["currency"],
                "quantity": quantity,
                "price": round(latest_price, 4 if asset["market"] == "Forex" else 2),
                "purchase_price": round(safe_float(row["purchase_price"]), 4),
                "value": round(market_value, 2),
                "change_percent": asset["change_percent"],
                "beginner_label": (
                    "Momentum is improving -> watch continuation"
                    if asset["change_percent"] >= 0
                    else "Momentum is weaker -> protect downside first"
                ),
            }
        )

    return {
        "holdings": holdings,
        "summary": {
            "total_investment": round(total_investment, 2),
            "current_value": round(current_value, 2),
            "profit_loss": round(current_value - total_investment, 2),
        },
    }


def generate_portfolio_prediction(portfolio: dict[str, Any]) -> dict[str, str]:
    holdings = portfolio.get("holdings", [])
    summary = portfolio.get("summary", {})

    if not holdings:
        return {
            "summary": "Your portfolio is empty right now, so there is no allocation signal yet.",
            "risk_level": "Low",
            "action": "Add one or more assets to start tracking exposure and trend quality.",
            "source": "rule",
        }

    winners = sum(1 for item in holdings if item["change_percent"] >= 0)
    losers = len(holdings) - winners
    profit_loss = safe_float(summary.get("profit_loss"))
    total_value = safe_float(summary.get("current_value"), 1)
    pnl_ratio = abs(profit_loss) / total_value * 100 if total_value else 0

    risk_level = "Low" if pnl_ratio < 2 else "Medium" if pnl_ratio < 6 else "High"
    leading_assets = ", ".join(item["symbol"] for item in holdings[:3])
    direction = "positive" if winners >= losers else "mixed to weak"

    fallback = {
        "summary": (
            f"Your portfolio tone is {direction}, led by {leading_assets}. "
            f"There are {winners} gaining positions and {losers} weaker positions right now."
        ),
        "risk_level": risk_level,
        "action": (
            "Keep watching trend confirmation in your strongest holdings."
            if winners >= losers
            else "Review weaker holdings first and reduce exposure if the weakness continues."
        ),
        "source": "rule",
    }

    holding_summary = ", ".join(
        f"{item['symbol']} ({item['change_percent']}%)" for item in holdings[:6]
    )
    prompt = (
        "You are summarizing a beginner investor portfolio in plain English.\n"
        f"Portfolio summary: current value {summary.get('current_value')}, profit/loss {summary.get('profit_loss')}.\n"
        f"Holdings: {holding_summary}\n"
        "Explain the portfolio outlook in 2 short sentences.\n"
        "Then give risk as Low, Medium, or High and one action suggestion.\n"
        "Format:\nSummary: ...\nRisk: ...\nAction: ..."
    )

    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": "llama3", "prompt": prompt, "stream": False},
            timeout=12,
        )
        response.raise_for_status()
        text = response.json().get("response", "")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        summary_text = fallback["summary"]
        risk = fallback["risk_level"]
        action = fallback["action"]
        for line in lines:
            lower = line.lower()
            if lower.startswith("summary"):
                summary_text = line.partition(":")[2].strip() or summary_text
            elif lower.startswith("risk"):
                candidate = line.partition(":")[2].strip().title()
                if candidate in {"Low", "Medium", "High"}:
                    risk = candidate
            elif lower.startswith("action"):
                action = line.partition(":")[2].strip() or action
        return {"summary": summary_text, "risk_level": risk, "action": action, "source": "ollama"}
    except requests.RequestException:
        return fallback


@app.route("/")
def index() -> str:
    return render_template("index.html", default_symbol=DEFAULT_SYMBOL)


@app.route("/get_stock_data")
def get_stock_data() -> Any:
    symbol = request.args.get("symbol", DEFAULT_SYMBOL)
    market = request.args.get("market")
    range_key = request.args.get("range", "day")
    try:
        return jsonify({"success": True, "data": fetch_market_data(symbol, market, range_key)})
    except Exception as error:
        return jsonify({"success": False, "message": str(error)}), 400


@app.route("/get_market_overview")
def get_market_overview() -> Any:
    try:
        return jsonify({"success": True, "data": build_market_overview()})
    except Exception as error:
        return jsonify({"success": False, "message": str(error)}), 400


@app.route("/add_stock", methods=["POST"])
def add_stock() -> Any:
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol", "")).strip()
    market = str(data.get("market", "")).strip().lower() or None
    quantity = safe_float(data.get("quantity"))
    if not symbol or quantity <= 0:
        return jsonify({"success": False, "message": "Provide a valid asset symbol and quantity."}), 400

    try:
        asset = fetch_market_data(symbol, market, "day")
    except Exception as error:
        return jsonify({"success": False, "message": str(error)}), 400

    with get_connection() as connection:
        connection.execute(
            "INSERT INTO portfolio (symbol, quantity, purchase_price) VALUES (?, ?, ?)",
            (asset["symbol"], quantity, safe_float(asset["price"])),
        )
        connection.commit()
    return jsonify({"success": True, "message": f"{asset['symbol']} added to portfolio."})


@app.route("/get_portfolio")
def get_portfolio() -> Any:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT id, symbol, quantity, purchase_price, created_at FROM portfolio ORDER BY created_at DESC"
        ).fetchall()
    try:
        return jsonify({"success": True, "data": calculate_portfolio_metrics(rows)})
    except Exception as error:
        return jsonify({"success": False, "message": str(error)}), 400


@app.route("/delete_stock/<int:holding_id>", methods=["DELETE"])
def delete_stock(holding_id: int) -> Any:
    with get_connection() as connection:
        deleted = connection.execute("DELETE FROM portfolio WHERE id = ?", (holding_id,))
        connection.commit()
    if deleted.rowcount == 0:
        return jsonify({"success": False, "message": "Holding not found."}), 404
    return jsonify({"success": True, "message": "Holding removed from portfolio."})


@app.route("/portfolio_predict")
def portfolio_predict() -> Any:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT id, symbol, quantity, purchase_price, created_at FROM portfolio ORDER BY created_at DESC"
        ).fetchall()
    try:
        portfolio = calculate_portfolio_metrics(rows)
        prediction = generate_portfolio_prediction(portfolio)
        return jsonify({"success": True, "data": prediction})
    except Exception as error:
        return jsonify({"success": False, "message": str(error)}), 400


init_db()


if __name__ == "__main__":
    app.run(debug=True)
