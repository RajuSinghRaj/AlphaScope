import hashlib
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote, urlencode
from types import SimpleNamespace
from unittest.mock import patch

from database.backup import (
    BackupValidationError, _classify_failure, _inspect_restore_listing, _libpq_env,
    _pg_dump_failure, _validate_acl_manifest, _validate_backup_manifest, _verify_target,
)
from database.import_sqlite import _validate_backup


class BackupToolTests(unittest.TestCase):
    def test_url_is_converted_to_tls_libpq_environment(self):
        credentials = quote("local-test-user", safe="") + ":" + quote("unit-test-only", safe="")
        url = "postgresql://" + credentials + "@db.example/neondb?" + urlencode({
            "sslmode": "verify-full", "channel_binding": "require"
        })
        env = _libpq_env(url)
        self.assertEqual(env["PGHOST"], "db.example")
        self.assertEqual(env["PGDATABASE"], "neondb")
        self.assertEqual(env["PGSSLMODE"], "verify-full")
        self.assertTrue(Path(env["PGSSLROOTCERT"]).is_file())
        self.assertEqual(env["PGCHANNELBINDING"], "require")
        self.assertNotIn("DATABASE_URL", env)

    def test_backup_requires_exactly_one_verify_full_parameter(self):
        urls = (
            "postgresql://local-test-user:unit-test-only@db.example/neondb?sslmode=disable",
            "postgresql://local-test-user:unit-test-only@db.example/neondb?sslmode=allow",
            "postgresql://local-test-user:unit-test-only@db.example/neondb?sslmode=prefer",
            "postgresql://local-test-user:unit-test-only@db.example/neondb?sslmode=require",
            "postgresql://local-test-user:unit-test-only@db.example/neondb",
            "postgresql://local-test-user:unit-test-only@db.example/neondb?sslmode=verify-full&sslmode=verify-full",
        )
        for url in urls:
            with self.subTest(url=url.split("?")[-1]), self.assertRaisesRegex(ValueError, "exactly one sslmode=verify-full"):
                _libpq_env(url)

    def test_acl_manifest_requires_target_owners_and_complete_grants(self):
        identity = {"database": "neondb", "endpoint": "ep-test", "branch": "br-test"}
        manifest = {
            "format": "alphascope-postgresql-access-manifest-v1", "target": identity,
            "database": {"name": "neondb", "owner": "db_owner", "acl": [
                {"grantee": "PUBLIC", "grantor": "db_owner", "privilege": "CONNECT", "grantable": False}]},
            "schemas": [{"name": "public", "owner": "schema_owner", "acl": [
                {"grantee": "PUBLIC", "grantor": "schema_owner", "privilege": "USAGE", "grantable": False}]}],
        }
        _validate_backup_manifest(manifest, identity)
        missing_acl = {**manifest, "database": {**manifest["database"], "acl": []}}
        with self.assertRaisesRegex(BackupValidationError, "missing database ACL entry"):
            _validate_backup_manifest(missing_acl, identity)
        incomplete = {**manifest, "schemas": [{**manifest["schemas"][0], "owner": ""}]}
        with self.assertRaisesRegex(BackupValidationError, "manifest validation failed"):
            _validate_backup_manifest(incomplete, identity)
        wrong_target = {**manifest, "target": {**identity, "branch": "other"}}
        with self.assertRaisesRegex(BackupValidationError, "manifest validation failed"):
            _validate_backup_manifest(wrong_target, identity)

    def test_pg_dump_failure_is_a_safe_distinct_diagnostic(self):
        self.assertEqual(_pg_dump_failure(b"SSL certificate failed for password=secret"), "pg_dump failed: TLS")

    def test_pg_restore_list_output_uses_header_database_and_toc_not_assumed_acl_entry(self):
        # PostgreSQL 18.6 local pg_dump --format=custom --create fixture output:
        # its header names the database; ACL entries are for schema objects.
        listing = (b"; Archive created at 2026-10-21\n"
                   b";     dbname: toc_fixture\n"
                   b";     TOC Entries: 8\n"
                   b";     Format: CUSTOM\n"
                   b";\n; Selected TOC Entries:\n"
                   b"219; 1259 16386 TABLE public fixture_rows as_restore_check\n"
                   b"4965; 0 0 ACL public TABLE fixture_rows as_restore_check\n"
                   b"4957; 0 16386 TABLE DATA public fixture_rows as_restore_check\n")
        report = _inspect_restore_listing(0, listing, "toc_fixture")
        self.assertEqual(report["entries"], 3)
        self.assertFalse(report["database_toc_entry"])
        self.assertFalse(report["database_acl_toc_entry"])

    def test_restore_list_failure_is_distinct(self):
        with self.assertRaisesRegex(BackupValidationError, "pg_restore --list failed"):
            _inspect_restore_listing(1, b"", "toc_fixture")

    def test_missing_database_header_is_distinct(self):
        with self.assertRaisesRegex(BackupValidationError, "missing database entry"):
            _inspect_restore_listing(0, b"; Format: CUSTOM\n1; 0 0 TABLE public t owner\n", "toc_fixture")

    def test_configured_ca_file_is_required_to_exist(self):
        url = "postgresql://local-test-user:unit-test-only@db.example/neondb?sslmode=verify-full&sslrootcert=C%3A%2Fmissing%2Froot.crt"
        with self.assertRaisesRegex(ValueError, "CA certificate file"):
            _libpq_env(url)

    def test_failure_categories_are_sanitized(self):
        self.assertEqual(_classify_failure(b"connection timed out"), "timeout")
        self.assertEqual(_classify_failure(b"SSL certificate verify failed"), "TLS")
        self.assertEqual(_classify_failure(b"password authentication failed"), "authentication")
        self.assertEqual(_classify_failure(b"database does not exist"), "database/role")
        self.assertEqual(_classify_failure(b"unexpected driver failure"), "other")

    def test_identity_verification_compares_database_endpoint_branch(self):
        class FakeConnection:
            def execute(self, sql):
                class Result:
                    def fetchone(self):
                        return {"database_name": "neondb", "endpoint_id": "ep-unit", "branch_id": "br-unit"}
                return Result()
        _verify_target(FakeConnection(), "neondb", "ep-unit", "br-unit", "ep-unit-pooler.region.neon.tech")
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            _verify_target(FakeConnection(), "other", "ep-unit", "br-unit", "ep-unit.region.neon.tech")

    def test_import_backup_guard_checks_path_hash_and_archive_without_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_dir = root / ".private_backups"
            backup_dir.mkdir()
            archive = backup_dir / "test.dump"
            archive.write_bytes(b"deterministic test archive")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with patch("database.import_sqlite.ROOT", root), patch(
                    "database.import_sqlite.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
                _validate_backup(archive, digest)
                run.assert_called_once()
                self.assertNotIn("DATABASE_URL", run.call_args.kwargs["env"])
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    _validate_backup(archive, "0" * 64)
                run.return_value = SimpleNamespace(returncode=1)
                with self.assertRaisesRegex(ValueError, "archive validation failed"):
                    _validate_backup(archive, digest)
                outside = root / "outside.dump"
                outside.write_bytes(b"test")
                with self.assertRaisesRegex(ValueError, "inside the private"):
                    _validate_backup(outside, hashlib.sha256(b"test").hexdigest())


if __name__ == "__main__":
    unittest.main()
