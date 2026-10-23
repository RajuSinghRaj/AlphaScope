"""Transactional, additive PostgreSQL schema migrations."""
from __future__ import annotations

MIGRATIONS = [
    (1, """
    -- Financial foundation.
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        base_currency TEXT NOT NULL DEFAULT 'USD',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(user_id, name)
    );
    CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);
    CREATE TABLE IF NOT EXISTS instruments (
        id TEXT PRIMARY KEY, symbol TEXT NOT NULL, asset_class TEXT NOT NULL,
        exchange TEXT, currency TEXT NOT NULL, provider TEXT, provider_symbol TEXT,
        exchange_timezone TEXT, UNIQUE(symbol, exchange, asset_class)
    );
    CREATE TABLE IF NOT EXISTS portfolios (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        name TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(account_id, name)
    );
    CREATE TABLE IF NOT EXISTS positions (
        id TEXT PRIMARY KEY,
        portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
        instrument_id TEXT NOT NULL REFERENCES instruments(id),
        quantity NUMERIC NOT NULL CHECK(quantity > 0),
        average_cost NUMERIC NOT NULL CHECK(average_cost >= 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(portfolio_id, instrument_id)
    );
    CREATE INDEX IF NOT EXISTS idx_positions_portfolio ON positions(portfolio_id);
    CREATE TABLE IF NOT EXISTS transactions (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
        instrument_id TEXT NOT NULL REFERENCES instruments(id),
        idempotency_key TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('buy','sell','deposit','withdrawal','fee')),
        quantity NUMERIC NOT NULL, unit_price NUMERIC NOT NULL,
        currency TEXT NOT NULL, occurred_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(account_id, idempotency_key)
    );
    CREATE INDEX IF NOT EXISTS idx_transactions_portfolio_time ON transactions(portfolio_id, occurred_at);
    CREATE TABLE IF NOT EXISTS watchlists (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        name TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(account_id, name)
    );
    CREATE TABLE IF NOT EXISTS watchlist_items (
        watchlist_id TEXT NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
        instrument_id TEXT NOT NULL REFERENCES instruments(id),
        added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(watchlist_id, instrument_id)
    );
    CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
        instrument_id TEXT NOT NULL REFERENCES instruments(id),
        idempotency_key TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('buy','sell')),
        order_type TEXT NOT NULL, quantity NUMERIC NOT NULL, limit_price NUMERIC,
        status TEXT NOT NULL DEFAULT 'draft', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(account_id, idempotency_key)
    );
    """),
    (2, """
    -- Authentication, durable sessions, and account-scoped holdings.
    ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL;
    CREATE TABLE IF NOT EXISTS auth_sessions (
        session_hash TEXT PRIMARY KEY,
        user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
        csrf_token TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions(expires_at);
    CREATE TABLE IF NOT EXISTS auth_attempts (
        ip_address TEXT NOT NULL, attempted_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_auth_attempts_ip_time ON auth_attempts(ip_address, attempted_at);
    CREATE TABLE IF NOT EXISTS account_holdings (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        symbol TEXT NOT NULL, quantity NUMERIC NOT NULL CHECK(quantity > 0),
        purchase_price NUMERIC NOT NULL CHECK(purchase_price >= 0),
        idempotency_key TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
        currency TEXT, asset_class TEXT,
        UNIQUE(account_id, idempotency_key)
    );
    CREATE INDEX IF NOT EXISTS idx_account_holdings_owner_time ON account_holdings(account_id, created_at);
    """),
    (3, """
    -- Batch 3 order history and explicitly unowned legacy-data quarantine.
    ALTER TABLE orders ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'local_simulator';
    ALTER TABLE orders ADD COLUMN IF NOT EXISTS provider_order_id TEXT;
    ALTER TABLE orders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
    ALTER TABLE orders ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ;
    ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
    ALTER TABLE orders ADD COLUMN IF NOT EXISTS rejection_reason TEXT;
    ALTER TABLE orders ADD COLUMN IF NOT EXISTS preview_json TEXT;
    CREATE TABLE IF NOT EXISTS order_events (
        id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        status TEXT NOT NULL, detail TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_order_events_account_time ON order_events(account_id, created_at);
    CREATE TABLE IF NOT EXISTS portfolio (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        symbol TEXT NOT NULL, quantity NUMERIC NOT NULL CHECK(quantity > 0),
        purchase_price NUMERIC NOT NULL DEFAULT 0 CHECK(purchase_price >= 0),
        created_at TIMESTAMPTZ DEFAULT now(),
        owner_account_id TEXT REFERENCES accounts(id)
    );
    ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS purchase_price NUMERIC NOT NULL DEFAULT 0;
    ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS owner_account_id TEXT REFERENCES accounts(id);
    CREATE INDEX IF NOT EXISTS idx_portfolio_owner ON portfolio(owner_account_id);
    CREATE TABLE IF NOT EXISTS portfolio_idempotency (
        idempotency_key TEXT PRIMARY KEY,
        portfolio_id BIGINT NOT NULL REFERENCES portfolio(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL,
        owner_account_id TEXT REFERENCES accounts(id)
    );
    ALTER TABLE portfolio_idempotency ADD COLUMN IF NOT EXISTS owner_account_id TEXT REFERENCES accounts(id);
    CREATE TABLE IF NOT EXISTS legacy_data_quarantine (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        source_table TEXT NOT NULL, source_id TEXT NOT NULL,
        source_payload JSONB NOT NULL, reason TEXT NOT NULL,
        imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(source_table, source_id)
    );
    CREATE TABLE IF NOT EXISTS legacy_ownership_audit (
        source_table TEXT NOT NULL,
        source_id TEXT NOT NULL,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        rationale TEXT NOT NULL,
        approved_by TEXT NOT NULL,
        approved_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY(source_table, source_id)
    );
    """),
]

# Minimum postconditions for each completed stage. A migration marker is not
# trusted if its managed tables, columns, or explicit indexes have drifted away.
REQUIRED_COLUMNS_BY_VERSION = {
    1: {
        "users": {"id", "created_at"},
        "accounts": {"id", "user_id", "name", "base_currency", "created_at"},
        "instruments": {"id", "symbol", "asset_class", "exchange", "currency", "provider", "provider_symbol", "exchange_timezone"},
        "portfolios": {"id", "account_id", "name", "created_at"},
        "positions": {"id", "portfolio_id", "instrument_id", "quantity", "average_cost", "updated_at"},
        "transactions": {"id", "account_id", "portfolio_id", "instrument_id", "idempotency_key", "side", "quantity", "unit_price", "currency", "occurred_at", "created_at"},
        "watchlists": {"id", "account_id", "name", "created_at"},
        "watchlist_items": {"watchlist_id", "instrument_id", "added_at"},
        "orders": {"id", "account_id", "portfolio_id", "instrument_id", "idempotency_key", "side", "order_type", "quantity", "limit_price", "status", "created_at"},
    },
    2: {
        "users": {"email", "password_hash"},
        "auth_sessions": {"session_hash", "user_id", "csrf_token", "created_at", "expires_at"},
        "auth_attempts": {"ip_address", "attempted_at"},
        "account_holdings": {"id", "account_id", "symbol", "quantity", "purchase_price", "idempotency_key", "created_at", "currency", "asset_class"},
    },
    3: {
        "orders": {"provider", "provider_order_id", "updated_at", "submitted_at", "cancelled_at", "rejection_reason", "preview_json"},
        "order_events": {"id", "order_id", "account_id", "status", "detail", "created_at"},
        "portfolio": {"id", "symbol", "quantity", "purchase_price", "created_at", "owner_account_id"},
        "portfolio_idempotency": {"idempotency_key", "portfolio_id", "created_at", "owner_account_id"},
        "legacy_data_quarantine": {"id", "source_table", "source_id", "source_payload", "reason", "imported_at"},
        "legacy_ownership_audit": {"source_table", "source_id", "account_id", "rationale", "approved_by", "approved_at"},
    },
}
REQUIRED_CONSTRAINTS_BY_VERSION = {
    1: {
        ("users", "users_pkey"),
        ("accounts", "accounts_pkey"), ("accounts", "accounts_user_id_fkey"), ("accounts", "accounts_user_id_name_key"),
        ("instruments", "instruments_pkey"), ("instruments", "instruments_symbol_exchange_asset_class_key"),
        ("portfolios", "portfolios_pkey"), ("portfolios", "portfolios_account_id_fkey"), ("portfolios", "portfolios_account_id_name_key"),
        ("positions", "positions_pkey"), ("positions", "positions_portfolio_id_fkey"), ("positions", "positions_instrument_id_fkey"),
        ("positions", "positions_portfolio_id_instrument_id_key"), ("positions", "positions_quantity_check"), ("positions", "positions_average_cost_check"),
        ("transactions", "transactions_pkey"), ("transactions", "transactions_account_id_fkey"),
        ("transactions", "transactions_portfolio_id_fkey"), ("transactions", "transactions_instrument_id_fkey"),
        ("transactions", "transactions_account_id_idempotency_key_key"), ("transactions", "transactions_side_check"),
        ("watchlists", "watchlists_pkey"), ("watchlists", "watchlists_account_id_fkey"), ("watchlists", "watchlists_account_id_name_key"),
        ("watchlist_items", "watchlist_items_pkey"), ("watchlist_items", "watchlist_items_watchlist_id_fkey"), ("watchlist_items", "watchlist_items_instrument_id_fkey"),
        ("orders", "orders_pkey"), ("orders", "orders_account_id_fkey"), ("orders", "orders_portfolio_id_fkey"),
        ("orders", "orders_instrument_id_fkey"), ("orders", "orders_account_id_idempotency_key_key"), ("orders", "orders_side_check"),
    },
    2: {
        ("auth_sessions", "auth_sessions_pkey"), ("auth_sessions", "auth_sessions_user_id_fkey"),
        ("account_holdings", "account_holdings_pkey"), ("account_holdings", "account_holdings_account_id_fkey"),
        ("account_holdings", "account_holdings_account_id_idempotency_key_key"),
        ("account_holdings", "account_holdings_quantity_check"), ("account_holdings", "account_holdings_purchase_price_check"),
    },
    3: {
        ("order_events", "order_events_pkey"), ("order_events", "order_events_order_id_fkey"), ("order_events", "order_events_account_id_fkey"),
        ("portfolio", "portfolio_pkey"), ("portfolio", "portfolio_owner_account_id_fkey"),
        ("portfolio", "portfolio_quantity_check"), ("portfolio", "portfolio_purchase_price_check"),
        ("portfolio_idempotency", "portfolio_idempotency_pkey"), ("portfolio_idempotency", "portfolio_idempotency_portfolio_id_fkey"),
        ("portfolio_idempotency", "portfolio_idempotency_owner_account_id_fkey"),
        ("legacy_data_quarantine", "legacy_data_quarantine_pkey"), ("legacy_data_quarantine", "legacy_data_quarantine_source_table_source_id_key"),
        ("legacy_ownership_audit", "legacy_ownership_audit_pkey"), ("legacy_ownership_audit", "legacy_ownership_audit_account_id_fkey"),
    },
}


def _managed_table_names():
    names = {"schema_migrations"}
    for _, script in MIGRATIONS:
        for line in script.splitlines():
            parts = line.strip().split()
            if len(parts) >= 6 and parts[:5] == ["CREATE", "TABLE", "IF", "NOT", "EXISTS"]:
                names.add(parts[5].split("(")[0].strip('"').lower())
    return names


def _ledger_versions(connection, *, missing_ok=True):
    tables = {row["table_name"] for row in connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()}
    if "schema_migrations" not in tables:
        if missing_ok:
            return []
        raise RuntimeError("PostgreSQL migration ledger is missing")
    try:
        ledger_columns = {row["column_name"] for row in connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'schema_migrations'").fetchall()}
        ledger_constraints = {row["constraint_name"] for row in connection.execute(
            "SELECT constraint_name FROM information_schema.table_constraints WHERE table_schema = 'public' AND table_name = 'schema_migrations'").fetchall()}
        if not {"version", "applied_at"}.issubset(ledger_columns) or "schema_migrations_pkey" not in ledger_constraints:
            raise RuntimeError("PostgreSQL migration ledger is malformed")
        rows = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        versions = [int(row["version"]) for row in rows]
    except RuntimeError:
        raise
    except Exception:
        connection.rollback()
        raise RuntimeError("PostgreSQL migration ledger is malformed") from None
    expected = list(range(1, (versions[-1] if versions else 0) + 1))
    if versions != expected:
        raise RuntimeError("PostgreSQL migration ledger has a gap or duplicate version")
    if versions and versions[-1] > len(MIGRATIONS):
        raise RuntimeError("PostgreSQL migration ledger contains an unsupported future version")
    return versions


def _validate_applied_schema(connection, versions):
    if not versions:
        return
    columns = {}
    for row in connection.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'").fetchall():
        columns.setdefault(row["table_name"], set()).add(row["column_name"])
    indexes = {row["indexname"] for row in connection.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'").fetchall()}
    constraints = {(row["table_name"], row["constraint_name"]) for row in connection.execute(
        "SELECT table_name, constraint_name FROM information_schema.table_constraints WHERE constraint_schema = 'public'").fetchall()}
    missing = []
    for version in versions:
        for table, required in REQUIRED_COLUMNS_BY_VERSION[version].items():
            absent = required - columns.get(table, set())
            if absent:
                missing.append(f"{table}.{','.join(sorted(absent))}")
        script = MIGRATIONS[version - 1][1]
        import re
        expected_indexes = set(re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+([a-z_]+)", script, re.I))
        missing.extend(f"index:{name}" for name in expected_indexes - indexes)
        missing.extend(f"constraint:{table}.{name}" for table, name in REQUIRED_CONSTRAINTS_BY_VERSION[version]
                       if (table, name) not in constraints)
    if missing:
        raise RuntimeError("Recorded PostgreSQL migrations do not match the schema: " + "; ".join(sorted(set(missing))))


def migration_state(connection):
    versions = _ledger_versions(connection)
    if not versions:
        tables = {row["table_name"] for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'").fetchall()}
        if tables.intersection(_managed_table_names() - {"schema_migrations"}):
            raise RuntimeError("Managed tables exist without migration history; schema requires manual review")
    _validate_applied_schema(connection, versions)
    return versions[-1] if versions else 0


def migrate_postgres(connection):
    """Apply missing additive migrations in one transaction; repeat calls are safe."""
    try:
        before_tables = {row["table_name"] for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'").fetchall()}
        if "schema_migrations" not in before_tables and before_tables.intersection(_managed_table_names() - {"schema_migrations"}):
            raise RuntimeError("Managed tables exist without migration history; refusing to adopt an unknown schema")
        connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        versions = _ledger_versions(connection, missing_ok=False)
        if not versions and before_tables.intersection(_managed_table_names() - {"schema_migrations"}):
            raise RuntimeError("Managed tables exist without migration history; refusing to adopt an unknown schema")
        current = versions[-1] if versions else 0
        _validate_applied_schema(connection, versions)
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            connection.executescript(script)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (?) ON CONFLICT(version) DO NOTHING", (version,))
            versions.append(version)
        _validate_applied_schema(connection, versions)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
