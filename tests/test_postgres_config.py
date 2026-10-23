import os
import unittest
from unittest.mock import patch

from database import foundation
from database.postgres_migrations import MIGRATIONS


class PostgresConfigurationTests(unittest.TestCase):
    def test_missing_url_keeps_local_sqlite_backend(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(foundation.backend(), "sqlite")

    def test_postgres_urls_select_postgres_without_connecting(self):
        for url in ("postgresql://operator@db.example/neon?sslmode=require", "postgres://operator@host/db"):
            with self.subTest(url=url), patch.dict(os.environ, {"DATABASE_URL": url}):
                self.assertEqual(foundation.backend(), "postgresql")

    def test_invalid_url_is_rejected_without_fallback(self):
        with patch.dict(os.environ, {"DATABASE_URL": "mysql://host/db"}):
            with self.assertRaisesRegex(ValueError, "DATABASE_URL"):
                foundation.backend()

    def test_postgres_qmark_adapter_keeps_begin_immediate_safe(self):
        self.assertEqual(foundation.PostgresConnection._sql("BEGIN IMMEDIATE"), "BEGIN")
        self.assertEqual(foundation.PostgresConnection._sql("SELECT * FROM t WHERE id = ?"), "SELECT * FROM t WHERE id = %s")

    def test_neon_channel_binding_and_weak_tls_uri_options_are_normalized(self):
        url = "postgresql://operator@db.example/neon?sslmode=require&channel_binding=require"
        with patch.dict(os.environ, {"DATABASE_URL": url}):
            normalized = foundation.postgres_dsn()
            tls = foundation.postgres_tls_options()
        self.assertNotIn("sslmode", normalized)
        self.assertNotIn("channel_binding", normalized)
        self.assertEqual(tls["sslmode"], "verify-full")
        self.assertTrue(os.path.isfile(tls["sslrootcert"]))

    def test_configured_missing_ca_file_is_rejected(self):
        url = "postgresql://operator@db.example/neon?sslrootcert=C%3A%2Fmissing%2Fca.crt"
        with patch.dict(os.environ, {"DATABASE_URL": url}):
            with self.assertRaisesRegex(ValueError, "CA certificate file"):
                foundation.postgres_tls_options()

    def test_postgres_schema_has_account_scopes_and_quarantine(self):
        schema = "\n".join(migration for _, migration in MIGRATIONS)
        self.assertIn("UNIQUE(account_id, idempotency_key)", schema)
        self.assertIn("REFERENCES accounts(id) ON DELETE CASCADE", schema)
        self.assertIn("legacy_data_quarantine", schema)
        self.assertIn("owner_account_id TEXT REFERENCES accounts(id)", schema)


if __name__ == "__main__":
    unittest.main()
