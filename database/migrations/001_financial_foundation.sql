-- Additive SQLite foundation. Existing portfolio records are preserved.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    base_currency TEXT NOT NULL DEFAULT 'USD',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);
CREATE TABLE IF NOT EXISTS instruments (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    exchange TEXT,
    currency TEXT NOT NULL,
    provider TEXT,
    provider_symbol TEXT,
    exchange_timezone TEXT,
    UNIQUE(symbol, exchange, asset_class)
);
CREATE TABLE IF NOT EXISTS portfolios (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(account_id, name)
);
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    quantity TEXT NOT NULL CHECK (CAST(quantity AS NUMERIC) > 0),
    average_cost TEXT NOT NULL CHECK (CAST(average_cost AS NUMERIC) >= 0),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(portfolio_id, instrument_id)
);
CREATE INDEX IF NOT EXISTS idx_positions_portfolio ON positions(portfolio_id);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    idempotency_key TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell', 'deposit', 'withdrawal', 'fee')),
    quantity TEXT NOT NULL,
    unit_price TEXT NOT NULL,
    currency TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(account_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_transactions_portfolio_time ON transactions(portfolio_id, occurred_at);
CREATE TABLE IF NOT EXISTS watchlists (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(account_id, name)
);
CREATE TABLE IF NOT EXISTS watchlist_items (
    watchlist_id TEXT NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY(watchlist_id, instrument_id)
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    idempotency_key TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    order_type TEXT NOT NULL,
    quantity TEXT NOT NULL,
    limit_price TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(account_id, idempotency_key)
);
INSERT OR IGNORE INTO schema_migrations(version) VALUES (1);
