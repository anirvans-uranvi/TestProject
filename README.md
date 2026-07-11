# Nifty 50 Momentum & Dividend Screener

A Streamlit + Supabase decision-support dashboard that screens all current
Nifty 50 constituents on momentum, dividend yield, and PEG, and classifies
each as **Green / Amber / Red / Unavailable**.

> This dashboard is an analytical tool, not investment advice. Verify data
> and consider your risk tolerance before trading.

## Contents

- [Architecture](#architecture)
- [Setup](#setup)
- [Supabase configuration](#supabase-configuration)
- [Environment variables](#environment-variables)
- [Market data providers](#market-data-providers)
- [Calculation logic](#calculation-logic)
- [Running tests](#running-tests)
- [Scheduled refresh](#scheduled-refresh)
- [Docker](#docker)
- [Limitations](#limitations)

## Architecture

```
app.py                  Login/landing page (Supabase Auth)
pages/                  Streamlit multipage app
  1_Dashboard.py         Screener table, metric cards, filters, CSV export
  2_Stock_Detail.py       Price/volume/dividend charts, scorecard, alerts, position notes
  3_Alerts.py             Alert CRUD + notification history
  4_Settings.py            Per-user thresholds, theme, notification channels
src/
  config.py               Pydantic Settings (env-driven)
  data_providers/         PriceDataProvider / FundamentalsDataProvider + Dhan/mock/manual impls
  models/                 Pydantic domain models
  calculations/           Pure functions: returns, dividend yield, classification, moving averages
  services/                Orchestration: screener, refresh, alerts, market calendar, explanations
  repositories/            Supabase access layer (one module per table/concern)
  notifications/           NotificationAdapter interface + in-app implementation
  utils/                   Formatting, timezones, Streamlit session/UI helpers
scripts/
  fetch_nifty50_constituents.py   Refresh companies/nifty50_constituents
  seed_mock_data.py                Backfill synthetic prices/fundamentals/dividends/snapshots
  run_refresh.py                    CLI entrypoint for cron/GitHub Actions/APScheduler
supabase/
  migrations/               Schema, RLS policies, views/functions
  seed.sql                   Current Nifty 50 constituents + companies (reference data only)
tests/                     Pytest suite (calculations, providers, services)
```

**Data flow**: providers fetch raw quotes/OHLCV/fundamentals → repositories
normalize and persist to `price_history` / `fundamental_snapshots` /
`dividend_events` → `screener_service` reads that normalized data, runs the
pure calculation engine, and persists one row per symbol per day to
`daily_screener_snapshots` (the audit trail) → Streamlit pages read the
`latest_screener_view` and re-apply the signed-in user's own thresholds
client-side (see `src/services/threshold_override.py`) so per-user
threshold changes don't require a server-side recompute.

## Setup

Requires Python 3.11+ (tested with 3.11-3.14) and a Supabase project.

```bash
python -m venv .venv
source .venv/bin/activate        # .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env             # then fill in SUPABASE_* values
```

Apply the schema to your Supabase project (via the Supabase CLI, or paste
each file into the SQL editor in order):

```bash
supabase link --project-ref <your-project-ref>
supabase db push                 # applies supabase/migrations/*.sql
psql "$DATABASE_URL" -f supabase/seed.sql   # or run seed.sql in the SQL editor
```

For local development without any paid market-data credentials:

```bash
# .env: MARKET_DATA_PROVIDER=mock, FUNDAMENTALS_PROVIDER=mock
python scripts/seed_mock_data.py     # backfills ~400 days of synthetic data
streamlit run app.py
```

Create your first account from the app's sign-in screen (Supabase Auth
email/password); confirm-by-email depends on your Supabase project's Auth
settings.

## Supabase configuration

- **Auth**: email/password is enabled by default in a new Supabase
  project. Multi-user support relies on Row Level Security -- every
  per-user table (`user_settings`, `saved_filters`, `user_positions`,
  `alerts`, `notification_log`) is scoped to `auth.uid() = user_id`
  (see `supabase/migrations/0002_rls_policies.sql`). Shared market data
  is read-only to any authenticated user.
- **Service role key**: `scripts/*.py` (refresh jobs) use
  `SUPABASE_SERVICE_ROLE_KEY` to bypass RLS and write shared data on
  behalf of all users. This key must **never** reach client-side Streamlit
  code -- `src/repositories/supabase_client.py` deliberately exposes two
  separate client factories (`get_service_client` vs `get_user_client`) so
  pages can only construct a user-scoped client.
- **Views/functions**: `latest_screener_view` (one joined row per current
  constituent) and `get_classification_history(symbol, days)` back the
  Dashboard and Stock Detail pages respectively -- see
  `supabase/migrations/0003_views_functions.sql`.

## Environment variables

See `.env.example` for the full list with comments. Key ones:

| Variable | Purpose |
|---|---|
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | Client-side (RLS-scoped) access |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-side only; refresh scripts |
| `MARKET_DATA_PROVIDER` | `dhan` or `mock` |
| `FUNDAMENTALS_PROVIDER` | `manual` or `mock` |
| `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN` | Required when `MARKET_DATA_PROVIDER=dhan` |
| `DEFAULT_DIVIDEND_YIELD_THRESHOLD`, `DEFAULT_PEG_THRESHOLD` | Fallback thresholds before a user configures Settings |

## Market data providers

The provider layer (`src/data_providers/`) is split into two independent
interfaces so a price vendor and a fundamentals vendor can be swapped
separately:

- **`PriceDataProvider`**: `DhanProvider` (live, via [DhanHQ API
  v2](https://dhanhq.co/docs/v2/)) or `MockPriceProvider` (deterministic
  synthetic OHLCV, no credentials needed).
- **`FundamentalsDataProvider`**: `ManualFundamentalsProvider` (reads
  hand-curated CSVs in `data/`) or `MockFundamentalsProvider` (synthetic).

Select via `MARKET_DATA_PROVIDER` / `FUNDAMENTALS_PROVIDER` in `.env`.
Adding a real vendor: implement the relevant ABC in `src/data_providers/`
and add a branch in `src/data_providers/factory.py`.

## Calculation logic

All calculation code lives in `src/calculations/` as pure functions with
no I/O, so they're fully unit-tested (see [Running tests](#running-tests)).

```
1-day return (%)  = ((latest price / previous trading-day close) - 1) x 100
5-day return (%)  = ((latest price / close 5 trading days ago) - 1) x 100
20-day return (%) = ((latest price / close 20 trading days ago) - 1) x 100
TTM dividend yield (%) = (sum of cash dividends, trailing 12 months / latest price) x 100
```

Adjusted close is preferred over raw close when available
(`PricePoint.effective_close`).

Criteria: **A** = TTM yield > threshold (default 3%) · **B** = 1D, 5D, and
20D returns all strictly > 0% · **C** = PEG > threshold (default 1.0).
Exactly 0% return is neutral and fails B. A criterion whose inputs are
missing evaluates to `None`, never `False` -- rows with any `None`
criterion are **Unavailable**, not Red. See
`src/calculations/classification.py` for the exact rules and
`tests/test_calculations_classification.py` for boundary coverage (exactly
0%, exactly-at-threshold, missing-vs-confirmed-zero, staleness).

| Status | Rule |
|---|---|
| Green | A, B, and C all pass |
| Amber | one or two of A, B, C pass |
| Red | none of A, B, C pass |
| Unavailable | any criterion has missing inputs, or data is stale beyond the configured threshold |

Thresholds and the staleness window are configurable per-user in
**Settings**; `src/services/threshold_override.py` re-applies a signed-in
user's thresholds to the server-computed `daily_screener_snapshots` row at
read time, so the persisted audit trail always reflects the system-default
thresholds while the UI reflects the viewer's own.

## Running tests

```bash
pytest                 # unit tests only (default; integration tests need a live Supabase)
pytest -m integration   # requires SUPABASE_* env vars pointed at a real/local project
```

The suite covers: return calculations (including insufficient-history and
zero-base edge cases), TTM dividend yield (including the
missing-vs-confirmed-zero distinction), classification boundaries (exactly
0%, exactly-at-threshold, missing data, staleness), market-calendar logic
(trading days, NSE holidays, market-state transitions), alert evaluation
(every alert type, cooldown, dedupe-key stability), and the mock
providers.

## Scheduled refresh

Three interchangeable mechanisms, pick one (or run more than one --
`provider_fetch_log` and DB constraints make refreshes idempotent):

1. **GitHub Actions** (`.github/workflows/refresh_prices.yml`): cron jobs
   for intraday (every 15 min during NSE hours), EOD, fundamentals, and
   screener recompute. Needs `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
   (and `DHAN_*` if using the live provider) as repo secrets.
2. **APScheduler daemon**: `python scripts/run_refresh.py --mode=all
   --daemon` (also the `scheduler` service in `docker-compose.yml`).
3. **Manual/cron**: `python scripts/run_refresh.py --mode=<intraday|eod|fundamentals|screener|all>`
   from any external scheduler (e.g. Supabase's own pg_cron calling an Edge
   Function that shells out, or a plain crontab).

All three write to `provider_fetch_log` (success/failure, retry count) and
retry transient provider failures with exponential backoff
(`tenacity`, in `src/services/refresh_service.py` and
`src/data_providers/dhan_provider.py`).

## Docker

```bash
docker compose up app          # Streamlit app only
docker compose up               # + the APScheduler refresh daemon
```

## Limitations

- **PE / PEG / dividend data coverage is the main known gap.** DhanHQ v2
  (the configured live price provider) only exposes prices (OHLCV,
  quotes) -- it does not provide PE, PEG, EPS, market cap, or
  dividend/corporate-action data, and no other licensed fundamentals
  vendor was in scope for this build. `ManualFundamentalsProvider` reads
  hand-curated CSVs (`data/manual_fundamentals.csv`,
  `data/manual_dividends.csv`, currently empty templates -- see
  `data/README.md`) as a stopgap; rows older than 120 days are flagged
  stale. **To close this gap**, license a fundamentals data vendor with
  NSE coverage and implement `FundamentalsDataProvider` against it (see
  `src/data_providers/base.py`), then set `FUNDAMENTALS_PROVIDER`
  accordingly -- no other code changes are needed.
- **Dhan instrument-master parsing is defensive but unverified against a
  live account.** `src/data_providers/dhan_provider.py` resolves NSE
  symbols to Dhan `security_id`s via fuzzy column matching against Dhan's
  published instrument-master CSV, and the historical/LTP endpoint
  request/response shapes follow the DhanHQ v2 docs as researched at
  build time. Verify against a live Dhan account/sandbox before trusting
  it in production -- Dhan has changed response shapes across releases.
- **NSE holiday calendar is hardcoded per year** in
  `src/services/market_calendar.py` and must be updated annually (falls
  back to weekday-only trading-day detection for years not listed).
- **Nifty 50 constituent list is a point-in-time snapshot** (compiled
  2026-07-11) seeded via `supabase/seed.sql` /
  `scripts/fetch_nifty50_constituents.py`. NSE reconstitutes the index
  semi-annually (Jan 31 / Jul 31 cutoffs) -- re-run the fetch script with
  an updated `CURRENT_CONSTITUENTS` list after each reconstitution.
- **Email/Telegram/Slack/browser-push notifications are extension
  points, not implemented.** Only the in-app channel
  (`src/notifications/inapp_adapter.py`, backed by `notification_log`) is
  wired up; the other adapter files document exactly what to implement.
- **Theme support is partial**: Streamlit's own light/dark toggle (top-right
  menu) works out of the box; the per-user `theme` setting in Settings
  additionally drives the Plotly chart template, but does not restyle the
  rest of the Streamlit chrome.
- **Intraday price storage is a same-day upsert** into `price_history`
  (today's row's `close`/`adjusted_close` updated repeatedly during market
  hours), not a separate tick-level table -- sufficient for the "latest
  price" and return calculations required here, but not a full order-book
  or tick history.
