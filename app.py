from __future__ import annotations

import os
import hashlib
import json
import math
import re
import secrets
import sqlite3
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
import time

import requests
from flask import Flask, g, jsonify, redirect, render_template, request
from database.foundation import backend as database_backend, connect as database_connect, migrate as migrate_foundation
from werkzeug.security import check_password_hash, generate_password_hash
from market_intelligence import analyze_move, fetch_fred_series, fetch_news_items
from rate_limit import ProcessRateLimiter


BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "database"
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
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "1" if (os.getenv("RENDER") or os.getenv("VERCEL")) else "0") == "1"
app.config["SESSION_COOKIE_DOMAIN"] = os.getenv("SESSION_COOKIE_DOMAIN") or None
AUTH_SESSION_SECONDS = 7 * 24 * 60 * 60
AUTH_COOKIE = "alphascope_sid"
ASSET_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
OVERVIEW_CACHE: tuple[float, dict[str, list[dict[str, Any]]]] | None = None
NEWS_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
MAX_ASSET_CACHE_ENTRIES = 512


def _bounded_positive_setting(name: str, default: int, maximum: int) -> int:
    try:
        return min(max(1, int(os.getenv(name, str(default)))), maximum)
    except (TypeError, ValueError):
        return default


PUBLIC_RATE_WINDOW_SECONDS = _bounded_positive_setting("ALPHASCOPE_RATE_LIMIT_WINDOW_SECONDS", 60, 3600)
PUBLIC_RATE_LIMITS = {
    "get_stock_data": _bounded_positive_setting("ALPHASCOPE_RATE_LIMIT_MARKET_DATA", 60, 10000),
    "get_market_overview": _bounded_positive_setting("ALPHASCOPE_RATE_LIMIT_MARKET_OVERVIEW", 12, 10000),
    "news_feed": _bounded_positive_setting("ALPHASCOPE_RATE_LIMIT_NEWS", 20, 10000),
    "macro_dashboard": _bounded_positive_setting("ALPHASCOPE_RATE_LIMIT_MACRO", 12, 10000),
    "move_analysis": _bounded_positive_setting("ALPHASCOPE_RATE_LIMIT_MOVE_ANALYSIS", 20, 10000),
}
PUBLIC_RATE_LIMITER = ProcessRateLimiter(
    max_keys=_bounded_positive_setting("ALPHASCOPE_RATE_LIMIT_MAX_KEYS", 4096, 65536)
)


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


@app.before_request
def limit_public_provider_routes():
    """Limit provider-backed public routes using only the direct WSGI peer address."""
    endpoint = request.endpoint
    limit = PUBLIC_RATE_LIMITS.get(endpoint)
    if limit is None:
        return None
    allowed, retry_after = PUBLIC_RATE_LIMITER.check(
        endpoint, request.remote_addr, limit, PUBLIC_RATE_WINDOW_SECONDS
    )
    if allowed:
        return None
    response = jsonify({"success": False, "message": "Too many requests. Please retry shortly."})
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


def get_connection() -> sqlite3.Connection:
    return database_connect()


def init_db() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    if database_backend() == "postgresql":
        # Remote schema changes are an explicit operator action, never an app-start side effect.
        with get_connection() as connection:
            try:
                from database.postgres_migrations import MIGRATIONS, migration_state
                version = migration_state(connection)
            except Exception:
                connection.rollback()
                version = 0
        if version != len(MIGRATIONS):
            raise RuntimeError("PostgreSQL schema is not ready. Review target and backup, then use the guarded database migration command.")
        return
    with get_connection() as connection:
        migrate_foundation(connection)
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
        connection.execute("""CREATE TABLE IF NOT EXISTS portfolio_idempotency (
            idempotency_key TEXT PRIMARY KEY,
            portfolio_id INTEGER NOT NULL REFERENCES portfolio(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL
        )""")
        connection.executescript((DB_DIR / "migrations" / "002_auth_sessions.sql").read_text(encoding="utf-8"))
        holding_columns = {row["name"] for row in connection.execute("PRAGMA table_info(account_holdings)")}
        if "currency" not in holding_columns:
            connection.execute("ALTER TABLE account_holdings ADD COLUMN currency TEXT")
        if "asset_class" not in holding_columns:
            connection.execute("ALTER TABLE account_holdings ADD COLUMN asset_class TEXT")
        user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
        if "email" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN email TEXT")
        if "password_hash" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL")
        portfolio_columns = {row["name"] for row in connection.execute("PRAGMA table_info(portfolio)")}
        if "owner_account_id" not in portfolio_columns:
            connection.execute("ALTER TABLE portfolio ADD COLUMN owner_account_id TEXT REFERENCES accounts(id)")
        idem_columns = {row["name"] for row in connection.execute("PRAGMA table_info(portfolio_idempotency)")}
        if "owner_account_id" not in idem_columns:
            connection.execute("ALTER TABLE portfolio_idempotency ADD COLUMN owner_account_id TEXT REFERENCES accounts(id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_owner ON portfolio(owner_account_id)")
        order_columns = {row["name"] for row in connection.execute("PRAGMA table_info(orders)")}
        for column, declaration in {
            "provider": "TEXT NOT NULL DEFAULT 'local_simulator'",
            "provider_order_id": "TEXT",
            "updated_at": "TEXT",
            "submitted_at": "TEXT",
            "cancelled_at": "TEXT",
            "rejection_reason": "TEXT",
            "preview_json": "TEXT",
        }.items():
            if column not in order_columns:
                connection.execute(f"ALTER TABLE orders ADD COLUMN {column} {declaration}")
        connection.executescript((DB_DIR / "migrations" / "003_batch3_order_events.sql").read_text(encoding="utf-8"))
        connection.commit()


def session_digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


@app.before_request
def load_auth_session() -> None:
    token = request.cookies.get(AUTH_COOKIE, "")
    digest = session_digest(token) if re.fullmatch(r"[0-9a-f]{64}", token) else ""
    now = datetime.now(timezone.utc)
    with get_connection() as connection:
        connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now.isoformat(),))
        session_row = connection.execute(
            "SELECT session_hash, user_id, csrf_token FROM auth_sessions WHERE session_hash = ? AND expires_at > ?",
            (digest, now.isoformat()),
        ).fetchone() if digest else None
        if session_row is None:
            token = secrets.token_hex(32)
            digest = session_digest(token)
            csrf = secrets.token_urlsafe(32)
            connection.execute("INSERT INTO auth_sessions VALUES (?, NULL, ?, ?, ?)",
                               (digest, csrf, now.isoformat(), (now + timedelta(seconds=AUTH_SESSION_SECONDS)).isoformat()))
            g.new_session_cookie = token
            g.csrf_token = csrf
            g.session_hash = digest
            g.user_id = None
        else:
            g.session_hash = session_row["session_hash"]
            g.user_id = session_row["user_id"]
            g.csrf_token = session_row["csrf_token"]
        g.account_id = None
        if g.user_id:
            row = connection.execute("SELECT id FROM accounts WHERE user_id = ? ORDER BY created_at LIMIT 1", (g.user_id,)).fetchone()
            g.account_id = row["id"] if row else None


@app.after_request
def set_auth_cookie(response):
    token = getattr(g, "new_session_cookie", None)
    if token:
        response.set_cookie(AUTH_COOKIE, token, max_age=AUTH_SESSION_SECONDS, httponly=True,
                            secure=app.config["SESSION_COOKIE_SECURE"], samesite="Lax", path="/", domain=app.config["SESSION_COOKIE_DOMAIN"])
    if request.path.startswith(("/login", "/register", "/logout", "/auth", "/get_portfolio", "/add_stock", "/delete_stock", "/portfolio_predict", "/api/watchlist", "/api/orders")):
        response.headers["Cache-Control"] = "no-store"
    return response


def csrf_is_valid() -> bool:
    body = request.get_json(silent=True) or {}
    candidate = request.headers.get("X-CSRF-Token") or body.get("csrf_token") or request.form.get("csrf_token", "")
    return bool(candidate and secrets.compare_digest(str(candidate), g.csrf_token))


def auth_error(message: str, status: int):
    if request.is_json:
        return jsonify({"success": False, "message": message}), status
    return render_template("auth.html", mode=request.path.strip("/"), error=message, csrf_token=g.csrf_token), status


def rate_limited(connection: sqlite3.Connection) -> bool:
    ip = ProcessRateLimiter.client_key(request.remote_addr)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    connection.execute("DELETE FROM auth_attempts WHERE attempted_at < ?", (cutoff,))
    attempts = connection.execute("SELECT COUNT(*) FROM auth_attempts WHERE ip_address = ? AND attempted_at >= ?", (ip, cutoff)).fetchone()[0]
    return attempts >= 30


def record_auth_failure(connection: sqlite3.Connection) -> None:
    ip = ProcessRateLimiter.client_key(request.remote_addr)
    connection.execute("INSERT INTO auth_attempts(ip_address, attempted_at) VALUES (?, ?)",
                       (ip, datetime.now(timezone.utc).isoformat()))


def rotate_session(connection: sqlite3.Connection, user_id: str) -> str:
    token = secrets.token_hex(32)
    now = datetime.now(timezone.utc)
    connection.execute("DELETE FROM auth_sessions WHERE session_hash = ?", (g.session_hash,))
    connection.execute("INSERT INTO auth_sessions(session_hash, user_id, csrf_token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                       (session_digest(token), user_id, secrets.token_urlsafe(32), now.isoformat(),
                        (now + timedelta(seconds=AUTH_SESSION_SECONDS)).isoformat()))
    return token


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        number = float(value)
        return number if math.isfinite(number) else default
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
    try:
        return fetch_news_items(query, market, session=create_http_session())
    except Exception:
        return []


def generate_rule_based_insight(
    price: float,
    change: float,
    trend: list[float],
    market: str,
    news_context: list[dict[str, str]],
) -> dict[str, str]:
    risk_level = determine_risk(change, trend)
    news_bias = "Unscored"
    spread = max(trend) - min(trend) if trend else 0
    baseline = max(abs(trend[-1]), 1) if trend else 1
    volatility_ratio = spread / baseline * 100
    trend_up = len(trend) >= 2 and trend[-1] >= trend[0]
    pattern = detect_market_pattern(trend, change)

    if pattern == "breakout":
        summary = f"{market} price is above the recent range near {price:.2f}. This price pattern does not establish its cause."
    elif pattern == "breakdown":
        summary = f"{market} price is below the recent range near {price:.2f}. This price pattern does not establish its cause."
    elif pattern == "consolidation":
        summary = f"{market} price has moved within a relatively tight band near {price:.2f}. No cause is inferred."
    elif change >= 0:
        summary = f"{market} price is higher over the selected history and is near {price:.2f}. Price direction does not identify a cause."
    else:
        summary = f"{market} price is lower over the selected history and is near {price:.2f}. Price direction does not identify a cause."
    beginner_tip = build_beginner_tip(market, news_bias, risk_level, pattern, trend_up, volatility_ratio)

    return {
        "summary": summary,
        "risk_level": risk_level,
        "beginner_tip": beginner_tip,
        "source": "price_history_only",
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
    # Deterministic, price-only summary. Headlines remain separately linked and are
    # never converted into causal claims by an ungrounded local model.
    return generate_rule_based_insight(price, change, trend, market, news_context)


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
        open_number = safe_float(open_value, float("nan"))
        high_number = safe_float(high_value, float("nan"))
        low_number = safe_float(low_value, float("nan"))
        close_number = safe_float(close_value, float("nan"))
        if not all(math.isfinite(value) and value > 0 for value in (open_number, high_number, low_number, close_number)):
            continue
        ts_ms = int(timestamp) * 1000
        if previous_ts_ms is not None and ts_ms - previous_ts_ms > gap_ms:
            # Break the line between separate trading sessions so the chart
            # does not draw a misleading diagonal across market-closed periods.
            points.append({"x": previous_ts_ms + 1, "y": None})
        points.append({"x": ts_ms, "y": round(close_number, 4)})
        candles.append(
            {
                "x": ts_ms,
                "o": round(open_number, 4),
                "h": round(high_number, 4),
                "l": round(low_number, 4),
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
    if current_price <= 0:
        current_price = closes_clean[-1]
    if previous_close <= 0:
        previous_close = closes_clean[0]
    change_amount = current_price - previous_close if previous_close > 0 and current_price > 0 else None
    change_percent = ((current_price - previous_close) / previous_close) * 100 if previous_close else 0
    name = meta.get("shortName") or DISPLAY_NAMES.get(symbol.upper(), symbol.upper())
    currency = meta.get("currency") or ("INR" if market == "India" else "USD")
    exchange = meta.get("fullExchangeName") or meta.get("exchangeName") or market
    source_timestamp = safe_float(meta.get("regularMarketTime"), timestamps[-1] if timestamps else 0)
    news = fetch_news(name, market)
    insight = generate_ai_insight(current_price, change_percent, closes_clean, market, symbol, news)

    return {
        "symbol": symbol.upper(),
        "name": name,
        "market": market,
        "currency": currency,
        "exchange": exchange,
        "price": round(current_price, 4 if market == "Forex" else 2),
        "change": round(change_amount, 4 if market == "Forex" else 2) if change_amount is not None else None,
        "change_percent": round(change_percent, 2),
        "range": range_key,
        "history": {
            "points": points,
            "candles": candles,
        },
        "trend_status": build_trend_status(change_percent, closes_clean),
        "insight": insight,
        "news": news,
        "move_analysis": analyze_move(
            points,
            "Yahoo Finance chart snapshot/history",
            news,
        ),
        "mode": "snapshot",
        "data_status": "snapshot",
        "freshness": quote_freshness(datetime.fromtimestamp(source_timestamp, timezone.utc).isoformat().replace("+00:00", "Z") if source_timestamp > 0 else None),
        "source": "Yahoo Finance chart endpoint (snapshot/history; not a live exchange feed)",
        "event_timestamp": datetime.fromtimestamp(source_timestamp, timezone.utc).isoformat().replace("+00:00", "Z") if source_timestamp > 0 else None,
        "last_received_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "news_status": "available" if news else "unavailable",
    }


def fetch_market_data(symbol: str, market: str | None = None, range_key: str = "day") -> dict[str, Any]:
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9.^=-]{1,20}", symbol.strip()):
        raise ValueError("Enter a valid supported symbol.")
    market_key = (market or "").strip().lower()
    if market_key and market_key not in MARKET_PRESETS:
        raise ValueError("Select a supported market.")
    if range_key not in RANGE_CONFIG:
        raise ValueError("Select a supported chart range.")
    cache_key = (market_key, symbol.strip().upper(), range_key)
    now = time.time()
    cached = ASSET_CACHE.get(cache_key)
    if cached and now - cached[0] < 20:
        return cached[1]
    for expired_key, (stamp, _) in list(ASSET_CACHE.items()):
        if now - stamp >= 20:
            ASSET_CACHE.pop(expired_key, None)

    candidates = normalize_symbol(symbol, market_key)
    final_error = None
    for candidate in candidates:
        try:
            result = fetch_yahoo_chart(candidate, range_key)
            parsed = parse_chart_result(candidate, range_key, result)
            if cache_key not in ASSET_CACHE and len(ASSET_CACHE) >= MAX_ASSET_CACHE_ENTRIES:
                oldest_key = min(ASSET_CACHE, key=lambda key: ASSET_CACHE[key][0])
                ASSET_CACHE.pop(oldest_key, None)
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
                        "change": data.get("change"),
                        "change_percent": data["change_percent"],
                        "market": data["market"],
                        "data_status": data.get("data_status", "snapshot"),
                        "freshness": data.get("freshness", "unknown"),
                        "source": data.get("source"),
                        "event_timestamp": data.get("event_timestamp"),
                        "last_received_at": data.get("last_received_at"),
                    }
                )
            except Exception:
                continue
        overview[market_name] = cards
    OVERVIEW_CACHE = (now, overview)
    return overview


def quote_freshness(event_timestamp: str | None) -> str:
    if not event_timestamp:
        return "unknown"
    try:
        source_time = datetime.fromisoformat(event_timestamp.replace("Z", "+00:00"))
        age_seconds = (datetime.now(timezone.utc) - source_time.astimezone(timezone.utc)).total_seconds()
        return "stale" if age_seconds > 86400 or age_seconds < -300 else "snapshot-current"
    except (AttributeError, TypeError, ValueError):
        return "unknown"


def calculate_portfolio_metrics(rows: list[sqlite3.Row]) -> dict[str, Any]:
    holdings: list[dict[str, Any]] = []
    totals: dict[str, dict[str, Decimal]] = {}
    complete_marks = True

    def amount(value: Decimal | None) -> str | None:
        if value is None:
            return None
        return format(value.quantize(Decimal("0.01")), "f")

    for row in rows:
        try:
            quantity = Decimal(str(row["quantity"]))
            unit_cost = Decimal(str(row["purchase_price"]))
            if not quantity.is_finite() or quantity <= 0 or not unit_cost.is_finite() or unit_cost < 0:
                raise InvalidOperation
            cost_basis = quantity * unit_cost
        except (InvalidOperation, ValueError, OverflowError):
            complete_marks = False
            continue

        asset = None
        try:
            asset = fetch_market_data(row["symbol"], row["asset_class"].lower() if row["asset_class"] else None, "day")
        except Exception:
            complete_marks = False
        currency = (asset.get("currency") if asset else None) or row["currency"] or "UNKNOWN"
        market = (asset.get("market") if asset else None) or row["asset_class"] or "Unknown"
        bucket = totals.setdefault(currency, {"cost": Decimal("0"), "value": Decimal("0")})
        bucket["cost"] += cost_basis
        price = None
        market_value = None
        pnl = None
        if asset:
            try:
                candidate = Decimal(str(asset["price"]))
                if candidate.is_finite() and candidate > 0:
                    price = candidate
                    market_value = quantity * price
                    pnl = market_value - cost_basis
                    bucket["value"] += market_value
            except (InvalidOperation, ValueError, KeyError, OverflowError):
                pass
        event_timestamp = asset.get("event_timestamp") if asset else None
        freshness = quote_freshness(event_timestamp) if asset else "unavailable"
        if asset and freshness != "snapshot-current":
            complete_marks = False
        if market_value is None:
            complete_marks = False
        holdings.append({
            "id": row["id"], "symbol": asset["symbol"] if asset else row["symbol"],
            "market": market, "currency": currency, "quantity": str(quantity),
            "price": str(price) if price is not None else None,
            "average_entry": str(unit_cost), "purchase_price": str(unit_cost),
            "cost_basis": str(cost_basis), "value": str(market_value) if market_value is not None else None,
            "unrealized_pnl": str(pnl) if pnl is not None else None,
            "change_percent": asset.get("change_percent") if asset else None,
            "source": asset.get("source", "Unavailable") if asset else "Unavailable",
            "data_status": asset.get("data_status", "unavailable") if asset else "unavailable",
            "freshness": freshness,
            "event_timestamp": event_timestamp,
            "last_received_at": asset.get("last_received_at") if asset else None,
        })

    single_currency = len(totals) <= 1 and "UNKNOWN" not in totals
    only_bucket = next(iter(totals.values()), {"cost": Decimal("0"), "value": Decimal("0")})
    complete = complete_marks and single_currency
    by_currency = {currency: {"cost_basis": amount(values["cost"]),
                              "market_value": amount(values["value"]) if complete_marks else None,
                              "unrealized_pnl": amount(values["value"] - values["cost"]) if complete_marks else None}
                   for currency, values in totals.items()}
    return {"holdings": holdings,
            "summary": {"total_investment": amount(only_bucket["cost"]) if single_currency else None,
                        "current_value": amount(only_bucket["value"]) if complete else None,
                        "profit_loss": amount(only_bucket["value"] - only_bucket["cost"]) if complete else None,
                        "currency": next(iter(totals), "USD") if single_currency else None,
                        "totals_by_currency": by_currency,
                        "valuation_status": "complete" if complete else "partial_or_multi_currency",
                        "cash_balance": None, "equity": None, "buying_power": None,
                        "account_source": "local portfolio records; not broker-synced",
                        "realized_pnl": None}}


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


@app.get("/health")
def health_check():
    import sys
    return jsonify({
        "status": "ok",
        "service": "alphascope",
        "db_backend": database_backend(),
        "python": sys.version.split()[0],
    })


@app.get("/auth/csrf")
def auth_csrf():
    return jsonify({"csrf_token": g.csrf_token})


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("auth.html", mode="register", error="", csrf_token=g.csrf_token)
    if not csrf_is_valid():
        return auth_error("Form expired. Reload the page and try again.", 400)
    data = request.get_json(silent=True) or request.form
    email, password = str(data.get("email", "")).strip().casefold(), str(data.get("password", ""))
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254 or not 12 <= len(password) <= 256:
        return auth_error("Enter a valid email and a password between 12 and 256 characters.", 400)
    import uuid
    with get_connection() as connection:
        if rate_limited(connection):
            return auth_error("Too many attempts. Try again in 15 minutes.", 429)
        if connection.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            record_auth_failure(connection)
            return auth_error("Unable to create an account with those details.", 400)
        user_id, account_id = str(uuid.uuid4()), str(uuid.uuid4())
        try:
            connection.execute("INSERT INTO users(id, email, password_hash) VALUES (?, ?, ?)",
                               (user_id, email, generate_password_hash(password)))
            connection.execute("INSERT INTO accounts(id, user_id, name) VALUES (?, ?, 'Personal')", (account_id, user_id))
            connection.execute("INSERT INTO portfolios(id, account_id, name) VALUES (?, ?, 'Main')", (str(uuid.uuid4()), account_id))
            token = rotate_session(connection, user_id)
        except sqlite3.IntegrityError:
            record_auth_failure(connection)
            return auth_error("Unable to create an account with those details.", 400)
    response = jsonify({"success": True, "redirect": "/"}) if request.is_json else redirect("/")
    response.set_cookie(AUTH_COOKIE, token, max_age=AUTH_SESSION_SECONDS, httponly=True,
                        secure=app.config["SESSION_COOKIE_SECURE"], samesite="Lax", path="/", domain=app.config["SESSION_COOKIE_DOMAIN"])
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("auth.html", mode="login", error="", csrf_token=g.csrf_token)
    if not csrf_is_valid():
        return auth_error("Form expired. Reload the page and try again.", 400)
    data = request.get_json(silent=True) or request.form
    email, password = str(data.get("email", "")).strip().casefold(), str(data.get("password", ""))
    with get_connection() as connection:
        if rate_limited(connection):
            return auth_error("Too many attempts. Try again in 15 minutes.", 429)
        user = connection.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        if len(password) > 256 or not user or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
            record_auth_failure(connection)
            return auth_error("Email or password is incorrect.", 401)
        token = rotate_session(connection, user["id"])
    response = jsonify({"success": True, "redirect": "/"}) if request.is_json else redirect("/")
    response.set_cookie(AUTH_COOKIE, token, max_age=AUTH_SESSION_SECONDS, httponly=True,
                        secure=app.config["SESSION_COOKIE_SECURE"], samesite="Lax", path="/", domain=app.config["SESSION_COOKIE_DOMAIN"])
    return response


@app.post("/logout")
def logout():
    if not g.user_id or not csrf_is_valid():
        return jsonify({"success": False, "message": "Authentication or request token is invalid."}), 401
    with get_connection() as connection:
        connection.execute("DELETE FROM auth_sessions WHERE session_hash = ?", (g.session_hash,))
    response = redirect("/login")
    response.delete_cookie(AUTH_COOKIE, path="/", domain=app.config["SESSION_COOKIE_DOMAIN"])
    return response


@app.route("/")
def index() -> str:
    if not g.user_id:
        return redirect("/login")
    market_socket_url = os.getenv("MARKET_WS_URL", "")
    if not market_socket_url and not (os.getenv("RENDER") or os.getenv("VERCEL")) and os.getenv("APCA_API_KEY_ID") and os.getenv("APCA_API_SECRET_KEY"):
        market_socket_url = "ws://localhost:8765"
    return render_template("index.html", default_symbol=DEFAULT_SYMBOL, csrf_token=g.csrf_token,
                           market_socket_url=market_socket_url)


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


@app.route("/api/macro")
def macro_dashboard() -> Any:
    # FRED requires a registered key. Without one we explicitly return the
    # unavailable state; no placeholder observations or calendar events.
    series = []
    for sid in ("CPIAUCSL", "FEDFUNDS", "UNRATE", "GDP", "DGS10"):
        try:
            series.append(fetch_fred_series(sid, os.getenv("FRED_API_KEY")))
        except requests.RequestException:
            series.append({"series_id": sid, "status": "unavailable", "observations": [],
                           "message": "FRED could not be reached. Retry later."})
        except (ValueError, TypeError):
            series.append({"series_id": sid, "status": "unavailable", "observations": [],
                           "message": "FRED returned an invalid response."})
    return jsonify({"success": True, "source": "FRED / Federal Reserve Bank of St. Louis", "data": series,
                    "events": {"status": "unavailable", "message": "No free, verified event calendar is configured. See official FOMC calendar and BEA/BLS release calendars."},
                    "official_calendars": ["https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", "https://www.bea.gov/news", "https://www.bls.gov/schedule/news_release/"]})


@app.route("/api/news")
def news_feed() -> Any:
    query = request.args.get("q", DEFAULT_SYMBOL).strip()[:80]
    market = request.args.get("market", "US").strip()[:20]
    if not query:
        return jsonify({"success": False, "message": "Provide a search term."}), 400
    try:
        items = fetch_news_items(query, market, session=create_http_session())
        return jsonify({"success": True, "status": "available" if items else "empty", "source": "Google News RSS; publisher links retained", "data": items})
    except (requests.RequestException, ValueError, ET.ParseError):
        return jsonify({"success": True, "status": "unavailable", "source": "Google News RSS", "data": []})


@app.route("/api/move-analysis")
def move_analysis() -> Any:
    symbol = request.args.get("symbol", DEFAULT_SYMBOL)
    market = request.args.get("market")
    range_key = request.args.get("range", "day")
    try:
        asset = fetch_market_data(symbol, market, range_key)
        result = analyze_move(asset["history"]["points"], asset.get("source", "Unknown snapshot source"), asset.get("news", []))
        return jsonify({"success": True, "data": result})
    except Exception as error:
        return jsonify({"success": False, "message": str(error)}), 400


@app.route("/add_stock", methods=["POST"])
def add_stock() -> Any:
    if not g.user_id or not g.account_id:
        return jsonify({"success": False, "message": "Sign in to access your portfolio."}), 401
    if not csrf_is_valid():
        return jsonify({"success": False, "message": "Invalid request token."}), 400
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol", "")).strip()
    market = str(data.get("market", "")).strip().lower() or infer_market_for_symbol(symbol)
    try:
        quantity = Decimal(str(data.get("quantity", "")))
        if not quantity.is_finite() or quantity <= 0 or quantity > Decimal("1000000000000") or quantity.as_tuple().exponent < -8:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return jsonify({"success": False, "message": "Quantity must be a positive decimal with at most 8 fractional digits."}), 400
    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if not idempotency_key or len(idempotency_key) > 128:
        return jsonify({"success": False, "message": "A valid Idempotency-Key header is required."}), 400
    if not symbol:
        return jsonify({"success": False, "message": "Provide a valid asset symbol and quantity."}), 400

    try:
        asset = validated_trade_asset(symbol, market)
    except Exception as error:
        return jsonify({"success": False, "message": str(error)}), 400

    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT id, symbol, quantity FROM account_holdings WHERE account_id = ? AND idempotency_key = ?", (g.account_id, idempotency_key)).fetchone()
            if existing:
                connection.commit()
                if existing["symbol"] != asset["symbol"] or Decimal(existing["quantity"]) != quantity:
                    return jsonify({"success": False, "message": "This Idempotency-Key was already used for a different holding."}), 409
                return jsonify({"success": True, "message": "This holding was already recorded.", "id": existing["id"]})
            import uuid
            holding_id = str(uuid.uuid4())
            connection.execute("INSERT INTO account_holdings(id, account_id, symbol, quantity, purchase_price, idempotency_key, created_at, currency, asset_class) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (holding_id, g.account_id, asset["symbol"], str(quantity), str(Decimal(str(asset["price"]))), idempotency_key, datetime.now(timezone.utc).isoformat(), asset.get("currency", "USD"), asset.get("market", "US")))
            connection.commit()
    except sqlite3.IntegrityError:
        return jsonify({"success": False, "message": "This request was already recorded."}), 409
    return jsonify({"success": True, "id": holding_id, "message": f"{asset['symbol']} added to portfolio."})


def primary_portfolio(connection: sqlite3.Connection, account_id: str) -> str | None:
    row = connection.execute("SELECT id FROM portfolios WHERE account_id = ? ORDER BY created_at LIMIT 1", (account_id,)).fetchone()
    return row["id"] if row else None


def ensure_instrument(connection: sqlite3.Connection, asset: dict[str, Any]) -> str:
    symbol, asset_class, exchange = asset["symbol"], asset["market"], asset.get("exchange")
    row = connection.execute("SELECT id FROM instruments WHERE symbol = ? AND asset_class = ? AND COALESCE(exchange, '') = COALESCE(?, '')",
                             (symbol, asset_class, exchange)).fetchone()
    if row:
        return row["id"]
    instrument_id = str(uuid.uuid4())
    connection.execute("INSERT INTO instruments(id, symbol, asset_class, exchange, currency, provider, provider_symbol) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (instrument_id, symbol, asset_class, exchange, asset.get("currency", "USD"), "Yahoo Finance", symbol))
    return instrument_id


def default_watchlist(connection: sqlite3.Connection, account_id: str) -> str:
    row = connection.execute("SELECT id FROM watchlists WHERE account_id = ? AND name = 'Default'", (account_id,)).fetchone()
    if row:
        return row["id"]
    watchlist_id = str(uuid.uuid4())
    connection.execute("INSERT INTO watchlists(id, account_id, name) VALUES (?, ?, 'Default')", (watchlist_id, account_id))
    return watchlist_id


def order_event(connection: sqlite3.Connection, order_id: str, account_id: str, status: str, detail: str) -> None:
    connection.execute("INSERT INTO order_events(id, order_id, account_id, status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (str(uuid.uuid4()), order_id, account_id, status, detail, datetime.now(timezone.utc).isoformat()))


def validated_trade_asset(symbol: Any, market: Any) -> dict[str, Any]:
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9^.=\-]{1,20}", symbol.strip()):
        raise ValueError("Enter a valid supported symbol.")
    if market not in {"us", "india", "crypto", "forex"}:
        raise ValueError("Select a supported market.")
    return fetch_market_data(symbol.strip(), market, "day")


def infer_market_for_symbol(symbol: str) -> str:
    cleaned = symbol.strip().upper()
    if cleaned in CRYPTO_ALIASES or cleaned.endswith("-USD"):
        return "crypto"
    if cleaned.endswith((".NS", ".BO")) or cleaned in INDIA_PLAIN_SYMBOLS:
        return "india"
    if cleaned.endswith("=X") or cleaned in FOREX_ALIASES:
        return "forex"
    return "us"


def parse_trade_quantity(value: Any) -> Decimal:
    try:
        quantity = Decimal(str(value))
        if not quantity.is_finite() or quantity <= 0 or quantity > Decimal("1000000000000") or quantity.as_tuple().exponent < -8:
            raise InvalidOperation
        return quantity
    except (InvalidOperation, ValueError):
        raise ValueError("Quantity must be positive, finite, at most 12 digits before the decimal, and no more than 8 decimal places.")


@app.get("/api/watchlist")
def list_watchlist() -> Any:
    if not g.user_id or not g.account_id:
        return jsonify({"success": False, "message": "Sign in to access your watchlist."}), 401
    with get_connection() as connection:
        watchlist_id = default_watchlist(connection, g.account_id)
        rows = connection.execute("SELECT i.symbol, i.asset_class, i.exchange, i.currency FROM watchlist_items w JOIN instruments i ON i.id = w.instrument_id WHERE w.watchlist_id = ? ORDER BY w.added_at DESC", (watchlist_id,)).fetchall()
        connection.commit()
    items = []
    for row in rows:
        try:
            quote = fetch_market_data(row["symbol"], row["asset_class"].lower(), "day")
            items.append({"symbol": quote["symbol"], "name": quote["name"], "market": quote["market"], "price": quote["price"],
                          "currency": quote["currency"], "change": quote.get("change"), "change_percent": quote["change_percent"], "source": quote["source"],
                          "data_status": quote["data_status"], "freshness": quote.get("freshness", quote_freshness(quote.get("event_timestamp"))), "event_timestamp": quote["event_timestamp"], "last_received_at": quote["last_received_at"]})
        except Exception:
            items.append({"symbol": row["symbol"], "market": row["asset_class"], "price": None, "currency": row["currency"],
                          "source": "Unavailable", "data_status": "unavailable", "freshness": "unavailable", "event_timestamp": None, "last_received_at": None})
    return jsonify({"success": True, "data": items, "limit": 30})


@app.post("/api/watchlist")
def add_watchlist_item() -> Any:
    if not g.user_id or not g.account_id:
        return jsonify({"success": False, "message": "Sign in to access your watchlist."}), 401
    if not csrf_is_valid():
        return jsonify({"success": False, "message": "Invalid request token."}), 400
    payload = request.get_json(silent=True) or {}
    try:
        asset = validated_trade_asset(payload.get("symbol"), payload.get("market"))
    except ValueError as error:
        return jsonify({"success": False, "message": str(error)}), 400
    except Exception:
        return jsonify({"success": False, "message": "Symbol could not be verified by the available market-data source."}), 422
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        watchlist_id = default_watchlist(connection, g.account_id)
        count = connection.execute("SELECT COUNT(*) FROM watchlist_items WHERE watchlist_id = ?", (watchlist_id,)).fetchone()[0]
        instrument_id = ensure_instrument(connection, asset)
        try:
            connection.execute("INSERT INTO watchlist_items(watchlist_id, instrument_id) VALUES (?, ?)", (watchlist_id, instrument_id))
        except sqlite3.IntegrityError:
            connection.rollback()
            return jsonify({"success": False, "message": "That symbol is already in your watchlist."}), 409
        if count >= 30:
            connection.rollback()
            return jsonify({"success": False, "message": "Watchlist is limited to 30 instruments."}), 409
        connection.commit()
    return jsonify({"success": True, "message": f"{asset['symbol']} added to watchlist."}), 201


@app.delete("/api/watchlist")
def remove_watchlist_item() -> Any:
    if not g.user_id or not g.account_id:
        return jsonify({"success": False, "message": "Sign in to access your watchlist."}), 401
    if not csrf_is_valid():
        return jsonify({"success": False, "message": "Invalid request token."}), 400
    payload = request.get_json(silent=True) or {}
    symbol = str(payload.get("symbol", "")).strip().upper()
    with get_connection() as connection:
        deleted = connection.execute("DELETE FROM watchlist_items WHERE watchlist_id IN (SELECT id FROM watchlists WHERE account_id = ? AND name = 'Default') AND instrument_id IN (SELECT id FROM instruments WHERE symbol = ?)", (g.account_id, symbol))
        connection.commit()
    if deleted.rowcount == 0:
        return jsonify({"success": False, "message": "Watchlist symbol not found."}), 404
    return jsonify({"success": True, "message": f"{symbol} removed from watchlist."})


@app.post("/api/orders/preview")
def preview_order() -> Any:
    if not g.user_id or not g.account_id:
        return jsonify({"success": False, "message": "Sign in to create an order preview."}), 401
    if not csrf_is_valid():
        return jsonify({"success": False, "message": "Invalid request token."}), 400
    payload = request.get_json(silent=True) or {}
    key = request.headers.get("Idempotency-Key", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
        return jsonify({"success": False, "message": "Provide a valid Idempotency-Key."}), 400
    side, order_type, market = payload.get("side"), payload.get("order_type"), payload.get("market")
    if side not in {"buy", "sell"} or order_type not in {"market", "limit"}:
        return jsonify({"success": False, "message": "Supported actions are buy or sell, using market or limit orders."}), 400
    try:
        quantity = parse_trade_quantity(payload.get("quantity"))
        limit_price = None
        if order_type == "limit":
            limit_price = Decimal(str(payload.get("limit_price", "")))
            if not limit_price.is_finite() or limit_price <= 0 or limit_price > Decimal("1000000000000") or limit_price.as_tuple().exponent < -8:
                raise InvalidOperation
        elif payload.get("limit_price") not in (None, ""):
            return jsonify({"success": False, "message": "Market orders must not include a limit price."}), 400
    except (ValueError, InvalidOperation):
        return jsonify({"success": False, "message": "Enter a positive finite quantity and, for limit orders, a positive limit price (up to 8 decimal places)."}), 400
    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9^.=\-]{1,20}", symbol.strip()):
        return jsonify({"success": False, "message": "Enter a valid supported symbol."}), 400
    with get_connection() as connection:
        duplicate = connection.execute("SELECT o.id, i.symbol, i.asset_class, o.side, o.order_type, o.quantity, o.limit_price, o.status, o.preview_json FROM orders o JOIN instruments i ON i.id = o.instrument_id WHERE o.account_id = ? AND o.idempotency_key = ?", (g.account_id, key)).fetchone()
    if duplicate:
        same = (duplicate["symbol"] == symbol.strip().upper() and duplicate["asset_class"].lower() == market and duplicate["side"] == side and duplicate["order_type"] == order_type
                and Decimal(duplicate["quantity"]) == quantity
                and (Decimal(duplicate["limit_price"]) if duplicate["limit_price"] else None) == limit_price)
        if not same:
            return jsonify({"success": False, "message": "This Idempotency-Key was already used for a different order."}), 409
        return jsonify({"success": True, "order_id": duplicate["id"], "status": duplicate["status"],
                        "preview": json.loads(duplicate["preview_json"] or "{}"), "idempotent_replay": True,
                        "confirmation_required": duplicate["status"] == "draft"})
    try:
        asset = validated_trade_asset(symbol, market)
        reference_price = Decimal(str(asset["price"]))
        if not reference_price.is_finite() or reference_price <= 0:
            raise ValueError
    except (ValueError, InvalidOperation):
        return jsonify({"success": False, "message": "Order preview requires a valid positive reference price."}), 422
    except Exception:
        return jsonify({"success": False, "message": "Market data is unavailable; no order preview was created."}), 503
    if side == "sell":
        with get_connection() as connection:
            holding_rows = connection.execute("SELECT quantity FROM account_holdings WHERE account_id = ? AND UPPER(symbol) = ?", (g.account_id, asset["symbol"])).fetchall()
            pending = connection.execute("SELECT quantity FROM orders o JOIN instruments i ON i.id = o.instrument_id WHERE o.account_id = ? AND i.symbol = ? AND o.side = 'sell' AND o.status = 'accepted'", (g.account_id, asset["symbol"])).fetchall()
        available = sum((Decimal(row["quantity"]) for row in holding_rows), Decimal("0")) - sum((Decimal(row["quantity"]) for row in pending), Decimal("0"))
        if quantity > available:
            return jsonify({"success": False, "message": "Simulated sell quantity exceeds locally tracked available holdings; short selling is disabled."}), 400
    portfolio_id = None
    order_id = str(uuid.uuid4())
    estimated = quantity * (limit_price if limit_price is not None else reference_price)
    preview = {"symbol": asset["symbol"], "name": asset["name"], "market": asset["market"], "side": side,
               "order_type": order_type, "quantity": str(quantity), "limit_price": str(limit_price) if limit_price is not None else None,
               "reference_price": str(reference_price), "estimated_notional": str(estimated), "currency": asset["currency"],
               "price_source": asset["source"], "price_status": asset["data_status"], "price_timestamp": asset["event_timestamp"],
               "last_received_at": asset["last_received_at"], "execution_mode": "local_simulator", "fill_status": "not_filled"}
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        portfolio_id = primary_portfolio(connection, g.account_id)
        if not portfolio_id:
            connection.rollback()
            return jsonify({"success": False, "message": "No portfolio is available for this account."}), 409
        instrument_id = ensure_instrument(connection, asset)
        try:
            connection.execute("INSERT INTO orders(id, account_id, portfolio_id, instrument_id, idempotency_key, side, order_type, quantity, limit_price, status, provider, created_at, updated_at, preview_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 'local_simulator', ?, ?, ?)",
                               (order_id, g.account_id, portfolio_id, instrument_id, key, side, order_type, str(quantity), str(limit_price) if limit_price is not None else None, now, now, json.dumps(preview)))
        except sqlite3.IntegrityError:
            connection.rollback()
            return jsonify({"success": False, "message": "Order already exists for this request."}), 409
        order_event(connection, order_id, g.account_id, "draft", "Reference-price preview created; no order has been submitted.")
        connection.commit()
    return jsonify({"success": True, "order_id": order_id, "status": "draft", "preview": preview,
                    "confirmation_required": True, "idempotent_replay": False}), 201


@app.post("/api/orders/<order_id>/confirm")
def confirm_order(order_id: str) -> Any:
    if not g.user_id or not g.account_id:
        return jsonify({"success": False, "message": "Sign in to confirm an order."}), 401
    if not csrf_is_valid():
        return jsonify({"success": False, "message": "Invalid request token."}), 400
    payload = request.get_json(silent=True) or {}
    if payload.get("confirmed") is not True:
        return jsonify({"success": False, "message": "Explicit order confirmation is required."}), 400
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute("SELECT id, status, provider FROM orders WHERE id = ? AND account_id = ?", (order_id, g.account_id)).fetchone()
        if not order:
            connection.rollback()
            return jsonify({"success": False, "message": "Order not found."}), 404
        if order["status"] == "accepted" and order["provider"] == "local_simulator":
            connection.commit()
            return jsonify({"success": True, "status": "accepted", "fill_status": "not_filled", "message": "Already accepted by the local simulator; no fill or balance change occurred."})
        if order["status"] != "draft" or order["provider"] != "local_simulator":
            connection.rollback()
            return jsonify({"success": False, "message": "Order is not a confirmable local-simulator draft."}), 409
        connection.execute("UPDATE orders SET status = 'accepted', submitted_at = ?, updated_at = ? WHERE id = ? AND account_id = ?", (now, now, order_id, g.account_id))
        order_event(connection, order_id, g.account_id, "accepted", "Accepted by the local simulator. No fill, cash movement, or position change was generated.")
        connection.commit()
    return jsonify({"success": True, "status": "accepted", "fill_status": "not_filled",
                    "message": "Accepted by the local simulator. This is not a brokerage order and no fill or balance change occurred."})


@app.post("/api/orders/<order_id>/cancel")
def cancel_order(order_id: str) -> Any:
    if not g.user_id or not g.account_id:
        return jsonify({"success": False, "message": "Sign in to cancel an order."}), 401
    if not csrf_is_valid():
        return jsonify({"success": False, "message": "Invalid request token."}), 400
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute("SELECT status FROM orders WHERE id = ? AND account_id = ? AND provider = 'local_simulator'", (order_id, g.account_id)).fetchone()
        if not order:
            connection.rollback()
            return jsonify({"success": False, "message": "Order not found."}), 404
        if order["status"] not in {"draft", "accepted"}:
            connection.rollback()
            return jsonify({"success": False, "message": "This order is no longer cancellable."}), 409
        connection.execute("UPDATE orders SET status = 'cancelled', cancelled_at = ?, updated_at = ? WHERE id = ? AND account_id = ?", (now, now, order_id, g.account_id))
        order_event(connection, order_id, g.account_id, "cancelled", "Cancelled in the local simulator; no external order was sent.")
        connection.commit()
    return jsonify({"success": True, "status": "cancelled", "message": "Simulated order cancelled."})


@app.get("/api/orders")
def order_history() -> Any:
    if not g.user_id or not g.account_id:
        return jsonify({"success": False, "message": "Sign in to view order activity."}), 401
    with get_connection() as connection:
        rows = connection.execute("SELECT o.id, o.side, o.order_type, o.quantity, o.limit_price, o.status, o.provider, o.created_at, o.updated_at, o.preview_json, i.symbol, i.currency FROM orders o JOIN instruments i ON i.id = o.instrument_id WHERE o.account_id = ? ORDER BY o.created_at DESC LIMIT 100", (g.account_id,)).fetchall()
        events = connection.execute("SELECT order_id, status, detail, created_at FROM order_events WHERE account_id = ? ORDER BY created_at DESC LIMIT 200", (g.account_id,)).fetchall()
    return jsonify({"success": True,
                    "data": [{"id": row["id"], "symbol": row["symbol"], "side": row["side"], "order_type": row["order_type"],
                              "quantity": row["quantity"], "limit_price": row["limit_price"], "currency": row["currency"],
                              "status": row["status"], "provider": row["provider"], "created_at": row["created_at"],
                              "updated_at": row["updated_at"], "fill_status": "not_filled",
                              "preview": json.loads(row["preview_json"] or "{}") } for row in rows],
                    "events": [dict(row) for row in events]})


@app.route("/get_portfolio")
def get_portfolio() -> Any:
    if not g.user_id or not g.account_id:
        return jsonify({"success": False, "message": "Sign in to access your portfolio."}), 401
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT id, symbol, quantity, purchase_price, created_at, currency, asset_class FROM account_holdings WHERE account_id = ? ORDER BY created_at DESC",
            (g.account_id,),
        ).fetchall()
    try:
        return jsonify({"success": True, "data": calculate_portfolio_metrics(rows)})
    except Exception as error:
        return jsonify({"success": False, "message": str(error)}), 400


@app.route("/delete_stock/<holding_id>", methods=["DELETE"])
def delete_stock(holding_id: str) -> Any:
    if not g.user_id or not g.account_id:
        return jsonify({"success": False, "message": "Sign in to access your portfolio."}), 401
    if not csrf_is_valid():
        return jsonify({"success": False, "message": "Invalid request token."}), 400
    with get_connection() as connection:
        deleted = connection.execute("DELETE FROM account_holdings WHERE id = ? AND account_id = ?", (holding_id, g.account_id))
        connection.commit()
    if deleted.rowcount == 0:
        return jsonify({"success": False, "message": "Holding not found."}), 404
    return jsonify({"success": True, "message": "Holding removed from portfolio."})


@app.route("/portfolio_predict")
def portfolio_predict() -> Any:
    if not g.user_id or not g.account_id:
        return jsonify({"success": False, "message": "Sign in to access your portfolio."}), 401
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT id, symbol, quantity, purchase_price, created_at, currency, asset_class FROM account_holdings WHERE account_id = ? ORDER BY created_at DESC",
            (g.account_id,),
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
