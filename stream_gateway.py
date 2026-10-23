"""Separate authenticated WebSocket gateway. Run with `python stream_gateway.py`."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from time import monotonic
from http.cookies import SimpleCookie

import websockets
from database.foundation import connect
from market_stream import SharedAlpacaFeed, stream_symbol

COOKIE = "alphascope_sid"
ALLOWED_ORIGINS = [value.strip() for value in os.getenv("ALPHASCOPE_ALLOWED_ORIGINS", "http://localhost:5000,http://127.0.0.1:5000").split(",") if value.strip()]


def authenticated_user(headers) -> bool:
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("Cookie", ""))
        token = cookie[COOKIE].value
    except Exception:
        return False
    import hashlib
    digest = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    with connect() as db:
        row = db.execute("SELECT 1 FROM auth_sessions WHERE session_hash = ? AND user_id IS NOT NULL AND expires_at > ?", (digest, datetime.now(timezone.utc).isoformat())).fetchone()
    return row is not None


async def main() -> None:
    feed = SharedAlpacaFeed()
    await feed.start()

    async def handler(socket):
        if not authenticated_user(socket.request.headers):
            await socket.close(code=4401, reason="authentication required")
            return
        if not feed.configured:
            await socket.send(json.dumps({"type": "status", "state": "unavailable", "reason": "provider credentials are not configured"}))
        client = socket
        try:
            await feed.set_client_symbols(client, set())
        except RuntimeError:
            await socket.close(code=4429, reason="gateway connection limit")
            return
        message_times = []

        async def send_events():
            while True:
                state = feed.clients.get(client)
                if state is None:
                    return
                try:
                    event = await asyncio.wait_for(state.queue.get(), timeout=10)
                    await socket.send(json.dumps(event, separators=(",", ":")))
                except asyncio.TimeoutError:
                    health = {name: {"connected": item.connected, "stale": item.stale} for name, item in feed.health.items()}
                    await socket.send(json.dumps({"type": "status", "state": "connected" if any(x["connected"] and not x["stale"] for x in health.values()) else "stale", "feeds": health}))

        sender = asyncio.create_task(send_events())
        try:
            async for raw in socket:
                now = monotonic()
                message_times[:] = [when for when in message_times if now - when < 60]
                if len(message_times) >= 60:
                    await socket.close(code=4429, reason="subscription rate limit")
                    return
                message_times.append(now)
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    await socket.send(json.dumps({"type": "error", "message": "invalid JSON"}))
                    continue
                action = message.get("action")
                symbols = message.get("symbols", [])
                if action not in {"subscribe", "unsubscribe"} or not isinstance(symbols, list) or len(symbols) > 20:
                    await socket.send(json.dumps({"type": "error", "message": "invalid action or subscription limit exceeded"}))
                    continue
                normalized = set()
                invalid = []
                for value in symbols:
                    parsed = stream_symbol(value) if isinstance(value, str) else None
                    if not parsed:
                        invalid.append(value)
                    else:
                        normalized.add(parsed[0] == "crypto" and parsed[1].replace("/", "-") or parsed[1])
                if invalid:
                    await socket.send(json.dumps({"type": "error", "message": "unsupported symbol", "symbols": invalid[:5]}))
                    continue
                state = feed.clients[client]
                updated = state.symbols | normalized if action == "subscribe" else state.symbols - normalized
                if len(updated) > 20:
                    await socket.send(json.dumps({"type": "error", "message": "maximum of 20 active subscriptions"}))
                    continue
                try:
                    await feed.set_client_symbols(client, updated)
                except RuntimeError as error:
                    await socket.send(json.dumps({"type": "error", "message": str(error)}))
                    continue
                await socket.send(json.dumps({"type": "subscription", "symbols": sorted(state.symbols)}))
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            await feed.remove_client(client)

    host = os.getenv("MARKET_WS_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", os.getenv("MARKET_WS_PORT", "8765")))
    try:
        async with websockets.serve(handler, host, port, origins=ALLOWED_ORIGINS, ping_interval=20, ping_timeout=20,
                                   max_size=4096, max_queue=32):
            await asyncio.Future()
    finally:
        await feed.close()


if __name__ == "__main__":
    asyncio.run(main())
