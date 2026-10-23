import os
import tempfile
import uuid
import atexit
from datetime import datetime, timezone
from pathlib import Path
import unittest
from contextlib import nullcontext
from unittest.mock import patch
from rate_limit import ProcessRateLimiter

TEMP_DB = Path(tempfile.gettempdir()) / f"alphascope-test-{uuid.uuid4().hex}.sqlite3"
atexit.register(lambda: TEMP_DB.unlink(missing_ok=True))
os.environ["DATABASE_URL"] = f"sqlite:///{TEMP_DB}"

import app as application  # noqa: E402
from database.foundation import connect, migrate  # noqa: E402


class FoundationTests(unittest.TestCase):
    def setUp(self):
        application.app.config["TESTING"] = True
        self.client = application.app.test_client()
        csrf_response = self.client.get("/auth/csrf")
        self.csrf = csrf_response.json["csrf_token"]
        self.email = f"user-{uuid.uuid4()}@example.test"
        self.client.post("/register", json={"email": self.email, "password": "correct horse battery staple", "csrf_token": self.csrf})
        self.csrf = self.client.get("/auth/csrf").json["csrf_token"]

    def test_postgres_startup_refuses_uninitialized_schema_without_migrating(self):
        class EmptyPostgresConnection:
            def __init__(self):
                self.statements = []
            def execute(self, sql, params=()):
                self.statements.append(sql)
                class Result:
                    def fetchall(self): return []
                return Result()
            def rollback(self): pass

        connection = EmptyPostgresConnection()
        with patch.object(application, "database_backend", return_value="postgresql"), \
                patch.object(application, "get_connection", return_value=nullcontext(connection)):
            with self.assertRaisesRegex(RuntimeError, "schema is not ready"):
                application.init_db()
        self.assertFalse(any("CREATE TABLE" in statement.upper() for statement in connection.statements))

    def test_foundation_foreign_keys_and_decimal_text_fields(self):
        with connect() as db:
            migrate(db)
            columns = {row["name"]: row["type"] for row in db.execute("PRAGMA table_info(positions)")}
            self.assertEqual(columns["quantity"], "TEXT")
            with self.assertRaises(Exception):
                db.execute("INSERT INTO accounts(id, user_id, name) VALUES ('a', 'missing', 'main')")

    @patch.object(application, "fetch_news", return_value=[])
    def test_quote_contract_identifies_snapshot_and_skips_nonfinite_bars(self, _news):
        data = application.parse_chart_result("AAPL", "day", {
            "meta": {"currency": "USD", "exchangeName": "NASDAQ", "regularMarketPrice": 101,
                     "chartPreviousClose": 100, "shortName": "Apple"},
            "timestamp": [1_800_000_000, 1_800_000_300, 1_800_000_600],
            "indicators": {"quote": [{"open": [100, 0, 100], "high": [101, 101, 102],
                                           "low": [99, 99, 100], "close": [100, float("nan"), 101]}]},
        })
        self.assertEqual(data["data_status"], "snapshot")
        self.assertIn("Yahoo Finance", data["source"])
        self.assertEqual(data["change"], 1)
        self.assertEqual(data["change_percent"], 1)
        self.assertIsNotNone(data["event_timestamp"])
        self.assertIsNotNone(data["last_received_at"])
        self.assertEqual(len(data["history"]["candles"]), 2)
        self.assertEqual(data["move_analysis"]["status"], "measured_move")

    def test_market_endpoint_rejects_oversized_and_malformed_symbols_offline(self):
        with patch.object(application, "fetch_yahoo_chart") as upstream:
            for symbol in ("A" * 21, "../../etc/passwd"):
                response = self.client.get("/get_stock_data", query_string={"symbol": symbol})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json["success"])
            upstream.assert_not_called()

    def test_asset_cache_has_a_fixed_size_limit(self):
        with patch.object(application, "ASSET_CACHE", {}), \
                patch.object(application, "MAX_ASSET_CACHE_ENTRIES", 1), \
                patch.object(application, "fetch_yahoo_chart", return_value={}), \
                patch.object(application, "parse_chart_result", side_effect=lambda symbol, *_: {"symbol": symbol}):
            application.fetch_market_data("AAPL")
            application.fetch_market_data("MSFT")
            self.assertEqual(len(application.ASSET_CACHE), 1)
            self.assertIn(("", "MSFT", "day"), application.ASSET_CACHE)

    def test_public_route_limits_and_spoofed_forwarding_headers(self):
        original_limiter = application.PUBLIC_RATE_LIMITER
        application.PUBLIC_RATE_LIMITER = ProcessRateLimiter(max_keys=10)
        try:
            with patch.dict(application.PUBLIC_RATE_LIMITS, {"get_stock_data": 1, "news_feed": 1}), \
                    patch.object(application, "fetch_market_data", return_value={"symbol": "AAPL"}) as market, \
                    patch.object(application, "fetch_news_items", return_value=[]) as news:
                peer = {"REMOTE_ADDR": "192.0.2.55"}
                first = self.client.get("/get_stock_data?symbol=AAPL", environ_base=peer)
                self.assertEqual(first.status_code, 200)
                limited = self.client.get("/get_stock_data?symbol=AAPL", environ_base=peer,
                                          headers={"X-Forwarded-For": "203.0.113.200"})
                self.assertEqual(limited.status_code, 429)
                self.assertEqual(limited.headers.get("Retry-After"), "60")
                self.assertEqual(limited.json["message"], "Too many requests. Please retry shortly.")
                self.assertEqual(market.call_count, 1)

                self.assertEqual(self.client.get("/api/news?q=alpha", environ_base=peer).status_code, 200)
                news_limited = self.client.get("/api/news?q=alpha", environ_base=peer)
                self.assertEqual(news_limited.status_code, 429)
                self.assertEqual(news.call_count, 1)
                self.assertEqual(self.client.get("/login", environ_base=peer).status_code, 200)
                static_response = self.client.get("/static/css/styles.css", environ_base=peer)
                self.assertEqual(static_response.status_code, 200)
                static_response.close()
        finally:
            application.PUBLIC_RATE_LIMITER = original_limiter

    def test_malformed_peer_address_is_safely_bounded_by_unknown_bucket(self):
        original_limiter = application.PUBLIC_RATE_LIMITER
        application.PUBLIC_RATE_LIMITER = ProcessRateLimiter(max_keys=10)
        try:
            with patch.dict(application.PUBLIC_RATE_LIMITS, {"get_stock_data": 1}), \
                    patch.object(application, "fetch_market_data", return_value={"symbol": "AAPL"}):
                peer = {"REMOTE_ADDR": "malformed-peer"}
                self.assertEqual(self.client.get("/get_stock_data?symbol=AAPL", environ_base=peer).status_code, 200)
                self.assertEqual(self.client.get("/get_stock_data?symbol=AAPL", environ_base=peer).status_code, 429)
        finally:
            application.PUBLIC_RATE_LIMITER = original_limiter

    @patch.dict(os.environ, {"FRED_API_KEY": ""})
    def test_macro_route_returns_explicit_no_key_and_no_fake_events(self):
        response = self.client.get("/api/macro")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(row["status"] == "unavailable" and row["observations"] == [] for row in response.json["data"]))
        self.assertEqual(response.json["events"]["status"], "unavailable")

    @patch.object(application, "fetch_news_items", return_value=[])
    def test_news_route_reports_empty_provider_state(self, _news):
        response = self.client.get("/api/news?q=AAPL&market=US")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "empty")
        self.assertEqual(response.json["data"], [])

    @patch.object(application, "fetch_market_data", return_value={"symbol": "AAPL", "price": 12.34})
    def test_add_is_idempotent_and_duplicate_is_not_written(self, _market_data):
        payload = {"symbol": "AAPL", "quantity": "0.12345678", "market": "us"}
        headers = {"Idempotency-Key": "test-request-unique-001", "X-CSRF-Token": self.csrf}
        first = self.client.post("/add_stock", json=payload, headers=headers)
        second = self.client.post("/add_stock", json=payload, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM account_holdings WHERE idempotency_key = ?", (headers["Idempotency-Key"],)).fetchone()[0], 1)

    @patch.object(application, "fetch_market_data", return_value={"symbol": "AAPL", "price": 12.34})
    def test_invalid_quantity_rejected(self, _market_data):
        response = self.client.post("/add_stock", json={"symbol": "AAPL", "quantity": "0.000000001"}, headers={"Idempotency-Key": "test-invalid", "X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 400)

    def test_portfolio_requires_auth_and_csrf(self):
        anonymous = application.app.test_client()
        self.assertEqual(anonymous.get("/get_portfolio").status_code, 200)
        self.assertEqual(anonymous.get("/portfolio_predict").status_code, 200)
        self.assertEqual(anonymous.post("/add_stock", json={}).status_code, 400)
        self.assertEqual(anonymous.delete("/delete_stock/not-an-id").status_code, 400)
        response = self.client.post("/add_stock", json={"symbol": "AAPL", "quantity": "1"}, headers={"Idempotency-Key": "no-csrf"})
        self.assertEqual(response.status_code, 400)

    @patch.object(application, "fetch_market_data", return_value={"symbol": "AAPL", "price": 12.34})
    def test_users_cannot_read_or_delete_each_others_holdings(self, _market_data):
        created = self.client.post("/add_stock", json={"symbol": "AAPL", "quantity": "0.12345678"}, headers={"Idempotency-Key": "owner-a", "X-CSRF-Token": self.csrf})
        self.assertEqual(created.status_code, 200, created.json)
        holding_id = created.json["id"]
        other = application.app.test_client()
        other_csrf = other.get("/auth/csrf").json["csrf_token"]
        other.post("/register", json={"email": f"other-{uuid.uuid4()}@example.test", "password": "correct horse battery staple", "csrf_token": other_csrf})
        self.assertEqual(other.get("/get_portfolio").json["data"]["holdings"], [])
        response = other.delete(f"/delete_stock/{holding_id}", headers={"X-CSRF-Token": other.get("/auth/csrf").json["csrf_token"]})
        self.assertEqual(response.status_code, 404)

    @patch.object(application, "fetch_market_data", return_value={"symbol": "AAPL", "price": 12.34})
    def test_quantity_precision_persists_as_decimal_text(self, _market_data):
        self.client.post("/add_stock", json={"symbol": "AAPL", "quantity": "0.12345678"}, headers={"Idempotency-Key": "precision", "X-CSRF-Token": self.csrf})
        with connect() as db:
            stored = db.execute("SELECT quantity FROM account_holdings WHERE idempotency_key = 'precision'").fetchone()[0]
        self.assertEqual(stored, "0.12345678")

    def test_legacy_records_stay_unowned(self):
        with connect() as db:
            db.execute("INSERT INTO portfolio(symbol, quantity, purchase_price) VALUES ('OLD', 1.25, 10.50)")
            legacy = db.execute("SELECT owner_account_id, quantity, purchase_price FROM portfolio WHERE symbol = 'OLD'").fetchone()
            self.assertIsNone(legacy["owner_account_id"])
            self.assertEqual((legacy["quantity"], legacy["purchase_price"]), (1.25, 10.5))
        self.assertEqual(self.client.get("/get_portfolio").json["data"]["holdings"], [])

    def test_login_rejects_invalid_password_and_rotates_server_session(self):
        anon = application.app.test_client()
        token = anon.get("/auth/csrf").json["csrf_token"]
        fail = anon.post("/login", json={"email": self.email, "password": "wrong password", "csrf_token": token})
        self.assertEqual(fail.status_code, 401)
        fresh = anon.get("/auth/csrf").json["csrf_token"]
        success = anon.post("/login", json={"email": self.email, "password": "correct horse battery staple", "csrf_token": fresh})
        self.assertEqual(success.status_code, 200)
        self.assertTrue(anon.get("/get_portfolio").json["success"])
        # FlaskClient.get_cookie() requires Flask>=2.3; read from the cookie jar instead.
        raw_cookie = next(
            (c.value for c in anon.cookie_jar if c.name == "alphascope_sid"), None
        )
        self.assertIsNotNone(raw_cookie, "alphascope_sid cookie not set after login")
        with connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM auth_sessions WHERE session_hash = ?", (raw_cookie,)).fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM auth_sessions WHERE session_hash = ?", (application.session_digest(raw_cookie),)).fetchone())

    def test_logout_revokes_session_and_disables_response_caching(self):
        # FlaskClient.get_cookie() requires Flask>=2.3; read from the cookie jar instead.
        raw_cookie = next(
            (c.value for c in self.client.cookie_jar if c.name == application.AUTH_COOKIE), None
        )
        self.assertIsNotNone(raw_cookie, "auth session cookie missing before logout")
        response = self.client.post("/logout", headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        with connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM auth_sessions WHERE session_hash = ?",
                                         (application.session_digest(raw_cookie),)).fetchone())

    def test_auth_failure_budget_does_not_count_successful_login(self):
        peer = {"REMOTE_ADDR": "198.51.100.91"}
        csrf = self.client.get("/auth/csrf", environ_base=peer).json["csrf_token"]
        failed = self.client.post("/login", json={"email": self.email, "password": "wrong password", "csrf_token": csrf}, environ_base=peer)
        self.assertEqual(failed.status_code, 401)
        csrf = self.client.get("/auth/csrf", environ_base=peer).json["csrf_token"]
        passed = self.client.post("/login", json={"email": self.email, "password": "correct horse battery staple", "csrf_token": csrf}, environ_base=peer)
        self.assertEqual(passed.status_code, 200)
        with connect() as db:
            count = db.execute("SELECT COUNT(*) FROM auth_attempts WHERE ip_address = ?", (peer["REMOTE_ADDR"],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_stream_gateway_accepts_only_a_live_authenticated_session(self):
        from stream_gateway import authenticated_user
        # FlaskClient.get_cookie() requires Flask>=2.3; read from the cookie jar instead.
        raw_cookie = next(
            (c.value for c in self.client.cookie_jar if c.name == "alphascope_sid"), None
        )
        self.assertIsNotNone(raw_cookie, "alphascope_sid cookie missing")
        self.assertTrue(authenticated_user({"Cookie": f"alphascope_sid={raw_cookie}"}))
        with connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE session_hash = ?", (application.session_digest(raw_cookie),))
        self.assertFalse(authenticated_user({"Cookie": f"alphascope_sid={raw_cookie}"}))

    def test_local_stream_url_is_configured_without_exposing_credentials(self):
        with patch.dict(os.environ, {"APCA_API_KEY_ID": "key-test-only", "APCA_API_SECRET_KEY": "secret-test-only", "MARKET_WS_URL": ""}, clear=False):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ws://localhost:8765", response.data)
        self.assertNotIn(b"key-test-only", response.data)
        self.assertNotIn(b"secret-test-only", response.data)

    def test_insert_error_rolls_back_holding_and_idempotency_key(self):
        with connect() as db:
            db.execute("CREATE TRIGGER fail_account_holding BEFORE INSERT ON account_holdings BEGIN SELECT RAISE(ABORT, 'forced rollback'); END")
        with patch.object(application, "fetch_market_data", return_value={"symbol": "AAPL", "price": 12.34}):
            response = self.client.post("/add_stock", json={"symbol": "AAPL", "quantity": "1"}, headers={"Idempotency-Key": "rollback", "X-CSRF-Token": self.csrf})
            self.assertEqual(response.status_code, 409)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM account_holdings WHERE idempotency_key = 'rollback'").fetchone()[0], 0)
            db.execute("DROP TRIGGER fail_account_holding")


class BatchThreeWorkflowTests(unittest.TestCase):
    @staticmethod
    def asset(symbol="AAPL", market="US", currency="USD", price=12.34):
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"symbol": symbol, "name": "Apple", "market": market, "currency": currency,
                "price": price, "change": round(price - 12.0, 4), "change_percent": 1.2,
                "exchange": "NASDAQ", "source": "test snapshot",
                "data_status": "snapshot", "event_timestamp": now_ts,
                "last_received_at": now_ts}

    def setUp(self):
        application.app.config["TESTING"] = True
        self.client = application.app.test_client()
        self.csrf = self.client.get("/auth/csrf").json["csrf_token"]
        self.email = f"batch3-{uuid.uuid4()}@example.test"
        self.client.post("/register", json={"email": self.email, "password": "correct horse battery staple", "csrf_token": self.csrf})
        self.csrf = self.client.get("/auth/csrf").json["csrf_token"]

    def test_watchlist_account_isolation_duplicates_and_csrf(self):
        with patch.object(application, "fetch_market_data", return_value=self.asset()):
            headers = {"X-CSRF-Token": self.csrf}
            added = self.client.post("/api/watchlist", json={"symbol": "AAPL", "market": "us"}, headers=headers)
            self.assertEqual(added.status_code, 201, added.json)
            self.assertEqual(self.client.post("/api/watchlist", json={"symbol": "AAPL", "market": "us"}, headers=headers).status_code, 409)
            self.assertEqual(len(self.client.get("/api/watchlist").json["data"]), 1)
            other = application.app.test_client()
            csrf = other.get("/auth/csrf").json["csrf_token"]
            other.post("/register", json={"email": f"other-{uuid.uuid4()}@example.test", "password": "correct horse battery staple", "csrf_token": csrf})
            self.assertEqual(other.get("/api/watchlist").json["data"], [])
            self.assertEqual(other.delete("/api/watchlist", json={"symbol": "AAPL"}, headers={"X-CSRF-Token": other.get("/auth/csrf").json["csrf_token"]}).status_code, 404)
            self.assertEqual(self.client.delete("/api/watchlist", json={"symbol": "AAPL"}, headers=headers).status_code, 200)
            self.assertEqual(self.client.get("/api/watchlist").json["data"], [])

    def test_watchlist_and_orders_validate_auth_csrf_and_symbols(self):
        anonymous = application.app.test_client()
        self.assertEqual(anonymous.get("/api/watchlist").status_code, 200)
        self.assertEqual(anonymous.get("/api/orders").status_code, 200)
        self.assertEqual(self.client.post("/api/watchlist", json={"symbol": "../x", "market": "us"}, headers={"X-CSRF-Token": self.csrf}).status_code, 400)
        self.assertEqual(self.client.post("/api/watchlist", json={"symbol": "AAPL", "market": "us"}).status_code, 400)
        payload = {"symbol": "AAPL", "market": "us", "side": "buy", "order_type": "market", "quantity": "1"}
        for change in ({"quantity": "NaN"}, {"quantity": "0"}, {"order_type": "stop"}, {"side": "short"}, {"symbol": "../x"}):
            result = self.client.post("/api/orders/preview", json=dict(payload, **change), headers={"X-CSRF-Token": self.csrf, "Idempotency-Key": "invalid-order-" + str(len(change))})
            self.assertEqual(result.status_code, 400, result.json)
        invalid_limit = dict(payload, order_type="limit", limit_price="0")
        self.assertEqual(self.client.post("/api/orders/preview", json=invalid_limit, headers={"X-CSRF-Token": self.csrf, "Idempotency-Key": "invalid-limit-001"}).status_code, 400)

    def test_order_preview_confirmation_is_idempotent_and_never_fills(self):
        with patch.object(application, "fetch_market_data", return_value=self.asset()):
            payload = {"symbol": "AAPL", "market": "us", "side": "buy", "order_type": "limit", "quantity": "0.125", "limit_price": "11.25"}
            headers = {"X-CSRF-Token": self.csrf, "Idempotency-Key": "order-preview-0001"}
            preview = self.client.post("/api/orders/preview", json=payload, headers=headers)
            self.assertEqual(preview.status_code, 201, preview.json)
            self.assertTrue(preview.json["confirmation_required"])
            self.assertEqual(preview.json["preview"]["reference_price"], "12.34")
            self.assertEqual(preview.json["preview"]["estimated_notional"], "1.40625")
            replay = self.client.post("/api/orders/preview", json=payload, headers=headers)
            self.assertTrue(replay.json["idempotent_replay"])
            self.assertEqual(replay.json["order_id"], preview.json["order_id"])
            self.assertEqual(self.client.post("/api/orders/preview", json=dict(payload, quantity="2"), headers=headers).status_code, 409)
            order_id = preview.json["order_id"]
            self.assertEqual(self.client.post(f"/api/orders/{order_id}/confirm", json={}, headers={"X-CSRF-Token": self.csrf}).status_code, 400)
            confirm = self.client.post(f"/api/orders/{order_id}/confirm", json={"confirmed": True}, headers={"X-CSRF-Token": self.csrf})
            self.assertEqual((confirm.json["status"], confirm.json["fill_status"]), ("accepted", "not_filled"))
            self.assertIn("no fill", confirm.json["message"].lower())
            repeat = self.client.post(f"/api/orders/{order_id}/confirm", json={"confirmed": True}, headers={"X-CSRF-Token": self.csrf})
            self.assertEqual(repeat.json["status"], "accepted")
            with connect() as db:
                row = db.execute("SELECT status, provider, provider_order_id FROM orders WHERE id = ?", (order_id,)).fetchone()
                self.assertEqual((row["status"], row["provider"], row["provider_order_id"]), ("accepted", "local_simulator", None))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
            history = self.client.get("/api/orders").json["data"]
            self.assertEqual(history[0]["fill_status"], "not_filled")
            self.assertEqual(self.client.post(f"/api/orders/{order_id}/cancel", headers={"X-CSRF-Token": self.csrf}).json["status"], "cancelled")
            self.assertEqual(self.client.post(f"/api/orders/{order_id}/cancel", headers={"X-CSRF-Token": self.csrf}).status_code, 409)

    def test_simulator_never_submits_to_a_broker_even_if_live_endpoint_env_exists(self):
        with patch.object(application, "fetch_market_data", return_value=self.asset()), patch.object(application.requests, "post") as broker_post, patch.dict(os.environ, {"ALPACA_API_URL": "https://api.alpaca.markets"}, clear=False):
            preview = self.client.post("/api/orders/preview", json={"symbol": "AAPL", "market": "us", "side": "buy", "order_type": "market", "quantity": "1"}, headers={"X-CSRF-Token": self.csrf, "Idempotency-Key": "sim-only-order-001"})
            confirm = self.client.post("/api/orders/" + preview.json["order_id"] + "/confirm", json={"confirmed": True}, headers={"X-CSRF-Token": self.csrf})
            self.assertEqual((preview.json["preview"]["execution_mode"], confirm.json["fill_status"]), ("local_simulator", "not_filled"))
            broker_post.assert_not_called()

    def test_order_cross_account_confirmation_and_sell_quantity_limits(self):
        with patch.object(application, "fetch_market_data", return_value=self.asset()):
            preview = self.client.post("/api/orders/preview", json={"symbol": "AAPL", "market": "us", "side": "buy", "order_type": "market", "quantity": "1"}, headers={"X-CSRF-Token": self.csrf, "Idempotency-Key": "account-order-0001"})
            other = application.app.test_client()
            csrf = other.get("/auth/csrf").json["csrf_token"]
            other.post("/register", json={"email": f"other-order-{uuid.uuid4()}@example.test", "password": "correct horse battery staple", "csrf_token": csrf})
            denied = other.post(f"/api/orders/{preview.json['order_id']}/confirm", json={"confirmed": True}, headers={"X-CSRF-Token": other.get("/auth/csrf").json["csrf_token"]})
            self.assertEqual(denied.status_code, 404)
            sell = self.client.post("/api/orders/preview", json={"symbol": "AAPL", "market": "us", "side": "sell", "order_type": "market", "quantity": "1"}, headers={"X-CSRF-Token": self.csrf, "Idempotency-Key": "oversell-order-001"})
            self.assertEqual(sell.status_code, 400)

    def test_portfolio_decimal_math_and_missing_mark_suppresses_total(self):
        with connect() as db:
            account_id = db.execute("SELECT id FROM accounts WHERE user_id = (SELECT id FROM users WHERE email = ?)", (self.email,)).fetchone()[0]
            db.execute("INSERT INTO account_holdings(id, account_id, symbol, quantity, purchase_price, idempotency_key, created_at, currency, asset_class) VALUES (?, ?, 'AAPL', '0.1', '10', ?, '2026-10-20T00:00:00Z', 'USD', 'US')", (str(uuid.uuid4()), account_id, str(uuid.uuid4())))
            rows = db.execute("SELECT id, symbol, quantity, purchase_price, created_at, currency, asset_class FROM account_holdings WHERE account_id = ?", (account_id,)).fetchall()
        with patch.object(application, "fetch_market_data", return_value=self.asset(price=10.555)):
            data = application.calculate_portfolio_metrics(rows)
        self.assertEqual(data["summary"]["total_investment"], "1.00")
        self.assertEqual(data["summary"]["current_value"], "1.06")
        self.assertEqual(data["summary"]["profit_loss"], "0.06")
        self.assertEqual(data["holdings"][0]["unrealized_pnl"], "0.0555")
        with patch.object(application, "fetch_market_data", side_effect=TimeoutError("offline")):
            missing = application.calculate_portfolio_metrics(rows)
        self.assertIsNone(missing["summary"]["current_value"])
        self.assertIsNone(missing["holdings"][0]["price"])
        self.assertEqual(missing["holdings"][0]["data_status"], "unavailable")

        stale_asset = self.asset(price=10.555)
        stale_asset["event_timestamp"] = "2020-01-01T00:00:00Z"
        with patch.object(application, "fetch_market_data", return_value=stale_asset):
            stale = application.calculate_portfolio_metrics(rows)
        self.assertEqual(stale["holdings"][0]["freshness"], "stale")
        self.assertIsNone(stale["summary"]["current_value"])

        with connect() as db:
            db.execute("INSERT INTO account_holdings(id, account_id, symbol, quantity, purchase_price, idempotency_key, created_at, currency, asset_class) VALUES (?, ?, 'RELIANCE.NS', '2', '100', ?, '2026-10-20T00:00:00Z', 'INR', 'India')", (str(uuid.uuid4()), account_id, str(uuid.uuid4())))
            mixed = db.execute("SELECT id, symbol, quantity, purchase_price, created_at, currency, asset_class FROM account_holdings WHERE account_id = ?", (account_id,)).fetchall()
        def mixed_mark(symbol, _market, _range):
            return self.asset(symbol=symbol, market="India", currency="INR", price=101) if symbol == "RELIANCE.NS" else self.asset(price=10.555)
        with patch.object(application, "fetch_market_data", side_effect=mixed_mark):
            mixed_portfolio = application.calculate_portfolio_metrics(mixed)
        self.assertIsNone(mixed_portfolio["summary"]["total_investment"])
        self.assertIsNone(mixed_portfolio["summary"]["current_value"])
        self.assertEqual(set(mixed_portfolio["summary"]["totals_by_currency"]), {"USD", "INR"})


if __name__ == "__main__":
    unittest.main()
