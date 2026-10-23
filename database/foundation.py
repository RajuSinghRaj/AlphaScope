"""SQLite and PostgreSQL connection setup with additive schema migrations."""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)
MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def backend() -> str:
    configured = os.getenv("DATABASE_URL", "").strip()
    if not configured or configured.startswith("sqlite:"):
        return "sqlite"
    if configured.startswith(("postgres://", "postgresql://")):
        return "postgresql"
    raise ValueError("DATABASE_URL must use sqlite:/// or a PostgreSQL URL")


def sqlite_path() -> Path:
    configured = os.getenv("DATABASE_URL", "").strip()
    if not configured:
        return Path("/tmp/db.sqlite3") if os.getenv("VERCEL") else ROOT / "database" / "db.sqlite3"
    if configured.startswith("sqlite:///"):
        return Path(unquote(configured.removeprefix("sqlite:///"))).resolve()
    if configured.startswith("sqlite:"):
        raise ValueError("Use a SQLite DATABASE_URL such as sqlite:///./database/db.sqlite3")
    raise ValueError("DATABASE_URL is not a SQLite URL")


def postgres_dsn() -> str:
    """Normalize Neon URLs for libpq while retaining mandatory TLS."""
    configured = os.environ["DATABASE_URL"]
    parsed = urlparse(configured)
    # Some packaged libpq builds used by psycopg2 reject Neon's optional
    # channel_binding URI parameter. TLS remains explicitly required below.
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key.lower() not in {"channel_binding", "sslmode", "sslrootcert"}]
    return urlunparse(parsed._replace(query=urlencode(query)))


def postgres_tls_options() -> dict[str, str]:
    """Return strict libpq TLS settings and a usable CA trust bundle."""
    configured = os.environ["DATABASE_URL"]
    parsed = urlparse(configured)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    root_cert = query.get("sslrootcert")
    if root_cert:
        root_cert = unquote(root_cert)
        if not Path(root_cert).is_file():
            raise ValueError("Configured PostgreSQL CA certificate file is unavailable")
    else:
        try:
            import certifi
            root_cert = certifi.where()
        except ImportError as exc:
            raise RuntimeError("PostgreSQL TLS verification requires a CA trust bundle") from exc
        if not Path(root_cert).is_file():
            raise RuntimeError("PostgreSQL TLS verification requires a CA trust bundle")
    return {"sslmode": "verify-full", "sslrootcert": root_cert}


class PostgresConnection:
    """Small DB-API facade retaining the application's existing qmark SQL."""
    def __init__(self, raw):
        self.raw = raw
        self.cursor = raw.cursor()

    @staticmethod
    def _sql(sql: str) -> str:
        sql = re.sub(r"^\s*BEGIN\s+IMMEDIATE\s*;?\s*$", "BEGIN", sql, flags=re.I)
        # Application statements use qmark placeholders and do not contain literal '?'.
        return sql.replace("?", "%s")

    def execute(self, sql, params=()):
        try:
            self.cursor.execute(self._sql(sql), params)
        except Exception as exc:
            # Preserve existing route-level duplicate handling without leaking driver details.
            try:
                import psycopg2
                if isinstance(exc, psycopg2.IntegrityError):
                    raise sqlite3.IntegrityError("database constraint violation") from None
            except ImportError:
                pass
            raise
        return self.cursor

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self): self.raw.commit()
    def rollback(self): self.raw.rollback()
    def close(self): self.raw.close()
    def __enter__(self): return self
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None: self.commit()
            else: self.rollback()
        finally:
            self.close()
        return False


def connect():
    if backend() == "postgresql":
        try:
            import psycopg2
            from psycopg2.extras import DictCursor
        except ImportError as exc:
            raise RuntimeError("PostgreSQL support requires the psycopg2-binary dependency") from exc
        try:
            raw = psycopg2.connect(postgres_dsn(), cursor_factory=DictCursor,
                                   connect_timeout=8, **postgres_tls_options(), application_name="alphascope")
            # Keep the existing API contract stable: SQLite returns timestamps as strings.
            from psycopg2.extensions import new_type, register_type, string_types
            timestamp_text = new_type(string_types[1184].values, "TIMESTAMPTZ_TEXT", lambda value, cursor: value)
            register_type(timestamp_text, raw)
            return PostgresConnection(raw)
        except Exception as exc:
            # Never propagate a driver exception that may embed a connection URL.
            code = getattr(exc, "pgcode", None) or getattr(exc, "errno", None)
            category = type(exc).__name__
            suffix = f" ({category}, code {code})" if code else f" ({category})"
            raise ConnectionError("Could not connect to the configured PostgreSQL database" + suffix) from None
    path = sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def migrate(connection) -> None:
    if backend() == "postgresql":
        from database.postgres_migrations import migrate_postgres
        migrate_postgres(connection)
        return
    connection.executescript((MIGRATIONS / "001_financial_foundation.sql").read_text(encoding="utf-8"))
    connection.commit()
