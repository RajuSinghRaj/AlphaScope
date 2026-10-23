-- Additive local simulation history. No brokerage account or executions are recorded.
CREATE TABLE IF NOT EXISTS order_events (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_events_account_time ON order_events(account_id, created_at);
