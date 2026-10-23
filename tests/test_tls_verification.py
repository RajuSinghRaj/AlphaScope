import unittest
from types import SimpleNamespace

from database.manage import _preview_client_tls_is_strict_and_direct, inspect_backend_tls


class TLSVerificationTests(unittest.TestCase):
    class FakeDB:
        def __init__(self, row=None, error=None):
            self.row = row
            self.error = error
            self.raw = SimpleNamespace(info=SimpleNamespace(
                ssl_in_use=True, backend_pid=41,
                ssl_attribute=lambda name: {"protocol": "TLSv1.3", "cipher": "test-cipher"}.get(name),
            ))

        def execute(self, sql):
            if "pg_stat_ssl" not in sql:
                raise AssertionError("unexpected SQL")
            if self.error:
                raise self.error
            return SimpleNamespace(fetchone=lambda: self.row)

    def test_ssl_true_is_verified(self):
        result = inspect_backend_tls(self.FakeDB({"query_pid": 41, "stats_pid": 41, "ssl": True, "version": "TLSv1.3", "cipher": "test"}))
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["version"], "TLSv1.3")
        self.assertEqual(result["client"]["status"], "verified")
        self.assertEqual(result["client"]["protocol"], "TLSv1.3")
        self.assertEqual(result["client"]["backend_pid"], 41)
        self.assertEqual(result["server"]["query_pid"], result["server"]["stats_pid"])

    def test_client_tls_inactive_is_explicit_failure(self):
        db = self.FakeDB({"query_pid": 41, "stats_pid": 41, "ssl": True, "version": "TLSv1.3", "cipher": "test"})
        db.raw.info.ssl_in_use = False
        result = inspect_backend_tls(db)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["client"]["status"], "failed")

    def test_ssl_false_is_explicit_failure(self):
        result = inspect_backend_tls(self.FakeDB({"query_pid": 41, "stats_pid": 41, "ssl": False, "version": None, "cipher": None}))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["server"]["reason"], "pg_stat_ssl reports ssl=false")

    def test_missing_row_is_unverified(self):
        result = inspect_backend_tls(self.FakeDB({
            "query_pid": 41, "stats_pid": None, "ssl": None, "version": None, "cipher": None
        }))
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["server"]["reason"], "no pg_stat_ssl row for current backend")

    def test_null_ssl_value_is_unverified_not_false(self):
        result = inspect_backend_tls(self.FakeDB({
            "query_pid": 41, "stats_pid": 41, "ssl": None, "version": None, "cipher": None
        }))
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["server"]["reason"], "pg_stat_ssl ssl value is NULL")

    def test_client_and_query_pid_mismatch_is_unverified(self):
        result = inspect_backend_tls(self.FakeDB({
            "query_pid": 42, "stats_pid": 42, "ssl": True,
            "version": "TLSv1.3", "cipher": "server-cipher"
        }))
        self.assertEqual(result["status"], "unverified")
        self.assertFalse(result["server"]["client_pid_matches_query"])
        self.assertEqual(result["server"]["status"], "verified")

    def test_negative_libpq_pid_is_invalid_and_not_compared(self):
        db = self.FakeDB({
            "query_pid": 1023, "stats_pid": 1023, "ssl": False,
            "version": None, "cipher": None
        })
        db.raw.info.backend_pid = -1284683716
        result = inspect_backend_tls(db)
        self.assertEqual(result["client"]["backend_pid"], -1284683716)
        self.assertIsNone(result["server"]["client_pid_matches_query"])
        self.assertEqual(result["status"], "unverified")

    def test_pid_mismatch_does_not_hide_server_ssl_false(self):
        result = inspect_backend_tls(self.FakeDB({
            "query_pid": 42, "stats_pid": 42, "ssl": False, "version": None, "cipher": None
        }))
        self.assertEqual(result["status"], "unverified")
        self.assertFalse(result["server"]["client_pid_matches_query"])
        self.assertEqual(result["server"]["status"], "failed")
        self.assertIs(result["server"]["ssl"], False)

    def test_unavailable_client_tls_api_stays_unverified(self):
        db = self.FakeDB({"query_pid": 41, "stats_pid": 41, "ssl": True,
                          "version": "TLSv1.3", "cipher": "server-cipher"})
        db.raw.info = SimpleNamespace(backend_pid=41)
        result = inspect_backend_tls(db)
        self.assertEqual(result["client"]["status"], "unverified")
        self.assertEqual(result["status"], "unverified")

    def test_permission_denied_is_unverified_and_sanitized(self):
        error = RuntimeError("sensitive server detail")
        error.pgcode = "42501"
        result = inspect_backend_tls(self.FakeDB(error=error))
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["server"]["reason"], "permission denied reading pg_stat_ssl")
        self.assertNotIn("sensitive", str(result))

    def test_client_tls_is_reported_separately_when_server_view_is_unavailable(self):
        result = inspect_backend_tls(self.FakeDB())
        self.assertEqual(result["client"]["status"], "verified")
        self.assertEqual(result["status"], "unverified")

    def test_read_only_preview_accepts_verified_direct_tls_without_claiming_backend_tls(self):
        tls = {"client": {"status": "verified"}, "server": {"status": "failed", "ssl": False}}
        url = "postgresql://test-user:test-password@ep-target.region.aws.neon.tech/neondb?sslmode=verify-full"
        self.assertTrue(_preview_client_tls_is_strict_and_direct(tls, url, "ep-target"))

    def test_read_only_preview_rejects_unverified_or_pooled_or_weak_tls(self):
        url = "postgresql://test-user:test-password@ep-target.region.aws.neon.tech/neondb?sslmode=verify-full"
        tls = {"client": {"status": "unverified"}}
        self.assertFalse(_preview_client_tls_is_strict_and_direct(tls, url, "ep-target"))
        for candidate in (
            url.replace("ep-target.region", "ep-target-pooler.region"),
            url.replace("verify-full", "require"),
            url + "&sslmode=verify-full",
        ):
            self.assertFalse(_preview_client_tls_is_strict_and_direct({"client": {"status": "verified"}}, candidate, "ep-target"))


if __name__ == "__main__":
    unittest.main()
