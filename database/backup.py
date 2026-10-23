"""Create a private, timestamped custom-format PostgreSQL backup.

Run only after confirming network access and target identity:
    python -m database.backup --expected-database neondb \
      --expected-endpoint ep-... --expected-branch br-...
The URL is parsed in-process; libpq receives its fields through the child process
environment, never as command-line arguments or printed output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from database.foundation import ROOT, backend, connect

BACKUP_DIR = ROOT / ".private_backups"
PG_BIN = Path(r"C:\Program Files\PostgreSQL\18\bin")


class BackupValidationError(RuntimeError):
    """Safe, non-sensitive backup validation failure."""


def _secure_backup_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.resolve().relative_to(ROOT.resolve())
    except ValueError as exc:
        raise OSError("Backup directory resolved outside the project workspace") from exc
    if os.name == "nt":
        script = (
            "$ErrorActionPreference='Stop'; "
            "$p=$env:ALPHASCOPE_BACKUP_DIR; "
            "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
            "$acl=Get-Acl -LiteralPath $p; "
            "$expected=@($sid,'S-1-5-18','S-1-5-32-544'); "
            "$inherited=@($acl.Access | Where-Object { $_.IsInherited }); "
            "$explicit=@($acl.Access | Where-Object { -not $_.IsInherited }); "
            "$actual=@($explicit | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } | Sort-Object -Unique); "
            "$valid=($acl.Owner -eq ([Security.Principal.SecurityIdentifier]$sid).Translate([Security.Principal.NTAccount]).Value -and $inherited.Count -eq 0 -and "
            "(@($actual | Where-Object { $_ -notin $expected }).Count -eq 0) -and "
            "(@($expected | Where-Object { $_ -notin $actual }).Count -eq 0) -and "
            "(@($explicit | Where-Object { $_.AccessControlType -ne 'Allow' -or $_.FileSystemRights -notmatch 'FullControl' }).Count -eq 0)); "
            "if (-not $valid) { "
            "& icacls.exe $p '/inheritance:r' '/grant:r' ('*'+$sid+':(OI)(CI)F') '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null; "
            "if ($LASTEXITCODE -ne 0) { exit 3 }; "
            "$acl=Get-Acl -LiteralPath $p; $inherited=@($acl.Access | Where-Object { $_.IsInherited }); "
            "$explicit=@($acl.Access | Where-Object { -not $_.IsInherited }); "
            "$actual=@($explicit | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } | Sort-Object -Unique); "
            "if ($inherited.Count -ne 0 -or @($actual | Where-Object { $_ -notin $expected }).Count -ne 0 -or @($expected | Where-Object { $_ -notin $actual }).Count -ne 0) { exit 4 } "
            "}"
        )
        safe_env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        safe_env["ALPHASCOPE_BACKUP_DIR"] = str(path)
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                cwd=ROOT, env=safe_env, capture_output=True, check=False)
        if result.returncode:
            raise OSError("Could not restrict backup directory permissions")
    else:
        path.chmod(0o700)


def _verify_git_ignored(path: Path) -> None:
    relative = path.relative_to(ROOT).as_posix() + "/archive.dump"
    result = subprocess.run(["git", "check-ignore", "--quiet", relative], cwd=ROOT,
                            capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("Backup directory is not ignored by Git")


def _libpq_env(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"} or not all((parsed.hostname, parsed.username, parsed.password, parsed.path.strip("/"))):
        raise ValueError("DATABASE_URL is not a complete PostgreSQL URL")
    query = parse_qs(parsed.query)
    if query.get("sslmode", []) != ["verify-full"]:
        raise ValueError("PostgreSQL backup requires exactly one sslmode=verify-full parameter")
    root_cert = query.get("sslrootcert", [None])[0]
    if root_cert and root_cert.lower() != "system":
        if not Path(root_cert).is_file():
            raise ValueError("Configured PostgreSQL CA certificate file is unavailable")
    elif not root_cert:
        try:
            from requests.certs import where as certificate_bundle
            root_cert = certificate_bundle()
        except (ImportError, OSError) as exc:
            raise RuntimeError("PostgreSQL backup requires a CA trust bundle") from exc
        if not Path(root_cert).is_file():
            raise RuntimeError("PostgreSQL backup requires a CA trust bundle")
    env = {
        "PGHOST": parsed.hostname,
        "PGPORT": str(parsed.port or 5432),
        "PGDATABASE": unquote(parsed.path.lstrip("/")),
        "PGUSER": unquote(parsed.username),
        "PGPASSWORD": unquote(parsed.password),
        "PGSSLMODE": "verify-full",
        "PGSSLROOTCERT": root_cert,
        "PGCONNECT_TIMEOUT": "12",
        "PGAPPNAME": "alphascope-backup",
    }
    for url_name, env_name in (("sslcert", "PGSSLCERT"), ("sslkey", "PGSSLKEY")):
        if query.get(url_name):
            env[env_name] = query[url_name][0]
    if query.get("channel_binding"):
        env["PGCHANNELBINDING"] = query["channel_binding"][0]
    return env


def _verify_target(connection, expected_database, expected_endpoint, expected_branch, configured_host):
    row = connection.execute(
        "SELECT current_database() AS database_name, current_setting('neon.endpoint_id',true) AS endpoint_id, current_setting('neon.branch_id',true) AS branch_id"
    ).fetchone()
    endpoint = row["endpoint_id"] or ""
    host_match = bool(re.match(r"^" + re.escape(endpoint.lower()) + r"(?:-pooler)?\.", configured_host.lower()))
    if row["database_name"] != expected_database or endpoint != expected_endpoint or row["branch_id"] != expected_branch or not host_match:
        raise RuntimeError("Neon target identity mismatch")
    return {"database": row["database_name"], "endpoint": endpoint, "branch": row["branch_id"]}


def _collect_acl_manifest(connection, identity):
    database = connection.execute(
        "SELECT d.datname AS name, pg_get_userbyid(d.datdba) AS owner, "
        "CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END AS grantee, "
        "CASE WHEN a.grantor = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantor) END AS grantor, "
        "a.privilege_type AS privilege, a.is_grantable AS grantable "
        "FROM pg_database d CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) a "
        "WHERE d.datname = current_database() ORDER BY grantee, privilege"
    ).fetchall()
    if not database:
        raise RuntimeError("Could not capture target database ownership and ACLs")
    schemas = connection.execute(
        "SELECT n.oid, n.nspname AS name, pg_get_userbyid(n.nspowner) AS owner, "
        "CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END AS grantee, "
        "CASE WHEN a.grantor = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantor) END AS grantor, "
        "a.privilege_type AS privilege, a.is_grantable AS grantable "
        "FROM pg_namespace n LEFT JOIN LATERAL "
        "aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) a ON TRUE "
        "WHERE n.nspname <> 'information_schema' AND left(n.nspname, 3) <> 'pg_' "
        "ORDER BY n.nspname, grantee, privilege"
    ).fetchall()
    schema_map = {}
    for row in schemas:
        item = schema_map.setdefault(row["name"], {"name": row["name"], "owner": row["owner"], "acl": []})
        if row["grantee"] is not None:
            item["acl"].append({"grantee": row["grantee"], "grantor": row["grantor"],
                                "privilege": row["privilege"], "grantable": bool(row["grantable"])})
    manifest = {
        "format": "alphascope-postgresql-access-manifest-v1",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "target": identity,
        "ownership_policy": "The dump preserves owner and ACL TOC entries. This manifest records database/schema owners and effective ACL grants for review; referenced roles must exist before restore. Never apply it without reviewing target role mappings.",
        "database": {
            "name": database[0]["name"], "owner": database[0]["owner"],
            "acl": [{"grantee": row["grantee"], "grantor": row["grantor"],
                     "privilege": row["privilege"], "grantable": bool(row["grantable"])} for row in database],
        },
        "schemas": list(schema_map.values()),
    }
    _validate_acl_manifest(manifest, identity)
    return manifest


def _validate_acl_manifest(manifest, expected_identity):
    if (manifest.get("format") != "alphascope-postgresql-access-manifest-v1"
            or manifest.get("target") != expected_identity):
        raise ValueError("Access manifest target or format validation failed")
    database = manifest.get("database")
    if not isinstance(database, dict) or not database.get("name") or not database.get("owner"):
        raise ValueError("Access manifest is missing database ownership")
    if database["name"] != expected_identity.get("database"):
        raise ValueError("Access manifest database ACL validation failed")
    if not isinstance(database.get("acl"), list) or not database["acl"]:
        raise BackupValidationError("missing database ACL entry")
    schemas = manifest.get("schemas")
    if not isinstance(schemas, list) or not schemas:
        raise ValueError("Access manifest is missing schema ownership and ACLs")
    names = set()
    for schema in schemas:
        if (not isinstance(schema, dict) or not schema.get("name") or not schema.get("owner")
                or not isinstance(schema.get("acl"), list) or schema["name"] in names):
            raise ValueError("Access manifest schema ownership and ACL validation failed")
        names.add(schema["name"])
        for entry in schema["acl"]:
            if not all(key in entry for key in ("grantee", "grantor", "privilege", "grantable")):
                raise ValueError("Access manifest contains an incomplete schema ACL entry")
    for entry in database["acl"]:
        if not all(key in entry for key in ("grantee", "grantor", "privilege", "grantable")):
            raise ValueError("Access manifest contains an incomplete database ACL entry")


def _inspect_restore_listing(return_code, stdout, expected_database):
    if return_code or not stdout.strip():
        raise BackupValidationError("pg_restore --list failed")
    text = stdout.decode("utf-8", errors="replace")
    match = re.search(r"^;\s*dbname:\s*(.*?)\s*$", text, re.MULTILINE)
    if not match:
        raise BackupValidationError("missing database entry")
    if match.group(1) != expected_database:
        raise BackupValidationError("database entry mismatch")
    toc = [line for line in stdout.splitlines()
           if line.strip() and not line.lstrip().startswith(b";")]
    descriptions = []
    for line in toc:
        fields = line.split(b";", 1)[-1].split()
        if len(fields) >= 4:
            descriptions.append((fields[3].upper(), [field.upper() for field in fields[4:]]))
    return {"entries": len(toc),
            "database_toc_entry": any(kind == "DATABASE" for kind, _ in descriptions),
            "database_acl_toc_entry": any(kind == "ACL" and "DATABASE" in fields
                                           for kind, fields in descriptions)}


def _validate_backup_manifest(manifest, expected_identity):
    try:
        _validate_acl_manifest(manifest, expected_identity)
    except BackupValidationError:
        raise
    except Exception as exc:
        raise BackupValidationError("manifest validation failed") from exc


def _classify_failure(stderr: bytes) -> str:
    text = stderr.decode("utf-8", errors="ignore").lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if any(term in text for term in ("ssl", "tls", "certificate")):
        return "TLS"
    if "authentication" in text or "password" in text:
        return "authentication"
    if "does not exist" in text and ("database" in text or "role" in text):
        return "database/role"
    return "other"


def _pg_dump_failure(stderr: bytes) -> str:
    return "pg_dump failed: " + _classify_failure(stderr)


def create_backup(expected_database, expected_endpoint, expected_branch, output_name=None):
    if backend() != "postgresql":
        raise RuntimeError("DATABASE_URL must select PostgreSQL")
    parsed = urlparse(os.environ["DATABASE_URL"])
    host = (parsed.hostname or "").lower()
    if not host.startswith(expected_endpoint.lower() + ".") or "-pooler." in host:
        raise RuntimeError("Backup requires the expected direct Neon endpoint")
    dump = PG_BIN / "pg_dump.exe" if os.name == "nt" else Path(shutil.which("pg_dump") or "")
    restore = PG_BIN / "pg_restore.exe" if os.name == "nt" else Path(shutil.which("pg_restore") or "")
    if not dump.is_file() or not restore.is_file():
        raise FileNotFoundError("pg_dump and pg_restore executables are required")
    with connect() as db:
        db.execute("SET TRANSACTION READ ONLY")
        identity = _verify_target(db, expected_database, expected_endpoint, expected_branch, parsed.hostname or "")
        acl_manifest = _collect_acl_manifest(db, identity)
    _secure_backup_dir(BACKUP_DIR)
    _verify_git_ignored(BACKUP_DIR)
    if output_name is None:
        output_name = "neon-pre-migration-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".dump"
    if Path(output_name).name != output_name or not output_name.lower().endswith(".dump"):
        raise ValueError("Backup output must be a .dump filename inside the private backup directory")
    output = BACKUP_DIR / output_name
    manifest_path = output.with_name(output.name + ".acl.json")
    if output.exists() or manifest_path.exists():
        raise FileExistsError("Refusing to overwrite an existing backup file")
    child_env = {key: value for key, value in os.environ.items()
                 if key in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA"}}
    child_env.update(_libpq_env(os.environ["DATABASE_URL"]))
    # Keep owner and ACL TOC entries, including database-level grants. The
    # companion manifest captures target database/schema owners and grants.
    args = [str(dump), "--format=custom", "--create", "--no-password", "--file", str(output)]
    result = subprocess.run(args, cwd=ROOT, env=child_env, capture_output=True, check=False)
    if result.returncode:
        output.unlink(missing_ok=True)
        raise RuntimeError(_pg_dump_failure(result.stderr))
    if not output.is_file() or output.stat().st_size <= 0:
        output.unlink(missing_ok=True)
        raise RuntimeError("pg_dump produced a missing or empty archive")
    validation = subprocess.run([str(restore), "--list", str(output)], cwd=ROOT,
                                env={k: v for k, v in os.environ.items()
                                     if k in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA"}},
                                capture_output=True, check=False)
    try:
        archive_report = _inspect_restore_listing(validation.returncode, validation.stdout, expected_database)
    except BackupValidationError:
        output.unlink(missing_ok=True)
        raise
    try:
        with manifest_path.open("x", encoding="utf-8", newline="\n") as manifest_file:
            json.dump(acl_manifest, manifest_file, indent=2, sort_keys=True)
            manifest_file.write("\n")
        loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _validate_backup_manifest(loaded_manifest, identity)
    except BackupValidationError:
        output.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise
    except Exception:
        output.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise BackupValidationError("manifest validation failed") from None
    return {"path": str(output), "size_bytes": output.stat().st_size, "archive_valid": True,
            "archive_entries": archive_report["entries"], "pg_dump_exit_code": result.returncode,
            "pg_dump_stderr_empty": not bool(result.stderr),
            "database_toc_entry": archive_report["database_toc_entry"],
            "database_acl_toc_entry": archive_report["database_acl_toc_entry"],
            "sha256": _file_sha256(output), "acl_manifest_path": str(manifest_path),
            "acl_manifest_size_bytes": manifest_path.stat().st_size,
            "acl_manifest_sha256": _file_sha256(manifest_path),
            "acl_manifest_schemas": len(loaded_manifest["schemas"]),
            "acl_manifest_database_grants": len(loaded_manifest["database"]["acl"]),
            "acl_manifest_schema_grants": sum(len(schema["acl"]) for schema in loaded_manifest["schemas"])}


def _file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as backup:
        for chunk in iter(lambda: backup.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create a private custom-format Neon database backup")
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--expected-endpoint", required=True)
    parser.add_argument("--expected-branch", required=True)
    parser.add_argument("--output-name")
    args = parser.parse_args(argv)
    try:
        result = create_backup(args.expected_database, args.expected_endpoint, args.expected_branch, args.output_name)
    except Exception as exc:
        message = str(exc)
        if message.startswith("pg_dump failed:") or message in {
            "pg_restore --list failed", "missing database entry", "database entry mismatch",
            "missing database ACL entry", "manifest validation failed", "pg_dump produced a missing or empty archive",
            "Neon target identity mismatch", "PostgreSQL backup requires TLS",
        }:
            category = message
        else:
            category = type(exc).__name__
        parser.exit(1, "Backup stopped safely: " + category + "\n")
    print(f"Backup path: {result['path']}")
    print(f"Backup size: {result['size_bytes']} bytes")
    print(f"Backup SHA-256: {result['sha256']}")
    print(f"Archive validation: passed ({result['archive_entries']} entries; pg_dump exit {result['pg_dump_exit_code']}; stderr empty={result['pg_dump_stderr_empty']})")
    print(f"Archive TOC database entry: {result['database_toc_entry']}; database ACL TOC entry: {result['database_acl_toc_entry']} (database ACL is captured in the companion manifest)")
    print(f"Access manifest path: {result['acl_manifest_path']}")
    print(f"Access manifest size: {result['acl_manifest_size_bytes']} bytes")
    print(f"Access manifest SHA-256: {result['acl_manifest_sha256']}")
    print(f"Access manifest validation: passed ({result['acl_manifest_schemas']} schemas; {result['acl_manifest_database_grants']} database ACL entries; {result['acl_manifest_schema_grants']} schema ACL entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
