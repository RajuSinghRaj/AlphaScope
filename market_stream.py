"""Alpaca market-data adapter primitives; never synthesizes market events."""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)


def stream_symbol(symbol: str) -> tuple[str, str] | None:
    value = symbol.strip().upper()
    if value.endswith("-USD"):
        base = value[:-4]
        if base.isalpha() and len(base) <= 12:
            return "crypto", f"{base}/USD"
    if value.isalpha() and 1 <= len(value) <= 10:
        return "stocks", value
    return None


def normalize_alpaca_event(item: dict[str, Any], asset_class: str) -> dict[str, Any] | None:
    kind = item.get("T")
    symbol = str(item.get("S", ""))
    if not symbol or kind not in {"t", "q", "b", "u", "d"}:
        return None
    if asset_class == "crypto":
        mapped = symbol.replace("/", "-")
    else:
        mapped = symbol
    result: dict[str, Any] = {
        "type": {"t": "trade", "q": "quote", "b": "bar", "u": "bar", "d": "bar"}[kind],
        "symbol": mapped,
        "provider_symbol": symbol,
        "provider": "alpaca",
        "asset_class": asset_class,
        "currency": "USD",
        "timestamp": item.get("t"),
    }
    if kind == "t":
        result.update(price=item.get("p"), size=item.get("s"), exchange=item.get("x"), trade_id=item.get("i"))
    elif kind == "q":
        result.update(bid=item.get("bp"), bid_size=item.get("bs"), ask=item.get("ap"), ask_size=item.get("as"), exchange=item.get("bx"))
    else:
        result.update(open=item.get("o"), high=item.get("h"), low=item.get("l"), close=item.get("c"), volume=item.get("v"), interval="1m")
    return result


@dataclass
class FeedHealth:
    connected: bool = False
    last_event_at: float | None = None
    error: str | None = None
    subscribed_symbols: set[str] = field(default_factory=set)

    @property
    def stale(self) -> bool:
        return not self.connected or self.last_event_at is None or time.monotonic() - self.last_event_at > 30


@dataclass
class ClientSubscriptions:
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=100))
    symbols: set[str] = field(default_factory=set)


class SharedAlpacaFeed:
    """One upstream connection per asset class, shared by downstream clients."""

    STOCK_URL = "wss://stream.data.alpaca.markets/v2/iex"
    CRYPTO_URL = "wss://stream.data.alpaca.markets/v1beta3/crypto/us"

    def __init__(self, key: str | None = None, secret: str | None = None, retry_base: float = 1.0):
        self.key = os.getenv("APCA_API_KEY_ID", "") if key is None else key
        self.secret = os.getenv("APCA_API_SECRET_KEY", "") if secret is None else secret
        self.retry_base = max(0.001, retry_base)
        self.clients: dict[Any, ClientSubscriptions] = {}
        self.health = {"stocks": FeedHealth(), "crypto": FeedHealth()}
        self.tasks: list[asyncio.Task] = []
        self.lock = asyncio.Lock()
        self.seen_events: set[tuple[Any, ...]] = set()
        self.last_event_by_symbol: dict[tuple[str, str, str], str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.key and self.secret)

    async def start(self) -> None:
        if not self.configured:
            return
        if not self.tasks:
            self.tasks = [asyncio.create_task(self._run("stocks", self.STOCK_URL)),
                          asyncio.create_task(self._run("crypto", self.CRYPTO_URL))]

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def set_client_symbols(self, client: Any, symbols: set[str]) -> None:
        async with self.lock:
            if client not in self.clients and len(self.clients) >= 100:
                raise RuntimeError("gateway connection limit reached")
            all_symbols = set(symbols)
            for existing_client, state in self.clients.items():
                if existing_client != client:
                    all_symbols.update(state.symbols)
            stocks = {symbol for symbol in all_symbols if (stream_symbol(symbol) or (None,))[0] == "stocks"}
            crypto = {symbol for symbol in all_symbols if (stream_symbol(symbol) or (None,))[0] == "crypto"}
            if len(stocks) > 30 or len(crypto) > 100:
                raise RuntimeError("shared provider subscription limit reached")
            if client not in self.clients:
                self.clients[client] = ClientSubscriptions()
            self.clients[client].symbols = symbols

    async def remove_client(self, client: Any) -> None:
        async with self.lock:
            self.clients.pop(client, None)

    async def _desired(self, asset_class: str) -> set[str]:
        async with self.lock:
            desired = set()
            for state in self.clients.values():
                for symbol in state.symbols:
                    parsed = stream_symbol(symbol)
                    if parsed and parsed[0] == asset_class:
                        desired.add(parsed[1])
            return desired

    async def publish(self, event: dict[str, Any]) -> None:
        async with self.lock:
            for state in self.clients.values():
                if event["symbol"] not in state.symbols:
                    continue
                if state.queue.full():
                    try:
                        state.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                state.queue.put_nowait(event)

    def _accept(self, event: dict[str, Any]) -> bool:
        key = (event["asset_class"], event["type"], event["symbol"], event.get("timestamp"), event.get("trade_id"))
        if key in self.seen_events:
            return False
        self.seen_events.add(key)
        if len(self.seen_events) > 5000:
            self.seen_events.pop()
        order_key = (event["asset_class"], event["type"], event["symbol"])
        previous = self.last_event_by_symbol.get(order_key)
        timestamp = event.get("timestamp")
        if previous and timestamp and timestamp < previous:
            return False
        if timestamp:
            self.last_event_by_symbol[order_key] = timestamp
        if len(self.last_event_by_symbol) > 5000:
            self.last_event_by_symbol.pop(next(iter(self.last_event_by_symbol)))
        return True

    async def _run(self, asset_class: str, url: str) -> None:
        delay = self.retry_base
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_queue=512) as socket:
                    await socket.send(json.dumps({"action": "auth", "key": self.key, "secret": self.secret}))
                    authenticated = False
                    deadline = asyncio.get_running_loop().time() + 10
                    while not authenticated:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise RuntimeError("Provider authentication timed out")
                        response = json.loads(await asyncio.wait_for(socket.recv(), timeout=remaining))
                        for message in response if isinstance(response, list) else [response]:
                            if message.get("T") == "error":
                                raise RuntimeError("Provider rejected authentication or feed entitlement")
                            if message.get("T") == "success" and message.get("msg") == "authenticated":
                                authenticated = True
                    delay = self.retry_base
                    health = self.health[asset_class]
                    health.connected, health.error = True, None
                    subscribed: set[str] = set()
                    while True:
                        desired = await self._desired(asset_class)
                        added, removed = desired - subscribed, subscribed - desired
                        if added:
                            await socket.send(json.dumps({"action": "subscribe", "trades": sorted(added), "quotes": sorted(added), "bars": sorted(added)}))
                        if removed:
                            await socket.send(json.dumps({"action": "unsubscribe", "trades": sorted(removed), "quotes": sorted(removed), "bars": sorted(removed)}))
                        subscribed = desired
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=1)
                        except asyncio.TimeoutError:
                            continue
                        payload = json.loads(raw)
                        for item in payload if isinstance(payload, list) else [payload]:
                            if item.get("T") == "error":
                                health.connected = False
                                health.error = "Provider rejected subscription or feed entitlement"
                                raise RuntimeError("Provider rejected subscription or feed entitlement")
                            if item.get("T") == "subscription":
                                health.subscribed_symbols = set(item.get("trades", [])) | set(item.get("quotes", [])) | set(item.get("bars", []))
                            event = normalize_alpaca_event(item, asset_class)
                            if event and self._accept(event):
                                health.last_event_at = time.monotonic()
                                await self.publish(event)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                health = self.health[asset_class]
                health.connected = False
                health.subscribed_symbols.clear()
                health.error = type(error).__name__
                await asyncio.sleep(delay + random.uniform(0, delay * 0.25))
                delay = min(delay * 2, 30)
