"""Guarded PostgreSQL schema migration entry point.

Preview: python -m database.manage preview
Apply only after a separately verified, recoverable backup exists:
python -m database.manage migrate --expected-database NAME --expected-endpoint ID --expected-branch ID --backup PATH --backup-sha256 HASH
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from database.foundation import backend, connect
from database.postgres_migrations import MIGRATIONS, migration_state, migrate_postgres
from database.foundation import ROOT


def inspect_backend_tls(db):
    """Compare libpq and PostgreSQL TLS evidence for the same backend."""
    raw = getattr(db, "raw", None)
    info = getattr(raw, "info", None)
    client_tls = getattr(info, "ssl_in_use", None)
    client_pid = getattr(info, "backend_pid", None)

    def ssl_attribute(name):
        try:
            return info.ssl_attribute(name) if info is not None else None
        except Exception:
            return None

    client = {
        "status": "verified" if client_tls is True else "failed" if client_tls is False else "unverified",
        "backend_pid": client_pid,
        "protocol": ssl_attribute("protocol"),
        "cipher": ssl_attribute("cipher"),
    }
    try:
        tls = db.execute(
            "SELECT p.pid AS query_pid, s.pid AS stats_pid, s.ssl, s.version, s.cipher "
            "FROM (SELECT pg_backend_pid() AS pid) p "
            "LEFT JOIN pg_stat_ssl s ON s.pid = p.pid"
        ).fetchone()
    except Exception as exc:
        if getattr(exc, "pgcode", None) == "42501":
            server = {"status": "unverified", "reason": "permission denied reading pg_stat_ssl"}
        else:
            server = {"status": "unverified", "reason": "pg_stat_ssl query unavailable"}
        return _combine_tls_status(client, server)
    if tls is None:
        return _combine_tls_status(client, {"status": "unverified", "reason": "no current backend result"})
    query_pid = tls["query_pid"]
    # libpq documents this as a server process PID. A negative proxy-provided
    # value cannot identify a PostgreSQL backend process, so do not compare it
    # as though it were a valid PID.
    client_pid_valid = isinstance(client_pid, int) and not isinstance(client_pid, bool) and client_pid > 0
    query_pid_valid = isinstance(query_pid, int) and not isinstance(query_pid, bool) and query_pid > 0
    pid_match = client_pid == query_pid if client_pid_valid and query_pid_valid else None
    server = {"query_pid": query_pid, "stats_pid": tls["stats_pid"],
              "client_pid_matches_query": pid_match, "ssl": tls["ssl"],
              "version": tls["version"], "cipher": tls["cipher"]}
    if tls["stats_pid"] is None:
        server.update(status="unverified", reason="no pg_stat_ssl row for current backend")
    elif tls["ssl"] is None:
        server.update(status="unverified", reason="pg_stat_ssl ssl value is NULL")
    elif tls["ssl"] is False:
        server.update(status="failed", reason="pg_stat_ssl reports ssl=false")
    elif tls["ssl"] is True:
        server.update(status="verified", version=tls["version"], cipher=tls["cipher"])
    else:
        server.update(status="unverified", reason="pg_stat_ssl returned an unexpected ssl value")
    return _combine_tls_status(client, server)


def _combine_tls_status(client, server):
    client_status = client["status"]
    if client_status == "failed":
        return {"status": "failed", "reason": "libpq reports no active TLS connection", "client": client, "server": server}
    if server.get("client_pid_matches_query") is False:
        return {"status": "unverified", "reason": "libpq backend PID differs from the SQL backend PID; pooled connections can retain a stale libpq PID", "client": client, "server": server}
    if server.get("client_pid_matches_query") is None:
        return {"status": "unverified", "reason": "could not compare libpq and SQL backend PIDs", "client": client, "server": server}
    if server["status"] == "failed":
        return {"status": "failed", "reason": server["reason"], "client": client, "server": server}
    status = "verified" if client_status == "verified" and server["status"] == "verified" else "unverified"
    result = {"status": status, "client": client, "server": server}
    if status == "verified":
        result.update(version=server["version"], cipher=server["cipher"])
    return result


def _preview_client_tls_is_strict_and_direct(tls, connection_url, expected_endpoint):
    """Allow read-only preview over verified direct TLS without claiming backend TLS."""
    if tls.get("client", {}).get("status") != "verified":
        return False
    try:
        parsed = urlparse(connection_url)
        sslmode = parse_qs(parsed.query, keep_blank_values=True).get("sslmode", [])
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or "").lower()
    return (parsed.scheme in {"postgres", "postgresql"}
            and sslmode == ["verify-full"]
            and host.startswith(expected_endpoint.lower() + ".")
            and "-pooler." not in host)


def _planned_objects():
    scripts = "\n".join(script for _, script in MIGRATIONS)
    tables = sorted({"schema_migrations", *re.findall(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([a-z_]+)", scripts, re.I)})
    indexes = sorted(set(re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+([a-z_]+)", scripts, re.I)))
    constraints = []
    for table, definition in re.findall(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([a-z_]+)\s*\((.*?)\n\s*\);", scripts, re.I | re.S):
        for line in definition.splitlines():
            clause = line.strip().rstrip(",")
            if re.search(r"\b(PRIMARY\s+KEY|REFERENCES|UNIQUE|CHECK)\b", clause, re.I):
                constraints.append(f"{table}: {clause}")
    for table, column_definition in re.findall(r"ALTER\s+TABLE\s+([a-z_]+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+([^;]+)", scripts, re.I):
        if re.search(r"\bREFERENCES\b", column_definition, re.I):
            constraints.append(f"{table}: {column_definition.strip()}")
    return tables, indexes, sorted(set(constraints))


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely inspect or migrate the configured AlphaScope PostgreSQL schema")
    parser.add_argument("command", choices=("verify", "preview", "migrate"))
    parser.add_argument("--expected-database")
    parser.add_argument("--expected-endpoint")
    parser.add_argument("--expected-branch")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--backup-sha256")
    args = parser.parse_args()
    if not (args.expected_database and args.expected_endpoint and args.expected_branch):
        parser.error("Expected database, endpoint, and branch IDs are required before connecting")
    if backend() != "postgresql":
        parser.error("DATABASE_URL must point to the intended PostgreSQL database")
    with connect() as db:
        if args.command in {"verify", "preview"}:
            db.execute("SET TRANSACTION READ ONLY")
        identity = db.execute("SELECT current_database() AS name, current_setting('neon.endpoint_id',true) AS endpoint, current_setting('neon.branch_id',true) AS branch").fetchone()
        configured_host = urlparse(os.environ["DATABASE_URL"]).hostname or ""
        endpoint_id = identity["endpoint"] or ""
        configured_endpoint_matches = bool(re.match(r"^" + re.escape(endpoint_id.lower()) + r"(?:-pooler)?\.", configured_host.lower()))
        if (identity["name"] != args.expected_database or identity["endpoint"] != args.expected_endpoint
                or identity["branch"] != args.expected_branch or not configured_endpoint_matches):
            parser.error("Connected PostgreSQL target did not match the expected database, endpoint, and branch")
        tls = inspect_backend_tls(db)
        if args.command == "verify":
            print(f"Server database: {identity['name']}")
            print(f"Neon endpoint ID: {identity['endpoint']}")
            print(f"Neon branch ID: {identity['branch']}")
            if tls["status"] != "verified":
                client = tls["client"]
                server = tls["server"]
                print(f"Client TLS status: {client['status']}; backend PID: {client['backend_pid']}; protocol: {client['protocol']}; cipher: {client['cipher']}")
                print(f"Server pg_stat_ssl status: {server['status']}; query PID: {server.get('query_pid')}; pg_stat_ssl PID: {server.get('stats_pid')}; ssl: {server.get('ssl', 'unavailable')}; client PID matches query PID: {server.get('client_pid_matches_query')}; {tls.get('reason', server.get('reason', ''))}")
                return 2
            client = tls["client"]
            server = tls["server"]
            print(f"Client TLS status: {client['status']}; backend PID: {client['backend_pid']}; protocol: {client['protocol']}; cipher: {client['cipher']}")
            print(f"Server pg_stat_ssl status: verified ({server['version']}, {server['cipher']}); query PID: {server['query_pid']}; pg_stat_ssl PID: {server['stats_pid']}; client PID matches query PID: {server.get('client_pid_matches_query')}")
            return 0
        if args.command == "preview":
            if not _preview_client_tls_is_strict_and_direct(tls, os.environ["DATABASE_URL"], args.expected_endpoint):
                parser.error("Read-only preview requires verified client TLS with sslmode=verify-full on the direct endpoint")
        elif tls["status"] != "verified":
            parser.error("Migration requires verified client and backend TLS; no remote change was made")
        server_version = db.execute("SELECT current_setting('server_version') AS version").fetchone()["version"]
        current = migration_state(db)
        print(f"Server database: {identity['name']}")
        print(f"Neon endpoint ID: {identity['endpoint']}")
        print(f"Neon branch ID: {identity['branch']}")
        client = tls["client"]
        server = tls["server"]
        print(f"Client TLS status: {client['status']}; protocol: {client['protocol']}; cipher: {client['cipher']}")
        print(f"Backend TLS evidence: {server['status']}; query PID: {server.get('query_pid')}; pg_stat_ssl PID: {server.get('stats_pid')}; ssl: {server.get('ssl', 'unavailable')}; protocol: {server.get('version')}; cipher: {server.get('cipher')}; client PID matches query PID: {server.get('client_pid_matches_query')}")
        print(f"PostgreSQL server version: {server_version}")
        print(f"Applied schema version: {current}")
        pending = [version for version, _ in MIGRATIONS if version > current]
        print(f"Pending additive schema versions: {pending or 'none'}")
        if args.command == "preview":
            proposed_tables, proposed_indexes, proposed_constraints = _planned_objects()
            existing_tables = [r["table_name"] for r in db.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"
            ).fetchall()]
            existing_indexes = [r["indexname"] for r in db.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' ORDER BY indexname"
            ).fetchall()]
            existing_constraints = [
                {"table": r["table_name"], "name": r["constraint_name"], "definition": r["definition"]}
                for r in db.execute(
                    "SELECT c.relname AS table_name, p.conname AS constraint_name, "
                    "pg_get_constraintdef(p.oid) AS definition "
                    "FROM pg_constraint p JOIN pg_class c ON c.oid = p.conrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' ORDER BY c.relname, p.conname"
                ).fetchall()]
            print(f"Proposed tables: {proposed_tables}")
            print(f"Proposed explicit indexes: {proposed_indexes}")
            print(f"Proposed table constraints and foreign keys: {proposed_constraints}")
            print(f"Existing public tables: {existing_tables}")
            print(f"Existing public indexes: {existing_indexes}")
            print(f"Existing public constraints: {existing_constraints}")
            print(f"Proposed tables already present by name: {sorted(set(proposed_tables) & set(existing_tables))}")
            print(f"Proposed indexes already present by name: {sorted(set(proposed_indexes) & set(existing_indexes))}")
            print("Preview only; no schema or data was changed.")
            return 0
        if not args.backup or not args.backup.is_file() or not args.backup_sha256:
            parser.error("A verified backup file and its SHA-256 are required; no remote change was made")
        try:
            args.backup.resolve().relative_to((ROOT / ".private_backups").resolve())
        except ValueError:
            parser.error("Backup must be inside the private ignored backup directory; no remote change was made")
        digest_obj = hashlib.sha256()
        with args.backup.open("rb") as backup:
            for chunk in iter(lambda: backup.read(1024 * 1024), b""):
                digest_obj.update(chunk)
        digest = digest_obj.hexdigest()
        if digest.lower() != args.backup_sha256.lower():
            parser.error("Backup SHA-256 did not match; no remote change was made")
        restore_tool = Path(r"C:\Program Files\PostgreSQL\18\bin\pg_restore.exe")
        if not restore_tool.is_file() or subprocess.run([str(restore_tool), "--list", str(args.backup)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA"}},
                check=False).returncode:
            parser.error("Backup must be a readable pg_dump custom-format archive validated by pg_restore; no remote change was made")
        migrate_postgres(db)
        print("Additive schema migrations applied transactionally.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
