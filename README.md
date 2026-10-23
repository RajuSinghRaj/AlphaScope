# AlphaScope

AlphaScope is a Flask-based multi-market intelligence terminal for:

- US stocks
- Indian stocks
- Crypto
- Forex

It includes market snapshots and charts, price-history analysis, linked news context, theme support, and an account-scoped portfolio workspace. Yahoo Finance snapshots remain the default; an optional authenticated Alpaca WebSocket path supports US stocks and crypto when separately configured.

## Local run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

## Push to GitHub

If this folder is not already a Git repository:

```powershell
git init
git add .
git commit -m "Initial AlphaScope app"
```

Create a new empty GitHub repo, then connect and push:

```powershell
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

## Deploy on Render

This project includes a `render.yaml` and uses `gunicorn`.

### Option 1: From GitHub repo

1. Push the code to GitHub.
2. Go to Render.
3. Create a new Web Service from your GitHub repository.
4. Render should detect:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`

### Option 2: Use the included Render blueprint

1. Push this repo to GitHub.
2. In Render, choose `New +` -> `Blueprint`.
3. Select the repo.
4. Render will read `render.yaml`.

## Notes

- The SQLite database is fine for local use and light demos.
- For serious production use, switch from SQLite to Postgres.
- Market explanations are deterministic and use observed price history; they do not claim a cause without linked, time-aligned evidence.

## Deploy on Vercel

This repo now includes:

- `api/index.py`
- `vercel.json`

### Vercel steps

1. Import the GitHub repo into Vercel.
2. Keep the default project settings.
3. Deploy.

### Important Vercel notes

- Vercel runs this Flask app as a serverless Python function.
- Explanations use deterministic price-history evidence and do not require a hosted language model.
- SQLite is not persistent on Vercel. For compatibility, the app uses `/tmp/db.sqlite3` there.
- That means portfolio data on Vercel is temporary and may reset between deployments or cold starts.

For hosted multi-user use, provide durable shared storage before deployment. The local
SQLite foundation does not offer persistence across ephemeral Vercel/Render instances.
Neon and other hosted databases remain disconnected in Batch 1.

## Batch 1 backend foundation

- Local schema: `database/migrations/001_financial_foundation.sql`; apply it via the
  app's startup migration to the configured local SQLite database. It is additive and
  does not import or invent transactions for old holdings.
- Local database path remains `database/db.sqlite3` (or `/tmp/db.sqlite3` on Vercel).
  `DATABASE_URL=sqlite:///./database/db.sqlite3` can select a SQLite file explicitly.
  PostgreSQL URLs select the PostgreSQL backend. A configured database error does not
  silently fall back to another file. PostgreSQL schema changes are not run on app startup.
- Portfolio add requests require an `Idempotency-Key`; the browser generates one per
  submission. New account holdings use decimal-string quantity and cost fields.
  Unowned legacy rows retain their original SQLite `REAL` values pending explicit
  ownership mapping; they are not copied into new accounts.
- Yahoo Finance chart/snapshot data is fetched through HTTPS and short-lived in-process
  caches. `yfinance` is installed but is not used by the current route code. Yahoo's
  chart endpoint has no supported upstream WebSocket feed. When the optional Alpaca
  stream is off or stale, the frontend's thirty-second refresh is snapshot polling.
- Render web services accept inbound WebSockets; the Flask UI and stream gateway run as
  separate processes. Vercel Functions document WebSocket support with duration and
  reconnect constraints, but its `/tmp` database cannot serve as a shared session store
  for a separate gateway. Deployment files and production traffic remain unchanged.

## Batch 1B authentication and streaming

- `/register`, `/login`, `/logout` use Werkzeug's password hashing and opaque, random
  session cookies. Only SHA-256 digests of session cookies are stored server-side;
  sessions expire after seven days. Auth routes are rate limited by remote IP.
- Portfolio read, create, delete, and prediction routes require a valid session. Writes
  require the session CSRF token. `HttpOnly`, `SameSite=Lax` cookie flags are enabled;
  set `SESSION_COOKIE_SECURE=1` behind HTTPS (Render/Vercel are detected by default).
  For a gateway on an app subdomain, set `SESSION_COOKIE_DOMAIN` to the shared parent
  domain; otherwise route the WebSocket through the app host.
- New holdings are written to account-scoped `account_holdings` with quantity and cost
  stored as decimal strings, an account-scoped idempotency constraint, and one SQLite
  transaction. Existing `portfolio` rows remain unchanged and unowned. They are not
  shown to users and are not adopted automatically; map their owners explicitly before
  importing them. Existing rows continue to have their original SQLite `REAL` values.
- Alpaca was selected for the initial adapter because its documented feeds support US
  stocks and crypto via WebSockets. The adapter uses the IEX stock feed and Alpaca US
  crypto stream; it does not cover the current India/forex instruments. Alpaca's Basic
  stock coverage is IEX only, 30 symbols, with 200 historical API calls/minute; broader
  US exchange coverage requires a paid entitlement. The exact current account entitlements
  must be confirmed with the provider. [Alpaca feed plans](https://docs.alpaca.markets/us/docs/about-market-data-api),
  [stock WebSocket docs](https://docs.alpaca.markets/us/docs/streaming-market-data),
  [crypto WebSocket docs](https://docs.alpaca.markets/us/docs/real-time-crypto-pricing-data).
- `market_stream.py` contains the shared upstream adapter, event normalization, shared
  subscriptions, bounded queues, reconnect backoff, and stale status. `stream_gateway.py`
  serves authenticated browser WebSockets on a separate process. With local APCA
  credentials, the frontend defaults to `ws://localhost:8765`; hosted deployments set
  `MARKET_WS_URL`. Unsupported markets and disconnected feeds retain the snapshot
  fallback. APCA credentials are loaded only by the backend
  from `.env` or process environment; they are never embedded in browser configuration.
- The separate gateway was verified against Alpaca IEX: credentials authenticated,
  AAPL subscription was acknowledged, and a genuine timestamped Alpaca trade reached
  the authenticated gateway client and the frontend trade-handler harness. This was a
  short live verification; it is not a guarantee of ongoing provider availability.

### Local run

```powershell
pip install -r requirements.txt
$env:DATABASE_URL = "sqlite:///./database/db.sqlite3"
$env:SESSION_COOKIE_SECURE = "0"
$env:MARKET_WS_URL = "ws://localhost:8765"
$env:ALPHASCOPE_ALLOWED_ORIGINS = "http://localhost:5000"
# Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in .env; .env is loaded server-side.
python app.py
```

Run the gateway in a second PowerShell window with the same database and Alpaca
variables:

```powershell
$env:DATABASE_URL = "sqlite:///./database/db.sqlite3"
# Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in .env; never put them in browser config.
$env:ALPHASCOPE_ALLOWED_ORIGINS = "http://localhost:5000"
python stream_gateway.py
```

If Alpaca keys are not set, the gateway reports streaming unavailable and the app uses
Yahoo snapshots. For hosted use, run the gateway as its own persistent service, route
`MARKET_WS_URL` to it, allow the app's exact browser origin, and ensure both services
share the same durable database/session store. Current Render/Vercel SQLite settings
are not a safe shared production session store; use a shared PostgreSQL database for
multi-instance sessions and configure the gateway to use the same database.

## Batch 4 PostgreSQL readiness

- Install dependencies with `pip install -r requirements.txt`. For local SQLite,
  continue using `DATABASE_URL=sqlite:///./database/db.sqlite3` or leave it unset.
- For Neon, set `DATABASE_URL` only in the backend environment, using the Neon
  PostgreSQL URL (TLS is required). Use a pooled endpoint for normal web traffic only
  after checking deployment pooling limits; use the direct endpoint for admin tooling.
  Never place the URL in frontend configuration or commit it. `.env` is ignored by Git.
- Both the application connection and backup-tool `pg_dump` environment enforce
  `sslmode=verify-full` with a CA trust bundle. The backup tool requires exactly one
  `sslmode=verify-full` parameter and the expected direct endpoint.
- The PostgreSQL adapter uses psycopg2 and parameterized qmark statements. Its additive
  schema is defined in `database/postgres_migrations.py`; legacy unowned `portfolio`
  rows have no automatic owner assignment. The schema includes a quarantine table for
  a future, explicitly reviewed data import. Existing SQLite records are not copied by
  the schema migration.
- Future remote sequence: read-only identity plus strict TLS verification; create and
  validate a private backup; review the migration preview; obtain separate approval
  before applying migrations; review the import preview; then obtain separate approval
  before importing. Stop at any identity/TLS mismatch. The remote migration preview
  below is only run after the backup has been validated.
- Create a timestamped custom-format backup. The command checks the same target IDs,
  passes libpq credentials only through its child process environment, restricts the
  `.private_backups` directory to the current user, SYSTEM, and Administrators, and
  validates the archive with `pg_restore --list`. The archive keeps database/object
  ownership and ACL entries; a companion private JSON manifest records database and
  schema owners plus effective grants. The manifest contains no passwords or URLs.
  Restore requires the referenced roles to exist and their owner/grant mappings to be
  reviewed. `pg_dump` does not export role definitions or password hashes; provision
  roles separately through the approved provider process. Do not apply manifest ACLs
  blindly to a different target.

  ```powershell
  python -m database.backup --expected-database neondb --expected-endpoint ep-bitter-night-b5f6q0kd --expected-branch br-calm-mountain-b5vhl212
  $backup = Get-ChildItem .private_backups\neon-pre-migration-*.dump | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
  $backupHash = (Get-FileHash $backup.FullName -Algorithm SHA256).Hash
  Get-FileHash ($backup.FullName + '.acl.json') -Algorithm SHA256
  ```

- After validating the backup, migration previews run in a read-only transaction and
  never apply DDL. They report proposed tables, explicit indexes, constraints/foreign
  keys, and existing public schema definitions. Migration tracking rejects gaps,
  unsupported future versions, managed tables with no trustworthy ledger, and recorded
  stages whose required columns, named indexes, or constraints are missing. App startup
  refuses an uninitialized or drifted PostgreSQL schema. Verify the expected database,
  endpoint, branch, migration state, and object plan with:

  ```powershell
  python -m database.manage preview --expected-database neondb --expected-endpoint ep-bitter-night-b5f6q0kd --expected-branch br-calm-mountain-b5vhl212
  ```

- Review the exact local import report before any remote import. This offline command
  reads SQLite only and does not load `DATABASE_URL`:

  ```powershell
  python -m database.import_sqlite --source database/db.sqlite3 --dry-run
  ```

- After reviewing the migration preview, apply migrations only after separate explicit
  approval, using the verified backup:

  ```powershell
  python -m database.manage migrate --expected-database neondb --expected-endpoint ep-bitter-night-b5f6q0kd --expected-branch br-calm-mountain-b5vhl212 --backup $backup.FullName --backup-sha256 $backupHash
  ```

- After migration, review the destination import preview. Import only after a separate
  explicit approval, using the same verified backup and checksum:

  ```powershell
  python -m database.import_sqlite --source database/db.sqlite3 --postgres-preview --expected-database neondb --expected-endpoint ep-bitter-night-b5f6q0kd --expected-branch br-calm-mountain-b5vhl212
  python -m database.import_sqlite --source database/db.sqlite3 --apply --expected-database neondb --expected-endpoint ep-bitter-night-b5f6q0kd --expected-branch br-calm-mountain-b5vhl212 --backup $backup.FullName --backup-sha256 $backupHash
  ```

- The PostgreSQL migrations are versioned 1–3 and apply one additive stage at a time
  in a single transaction. `IF NOT EXISTS`/`ADD COLUMN IF NOT EXISTS` and migration
  tracking make completed stages safe to rerun; a failure rolls back the transaction.
  The local importer reconciles source/destination counts, checks conflicts and foreign
  keys, normalizes UTC timestamps, converts decimal values without binary floating
  conversion, and uses idempotent keys. Its `auth_attempts` import preserves duplicate
  event multiplicity by IP and timestamp. The four unowned legacy `portfolio` rows are
  stored in `legacy_data_quarantine` with no account assignment.
- An optional ownership decision file can be supplied to the importer's offline preview
  and, after separate approval, the importer. Each entry must name an existing source
  account plus a rationale, approver, and approval timestamp. Mapped rows are written to
  `legacy_ownership_audit`; unmapped rows remain quarantined. No such ownership decisions
  are included or presumed by this repository.
- The local SQLite source currently has migration history 1 and 2, lacks `order_events`
  and newer `orders`/`account_holdings` columns, and contains four unowned legacy rows.
  The offline report identifies these gaps; missing optional fields map to explicit
  defaults or NULL. Existing SQLite legacy `REAL` values cannot recover precision that
  SQLite already lost; their quarantine payload records decimal strings derived from
  their stored values. No remote backup, migration, or import has been run by this repo
  work. Do not claim PostgreSQL integration until a real PostgreSQL import and row-count
  reconciliation have been verified.
- To start Flask and the gateway, use the existing Batch 1 commands with the same
  backend `DATABASE_URL` configured for both processes. Set `MARKET_WS_URL` to the
  externally reachable gateway URL in hosted use and configure allowed browser origins.
  This code does not deploy either service or verify hosted multi-instance streaming.
- PostgreSQL import and hosted session/gateway behavior remain unverified against a live
  deployment. No broker execution is enabled.

## Local verification and Neon handoff

- Local setup uses Python 3.11+ and SQLite; no database server or API key is required.
  Create and activate a virtual environment, install `requirements.txt`, then run
  `python app.py`. If `DATABASE_URL` is unset, the local file is
  `database/db.sqlite3`; alternatively set it to
  `sqlite:///./database/db.sqlite3`. `.env` is loaded server-side and ignored by Git.
- Optional backend environment variables (placeholders only): `FRED_API_KEY` enables
  FRED observations; `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` enable the Alpaca
  market-data stream; `MARKET_WS_URL` points the browser to the gateway;
  `ALPHASCOPE_ALLOWED_ORIGINS` limits gateway origins; `SESSION_COOKIE_SECURE` and
  `SESSION_COOKIE_DOMAIN` configure session cookies. Do not put credentials in
  frontend configuration. Missing providers are labeled unavailable; Yahoo chart
  snapshots remain snapshots and are not live exchange events. Orders remain local
  simulator records and are never submitted to a broker.
- Public provider-backed HTTP limits default to **60 market snapshots, 12 market
  overviews, 20 news searches, 12 macro requests, and 20 move analyses per direct peer
  per 60 seconds**. Configure them with `ALPHASCOPE_RATE_LIMIT_MARKET_DATA`,
  `ALPHASCOPE_RATE_LIMIT_MARKET_OVERVIEW`, `ALPHASCOPE_RATE_LIMIT_NEWS`,
  `ALPHASCOPE_RATE_LIMIT_MACRO`, `ALPHASCOPE_RATE_LIMIT_MOVE_ANALYSIS`, and
  `ALPHASCOPE_RATE_LIMIT_WINDOW_SECONDS`. `ALPHASCOPE_RATE_LIMIT_MAX_KEYS` bounds the
  process-local map (default 4096); excess new peers share a per-route overflow bucket,
  and malformed peer addresses share an `unknown` bucket. These limits reset on process
  restart and are not shared across workers or instances. The app ignores
  `X-Forwarded-For` and other forwarding headers. Behind a reverse proxy, users may
  share the proxy's direct peer bucket; configure trusted proxy handling only when the
  proxy chain and direct network access are explicitly controlled. For shared limits
  across workers/instances, use a shared backend such as Redis.
- Login and duplicate-registration failures retain the existing database-backed limit
  of 30 failures per direct peer per 15 minutes. Successful logins and registrations do
  not consume that failure budget. Market rate limiting is separate from authentication,
  account authorization, and CSRF checks.
- Offline checks: `python -m unittest discover -s tests -v`,
  `python -m compileall -q app.py database market_intelligence.py market_stream.py stream_gateway.py tests`,
  `node --check static/js/app.js`, `node --check tests/verify_frontend_trade.cjs`,
  and `git diff --check`. The Node frontend trade check is a focused JavaScript
  harness, not browser end-to-end coverage. No browser automation result is claimed.
- Before any Neon operation, run a strict, read-only identity/TLS check for the expected
  database, endpoint, and branch. The client must use `sslmode=verify-full` with a
  trusted CA, and the current backend's `pg_stat_ssl` row must confirm TLS. A missing
  row or permission denial is unverified and blocks backup/migration/import. Never
  weaken TLS to force a connection.
- **Neon handoff (2026-10-21):** the last check used strict client certificate and
  hostname verification and matched `neondb`, `ep-bitter-night-b5f6q0kd`, and
  `br-calm-mountain-b5vhl212`, but the same backend's `pg_stat_ssl` row reported
  `ssl=false`. Locally, psycopg2 2.9.9/libpq 16 is installed, the app now sets
  `verify-full` and an installed CA bundle, and the server-side TLS check distinguishes
  false from missing/permission-denied rows. No remote backup/schema/import has run.
  Once internet access is reliable, in one read-only session verify the target identity,
  inspect `connection.info.ssl_in_use` (client TLS) and the current backend's
  `pg_stat_ssl` row (server view), and ask Neon to explain any mismatch. Stop if either
  side is false or unverified; do not bypass certificate verification. Proceed only
  after both identity and TLS are confirmed: create and validate a private custom-format
  backup; review the migration preview and obtain separate approval before migration;
  then review the import dry-run/count reconciliation and obtain separate approval
  before import. Keep the four unowned legacy portfolio rows in quarantine unless
  ownership is independently verified and explicitly approved.

## Batch 2 market intelligence

- The existing US, crypto, India, and forex overview continues to use Yahoo Finance
  chart snapshots. These are snapshots/history, not real-time exchange feeds or
  executable quotes. Quote payloads label the source, snapshot status, chart timestamp,
  and server receipt time. Alpaca IEX trade events remain distinctly labeled `Stream
  trade`; IEX is not consolidated SIP coverage.
- Alpaca crypto events are from Alpaca's supported crypto feed and represent that feed's
  coverage, not a universal market price. Indian equities and forex have snapshot or
  reference coverage only. Commodities and bond prices/yields are not supported quote
  products in this batch.
- `/api/macro` shows CPI, federal funds rate, unemployment, GDP, and the 10-year Treasury
  constant-maturity rate from FRED when `FRED_API_KEY` is configured. Observations carry
  observation dates; this implementation does not infer their release timestamps.
  The UI labels observations `current_for_frequency` or `stale` using explicit age
  windows (7 days daily, 90 monthly, 180 quarterly); these are display freshness
  heuristics, not revised-data guarantees.
  Without the optional key, values are explicitly unavailable. FRED requires a
  registered key, can throttle requests (documented limit 120/minute), and its terms
  require an in-app link to terms and applicable privacy/usage notice. See the
  [FRED API terms](https://fred.stlouisfed.org/docs/api/terms_of_use.html) and
  [observation endpoint docs](https://fred.stlouisfed.org/docs/api/fred/series_observations.html).
  Each series is cached for six hours. No paid feed is enabled.
- No third-party economic calendar is configured. The macro panel links official FOMC,
  BEA, and BLS calendars; it does not synthesize event schedules or consensus data.
- `/api/news` and the asset news panel use Google News RSS search. Results preserve
  publisher attribution when present, article link, parsed publication time (or an
  explicit unavailable state), and server receipt time. Duplicate headlines are removed.
  Headline sentiment is not scored. This is a search feed, not a licensed real-time
  market newswire; availability and coverage are not guaranteed.
- Movement summaries measure first-to-last valid points in the selected chart window.
  A linked, dated headline is included only if its publication time falls inside that
  window. Timing does not establish causation; otherwise the UI says the cause is
  undetermined. These summaries are descriptive, not predictions or investment advice.
- Optional macro setup: add `FRED_API_KEY` to the backend `.env` (never frontend code).
  Use a registered key, review the terms, and add required user-facing terms/privacy
  language before making this app available to other users.

## Batch 3 portfolio and order workflow

- `/api/watchlist` uses the existing `watchlists`, `watchlist_items`, and `instruments`
  tables. Each user's default list is isolated by account, capped at 30 supported
  instruments, and rejects duplicate symbols. Add/remove requests require CSRF tokens.
- The portfolio drawer shows local holdings, decimal-string quantities/cost basis,
  source-labelled snapshot marks, per-position market value and unrealized P&L. Portfolio
  totals are withheld when marks are unavailable/stale or currencies differ; per-currency
  subtotals are available in the API. Broker cash, equity, buying power, and realized P&L
  remain unavailable because this app has no brokerage sync or recorded fills. Legacy
  unowned portfolio rows remain unchanged and excluded.
- The order ticket supports simulated buy/sell market and limit drafts for the current
  Yahoo-covered markets. Previews show a non-executable reference price and estimated
  notional. Confirmation is a separate CSRF-protected action and requires the UI checkbox
  plus server-side confirmation. Sell quantity is limited to locally tracked holdings;
  pending simulated sells reserve quantity. There is no buying-power check.
- **Execution is local-simulator-only.** Accepted orders are activity-log entries, not
  brokerage submissions; they never fill, move cash, or alter positions. Cancellation
  only changes local simulator state. No Alpaca paper credentials or live credentials
  are read by the order service; there is no paper/live endpoint selection or live-order
  fallback. The additive `database/migrations/003_batch3_order_events.sql` stores status
  history. No destructive or hosted migration was run.
- Run the app and gateway using the Batch 1 commands above. The trade ticket needs no
  extra package or broker secret. Existing `.env` remains ignored. A future actual paper
  broker adapter must use separate paper credentials and a hard-coded or allowlisted
  sandbox endpoint, then add provider order-status reconciliation and fill/transaction
  accounting before displaying broker balances or realized P&L.
