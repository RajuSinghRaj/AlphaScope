-- Authentication state is server-side. Legacy portfolio rows intentionally remain unowned.
CREATE TABLE IF NOT EXISTS auth_sessions (
    session_hash TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions(expires_at);
CREATE TABLE IF NOT EXISTS auth_attempts (
    ip_address TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_attempts_ip_time ON auth_attempts(ip_address, attempted_at);
CREATE TABLE IF NOT EXISTS account_holdings (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    quantity TEXT NOT NULL CHECK (CAST(quantity AS NUMERIC) > 0),
    purchase_price TEXT NOT NULL CHECK (CAST(purchase_price AS NUMERIC) >= 0),
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_account_holdings_owner_time ON account_holdings(account_id, created_at);
INSERT OR IGNORE INTO schema_migrations(version) VALUES (2);
