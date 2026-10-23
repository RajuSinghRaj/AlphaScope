import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from database.import_sqlite import ImportConflict, build_report, main as import_main
from database.postgres_migrations import (MIGRATIONS, REQUIRED_COLUMNS_BY_VERSION,
                                          REQUIRED_CONSTRAINTS_BY_VERSION,
                                          migrate_postgres,
                                          migration_state)


SOURCE_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE users(id TEXT PRIMARY KEY, email TEXT, password_hash TEXT, created_at TEXT);
CREATE UNIQUE INDEX users_email_unique ON users(email) WHERE email IS NOT NULL;
CREATE TABLE accounts(id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), name TEXT, base_currency TEXT, created_at TEXT);
CREATE TABLE account_holdings(id TEXT PRIMARY KEY, account_id TEXT REFERENCES accounts(id), symbol TEXT, quantity TEXT, purchase_price TEXT, idempotency_key TEXT UNIQUE, created_at TEXT);
CREATE TABLE auth_attempts(ip_address TEXT, attempted_at TEXT);
CREATE TABLE portfolio(id INTEGER PRIMARY KEY, symbol TEXT, quantity REAL, purchase_price REAL, created_at TEXT, owner_account_id TEXT REFERENCES accounts(id));
CREATE TABLE portfolio_idempotency(idempotency_key TEXT PRIMARY KEY, portfolio_id INTEGER REFERENCES portfolio(id), created_at TEXT, owner_account_id TEXT REFERENCES accounts(id));
"""

DESTINATION_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE users(id TEXT PRIMARY KEY, email TEXT, password_hash TEXT, created_at TEXT);
CREATE UNIQUE INDEX users_email_unique ON users(email) WHERE email IS NOT NULL;
CREATE TABLE accounts(id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), name TEXT, base_currency TEXT, created_at TEXT);
CREATE TABLE account_holdings(id TEXT PRIMARY KEY, account_id TEXT REFERENCES accounts(id), symbol TEXT, quantity TEXT, purchase_price TEXT, idempotency_key TEXT UNIQUE, created_at TEXT, currency TEXT, asset_class TEXT, UNIQUE(account_id,idempotency_key));
CREATE TABLE auth_attempts(ip_address TEXT, attempted_at TEXT);
CREATE TABLE portfolio(id INTEGER PRIMARY KEY, symbol TEXT, quantity NUMERIC, purchase_price NUMERIC, created_at TEXT, owner_account_id TEXT REFERENCES accounts(id));
CREATE TABLE portfolio_idempotency(idempotency_key TEXT PRIMARY KEY, portfolio_id INTEGER REFERENCES portfolio(id), created_at TEXT, owner_account_id TEXT REFERENCES accounts(id));
CREATE TABLE legacy_data_quarantine(id INTEGER PRIMARY KEY AUTOINCREMENT, source_table TEXT, source_id TEXT, source_payload TEXT, reason TEXT, UNIQUE(source_table,source_id));
CREATE TABLE legacy_ownership_audit(source_table TEXT, source_id TEXT, account_id TEXT, rationale TEXT, approved_by TEXT, approved_at TEXT, PRIMARY KEY(source_table,source_id));
"""


class SQLiteImportTests(unittest.TestCase):
    def setUp(self):
        self.source = sqlite3.connect(":memory:")
        self.source.row_factory = sqlite3.Row
        self.source.executescript(SOURCE_SCHEMA)
        self.target = sqlite3.connect(":memory:")
        self.target.row_factory = sqlite3.Row
        self.target.executescript(DESTINATION_SCHEMA)
        self.source.execute("INSERT INTO users VALUES ('u1','u1@example.invalid','hash','2026-01-01 00:00:00')")
        self.source.execute("INSERT INTO accounts VALUES ('a1','u1','Personal','USD','2026-01-01T00:00:00Z')")
        self.source.execute("INSERT INTO account_holdings VALUES ('h1','a1','ABC','0.1234567890123456789','10.2300','hold-1','2026-01-02T03:04:05Z')")
        self.source.executemany("INSERT INTO auth_attempts VALUES (?,?)", [
            ('127.0.0.1','2026-01-02T03:04:05Z'), ('127.0.0.1','2026-01-02T03:04:05Z')
        ])
        self.source.executemany("INSERT INTO portfolio(symbol,quantity,purchase_price,created_at,owner_account_id) VALUES (?,?,?,?,NULL)", [
            ('OLD1',1.1,10.1,'2020-01-01 00:00:00'), ('OLD2',2.2,20.2,'2020-01-02 00:00:00'),
            ('OLD3',3.3,30.3,'2020-01-03 00:00:00'), ('OLD4',4.4,40.4,'2020-01-04 00:00:00'),
        ])
        self.source.commit()

    def tearDown(self):
        self.source.close()
        self.target.close()

    def test_dry_run_counts_and_quarantines_legacy_without_writing(self):
        report = build_report(self.source, self.target, destination_kind="sqlite", apply=False)
        portfolio = next(r for r in report["legacy_tables"] if r["source_table"] == "portfolio")
        holding = next(r for r in report["application_tables"] if r["table"] == "account_holdings")
        attempts = next(r for r in report["application_tables"] if r["table"] == "auth_attempts")
        self.assertEqual(portfolio["source_rows"], 4)
        self.assertEqual(portfolio["quarantined_unowned_rows"], 4)
        self.assertEqual(holding["would_insert"], 1)
        self.assertTrue(holding["plan_accounted"])
        self.assertEqual(attempts["would_insert"], 2)
        self.assertEqual(report["quarantine"]["new_rows"], 4)
        self.assertEqual(self.target.execute("SELECT COUNT(*) FROM account_holdings").fetchone()[0], 0)
        self.assertEqual(self.target.execute("SELECT COUNT(*) FROM legacy_data_quarantine").fetchone()[0], 0)

    def test_import_is_repeatable_and_preserves_precision_timestamps_and_unowned_rows(self):
        first = build_report(self.source, self.target, destination_kind="sqlite", apply=True)
        second = build_report(self.source, self.target, destination_kind="sqlite", apply=True)
        self.assertEqual(self.target.execute("SELECT COUNT(*) FROM legacy_data_quarantine").fetchone()[0], 4)
        self.assertEqual(self.target.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0], 0)
        self.assertEqual(self.target.execute("SELECT COUNT(*) FROM auth_attempts").fetchone()[0], 2)
        holding = self.target.execute("SELECT quantity,purchase_price,created_at,currency,asset_class FROM account_holdings WHERE id='h1'").fetchone()
        self.assertEqual(str(holding["quantity"]), "0.1234567890123456789")
        self.assertEqual(str(holding["purchase_price"]), "10.2300")
        self.assertEqual(holding["created_at"], "2026-01-02T03:04:05.000000+00:00")
        self.assertIsNone(holding["currency"])
        self.assertIsNone(holding["asset_class"])
        payloads = [json.loads(r[0]) for r in self.target.execute("SELECT source_payload FROM legacy_data_quarantine ORDER BY source_id")]
        self.assertEqual(len(payloads), 4)
        self.assertTrue(all(payload["owner_account_id"] is None for payload in payloads))
        self.assertEqual(first["quarantine"]["new_rows"], 4)
        self.assertEqual(second["quarantine"]["new_rows"], 0)
        self.assertEqual(second["quarantine"]["already_present"], 4)
        self.assertEqual(self.target.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_conflicting_existing_row_aborts_import(self):
        self.target.execute("INSERT INTO users VALUES ('u1','different@example.invalid','other','2026-01-01T00:00:00+00:00')")
        self.target.commit()
        with self.assertRaises(ImportConflict):
            build_report(self.source, self.target, destination_kind="sqlite", apply=True)
        self.assertEqual(self.target.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)

    def test_dry_run_detects_alternate_unique_conflicts(self):
        self.target.execute("INSERT INTO users VALUES ('other-id','u1@example.invalid','hash','2026-01-01T00:00:00+00:00')")
        self.target.commit()
        with self.assertRaisesRegex(ImportConflict, "Alternate unique-key collision"):
            build_report(self.source, self.target, destination_kind="sqlite", apply=False)

    def test_naive_timestamp_is_marked_utc_and_invalid_decimal_rejected(self):
        report = build_report(self.source)
        self.assertTrue(report["application_tables"])
        self.assertEqual(report["quarantine"]["new_rows"], 4)
        from database.import_sqlite import _normal_timestamp, _normal_decimal
        self.assertEqual(_normal_timestamp("2026-01-01 00:00:00"), "2026-01-01T00:00:00.000000+00:00")
        with self.assertRaises(ValueError):
            _normal_decimal("NaN")

    def test_explicit_ownership_mapping_is_audited_and_unmapped_rows_stay_quarantined(self):
        source_id = self.source.execute("SELECT id FROM portfolio ORDER BY id LIMIT 1").fetchone()[0]
        ownership = {"portfolio": {str(source_id): {
            "account_id": "a1", "rationale": "Verified from the account holder's signed records",
            "approved_by": "local-admin", "approved_at": "2026-10-20T12:00:00Z",
        }}}
        preview = build_report(self.source, self.target, destination_kind="sqlite", ownership_map=ownership)
        portfolio = next(r for r in preview["legacy_tables"] if r["source_table"] == "portfolio")
        self.assertEqual(portfolio["explicitly_mapped_rows"], 1)
        self.assertEqual(portfolio["quarantined_unowned_rows"], 3)
        self.assertTrue(portfolio["source_accounted"])
        self.assertEqual(self.target.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0], 0)

        build_report(self.source, self.target, destination_kind="sqlite", apply=True, ownership_map=ownership)
        imported = self.target.execute("SELECT owner_account_id FROM portfolio WHERE id=?", (source_id,)).fetchone()
        audit = self.target.execute("SELECT account_id, approved_by FROM legacy_ownership_audit WHERE source_table='portfolio' AND source_id=?", (str(source_id),)).fetchone()
        self.assertEqual(imported[0], "a1")
        self.assertEqual(tuple(audit), ("a1", "local-admin"))
        self.assertEqual(self.target.execute("SELECT COUNT(*) FROM legacy_data_quarantine").fetchone()[0], 3)

    def test_malformed_mapping_and_unknown_rows_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown source IDs"):
            build_report(self.source, ownership_map={"portfolio": {"999": {
                "account_id": "a1", "rationale": "Verified from signed records here",
                "approved_by": "local-admin", "approved_at": "2026-10-20T12:00:00Z",
            }}})
        with self.assertRaisesRegex(ValueError, "requires rationale"):
            build_report(self.source, ownership_map={"portfolio": {"1": {"account_id": "a1"}}})

    def test_conflicting_recorded_owner_is_never_overridden_by_mapping(self):
        self.source.execute("INSERT INTO users VALUES ('u2','u2@example.invalid','hash','2026-01-01T00:00:00Z')")
        self.source.execute("INSERT INTO accounts VALUES ('a2','u2','Other','USD','2026-01-01T00:00:00Z')")
        source_id = self.source.execute("SELECT id FROM portfolio ORDER BY id LIMIT 1").fetchone()[0]
        self.source.execute("UPDATE portfolio SET owner_account_id='a2' WHERE id=?", (source_id,))
        ownership = {"portfolio": {str(source_id): {
            "account_id": "a1", "rationale": "Verified from the account holder's signed records",
            "approved_by": "local-admin", "approved_at": "2026-10-20T12:00:00Z",
        }}}
        with self.assertRaisesRegex(ValueError, "conflicts with recorded portfolio owner"):
            build_report(self.source, ownership_map=ownership)

    def test_offline_cli_does_not_parse_or_connect_using_database_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "empty.sqlite3"
            sqlite3.connect(source_path).close()
            output = StringIO()
            with patch.dict(os.environ, {"DATABASE_URL": "invalid://deliberately-not-a-dsn"}), redirect_stdout(output):
                self.assertEqual(import_main(["--source", str(source_path), "--dry-run"]), 0)
            self.assertIn('"mode": "offline_dry_run"', output.getvalue())


class MigrationRepeatabilityTests(unittest.TestCase):
    class FakeCursor:
        rowcount = 1
        def __init__(self, rows=()):
            self.rows = list(rows)
        def fetchone(self):
            return self.rows[0] if self.rows else None
        def fetchall(self):
            return self.rows

    class FakeConnection:
        def __init__(self):
            self.version = 0
            self.ledger = False
            self.managed = False
            self.existing_tables = set()
            self.version_rows = None
            self.drop_column = None
            self.drop_constraint = None
            self.scripts = []
            self.commits = 0
            self.rollbacks = 0
        def execute(self, sql, params=()):
            if "information_schema.tables" in sql:
                tables = set(self.existing_tables)
                if self.ledger:
                    tables.add("schema_migrations")
                if self.managed:
                    tables.update({"users", "accounts", "instruments", "portfolios", "positions", "transactions", "watchlists", "watchlist_items", "orders", "auth_sessions", "auth_attempts", "account_holdings", "order_events", "portfolio", "portfolio_idempotency", "legacy_data_quarantine", "legacy_ownership_audit"})
                return MigrationRepeatabilityTests.FakeCursor({"table_name": name} for name in tables)
            if "information_schema.columns" in sql:
                columns = {}
                for version in range(1, self.version + 1):
                    for table, names in REQUIRED_COLUMNS_BY_VERSION[version].items():
                        columns.setdefault(table, set()).update(names)
                if self.ledger:
                    columns["schema_migrations"] = {"version", "applied_at"}
                if self.drop_column:
                    columns.get(self.drop_column[0], set()).discard(self.drop_column[1])
                return MigrationRepeatabilityTests.FakeCursor(
                    {"table_name": table, "column_name": column}
                    for table, names in columns.items() for column in names)
            if "FROM pg_indexes" in sql:
                import re
                names = set()
                for version in range(1, self.version + 1):
                    names.update(re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+([a-z_]+)", MIGRATIONS[version - 1][1], re.I))
                return MigrationRepeatabilityTests.FakeCursor({"indexname": name} for name in names)
            if "information_schema.table_constraints" in sql:
                names = set()
                for version in range(1, self.version + 1):
                    names.update(REQUIRED_CONSTRAINTS_BY_VERSION[version])
                if self.ledger:
                    names.add(("schema_migrations", "schema_migrations_pkey"))
                if self.drop_constraint:
                    names.discard(self.drop_constraint)
                return MigrationRepeatabilityTests.FakeCursor(
                    {"table_name": table, "constraint_name": name} for table, name in names)
            if "SELECT version FROM schema_migrations" in sql:
                versions = self.version_rows if self.version_rows is not None else list(range(1, self.version + 1))
                return MigrationRepeatabilityTests.FakeCursor({"version": version} for version in versions)
            if "CREATE TABLE IF NOT EXISTS schema_migrations" in sql:
                self.ledger = True
            if "INSERT INTO schema_migrations" in sql:
                self.version = max(self.version, params[0])
            return MigrationRepeatabilityTests.FakeCursor()
        def executescript(self, script):
            self.scripts.append(script)
            self.managed = True
        def commit(self): self.commits += 1
        def rollback(self): self.rollbacks += 1

    def test_migrations_are_sequenced_additive_and_rerunnable(self):
        self.assertEqual([version for version, _ in MIGRATIONS], [1, 2, 3])
        self.assertTrue(all(script.strip() for _, script in MIGRATIONS))
        self.assertIn("auth_sessions", MIGRATIONS[1][1])
        self.assertIn("order_events", MIGRATIONS[2][1])
        self.assertIn("legacy_data_quarantine", MIGRATIONS[2][1])
        self.assertIn("REFERENCES accounts(id)", MIGRATIONS[2][1])
        self.assertIn("UNIQUE(source_table, source_id)", MIGRATIONS[2][1])
        self.assertIn("CHECK(quantity > 0)", MIGRATIONS[0][1])
        self.assertFalse(any(any(token in script.upper() for token in ("DROP TABLE", "TRUNCATE ", "DELETE FROM"))
                             for _, script in MIGRATIONS))
        db = self.FakeConnection()
        migrate_postgres(db)
        migrate_postgres(db)
        self.assertEqual(len(db.scripts), 3)
        self.assertEqual(db.version, 3)
        self.assertEqual(db.commits, 2)
        self.assertEqual(db.rollbacks, 0)

    def test_migration_preview_includes_proposed_constraints_and_audit_schema(self):
        from database.manage import _planned_objects
        tables, indexes, constraints = _planned_objects()
        self.assertIn("legacy_ownership_audit", tables)
        self.assertIn("idx_order_events_account_time", indexes)
        self.assertTrue(any("order_events: order_id TEXT NOT NULL REFERENCES orders(id)" in item
                            for item in constraints))

    def test_migration_failure_requests_transaction_rollback(self):
        class FailingConnection(MigrationRepeatabilityTests.FakeConnection):
            def executescript(self, script):
                if "auth_sessions" in script:
                    raise RuntimeError("deterministic migration failure")
                super().executescript(script)
        db = FailingConnection()
        with self.assertRaisesRegex(RuntimeError, "deterministic"):
            migrate_postgres(db)
        self.assertEqual(db.commits, 0)
        self.assertEqual(db.rollbacks, 1)

    def test_migration_ledger_gaps_future_versions_and_schema_drift_stop_safely(self):
        db = self.FakeConnection()
        db.ledger = True
        db.version_rows = [1, 3]
        with self.assertRaisesRegex(RuntimeError, "gap or duplicate"):
            migration_state(db)

        db.version_rows = [1]
        db.version = 1
        db.managed = True
        db.drop_column = ("users", "created_at")
        with self.assertRaisesRegex(RuntimeError, "do not match the schema"):
            migration_state(db)

        fresh = self.FakeConnection()
        fresh.existing_tables.add("users")
        with self.assertRaisesRegex(RuntimeError, "without migration history"):
            migrate_postgres(fresh)
        self.assertEqual(fresh.commits, 0)
        self.assertEqual(fresh.scripts, [])

        drifted = self.FakeConnection()
        drifted.ledger = True
        drifted.managed = True
        drifted.version = 1
        drifted.drop_constraint = ("accounts", "accounts_user_id_fkey")
        with self.assertRaisesRegex(RuntimeError, "constraint:accounts.accounts_user_id_fkey"):
            migration_state(drifted)


if __name__ == "__main__":
    unittest.main()
