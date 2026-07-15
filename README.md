# Nifty 50 Momentum & Dividend Screener

A Streamlit + Supabase decision-support dashboard that screens all current
Nifty 50 constituents on momentum, dividend yield, and PEG, and classifies
each as **Green / Amber / Red / Unavailable**.

> This dashboard is an analytical tool, not investment advice. Verify data
> and consider your risk tolerance before trading.

**New to this codebase?** See [docs/CODEBASE_GUIDE.md](docs/CODEBASE_GUIDE.md)
for a developer-oriented walkthrough of how the code is organized, the
database schema, and common changes. This README covers setup and
operations; that doc covers the code itself.

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
  import_screener_csv.py            Import a screener.in CSV export as PE/PEG/dividend-yield data
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

If this project later moves to a real provider, clean up the mock rows
first -- see [Limitations](#limitations) below, this has already caused
one real data-accuracy bug on this project.

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
  `supabase/migrations/0003_views_functions.sql` and the fixes in
  `0004_fix_constituents_fk_and_view_defaults.sql` (adds the
  `nifty50_constituents -> companies` FK PostgREST needs for embedded
  queries, and defaults `status`/`data_quality` to Unavailable/`{}`
  instead of `NULL` for constituents with no snapshot yet).
- **Password reset uses a 6-digit code, not the email's magic link.**
  Supabase's recovery link puts the session token in the URL fragment
  (`#access_token=...`), which no server (including ours) ever receives,
  and Streamlit's own iframe sandbox blocks the only other way to grab it
  (JS navigating the parent page) -- confirmed directly, it throws
  `SecurityError: ... does not have permission to navigate the target
  frame`. So `request_password_reset`/`verify_recovery_code` in
  `src/utils/session.py` use Supabase's OTP code instead: the same
  recovery email also contains a 6-digit code via the `{{ .Token }}`
  template variable, verified server-side via `auth.verify_otp(...)` --
  no redirect handling needed. **This requires enabling that variable in
  your Supabase email template**: Dashboard -> Authentication -> Email
  Templates -> Reset Password -> add `{{ .Token }}` somewhere in the body
  (Supabase's default template doesn't show it by default, only the
  link). The link Supabase still includes is otherwise unused by this app.

## Environment variables

See `.env.example` for the full list with comments. Key ones:

| Variable | Purpose |
|---|---|
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | Client-side (RLS-scoped) access |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-side only; refresh scripts |
| `MARKET_DATA_PROVIDER` | `dhan`, `yfinance`, or `mock` |
| `FUNDAMENTALS_PROVIDER` | `yfinance`, `manual`, or `mock` |
| `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN` | Required when `MARKET_DATA_PROVIDER=dhan` |
| `DEFAULT_DIVIDEND_YIELD_THRESHOLD`, `DEFAULT_PEG_THRESHOLD` | Fallback thresholds before a user configures Settings |

## Market data providers

The provider layer (`src/data_providers/`) is split into two independent
interfaces so a price vendor and a fundamentals vendor can be swapped
separately:

- **`PriceDataProvider`**: `DhanProvider` (live, via [DhanHQ API
  v2](https://dhanhq.co/docs/v2/), a licensed broker -- prices only),
  `YFinancePriceProvider` (live, via the unofficial `yfinance` package,
  no key needed -- see caveats below), or `MockPriceProvider`
  (deterministic synthetic OHLCV, no credentials needed).
- **`FundamentalsDataProvider`**: `YFinanceFundamentalsProvider` (live PE/
  PEG/EPS/market-cap plus *real* per-event dividend history, no key
  needed), `ManualFundamentalsProvider` (reads hand-curated CSVs in
  `data/`, e.g. via the screener.in importer below), or
  `MockFundamentalsProvider` (synthetic).

Select via `MARKET_DATA_PROVIDER` / `FUNDAMENTALS_PROVIDER` in `.env`.
Adding a real vendor: implement the relevant ABC in `src/data_providers/`
and add a branch in `src/data_providers/factory.py`.

**yfinance (`MARKET_DATA_PROVIDER=yfinance` / `FUNDAMENTALS_PROVIDER=yfinance`)**
is the simplest way to get real data with zero paid credentials -- no
signup, no key. It's the only provider here that covers prices AND
fundamentals AND real dividend history from one source. The tradeoff:
`yfinance` wraps Yahoo Finance's internal JSON API rather than an
officially licensed feed. It's a stable, actively-maintained, widely used
library (not HTML scraping), but Yahoo's terms restrict automated
commercial use and Yahoo has rate-limited/blocked yfinance traffic before.
Treat it as good enough for personal/analytical use and prototyping;
switch to Dhan (prices) plus a real licensed fundamentals vendor before
relying on this for anything commercial. NSE symbols are addressed as
`<SYMBOL>.NS` (e.g. `RELIANCE.NS`) internally -- no config needed.

**Getting real PE/PEG/dividend data from screener.in**: screener.in has no
public API (they say so explicitly), so `scripts/import_screener_csv.py`
imports their official "Export screen results" CSV feature instead of
scraping. Build a screen containing the Nifty 50 symbols on screener.in,
export it, then:

```bash
python scripts/import_screener_csv.py path/to/export.csv
```

Columns are matched fuzzily by name (NSE Code / PE / PEG / Div Yld % /
Market Cap / EPS) since the export's exact columns depend on what you
chose to include. This writes straight to Supabase (`fundamental_snapshots`,
`dividend_events`), so the deployed app picks it up immediately -- no
redeploy needed. Re-run it periodically (e.g. weekly) as you re-export.
Note: screener.in's export gives a dividend *yield percentage*, not
individual ex-dividend dates, so the script records one synthetic
`dividend_events` row per symbol (tagged `source="screener_in_estimated"`)
sized to reproduce that yield -- it is an approximation, not real dividend
history.

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
20D returns all strictly > 0% · **C** = PEG <= threshold (default 1.0).
Note the direction flips for C: A and B pass *above* their threshold
(higher yield/returns are the desirable side), while C passes *at or
below* its threshold (a lower PEG is conventionally the desirable side --
priced reasonably relative to earnings growth). Exactly 0% return is
neutral and fails B; exactly-at-threshold PEG (e.g. 1.00 at the default
threshold) *passes* C, unlike A which fails at exactly-at-threshold. A
criterion whose inputs are missing evaluates to `None`, never `False` --
rows with any `None` criterion are **Unavailable**, not Red. See
`src/calculations/classification.py` for the exact rules and
`tests/test_calculations_classification.py` for boundary coverage (exactly
0%, exactly-at-threshold, missing-vs-confirmed-zero, staleness).

| Status | Rule |
|---|---|
| Green | A, B, and C all pass |
| Amber | one or two of A, B, C pass |
| Red | none of A, B, C pass |
| Unavailable | any criterion has missing inputs, or data is stale beyond the configured threshold |

PE/PEG/EPS/market cap feed A and C from whichever `fundamental_snapshots`
row is *most recent for that specific field*, not necessarily the row
for today -- see [`get_latest_fundamentals()`](docs/CODEBASE_GUIDE.md#repositories-srcrepositories).
A provider gap on a given day (e.g. yfinance's PEG intermittently
returning null) falls back to the last day that field had a real value,
rather than making the stock Unavailable. Only a field that has *never*
been available for a symbol reads as genuinely missing.

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

- **No officially licensed source for PE / PEG / dividend data was
  available in scope.** DhanHQ v2 (a licensed broker) only exposes prices
  -- no PE, PEG, EPS, market cap, or dividend data. NSE itself has no
  public self-serve API. screener.in and Trendlyne, the two vendor
  alternatives considered, both explicitly told us they have no public API
  either -- see their sections above. `YFinanceFundamentalsProvider` (the
  current default recommendation, see [Market data
  providers](#market-data-providers)) closes the functional gap for free
  using the unofficial `yfinance` package, but it's still not a licensed
  data agreement. `scripts/import_screener_csv.py` (screener.in's official
  CSV export) and `ManualFundamentalsProvider` (hand-curated CSVs in
  `data/`, currently empty templates -- see `data/README.md`) remain
  available as alternatives; manually-sourced rows are flagged stale after
  120 days. **To close this gap with a real licensing agreement**,
  implement `FundamentalsDataProvider` against a paid vendor (see
  `src/data_providers/base.py`) and set `FUNDAMENTALS_PROVIDER`
  accordingly -- no other code changes are needed.
- **Mock data seeded via `scripts/seed_mock_data.py` does not get cleaned
  up automatically when you switch to a real provider.** `price_history`
  and `dividend_events` are additive/upserted, so a real-provider refresh
  only overwrites rows for dates it actually fetches -- older mock price
  rows and *any* mock dividend event (dividends are deduplicated by exact
  amount, not overwritten by date) persist indefinitely otherwise. This
  caused a real bug on this project: a leftover mock dividend row
  inflated one stock's TTM dividend yield roughly 27x (1.13% shown vs.
  ~0.04% actual) until it was found and deleted. Before trusting numbers
  on a project that has ever run `seed_mock_data.py` and later switched
  providers, delete rows where `source = 'mock'` from both tables, then
  re-run `run_refresh.py --mode=screener`.
- **`yfinance` is an unofficial Yahoo Finance client, not a licensed
  feed.** It wraps Yahoo's internal JSON API rather than scraping HTML,
  and is a stable, widely-used library, but Yahoo's terms restrict
  automated commercial use and Yahoo has rate-limited/blocked yfinance
  traffic in the past. It's a reasonable default for personal/analytical
  use (which is what was requested here); replace it with Dhan (prices)
  plus a licensed fundamentals vendor before relying on this for a
  commercial product.
- **screener.in dividend yield is an estimate, not real dividend
  history.** Their CSV export gives a yield percentage, not individual
  ex-dividend dates, so `import_screener_csv.py` fabricates one dividend
  event per symbol sized to reproduce that percentage
  (`source="screener_in_estimated"`). This is fine for the TTM-yield
  criterion today but will silently age out of the 365-day window over
  the next year if not re-imported, and will never populate the Stock
  Detail dividend-history timeline with real historical payouts.
- **PEG is frequently unavailable from screener.in.** PEG isn't one of
  screener.in's default screen columns; it only comes through if you add
  a custom formula column for it. Stocks without a PEG value correctly
  show criterion C (and therefore often overall status) as Unavailable
  rather than a guess.
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
