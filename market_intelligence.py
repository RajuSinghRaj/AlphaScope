"""Free, attribution-preserving macro/news helpers and evidence-only move analysis."""
from __future__ import annotations

import math
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus

import requests

FRED_SERIES = {
    "CPIAUCSL": ("Consumer Price Index", "index, 1982-84=100", "monthly"),
    "FEDFUNDS": ("Federal Funds Effective Rate", "%", "monthly"),
    "UNRATE": ("Unemployment Rate", "%", "monthly"),
    "GDP": ("Gross Domestic Product", "billions of USD, seasonally adjusted annual rate", "quarterly"),
    "DGS10": ("10-Year Treasury Constant Maturity Rate", "%", "daily"),
}
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_TERMS = "https://fred.stlouisfed.org/docs/api/terms_of_use.html"
FRED_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
NEWS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
MAX_NEWS_CACHE_ENTRIES = 256


def _store_news_cache(key: str, stamp: float, articles: list[dict[str, Any]]) -> None:
    for expired_key, (cached_at, _) in list(NEWS_CACHE.items()):
        if stamp - cached_at >= 900:
            NEWS_CACHE.pop(expired_key, None)
    if key not in NEWS_CACHE and len(NEWS_CACHE) >= MAX_NEWS_CACHE_ENTRIES:
        oldest_key = min(NEWS_CACHE, key=lambda cached_key: NEWS_CACHE[cached_key][0])
        NEWS_CACHE.pop(oldest_key, None)
    NEWS_CACHE[key] = (stamp, articles)


def utc_iso(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_news_xml(xml_text: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """Parse RSS items, keeping provider publication time distinct from receipt time."""
    received = utc_iso(now)
    root = ET.fromstring(xml_text)
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link.startswith(("https://", "http://")):
            continue
        key = re.sub(r"\W+", " ", title.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        date_text = (item.findtext("pubDate") or "").strip()
        published = None
        try:
            parsed = parsedate_to_datetime(date_text)
            if parsed:
                published = utc_iso(parsed)
        except (TypeError, ValueError, OverflowError):
            pass
        source_element = item.find("source")
        source = (source_element.text or "").strip() if source_element is not None else ""
        description = re.sub(r"<[^>]*>", " ", item.findtext("description") or "")
        description = " ".join(description.split())
        found.append({"headline": title, "source": source or "Source on article page", "url": link,
                      "published_at": published, "received_at": received,
                      "summary": description[:300], "sentiment": "Unscored"})
        if len(found) == 10:
            break
    return found


def fetch_news_items(query: str, market: str, session: Any = requests, now: float | None = None) -> list[dict[str, Any]]:
    key = f"{market}:{query}".lower()
    stamp = time.time() if now is None else now
    cached = NEWS_CACHE.get(key)
    if cached and stamp - cached[0] < 900:
        return cached[1]
    response = session.get("https://news.google.com/rss/search?q=" + quote_plus(f"{query} {market} market"), timeout=8)
    response.raise_for_status()
    articles = normalize_news_xml(response.text)
    _store_news_cache(key, stamp, articles)
    return articles


def fetch_fred_series(series_id: str, api_key: str | None, session: Any = requests,
                      now: float | None = None) -> dict[str, Any]:
    sid = series_id.upper()
    if sid not in FRED_SERIES:
        raise ValueError("Unsupported FRED series")
    label, unit, frequency = FRED_SERIES[sid]
    if not api_key:
        return {"series_id": sid, "name": label, "unit": unit, "frequency": frequency,
                "status": "unavailable", "observations": [], "source": "FRED / Federal Reserve Bank of St. Louis",
                "source_url": "https://fred.stlouisfed.org/series/" + sid,
                "message": "Configure FRED_API_KEY to load observations.", "terms_url": FRED_TERMS}
    stamp = time.time() if now is None else now
    cached = FRED_CACHE.get(sid)
    if cached and stamp - cached[0] < 21600:
        return cached[1]
    response = session.get(FRED_URL, params={"series_id": sid, "api_key": api_key, "file_type": "json", "sort_order": "desc", "limit": 12}, timeout=8)
    response.raise_for_status()
    observations = []
    payload = response.json()
    for row in payload.get("observations", []):
        value = row.get("value")
        try:
            number = float(value)
            if not math.isfinite(number):
                continue
        except (TypeError, ValueError):
            continue
        date = row.get("date")
        if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            continue
        observations.append({"observation_date": date, "value": number, "unit": unit})
    freshness = "unavailable"
    if observations:
        age_days = (datetime.now(timezone.utc).date() - datetime.fromisoformat(observations[0]["observation_date"]).date()).days
        stale_after_days = {"daily": 7, "monthly": 90, "quarterly": 180}[frequency]
        freshness = "stale" if age_days > stale_after_days else "current_for_frequency"
    result = {"series_id": sid, "name": label, "unit": unit, "frequency": frequency,
              "status": "historical" if observations else "unavailable", "freshness": freshness,
              "observations": observations,
              "last_received_at": utc_iso(), "source": "FRED / Federal Reserve Bank of St. Louis",
              "source_url": "https://fred.stlouisfed.org/series/" + sid, "terms_url": FRED_TERMS,
              "message": None if observations else "Provider returned no numeric observations."}
    FRED_CACHE[sid] = (stamp, result)
    return result


def analyze_move(history: list[dict[str, Any]], source: str, news: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Compare first/last valid chart points, and only associate contemporaneous dated headlines."""
    values = []
    for point in history:
        try:
            ts, price = int(point["x"]), float(point["y"])
            if math.isfinite(price) and price > 0:
                values.append((ts, price))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if len(values) < 2:
        return {"status": "insufficient_evidence", "summary": "Not enough valid price observations to measure a move.",
                "evidence": [], "related_news": [], "confidence": "Insufficient evidence"}
    start, end = values[0], values[-1]
    change = (end[1] - start[1]) / start[1] * 100
    start_at = datetime.fromtimestamp(start[0] / 1000, timezone.utc)
    end_at = datetime.fromtimestamp(end[0] / 1000, timezone.utc)
    evidence = [{"type": "price_history", "source": source, "start_at": utc_iso(start_at), "start_price": start[1],
                "end_at": utc_iso(end_at), "end_price": end[1], "change_percent": change}]
    matches = []
    for article in news or []:
        try:
            published = datetime.fromisoformat(article["published_at"].replace("Z", "+00:00"))
            if start_at <= published <= end_at and article.get("url"):
                matches.append({"headline": article["headline"], "source": article.get("source"),
                                "url": article["url"], "published_at": article["published_at"]})
        except (KeyError, TypeError, ValueError):
            continue
    direction = "rose" if change > 0 else "fell" if change < 0 else "was unchanged"
    summary = f"Price {direction} {abs(change):.2f}% from {utc_iso(start_at)} to {utc_iso(end_at)} ({source})."
    if matches:
        summary += " The linked headlines were published within this comparison window; timing alone does not establish causation."
    else:
        summary += " No dated, linked headlines were verified within this comparison window, so the cause is undetermined."
    return {"status": "measured_move", "summary": summary, "evidence": evidence,
            "related_news": matches, "confidence": "Measured price data; causal evidence unestablished"}
