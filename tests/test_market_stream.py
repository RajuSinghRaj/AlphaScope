import unittest
import asyncio
import json
from unittest.mock import patch

from market_stream import ClientSubscriptions, FeedHealth, SharedAlpacaFeed, normalize_alpaca_event, stream_symbol
from stream_gateway import authenticated_user


class StreamAdapterTests(unittest.TestCase):
    def test_symbol_coverage_is_limited_to_us_stocks_and_usd_crypto(self):
        self.assertEqual(stream_symbol("AAPL"), ("stocks", "AAPL"))
        self.assertEqual(stream_symbol("BTC-USD"), ("crypto", "BTC/USD"))
        self.assertIsNone(stream_symbol("TCS.NS"))
        self.assertIsNone(stream_symbol("USDINR=X"))

    def test_trade_and_quote_normalization_keeps_bid_ask_distinct(self):
        trade = normalize_alpaca_event({"T": "t", "S": "AAPL", "p": 12.5, "s": 4, "x": "V", "t": "2026-10-20T12:00:00Z"}, "stocks")
        quote = normalize_alpaca_event({"T": "q", "S": "AAPL", "bp": 12.4, "ap": 12.6, "t": "2026-10-20T12:00:01Z"}, "stocks")
        self.assertEqual(trade["type"], "trade")
        self.assertEqual(trade["exchange"], "V")
        self.assertEqual((quote["bid"], quote["ask"]), (12.4, 12.6))
        self.assertNotIn("price", quote)

    def test_feed_drops_duplicate_and_out_of_order_provider_events(self):
        feed = SharedAlpacaFeed(key="key", secret="secret")
        later = normalize_alpaca_event({"T": "t", "S": "AAPL", "p": 12.5, "s": 4, "i": 2, "t": "2026-10-20T12:00:01Z"}, "stocks")
        earlier = normalize_alpaca_event({"T": "t", "S": "AAPL", "p": 12.4, "s": 4, "i": 1, "t": "2026-10-20T12:00:00Z"}, "stocks")
        self.assertTrue(feed._accept(later))
        self.assertFalse(feed._accept(later))
        self.assertFalse(feed._accept(earlier))

    def test_feed_health_marks_missing_or_old_events_stale(self):
        self.assertTrue(FeedHealth(connected=True).stale)
        self.assertTrue(FeedHealth(connected=False, last_event_at=1).stale)


class StreamHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_feed_broadcasts_only_to_subscribed_clients(self):
        feed = SharedAlpacaFeed(key="key", secret="secret")
        first, second = object(), object()
        await feed.set_client_symbols(first, {"AAPL"})
        await feed.set_client_symbols(second, {"BTC-USD"})
        event = {"type": "trade", "symbol": "AAPL", "price": 10}
        await feed.publish(event)
        self.assertEqual((await feed.clients[first].queue.get())["price"], 10)
        self.assertTrue(feed.clients[second].queue.empty())
        await feed.remove_client(first)
        self.assertNotIn(first, feed.clients)

    async def test_browser_gateway_rejects_missing_session_cookie(self):
        self.assertFalse(authenticated_user({"Cookie": ""}))

    async def test_provider_credentials_are_required_for_upstream(self):
        feed = SharedAlpacaFeed(key="", secret="")
        self.assertFalse(feed.configured)
        await feed.start()
        self.assertEqual(feed.tasks, [])

    async def test_upstream_reconnects_and_resubscribes_after_disconnect(self):
        class FakeSocket:
            def __init__(self, disconnect_after_auth):
                self.sent = []
                self.auth = json.dumps({"T": "success", "msg": "authenticated"})
                self.disconnect_after_auth = disconnect_after_auth

            async def send(self, value):
                self.sent.append(json.loads(value))

            async def recv(self):
                if self.auth:
                    value, self.auth = self.auth, None
                    return value
                if self.disconnect_after_auth:
                    self.disconnect_after_auth = False
                    raise ConnectionError("mock disconnect")
                await asyncio.Future()

        class FakeConnection:
            def __init__(self, socket):
                self.socket = socket

            async def __aenter__(self):
                return self.socket

            async def __aexit__(self, *_args):
                return False

        first, second = FakeSocket(True), FakeSocket(False)
        feed = SharedAlpacaFeed(key="key", secret="secret", retry_base=0.001)
        client = object()
        await feed.set_client_symbols(client, {"AAPL"})
        with patch("market_stream.websockets.connect", side_effect=[FakeConnection(first), FakeConnection(second)]) as connect:
            task = asyncio.create_task(feed._run("stocks", feed.STOCK_URL))
            try:
                for _ in range(100):
                    if connect.call_count >= 2 and feed.health["stocks"].connected:
                        break
                    await asyncio.sleep(0.005)
                self.assertGreaterEqual(connect.call_count, 2)
                self.assertTrue(any(message.get("action") == "subscribe" and "AAPL" in message.get("trades", []) for message in second.sent))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
