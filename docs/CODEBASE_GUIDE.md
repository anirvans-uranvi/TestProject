# Codebase Guide

This document is for a developer picking up this repository for the first
time. It explains *how the code is organized and why*, not how to deploy
it — for setup, environment variables, and operational limitations, see
[README.md](../README.md).

## Contents

- [What this app does](#what-this-app-does)
- [Layered architecture](#layered-architecture)
- [Directory map](#directory-map)
- [Database schema](#database-schema)
- [Domain models (`src/models/`)](#domain-models-srcmodels)
- [Calculation engine (`src/calculations/`)](#calculation-engine-srccalculations)
- [Data providers (`src/data_providers/`)](#data-providers-srcdata_providers)
- [Repositories (`src/repositories/`)](#repositories-srcrepositories)
- [Services (`src/services/`)](#services-srcservices)
- [Notifications (`src/notifications/`)](#notifications-srcnotifications)
- [Streamlit app (`app.py`, `pages/`)](#streamlit-app-apppy-pages)
- [Auth: a non-obvious quirk](#auth-a-non-obvious-quirk)
- [Utils (`src/utils/`)](#utils-srcutils)
- [Scripts (`scripts/`)](#scripts-scripts)
- [Edge Functions (`supabase/functions/`)](#edge-functions-supabasefunctions)
- [Tests (`tests/`)](#tests-tests)
- [Common changes, step by step](#common-changes-step-by-step)

## What this app does

A Streamlit dashboard that screens all current Nifty 50 stocks daily and
classifies each as **Green / Amber / Red / Unavailable** based on three
criteria: dividend yield, 1/5/20-day price momentum, and PEG ratio. Users
sign in (Supabase Auth), configure their own thresholds, set alerts, and
browse per-stock detail pages with charts.

The one thing to internalize before reading further: **raw market data,
normalized market data, and calculated results are three distinct layers,
stored in three distinct kinds of tables**, and the code is organized
around that same separation:

```
Provider (Dhan/yfinance/mock)  --fetch-->  raw quotes/OHLCV/fundamentals
        |
        v  (refresh_service normalizes + persists)
price_history / fundamental_snapshots / dividend_events   <- normalized, provider-agnostic
        |
        v  (screener_service reads normalized data, runs pure calculations)
daily_screener_snapshots   <- one calculated row per symbol per day (the audit trail)
        |
        v  (Streamlit pages read via latest_screener_view, re-apply per-user thresholds)
Dashboard / Stock Detail
```

## Layered architecture

```
pages/*.py  ─┐
app.py      ─┤  Streamlit UI layer. Reads/writes via repositories only.
             │  Auth/session state lives in src/utils/session.py.
             ▼
src/services/        Orchestration + business rules that need I/O
             │        (screener_service, refresh_service, alert_service,
             │         market_calendar, threshold_override, explanation)
             ▼
src/repositories/    One module per table/concern. Every function takes
             │        an explicit supabase `Client` argument -- callers
             │        decide whether to use a service-role client
             │        (bypasses RLS, server-side only) or a user-scoped
             │        client (RLS applies). See supabase_client.py.
             ▼
Supabase Postgres    Schema + RLS policies + views/functions
                      (supabase/migrations/*.sql)

src/data_providers/  Fetches from external vendors (Dhan/yfinance/mock/
                      manual CSV). Used only by refresh_service and the
                      one-off scripts -- pages never call a provider
                      directly, they only ever read already-persisted
                      data via repositories.

src/calculations/    Pure functions, no I/O, no Streamlit, no Supabase.
                      This is where the actual spec logic (returns, TTM
                      yield, Green/Amber/Red rules) lives, and it's the
                      most heavily unit-tested part of the codebase for
                      exactly that reason.

src/models/          Pydantic models shared by every layer above.
src/utils/           Cross-cutting helpers: formatting, timezones,
                      Streamlit session/auth, shared UI fragments, logging.
```

Why split calculations out as pure functions instead of methods on a
service class: every rule in the spec (exactly-0%-is-neutral, missing
data must never read as a failed criterion, PEG passes at-or-below its
threshold while the other two criteria pass strictly above theirs) is a
one-line, deterministic, easily-misremembered rule. Keeping them as
standalone functions with no dependencies means every rule has a direct,
fast, no-mocking-required test in `tests/test_calculations_*.py`.

## Directory map

```
.streamlit/config.toml          Streamlit's own [theme] (light base, indigo primaryColor) + toolbarMode
app.py                          Login/landing page, Supabase Auth
pages/
  1_Dashboard.py                 Screener table, metric cards, filters, CSV export
  2_Stock_Detail.py               Price/volume/dividend charts, scorecard, alerts, position notes
  3_Alerts.py                     Alert CRUD + notification history
  4_Settings.py                    Per-user thresholds, theme, change password
  5_Options.py                     F&O: futures term structure + 5% CSP/CC breakdown per stock
src/
  config.py                       Pydantic Settings, reads .env
  models/                          Pydantic domain models + enums
  calculations/                    Pure functions: returns, dividends, classification, moving averages
  data_providers/                  PriceDataProvider / FundamentalsDataProvider + 4 implementations
  repositories/                    Supabase access, one module per table/concern
  services/                        Orchestration: screener, refresh, alerts, market calendar, explanations
  notifications/                  NotificationAdapter interface + in-app implementation
  utils/                           Formatting, timezones, Streamlit session/auth, shared UI, logging
scripts/
  fetch_nifty50_constituents.py   Refresh companies/nifty50_constituents from a maintained symbol list
  seed_mock_data.py                Backfill synthetic prices/fundamentals/dividends/snapshots + mock F&O (local dev)
  import_screener_csv.py           Import a screener.in CSV export as fundamentals data
  fetch_fo_data.py                  Backfill NSE F&O bhavcopy (futures + options) into Supabase (--days 60)
  cleanup_mock_data.py               Delete leftover source='mock' rows (dry-run by default, --confirm to delete)
  run_refresh.py                    CLI entrypoint for cron/GitHub Actions/APScheduler
supabase/
  migrations/                      Schema, RLS policies, views/functions, in numbered order
  seed.sql                          Current Nifty 50 constituents + companies (reference data only)
  functions/manual-refresh/         Edge Function (Deno/TypeScript) behind "Stock Data Refresh"
  functions/fo-refresh/              Edge Function (Deno/TypeScript) behind "F&O Data Refresh"
tests/                             Pytest suite -- almost entirely calculations/services, no network
```

## Database schema

All migrations live in `supabase/migrations/`, applied in numeric order
(`0001` → `0010`). Seventeen tables, in three groups (`0008` doesn't add a
table -- it just extends `provider_fetch_log.fetch_type`'s CHECK
constraint with `'fo'`, for the `fo-refresh` Edge Function's logging;
`0010` drops and recreates `dashboard_fo_metrics` with a different key
rather than adding a new table -- see "Dashboard cache" below):

**Reference data** (written by `scripts/fetch_nifty50_constituents.py` /
`seed.sql`, read-only to the app):
- `nifty50_constituents` — which symbols are in the index and when (supports historical reconstitution tracking)
- `companies` — name/sector/industry per symbol

**Market data** (written by `refresh_service` / provider scripts, read-only to the app):
- `price_history` — daily OHLCV, one row per symbol per trade_date
- `fundamental_snapshots` — PE/PEG/EPS/market cap/52-week high/52-week low, one row per symbol per as_of_date
- `dividend_events` — individual ex-dividend cash amounts
- `daily_screener_snapshots` — the calculated audit trail: one row per symbol per day with the computed returns, TTM yield, criteria A/B/C, the two 52-week high/low proximity flags, and status. This is what the classification-history chart on Stock Detail reads.
- `provider_fetch_log` — success/failure log for every provider call, used for the Dashboard's "data freshness" indicator and for retry/backoff auditing
- `futures_contracts` / `futures_daily_prices` / `option_contracts` / `option_daily_prices` — NSE F&O derivatives (migration `0007`), written by `scripts/fetch_fo_data.py`. See the Futures & Options section for the contract-dimension vs daily-price-fact split and why the source is the EOD bhavcopy.
- `dashboard_fo_metrics` — the Dashboard's precomputed "5% CSP"/"5% CC" cache, one row per **(symbol, expiry_date)** -- up to 3 rows per symbol, near/next/far (migration `0011`, replacing `0010`'s pmcc_* columns with cc_* ones; `0010` itself re-keyed `0009`'s one-row-per-symbol shape). See the Futures & Options section for why this exists and everywhere that recomputes it.

**Per-user data** (RLS-scoped to `auth.uid() = user_id`):
- `user_settings` — thresholds, theme
- `saved_filters` — named filter presets
- `user_positions` — entry/target/stop-loss/notes per symbol
- `alerts` — alert configs
- `notification_log` — alert-fired history, deduped via a unique `dedupe_key`

Two generated helpers, defined in `0003_views_functions.sql` (and patched
in `0004`):
- `latest_screener_view` — one joined row per current constituent (companies + its latest daily_screener_snapshot). This is what the Dashboard queries in a single call instead of joining client-side. `0004` added `coalesce(status, 'unavailable')` / `coalesce(data_quality, '{}')` here because a constituent with no snapshot yet would otherwise return `NULL` for those columns, which fails Pydantic validation on the `ScreenerRow` model. `0006` added `week_52_high`/`week_52_low`/`criterion_52w_high`/`criterion_52w_low` — **a real deploy-time error hit here**: `create or replace view` can only *append* new output columns; inserting them positionally in the middle of the existing `select` list (as the first draft of `0006` did) makes Postgres think you're renaming the columns that got pushed down a slot, and it fails with `42P16: cannot change name of view column ... HINT: Use ALTER VIEW ... RENAME COLUMN ... instead`. The fix is to always append new columns at the very end of the `select` list in any future `create or replace view` migration, never insert them mid-list — column *order* doesn't matter to the app since every read is by name (`ScreenerRow.model_validate(dict)`), so this costs nothing.
- `get_classification_history(symbol, days)` — a SQL function returning one symbol's snapshot history, used by the Stock Detail status-over-time chart.

Migration `0007` adds two more views on the same `DISTINCT ON` pattern:
`latest_futures_view` and `latest_option_chain_view` — the newest daily
row per open futures / option contract, so the Options page loads the
current term structure / chain in one query.

RLS (`0002_rls_policies.sql`): shared tables are `SELECT`-only for the
`authenticated` role (writes only happen via the service-role key, which
bypasses RLS entirely); per-user tables use `auth.uid() = user_id` on
every operation. `0004` also added a foreign key from
`nifty50_constituents.symbol` to `companies.symbol` — without it,
PostgREST can't resolve the embedded-resource query
`companies_repo.list_current_constituents()` uses (`select("symbol,
companies(...)")`); PostgREST needs a declared FK to know how to join two
tables via that syntax.

## Domain models (`src/models/`)

Pydantic v2 models, one file per concern (`company.py`, `market_data.py`,
`screener.py`, `user.py`, `alert.py`, `fetch_log.py`, `fo.py`), plus
`enums.py` for every `StrEnum` (`ScreenerStatus`, `MarketState`,
`AlertType`, `NotificationChannel`, `Theme`, `FetchType`, `FetchStatus`,
`DividendType`, `OptionType`). Everything is re-exported from
`src/models/__init__.py`. `fo.py` holds the four F&O models
(`FuturesContract`, `FuturesDailyPrice`, `OptionContract`,
`OptionDailyPrice`) — see the Futures & Options section.

Worth knowing:
- `PricePoint.effective_close` prefers `adjusted_close` over `close` — every return calculation goes through this property, not the raw fields directly.
- `DataQuality` (in `screener.py`) is a structured record of *which* inputs were missing/stale when a row was classified — it's not inferred after the fact, it's built alongside the classification so the UI can always explain an Unavailable row.
- `UserPosition.risk_reward_ratio` is a computed property, not stored — `(target - entry) / (entry - stop_loss)`, `None` if any leg is missing or risk is non-positive.

## Calculation engine (`src/calculations/`)

No I/O, no Streamlit, no Supabase imports — every function here takes
plain values and returns plain values, which is what makes them cheap to
test exhaustively.

- **`returns.py`**: `pct_return(latest, base)` and `return_1d/5d/20d(latest_price, historical_closes)`. `historical_closes` must be ordered oldest→newest and must NOT include the day `latest_price` came from — see the note on `screener_service.py` under [Services](#services-srcservices) for a real bug this exact boundary caused.
- **`dividends.py`**: `ttm_dividend_sum`/`ttm_dividend_yield(events, as_of_date, latest_price)`. An empty `dividend_events` list sums to `0.0` (a confirmed-zero yield), not `None` — missing-vs-zero is a distinction the *caller* (the provider/repo layer) is responsible for, based on whether a fundamentals fetch actually succeeded.
- **`classification.py`**: `criterion_a/b/c()` each return `bool | None` (`None` = missing input, never a fail). `criterion_a`/`criterion_b` pass strictly *above* their threshold; `criterion_c` (PEG) passes *at or below* its threshold — the direction is deliberately reversed for PEG, since a lower PEG is the conventionally desirable side. `classify(a, b, c, is_stale)` short-circuits to `UNAVAILABLE` if `is_stale` or any criterion is `None`, before ever checking pass/fail counts — this ordering is the whole point of the "missing is never a failure" rule. `build_classification(...)` is the one-stop version that also assembles the `DataQuality` record. `criterion_52w_high(latest_price, week_52_high)`/`criterion_52w_low(latest_price, week_52_low)` are separate, **display-only** functions — deliberately *not* threaded into `build_classification`/`classify`, so they have zero effect on Green/Amber/Red status. `criterion_52w_high` passes when price is below 90% of the 52-week high (`latest_price < 0.9 * week_52_high`); `criterion_52w_low` passes when price is above 110% of the 52-week low (`latest_price > 1.1 * week_52_low`). Both return `None` (not a fail) when either input is missing.
- **`moving_averages.py`**: `moving_average_series()` (pandas, for the Stock Detail chart, `min_periods=window` so a partial window renders as `NaN` not a misleading partial average) and `latest_moving_average()` (scalar, for scorecards).

`tests/test_calculations_*.py` specifically cover the boundary cases:
exactly 0% return (fails B), exactly 3.00% yield (fails A, strict `>`),
exactly PEG 1.00 at the default threshold (**passes** C, since C uses
`<=`), missing vs. confirmed-zero, and every missing-data combination for
`classify()`.

## Data providers (`src/data_providers/`)

Two abstract interfaces in `base.py`:

```python
class PriceDataProvider(ABC):
    def get_quote(symbol) -> Quote
    def get_quotes(symbols) -> dict[str, Quote]
    def get_historical_daily(symbol, from_date, to_date) -> list[PricePoint]

class FundamentalsDataProvider(ABC):
    def get_fundamentals(symbol, as_of) -> FundamentalSnapshot | None
    def get_dividend_history(symbol, from_date, to_date) -> list[DividendEvent]
```

They're split because a price vendor and a fundamentals vendor are
independently swappable — no single vendor considered for this project
covers both well. Implementations:

| | Price | Fundamentals |
|---|---|---|
| `dhan_provider.py` | `DhanProvider` — live, DhanHQ v2, prices only | — |
| `yfinance_provider.py` | `YFinancePriceProvider` | `YFinanceFundamentalsProvider` — both live, free, no key |
| `manual_fundamentals_provider.py` | — | `ManualFundamentalsProvider` — reads `data/*.csv`, populated by `scripts/import_screener_csv.py` |
| `mock_provider.py` | `MockPriceProvider` | `MockFundamentalsProvider` — deterministic synthetic data, seeded per-symbol |

`factory.py` picks the concrete class from `Settings.market_data_provider`
/ `Settings.fundamentals_provider` (`.env`: `MARKET_DATA_PROVIDER`,
`FUNDAMENTALS_PROVIDER`). **To add a new vendor**: implement the relevant
ABC, add one branch in `factory.py`, add the new value to the `Literal[...]`
type in `src/config.py`. Nothing else needs to change — `refresh_service`
and the scripts only ever go through the ABC's interface.

`dhan_provider.py` resolves NSE symbols to Dhan's numeric `security_id`
via fuzzy column-matching against Dhan's published instrument-master CSV
(cached with `@lru_cache`), since Dhan requires that ID rather than the
trading symbol directly. `yfinance_provider.py` just appends `.NS` to the
symbol. Both wrap network calls in `tenacity` retry with exponential
backoff and a client-side request-rate throttle.

## Repositories (`src/repositories/`)

One module per table/concern (`companies_repo.py`, `price_repo.py`,
`fundamentals_repo.py`, `dividends_repo.py`, `snapshot_repo.py`,
`settings_repo.py`, `alerts_repo.py`, `notification_repo.py`,
`fetch_log_repo.py`), plus `supabase_client.py` for client construction.

The one convention that matters everywhere in this layer: **every
function takes an explicit `Client` argument** — there's no module-level
singleton client. `supabase_client.py` exposes two factories:

```python
get_service_client()               # SUPABASE_SERVICE_ROLE_KEY, bypasses RLS
get_user_client(access_token, ...)  # SUPABASE_ANON_KEY + a logged-in user's JWT, RLS applies
```

Server-side scripts (`scripts/*.py`, `refresh_service.py`) always use
`get_service_client()`. Streamlit pages always use
`src.utils.session.get_user_client_cached()`, which wraps
`get_user_client()` with the current session's tokens from
`st.session_state`. **Never import `get_service_client` into `pages/*.py`**
— that would ship the service-role key's privileges to whatever a page
does, defeating RLS entirely.

`fundamentals_repo.get_latest_fundamentals()` does NOT simply return the
single most recent `fundamental_snapshots` row. Each field (`pe_ratio`,
`peg_ratio`, `eps`, `market_cap`, `week_52_high`, `week_52_low`) is carried forward **independently**
from the most recent row where that specific field was actually non-null
(`carry_forward_fields()`, a pure helper directly unit-tested in
`tests/test_fundamentals_repo.py`). This matters because a single day's
fetch commonly has gaps — yfinance's `pegRatio` in particular is
intermittently `None` for a symbol even on a day PE/EPS came back fine —
and treating "missing in today's row" the same as "never available"
would flag a stock Unavailable despite a perfectly good recent value
existing. There is deliberately no equivalent for `price_history` or
`dividend_events`: prices are only ever inserted with `close` populated
(no partial rows to fall back within), and dividend TTM yield already
sums *all* historical events in the trailing-365-day window rather than
reading a single "latest" row, so both already use whatever data exists
without needing this treatment.

## Services (`src/services/`)

- **`screener_service.py`** — `compute_screener_row(...)` is the pure calculation step (calls into `src/calculations/`, fully unit-tested in `tests/test_screener_service.py`). `refresh_screener_row_for_symbol(client, symbol, ...)` is the I/O wrapper: reads normalized data back out of Supabase, calls `compute_screener_row`, persists the result. **A real bug was found and fixed here**: the history-window upper bound must be `latest_point.trade_date - 1 day`, not a fixed `as_of_date - 1` — when no intraday quote has been fetched yet, `get_latest_close()` returns the most recent EOD row, which could be *older* than `as_of_date - 1`; using a fixed cutoff let that same row appear as both `latest_price` and the last element of `historical_closes`, silently forcing `return_1d` to exactly `0.0` for every symbol. If you ever touch this function, keep that comment — it's easy to reintroduce.
  **A second real bug, found later**: `valid_closes(history)` (a pure helper, unit-tested in `tests/test_screener_service.py::TestValidCloses`) filters out any `PricePoint` with no close at all before it becomes `historical_closes` — Yahoo's chart endpoint sometimes includes a timestamp for an NSE holiday with null OHLCV (confirmed directly by querying live `price_history`: several unrelated large caps all had an identical all-NULL row for the same date, sourced from `manual_edge`), and when that landed exactly at the "1 day ago" position, `return_1d` went `None` even though a real previous close existed just one day further back. The TypeScript Edge Function (`supabase/functions/manual-refresh/index.ts`) has the same `.filter((c) => c !== null)` fix on its own `historicalCloses` construction — keep both in sync, same as every other calculation ported there. Separately, `return_1d`/`5d`/`20d` correctly being `None` for a row was *displaying* as the literal string `"nan%"` on the Dashboard rather than `"—"` — see `formatting.py` below for that half of the fix.
- **`refresh_service.py`** — fetch (via a provider) → normalize → persist raw/normalized records, with retry + `provider_fetch_log` auditing. Intraday price upserts only include the columns actually fetched (`close`/`adjusted_close`) so a same-day EOD upsert filling `open`/`high`/`low` later isn't clobbered, and vice versa (PostgREST's upsert only sets columns present in the request body).
- **`alert_service.py`** — `evaluate_alert(alert, current_snapshot, previous_snapshot, stock_name, now)` is pure (no I/O) and covers all ten `AlertType` values, cooldown (`last_triggered_at` + `cooldown_minutes`), and a stable SHA-256 `dedupe_key` (same alert+symbol+day always produces the same key, so a DB-level unique constraint on `notification_log.dedupe_key` is the final backstop against double-firing). Callers persist the returned `NotificationEvent`s via `notifications/inapp_adapter.py`.
- **`market_calendar.py`** — NSE trading-day/market-state logic. The holiday list (`NSE_HOLIDAYS`) is hardcoded **per calendar year** and needs a manual update every year; falls back to weekday-only for years not listed.
- **`threshold_override.py`** — `daily_screener_snapshots` is computed server-side against *default* thresholds (the stable audit trail); a signed-in user can configure their own thresholds in Settings, so pages re-run `build_classification()` client-side against the row's stored raw inputs (which are threshold-independent) to reflect that choice, without a server-side recompute per user. Also recomputes `is_stale` from `data_quality.stale_minutes` against the user's own `stale_data_threshold_minutes` when available.
- **`explanation.py`** — `explain_classification(row)` builds the plain-English sentence shown on Stock Detail, branching on which criteria passed/failed/are missing.

## Notifications (`src/notifications/`)

`base.py` defines `NotificationAdapter.send(event) -> bool`. Only
`inapp_adapter.py` is implemented (writes to `notification_log`, surfaced
via the Alerts page). `email_adapter.py`, `telegram_adapter.py`,
`slack_adapter.py` are stubs — each raises `NotImplementedError` with a
docstring describing exactly what to wire up (credentials needed, what
API call to make). Extending notifications means implementing one of
these, not touching `alert_service.py`.

## Futures & Options (F&O) data

A separate, self-contained subsystem for NSE derivatives on the 50
constituents — futures + option chains — feeding the Options screen
(`pages/5_Options.py`). It does **not** go through the
`PriceDataProvider`/`FundamentalsDataProvider` ABCs; F&O has its own shape.

**Data source — and why it's the only viable one** (settled empirically):
- **yfinance carries no NSE derivatives** — `Ticker("RELIANCE.NS").options`
  is empty. Yahoo does not list NSE options/futures.
- **NSE's live option-chain API** (`/api/option-chain-equities`) returns
  HTTP 200 with hollow JSON (`expiryDates: None`) to non-interactive
  sessions — its anti-bot layer. Unusable from a script.
- **NSE F&O UDiFF bhavcopy** — the reliable source. One zip per trading
  day at `https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip`,
  downloads with just a browser User-Agent (no cookie handshake — note
  the `nsearchives` host; the older `archives.nseindia.com` host is now
  bot-blocked and serves a PDF). Each row is one contract's full trading
  day: OHLC, LTP, prev close, settlement, underlying (spot), open interest
  + change, volume, turnover, trades, expiry, strike, CE/PE, lot size.
  Instrument types: `STF` = stock future, `STO` = stock option (index
  `IDF`/`IDO` are ignored). **This is end-of-day data** (published ~6pm
  IST) — "latest price" means the most recent close/settlement, never an
  intraday live quote. There is no free live/intraday F&O feed.

**Greeks / implied volatility are intentionally NOT stored** — not in the
bhavcopy (or any free source), and computing them was scoped out. The
tables can gain those columns + a `greeks.py` later without reshaping.

**Schema (migration `0007_add_fo_tables.sql`) — four tables + two views.**
Futures and options are separate instruments, and each splits into a
*contract dimension* (the open-contracts registry, with expiry) and a flat
*daily-price fact* table (OHLC history, natural-key like `price_history`):
- `futures_contracts` / `futures_daily_prices`
- `option_contracts` / `option_daily_prices` (options carry `strike_price`
  + `option_type` CE/PE that futures don't)
- `latest_futures_view` / `latest_option_chain_view` — `DISTINCT ON` the
  newest daily row per open contract, so a page loads the current term
  structure / option chain in one query (mirrors `latest_screener_view`).
All four use the shared-market-data RLS pattern from `0002` (authenticated
read; writes only via the service-role key, which bypasses RLS). `is_open`
can't be derived from any single file (a contract appears in the bhavcopy
only while live, so expiry ≥ that file's date always holds); it's finalized
against the real calendar once per run by `fo_repo.refresh_open_flags`.

**Code layout:**
- `src/models/fo.py` — the four Pydantic models; `OptionType` (CE/PE) in
  `enums.py`.
- `src/data_providers/nse_fo_provider.py` — `fetch_fo_bhavcopy(trade_date,
  universe)` (download + parse); `parse_fo_bhavcopy(csv_text, ...)` is
  split out and pure so it's unit-tested against an inline fixture
  (`tests/test_nse_fo_provider.py`) with no network.
- `src/data_providers/mock_provider.py::MockFOProvider` — synthetic
  futures (3 monthly expiries) + option chains (strikes stepped around a
  spot), shaped as the same `FOBhavcopy` object, so the ingest path,
  Options screen and tests run offline.
- `src/repositories/fo_repo.py` — natural-key upserts (chunked, since one
  day is ~9k option rows), `refresh_open_flags`, and reads off the views.
- `src/services/fo_service.py` — `ingest_fo_day(client, book)` persists a
  parsed day; `option_chain_summary` / `futures_term_structure` are pure
  presentation helpers (tested in
  `tests/test_fo_service.py`). `csp_5pct_map(put_rows, spot_by_symbol)` /
  `csp_5pct_for_rows(pe_rows, spot, expiry_date)` compute "5% CSP", and
  `cc_5pct_map(call_rows, spot_by_symbol)` / `cc_5pct_for_rows(ce_rows,
  spot, expiry_date)` compute "5% CC" — the single implementation shared
  by the Options screen's "5% CSP"/"5% CC" breakdown sections (see below)
  and, via `dashboard_metrics_rows`/`recompute_dashboard_metrics` below,
  the Dashboard's two F&O-derived columns. Each restricts to a symbol's
  own nearest available expiry and returns not just the final percentage
  but every intermediate value used to get there (strike, premium, spot,
  expiry, `trade_date`) — needed by the Options screen's breakdown;
  `csp_5pct_for_rows`/`cc_5pct_for_rows` are the single-expiry cores
  `csp_5pct_map`/`cc_5pct_map` delegate to, used directly by the Options
  screen (CSP) or the Dashboard's cache (both) to compute a near/next/far
  month row each (the same term-structure shape the Futures section
  already uses), one call per expiry. Both prefer strikes with the
  freshest available `trade_date` over a purely-nearest-strike match
  (`_freshest_rows`) — `strike ≈ target` can still be a contract that
  hasn't traded in weeks if it's illiquid, while a neighboring strike
  keeps updating daily.

  **"5% CC" (covered call)** is deliberately simple: sell 1 lot of the
  OTM call whose strike is closest to **5% above** spot (the mirror
  image of "5% CSP"'s `target = spot * 0.95` search) — no ITM leg, no PE
  leg, just the one call. Two percentages come out of `cc_5pct_for_rows`:
  `cc_pct` = premium ÷ spot × 100 (the covered-call yield on the stock's
  own price — this is the value both the Dashboard and the Options
  screen label "5% CC"), and `assignment_profit_pct` = premium ÷ (strike
  − spot) × 100 (premium as a fraction of the capital-gain room left
  before assignment caps further upside — shown on the Options screen
  only, as "Assignment Profit"; `None` when strike equals spot rather
  than dividing by zero). This replaced an earlier "5% ITM PMCC"
  (poor-man's-covered-call: buy an ITM call, sell a PE at the same
  strike, sell a further OTM call, net-credit ÷ ITM strike) on request —
  if you find references to `itm_pmcc_5pct_map`/`itm_pmcc_for_rows`
  anywhere (old commits, cached docs), that calculation no longer exists.
- `dashboard_metrics_rows(option_rows, spot_by_symbol)` / `recompute_dashboard_metrics(client)`
  — the Dashboard's precomputed-cache write path (`dashboard_fo_metrics`,
  migration `0011`). For each symbol with a spot price and open option
  legs, `dashboard_metrics_rows` calls `csp_5pct_for_rows` +
  `cc_5pct_for_rows` once per each of that symbol's **up to 3 nearest
  distinct expiries** (adding no new strike-selection math of its own),
  emitting one flat row per **(symbol, expiry)** — `cc_5pct_for_rows`'s
  `assignment_profit_pct` is deliberately not cached here, since the
  Dashboard only ever displays `cc_pct`; `recompute_dashboard_metrics`
  reads spot from `latest_screener_view` + open option legs from
  `latest_option_chain_view`, calls it, and upserts the whole table. See
  "Dashboard cache" below for why this exists and every path that calls it.
- `scripts/fetch_fo_data.py` — service-role backfill (`--days 60` default,
  `--date`, `--mock`), run by the operator (like the other seed scripts);
  processes oldest→newest then calls `refresh_open_flags(today)`.
  `scripts/seed_mock_data.py` also seeds ~30 mock F&O days for local dev.

**On-demand refresh**: the Dashboard's "📊 F&O Data Refresh" button hits a
second Edge Function, `supabase/functions/fo-refresh/` (see the Edge
Functions section below) — a TypeScript port of the same bhavcopy
fetch+parse, but only for the single most recent day and only if NSE has
actually published something newer than what's already loaded (checked via
`max(trade_date)` in `futures_daily_prices`), so a click when nothing's
new is a cheap read-only no-op rather than a silent re-fetch. It has no
external zip-library dependency — see the Edge Functions section for why.

**Dashboard cache (`dashboard_fo_metrics`, migration `0011`)**: the
Dashboard used to compute its "5% CSP"/"5% CC" columns live, on every
page load — pulling every open option leg for all 50 symbols (thousands
of rows, paginated) and running the nearest-strike search in Python in
the request path. That's now precomputed instead, the same way every
other Dashboard column already was (`daily_screener_snapshots`):
`dashboard_fo_metrics` holds up to 3 small rows per symbol -- one per
near/next/far monthly expiry -- keyed by **(symbol, expiry_date)**
(`fo_service.dashboard_metrics_rows`/`recompute_dashboard_metrics`), and
the Dashboard just reads them (`fo_repo.get_dashboard_fo_metrics`). This
is what backs the Dashboard's **"Options month" dropdown** (next to "Sort
By"): picking a different month is a pure re-render over already-cached
rows filtered to that `expiry_date` -- no new fetch, no recomputation.
Every refresh path that can change spot price or F&O data recomputes the
whole cache (all 3 months, every symbol) as its last step, so it's
correct immediately after any refresh finishes rather than on some
separate schedule:
- `scripts/run_refresh.py`'s `screener`/`all` modes (cron) — spot changed.
- `scripts/fetch_fo_data.py` — option data changed.
- `manual-refresh`/`fo-refresh` Edge Functions — the Dashboard's two
  on-demand refresh buttons. These can't call the Python implementation
  (no service-role key in Streamlit), so the calculation is ported to
  TypeScript too: `supabase/functions/_shared/dashboardMetrics.ts`
  (`cspFivePct`/`ccFivePct`/`recomputeDashboardMetrics`, tested in
  `dashboardMetrics.test.ts`) — the same duplicated-business-logic
  tradeoff `calculations.ts`/`bhavcopy.ts` already accept, for the same
  reason (a truly instant on-demand path). If you change the CSP/CC
  calculation in `fo_service.py`, mirror it here too.

`dashboard_fo_metrics` was originally one row per symbol (migration
`0009`, holding only the nearest expiry); migration `0010` dropped and
recreated it keyed by `(symbol, expiry_date)` instead once a per-month
dropdown was requested, since the whole point of the cache is that
picking a different month can't trigger live recomputation -- it has to
already be sitting in the table. Migration `0011` then dropped and
recreated it again, replacing `0010`'s `pmcc_*` columns (a three-leg
ITM/PE/OTM breakdown) with just `cc_strike`/`cc_premium`/`cc_pct`/
`cc_trade_date`, when "5% ITM PMCC" was replaced with the simpler "5% CC"
covered-call calculation on request. Each of these truncates the cache
(harmless, rebuilt on the next refresh) -- see each migration's own
comment for why a drop+recreate was safe here (a pure derived cache, no
history worth an `ALTER`).

All four write paths tolerate migration `0011` not being applied yet
(catch-and-log, not a hard failure) — same degrade-gracefully precedent as
the Dashboard's own `except APIError` → "N/A" handling.

## Streamlit app (`app.py`, `pages/`)

`app.py` is the landing page (Streamlit's "Home" in the sidebar nav,
titled "app"). Every page in `pages/` starts with
`require_login()` (from `src.utils.session`), which either lets the page
proceed (a valid session exists) or renders the Sign in / Create account /
Forgot password tabs and `st.stop()`s.

- **`1_Dashboard.py`** — loads `latest_screener_view` via `snapshot_repo.get_latest_screener()`, applies the signed-in user's thresholds via `threshold_override.apply_user_thresholds()`, renders metric cards (also usable as quick filters, wired through `st.session_state["status_filter"]`), sidebar filters, a "Sort By" control, and the screener table. The Status sidebar filter is a `st.multiselect` over `ALL_STATUSES = ["Green", "Amber", "Red", "Unavailable"]` — `status_filter` is always a *list* (any combination, not one-or-all), and the final row filter is a single `df["status"].isin([...])`, so selecting all four is equivalent to no filter at all. Saved filter presets normalize old single-string `"status"` values (from before this was a multiselect) into a list on load for backward compatibility. The "Minimum dividend yield" / "Minimum PEG" sidebar filters default to `0.0`, **not** `user_settings.dividend_yield_threshold`/`peg_threshold` — they're a separate display filter from the criterion A/C pass/fail thresholds, and defaulting them to the threshold value silently hid every stock below it on first load (a real bug, since fixed). Keep these two concepts distinct if you touch this page: the Settings-page thresholds decide Green/Amber/Red/Unavailable; these sidebar inputs just additionally hide rows below a value the user dials in themselves, and should default to "show everything." The header has two on-demand refresh buttons, each hitting its own Edge Function (see [Edge Functions](#edge-functions-supabasefunctions) below): "🔄 Stock Data Refresh" (cash market, `manual-refresh`) and "📊 F&O Data Refresh" (futures/options, `fo-refresh` — a no-op with an "already up to date" message if NSE hasn't published anything newer than what's loaded).

  **Metric cards**: seven buttons in a row (`st.columns(7)`) — `Total stocks`, `🟢 Green`/`🟠 Amber`/`🔴 Red` (each sets the status filter to just that one status), and `Yield > threshold`/`All momentum +ve`/`PEG ≤ threshold` (each sets `criterion_filter` instead). There is deliberately no `Unavailable` button — the status itself is still fully selectable via the sidebar's Status multiselect and still counts toward `ALL_STATUSES`, but it wasn't considered a useful one-click quick filter and was dropped to declutter the row (a purely cosmetic trim, not a behavior change to filtering).

  **Screener table columns**, left to right: `#` (serial number, just `enumerate()` over the current filtered/sorted rows — always 1..N of what's on screen, not a stored ID, so it renumbers on every filter/sort change), `Stock` (the NSE ticker symbol, e.g. `ADANIENT` — not the full company name; a redundant hidden `Symbol` key is still carried in each `display_rows` dict for the selectboxes below the table and the per-row link column, but since `Stock` moved to the symbol too, `Symbol` is now just an alias of the same value), `LTP` (latest price — renamed from "Latest price" for column-width economy), `52W High`/`52W Low` (value + `pass_fail_icon` for `criterion_52w_high`/`criterion_52w_low` — display-only proximity checks, **not** part of Green/Amber/Red; see `classification.py` above), `1D`/`5D`/`20D` (arrow + percentage only), `Momentum` (a single `pass_fail_icon(criterion_b)` — despite the name it's specifically criterion B, not a combined A/B/C view), two F&O-derived columns (see below), and `Dividend`/`PE`/`PEG` last (`Dividend` — renamed from "Dividend yield" — and `PEG` both carry a `pass_fail_icon` for criteria A and C respectively; `PE` is plain, not a criterion). There used to be a third F&O-derived column, the near-month future's price (header e.g. `Jul Future`, backed by `fo_service.near_month_futures_map`/`near_month_column_label`) — it was dropped on request, and those two now-unused `fo_service` functions (and their tests) were deleted along with it rather than left as dead code; futures data is no longer fetched on this page at all (only options, for the two columns below).

  **The two F&O-derived columns** (between `Momentum` and `Dividend`) come from a *separate* data source than the rest of the table — `dashboard_fo_metrics` (migration `0011`), the Dashboard's precomputed F&O cache, not `latest_screener_view` — joined in by symbol after filtering, rather than being part of `ScreenerRow`. `_load_dashboard_fo_metrics` returns the *raw* row list (up to 3 per symbol, one per near/next/far expiry); an **"Options month" selectbox**, rendered next to "Sort By"/"Descending" (`sort_by_col, sort_desc_col, month_col = st.columns([2, 1, 2])`), lists the distinct `expiry_date`s actually present (formatted `"%b %Y"`, e.g. "Jul 2026") and defaults to the nearest one via `st.session_state["dashboard_options_month"]` (same pattern as `dashboard_sort_label`). Picking a month is a pure re-render over already-cached rows -- `filtered["csp_5pct"]`/`filtered["cc_5pct"]` are built by filtering `dashboard_fo_metrics_rows` to that one `expiry_date` and mapping by symbol, no new fetch or recomputation triggered. (Note this assignment happens on `filtered`, not the earlier `df`, specifically so it can live down near the Sort By widgets after `filtered = df.copy()` has already run -- nothing between that copy and here reads either column.) This page only ever reads the final `csp_pct`/`cc_pct` key out of each row; see the Futures & Options section's "Dashboard cache" paragraph above for the full pipeline (`fo_service.dashboard_metrics_rows`/`recompute_dashboard_metrics`, and every refresh path that keeps it current) and `csp_5pct_for_rows`/`cc_5pct_for_rows`'s docs for the underlying formulas, which are also what the Options screen's "5% CSP"/"5% CC" breakdown sections use directly (unchanged, live, for a single symbol):
  - **`5% CSP`** — a cash-secured-put yield: for the strike nearest 5% below spot (the screener's `latest_price`, not the option chain's `underlying_price`, kept consistent both here and on the Options screen — see the bug note on `5_Options.py` below), the selected expiry's put premium as a percentage of that strike (`put_price / strike * 100`). **Deliberately not divided by exchange margin** — SPAN margin isn't available from NSE as a simple downloadable per-contract figure (it's a licensed CME Group multi-scenario risk calculation, confirmed via a live search of NSE's actual report index turning up nothing), so this uses the strike itself (the full notional a cash-secured-put seller sets aside) as the yield's denominator instead; `strike * lot_size` cancels out of both the premium and this ratio, so lot size never needs to appear in the formula at all.
  - **`5% CC`** — a covered-call yield: sell 1 lot of the call whose strike is closest to 5% *above* spot, expressed as a percentage of **spot** (not the strike) -- `premium / spot * 100`. This is deliberately the simplest possible covered-call read: no ITM leg, no PE leg, just the one OTM call's premium as a yield on the stock's own price. (The Options screen additionally shows "Assignment Profit" -- `premium / (strike - spot) * 100` -- but the Dashboard column only ever surfaces `cc_pct`.) This replaced an earlier three-leg "5% ITM PMCC" (poor-man's-covered-call) calculation on request; see `fo_service.py`'s bullet above for what that used to compute.

  **A real bug this surfaced**: `fo_repo.get_all_open_options()` initially had no pagination, and PostgREST caps a single response at a server-configured max (1000 rows on this project) regardless of how many rows actually match — against live data (~5,053 open PE legs across 50 symbols) this silently truncated to exactly 1000 rows, and whichever symbols fell outside that window (most of the universe, including RELIANCE/TCS/HDFCBANK) were missing from the 5% CSP column with **no error anywhere** — confirmed live (`PE rows: 1000` before, `5053` after). Fixed by a generic `fo_repo._paginate(query_builder, page_size=1000)` helper that `get_all_open_futures()`/`get_all_open_options()` go through, looping `.range()` calls until a page comes back short (this is still exercised by `recompute_dashboard_metrics`, which calls `get_all_open_options()` at refresh time, and by its TypeScript port's own paginated fetch). `tests/test_fo_repo.py` covers the pagination boundary cases (multi-page accumulation, an exact-multiple-of-page-size input not looping forever, empty results).

  The cache read (`fo_repo.get_dashboard_fo_metrics`) is cached the same way `_load_screener_rows`/`_load_last_fetch` are (`@st.cache_data(ttl=60)`, keyed on `dashboard_cache_bust`) — a handful of rows, not the thousands-of-rows option query this used to be (see "Dashboard cache" above for why that moved off this page's request path entirely). Both refresh buttons bump `dashboard_cache_bust` and `st.cache_data.clear()`, so a click always shows this session's own just-recomputed cache rows rather than stale 60s-old ones. Like `pages/5_Options.py`, a missing F&O schema (migration `0007`/`0011` not yet applied) degrades to "N/A" in both columns via the same `except APIError` pattern (and an empty/disabled month dropdown), rather than crashing the whole Dashboard.

  There is deliberately no dedicated `Status` column — it duplicated the per-criterion tick/cross columns already on screen without adding information. The underlying `status` field is still sortable/filterable (sidebar multiselect), just not rendered as its own column. `status_badge()` (colored text badge, e.g. "🟢 Green") is still used standalone on Stock Detail's header, where the status needs to stand alone rather than sit in a row with other context.

  The table itself is rendered by `render_screener_table()` (`src/utils/ui.py`), not `pandas.DataFrame.to_html()` — see the Tailwind CSS note under Utils below for why, and for how the mobile layout works. Its mobile card header only renders a status icon span if the row dict actually has a `"Status"` key (`"Status" in row`) — so it stays generically reusable for any future caller that does want a per-row status icon, without assuming one is always present.

  **Sorting and per-row navigation went through several failed designs before landing on native Streamlit widgets — read this before adding another interactive element to the table.** The original spec called for clickable column headers (click to sort ascending, click again to toggle descending), implemented as `<a href="?sort=...">` links inside `render_screener_table()`'s hand-rendered HTML. This went through three rounds of real bugs, each one only visible by actually clicking the deployed app: (1) an untargeted link inside `st.markdown(unsafe_allow_html=True)` gets `target="_blank"` forced onto it by Streamlit's own markdown renderer, popping a new browser tab; (2) adding `target="_self"`/`target="_top"` stopped the popup but broke sorting outright, because Streamlit Community Cloud wraps the deployed app in its own outer routing context that those target values navigate past; (3) even once the right target was found, clicking the header **logged the user out** — the actual, unfixable root cause: `src/utils/session.py`'s own docstring already states the Supabase auth session lives only in `st.session_state`, never a cookie/localStorage, and **any real browser navigation, regardless of `target=`, starts a brand-new WebSocket session with empty `st.session_state`** — Streamlit's own multipage sidebar nav and `st.page_link`/`st.switch_page` avoid this because those are React components with a JS click handler wired into Streamlit's own client-side router (no real page load happens), but a raw `<a href>` inside injected HTML has no such handler and is just a plain link to the browser. This is the same underlying fact the [Auth](#auth-a-non-obvious-quirk) section documents for password reset — read that section's mechanism explanation if you're tempted to add another `<a href>` inside any hand-rendered HTML block in this app. The fix was to drop links entirely: sorting is now a plain `st.selectbox("Sort By", ...)` + `st.checkbox("Descending", ...)` pair rendered above the table (`SORT_OPTIONS`, a fixed 6-entry list — `Stock`/`Momentum`/`5% CSP`/`Dividend`/`PE`/`PEG` — deliberately not every column, just the ones worth sorting by), and `render_screener_table()`'s `sortable_columns`/`active_sort_key`/`sort_desc` parameters now do nothing but decide which header gets a ▲/▼ arrow for visual feedback — headers are plain text, not links.

  The same constraint applies to the per-row "open in Stock Detail" 🔍 button rendered beside the table: it's a real `st.button()` (native widget, stays on the same session) in a narrow `st.columns([30, 1])` sliver to the table's right, one per row, rather than a link inside the table's own HTML. Because `st.button()`'s default height plus Streamlit's default gap between stacked elements is taller than a table row, the buttons visibly drifted further below their matching row the further down the list they were (measured live: ~800px of drift by row 50) until scoped CSS (targeting `.st-key-dashboard_stock_links`, the class Streamlit's `st.container(key=...)` adds) zeroed the inter-element gap and shrank the buttons to the table's own row height — if you add more rows of native widgets alongside this table in the future, expect to need the same treatment.
- **`2_Stock_Detail.py`** — the most feature-dense page: Plotly candlestick (falls back to a line chart if OHLC is incomplete) with volume subplot, moving averages, entry/target/stop-loss lines, dividend timeline, classification-history chart, position notes form, and inline alert creation. The Fundamentals column is rendered via `render_stat_grid()` instead of stacked `st.markdown` lines; the alert list uses `render_alert_row()` (see below) instead of printing the alert's raw Python `config` dict; the "Create a new alert" expander's inputs are now wrapped in an `st.form` (previously plain buttons), bringing it to parity with `3_Alerts.py`'s create-alert form, which already used this pattern. A "📊 View F&O / options" button hands the current symbol to `5_Options.py` via `st.session_state["fo_symbol"]` + `st.switch_page`.
- **`3_Alerts.py`** — alert CRUD (including portfolio-wide alerts, `symbol IS NULL`) and notification history. Alert rows use `render_alert_row()` (shared with Stock Detail — one formatting implementation, two call sites) instead of a raw dict dump. Notification history stays `st.dataframe`-only on every viewport, deliberately not given a Tailwind mobile-card alternative — see the design-system note under Utils for why.
- **`4_Settings.py`** — per-user thresholds, theme, change-password. The three permanently-disabled Email/Telegram/Slack notification checkboxes were collapsed into a single row of `render_pill()` "coming soon" badges next to the one real (In-app) checkbox, removing dead-weight disabled UI for unimplemented channels.
- **`5_Options.py`** — the F&O / Options screen for one stock (see the Futures & Options section above for the data pipeline). Symbol selector defaults to `st.session_state["fo_symbol"]` (set by the Dashboard's "Open in Options →" block or Stock Detail's button), falling back to `selected_symbol`. Renders: an expiry selector (drives the summary tiles and, indirectly, which expiry's rows are reused for the near-month CSP row below); summary tiles (spot / ATM strike / total CE OI / total PE OI / Put-Call ratio) via `render_stat_grid`, sourced from `fo_service.option_chain_summary(chain_rows)` for the selected expiry; a futures term-structure table (near/next/far, with basis vs spot) + a near-month daily-close Plotly chart; and two sections below that, **"5% CSP"** and **"5% CC"**, showing the actual calculation for the selected symbol rather than just the final Dashboard-column percentage. (There used to be a classic CE | Strike | PE option chain table between the futures chart and these two sections -- it was dropped on request; `chain_rows`/`option_chain_summary` are still fetched/used for the summary tiles and CSP's near-expiry row, but the pivoted per-strike display itself, and the now-unused `fo_service.shape_option_chain` pivot helper + its tests, were removed rather than left as dead code.):
  - **5% CSP** is a **near/next/far month table** (`fo_service.csp_5pct_for_rows`, one call per expiry — the same term-structure shape the Futures section above already uses), columns Term / Expiry / Spot / Strike / Put Premium / **Trade Date** / 5% CSP. The near row reuses the already-fetched `chain_rows` when the expiry selector above happens to be on the near expiry; next/far are fetched separately via `fo_repo.get_option_chain`. The Trade Date column is what actually surfaces a stale quote to the user — see `_freshest_rows`'s docstring above for why a strike's "latest" row can silently be weeks old.
  - **5% CC** is a single near-expiry breakdown (`fo_service.cc_5pct_map`): a stat grid of the inputs (strike, premium, spot), then two result stats -- **5% CC** (`premium / spot * 100`) and **Assignment Profit** (`premium / (strike - spot) * 100`, `None`/"N/A" if strike equals spot) -- each with its formula spelled out, plus the leg's trade date and the nearest expiry used. This replaced an earlier three-leg "5% ITM PMCC" breakdown (a stat grid + a Leg/Strike/Price/Trade Date/Cash flow table for three legs) on request; "Assignment Profit" only appears here, not on the Dashboard, which only ever caches/displays `cc_pct`.
  Both are restricted to the symbol's own nearest available expiry (CC unconditionally; CSP's near row specifically) **regardless of which expiry is selected above**, so the near-month numbers always match the Dashboard's cached values for the same stock. Shaping is done by `fo_service.option_chain_summary`/`futures_term_structure`/`csp_5pct_for_rows`/`cc_5pct_map`, not in the page.

  **A real bug found here, right after this section first shipped**: the CSP/CC breakdown's spot value (CC was still "ITM PMCC" at the time, but the bug and fix applied identically) was initially taken from `option_chain_summary(near_chain_rows)["spot"]` — the F&O bhavcopy's own `underlying_price` column — while the Dashboard's two columns (now the `dashboard_fo_metrics` cache, see above) use the cash-market `latest_price` from `latest_screener_view`. These two prices aren't the same value, so this page's numbers didn't match the Dashboard's for the same stock (confirmed live: ADANIENT showed 5% CSP = 0.54% on the Dashboard but 0.45% here, since a different spot picked a different nearest-5%-below strike, 3040 vs 3020). Fixed by fetching `snapshot_repo.get_latest_screener_row(client, symbol).latest_price` and using that as the spot for both calculations here too, instead of the chain's `underlying_price` — the top-of-page "Spot"/"ATM strike" summary tiles are unaffected and deliberately still use the chain's own `underlying_price` (correct for highlighting the ATM row in the actual option-chain data being displayed there). If you add another F&O-derived calculation to either screen, source spot the same way this one now does — from the screener, not the chain — to keep the two screens' numbers in agreement.

## Auth: a non-obvious quirk

**Password reset does not use Supabase's email link.** This was tried
first and doesn't work, for a reason worth understanding before touching
auth code again: Supabase's recovery link puts the session token in the
URL **fragment** (`#access_token=...&type=recovery`), which browsers never
send to any server. The obvious workaround — inject JS via `st.iframe`
that reads `window.parent.location.hash` and rewrites the parent URL — is
blocked by the browser itself: Streamlit's iframe sandbox doesn't include
`allow-top-navigation`, so any attempt to navigate the parent frame from
inside it throws
`SecurityError: ... does not have permission to navigate the target frame`,
confirmed directly in a live test. (Reading the parent's location *is*
allowed via `allow-same-origin`; navigating it is a separate, unrelated
sandbox permission, and Streamlit grants the former but not the latter.)

The actual fix, in `src/utils/session.py`: Supabase's password-recovery
email also carries a 6-digit one-time code via the `{{ .Token }}` template
variable (this requires editing the Reset Password template in the
Supabase dashboard to include it, and requires custom SMTP to be
configured — Supabase's built-in email service ignores template edits
entirely). The user types that code into the app's "Forgot password?" tab,
which is verified server-side via `auth.verify_otp({"email", "token",
"type": "recovery"})` — no redirect, no JS, no sandbox issue. If you're
tempted to "fix" the link-based flow later, read this section again first.

`Settings.app_base_url` (`.env: APP_BASE_URL`) is still used for
`email_redirect_to` on sign-up confirmation — that flow doesn't need the
token at all (the user just confirms and then signs in normally), so a
plain correct redirect URL is sufficient there.

`require_login()` now calls `inject_design_system(Theme.LIGHT)` as its
very first line, before even checking `is_password_recovery_pending()`.
Every page previously called `inject_tailwind()` itself, but only *after*
`require_login()` returned — meaning the unauthenticated login/signup/
forgot-password screen (and the mandatory post-recovery set-new-password
screen) rendered before any CSS/Tailwind was ever loaded. This is the
single enforcement point now, rather than relying on every page to order
its own calls correctly. It unconditionally uses the light theme here
since there's no signed-in user yet to read a `Theme` preference from;
every page re-injects with the user's actual `Theme` setting once loaded
(a later `<style>` tag wins the cascade over this one).

## Utils (`src/utils/`)

- **`session.py`** — all Supabase Auth + `st.session_state` handling: `sign_in`/`sign_up`/`sign_out`, `request_password_reset`/`verify_recovery_code`/`set_new_password`, `require_login()` (the gate every page calls), `get_user_client_cached()`.
- **`formatting.py`** — Indian-numbering-system currency formatting (`format_inr`, lakh/crore grouping), `format_pct`, `direction_arrow`, `pass_fail_badge` (✅ Pass/❌ Fail/N/A, with text), `pass_fail_icon` (✅/❌/—, symbol only — used throughout the Dashboard table's Momentum/Dividend yield/PEG columns; `pass_fail_badge` is kept for spots that still want the text, e.g. Stock Detail's scorecard). `alert_type_label()`/`summarize_alert_config()` — pure functions turning an `AlertType` + its raw `config` dict into human-readable text (e.g. "Price crosses above ₹1,000.00"), replacing what used to be a literal `f"config={a.config}"` Python-dict dump shown on both Stock Detail and Alerts; the exact `config` keys each branch reads (`level`/`direction`, `period`/`direction`, `threshold`/`direction`, `entry_price`, `target_price`/`stop_loss`) must stay in sync with whatever keys the alert-creation forms in `2_Stock_Detail.py`/`3_Alerts.py` actually write. **A real bug found here**: `format_inr`/`format_crores`/`format_pct`/`direction_arrow` all checked `value is None`, but `pages/1_Dashboard.py`'s `pd.DataFrame([r.model_dump() for r in rows])` silently converts a Pydantic model's correct `None` into `float('nan')` for any column that has real float values elsewhere in the same column (confirmed directly: a mixed-value column comes back `float64` dtype with `None` cells as `nan`, `nan is None` is `False`) — a genuinely-missing `return_1d` rendered as the literal string `"nan%"` on screen instead of `"—"`. All four formatters now route through a shared `_is_missing(value)` helper that also checks `math.isnan()`.
- **`timezones.py`** — `now_ist()`/`to_ist()`/`format_ist()`, thin wrappers around `pytz`.
- **`ui.py`** — shared fragments: `status_badge()` (colored HTML span with text, e.g. Stock Detail's header), `market_state_label()`, `buy_sell_label()` (Green→"Model Buy Watch" etc., per the spec's no-guarantee wording), `render_disclaimer()`, `plotly_template()`, `inject_tailwind()` / `render_screener_table()`, plus the design-system layer described below: `ACCENT` (indigo palette constants), `inject_global_styles()`/`inject_design_system()`, `_surface_classes()`, `render_card()`, `render_pill()`, `render_stat_tile()`/`render_stat_grid()`, `render_alert_row()`.
- **`logging.py`** — `get_logger(name)`, configures `logging.basicConfig` once from `Settings.log_level`.

**Tailwind CSS — how it's actually wired in, and why not the obvious way.** Streamlit renders its own native widgets (buttons, inputs, `st.dataframe`, columns, sidebar) through its own internal React components with no supported hook for external CSS frameworks to target them — Tailwind only styles HTML we hand-render ourselves via `st.markdown(html, unsafe_allow_html=True)` (the screener table, the design-system components below). Within that scope, there's a second, less obvious trap: Tailwind's current CDN distribution (the "Play CDN") is a `<script>` that scans the DOM at runtime and injects styles as it goes — but `st.markdown(unsafe_allow_html=True)` inserts HTML via `innerHTML`, and browsers never execute `<script>` tags inserted that way (a standard, deliberate DOM security behavior, not a Streamlit quirk). Loading the Play CDN script this way silently does nothing; there's no error, the styles just never apply. `inject_tailwind()` in `ui.py` instead loads the older, fully-precompiled Tailwind **v2** static stylesheet via a `<link rel="stylesheet">` tag, which — unlike `<script>` — *is* honored via `innerHTML`. Call it once near the top of any page before rendering Tailwind-classed HTML (every page already does).

`render_screener_table()` (`ui.py`) is the concrete payoff: it renders the Dashboard's screener data twice into one HTML blob — a normal `<table>` wrapped `hidden md:block` (visible only ≥768px) and a stacked list of cards wrapped `md:hidden` (visible only below that) — a pure-CSS responsive switch, no JS. This fixes a real pre-existing mobile problem: the previous `df.to_html()` table had no responsive handling at all and would overflow or squeeze unreadably on a phone. Because the static v2 build has no `dark:` variant available, light/dark table colors are chosen explicitly in Python (`_table_theme_classes()`) from the same `user_settings.theme` that already drives `plotly_template()`, rather than relying on a Tailwind dark-mode class that isn't in this CDN build.

**The design system — combining Tailwind with a global CSS override for native widgets.** Until this pass, Tailwind reached exactly one surface in the whole app: the Dashboard's screener table. Every other screen (landing page, login/signup/forgot-password, Stock Detail, Alerts, Settings) was 100% unstyled native Streamlit, since `inject_tailwind()` was called on every page but nothing on those pages actually used a Tailwind class. Tailwind *can't* reach native widgets at all (buttons, inputs, forms, sidebar, tabs, `st.metric`, `st.dataframe`, `st.expander` are React components Streamlit renders itself, with no exposed hook for an external CSS framework) — a Tailwind `<div>` can never wrap a native `st.button`/`st.form`, since hand-rendered HTML and native widgets are DOM siblings, not parent/child (each Streamlit element call appends its own separate node; one `st.markdown()` call's HTML can't "contain" a later `st.button()` call's output).

The fix is a second, complementary mechanism: `inject_global_styles(theme)` injects a global `<style>` block (plain CSS, not Tailwind classes) that reskins native widgets — border-radius, colors, focus states — using the same indigo `ACCENT` palette Tailwind-classed HTML uses (`ACCENT[600]` == Tailwind's own `indigo-600` hex value, so `bg-indigo-600` and `var(--accent-600)` are visually identical from one source of truth). `inject_design_system(theme)` calls both `inject_tailwind()` and `inject_global_styles(theme)` together and is what every page actually calls now (via `require_login()`, plus each page re-injecting with its own loaded `user_settings.theme` right after — see the Auth section above for why `require_login()` is the enforcement point).

Every CSS selector in `_GLOBAL_CSS_LIGHT`/`_GLOBAL_CSS_DARK` is `data-testid`/ARIA-role/`kind`-attribute based (`[data-testid="stForm"]`, `button[kind="primary"]`, `[data-testid="stTab"][aria-selected="true"]`, etc.), confirmed via live DOM inspection against the actually-installed Streamlit version (1.59.1) at implementation time — **never** target Streamlit's own `st-emotion-cache-*` class names, which are content-hashed and change across builds/versions; testids and ARIA attributes are the only part of Streamlit's generated markup that's stable to target. If you bump Streamlit's version and native widgets stop looking styled, re-verify these selectors the same way (a scratch script + browser devtools `[data-testid]` inspection) rather than guessing.

The dark branch additionally overrides `[data-testid="stAppViewContainer"]`/`stMain`/`stHeader`/`stSidebar` backgrounds, since `.streamlit/config.toml`'s `[theme]` section (added alongside this, for Streamlit's own officially-supported BaseWeb theming — focus rings, checkbox tick color, `kind="primary"` buttons) can only express one static base theme (`light`); without the dark CSS branch also recoloring those top-level containers, "dark" would leave dark-styled widgets floating on Streamlit's own light page background. `[client] toolbarMode = "minimal"` in that same file hides Streamlit's own built-in theme picker, so there's exactly one theme control in the app (Settings → Chart theme), not two competing ones.

New reusable Tailwind-HTML components in `ui.py`, all following the same `theme`-branching pattern `_table_theme_classes()` already established (`_surface_classes(theme)` is the generic-component equivalent of that function): `render_card(inner_html, theme)` — bordered/padded/shadowed wrapper for **static content only**, per the DOM-siblings constraint above; `render_pill(text, tone, theme)` — small badge, used for alert-type labels and Settings' "coming soon" tags; `render_stat_tile()`/`render_stat_grid()` — responsive (`grid-cols-1 md:grid-cols-N`) stat cards, replacing Stock Detail's previously-stacked-markdown Fundamentals column; `render_alert_row()` — formatted alert summary (pill + `summarize_alert_config()` text), replacing the raw dict dump on both Stock Detail and Alerts.

**The join-bug rule applies to every one of these** (see `render_screener_table()`'s existing comment for the full mechanism: joining multi-line indented f-string fragments leaves a whitespace-only line between them, which Streamlit's markdown parser treats as ending the current HTML block) — every new `render_*` function returns a single continuous-line string, never a multi-line indented literal, and this must hold for any future addition too. The one deliberate exception is the CSS `<style>` block itself: `<style>`/`<script>`/`<pre>` are CommonMark "HTML block type 1," terminated only by their closing tag, not by blank lines — so `_GLOBAL_CSS_LIGHT`/`_GLOBAL_CSS_DARK` are safe to write as ordinary multi-line triple-quoted strings, same as `inject_tailwind()`'s single `<link>` call always was.

**Notification history (`3_Alerts.py`) deliberately stays `st.dataframe`-only**, with no Tailwind mobile-card alternative, unlike the screener table. `render_screener_table()`'s dual-block technique works because both the table and the card list are Tailwind `<div>`s the code fully controls and can tag with `hidden md:block`/`md:hidden`; `st.dataframe` is one opaque native React subtree with no reliable way to attach a scoped class to just that one call without brittle DOM-adjacency assumptions that could break on a future Streamlit version. `st.dataframe` already has native horizontal scroll — an acceptable, if not ideal, mobile experience for this secondary/lower-traffic view.

**A real bug this shape of code caused, on real iPhones (desktop was fine):** the mobile cards were originally built one-per-row via a multi-line triple-quoted f-string (`f"""\n        <div ...>\n          ...\n        </div>\n        """`), joined with `"".join(cards)`. Streamlit's `st.markdown(unsafe_allow_html=True)` runs its content through a CommonMark-based Markdown parser (via `react-markdown`/`remark`) *before* trusting the raw HTML — it doesn't just dump the string into `innerHTML` verbatim. Joining those indented multi-line card strings back-to-back left a line containing *only whitespace* between each pair of cards (the trailing 8 spaces of one card's closing line, immediately followed by the leading 8 spaces of the next card's opening line) — and a whitespace-only line counts as a **blank line** in CommonMark, which is exactly what ends an HTML block. Every card after the first one then got re-parsed starting from a line indented ≥4 spaces with no open HTML block to continue — CommonMark's rule for that is "indented code block," so the raw `<div class="...">` markup rendered as literal escaped text instead of a card. This only reproduced below the `md:` breakpoint (phones), never on desktop, because the desktop `<table>`'s `<tr>` rows are built as genuinely single-line strings with `''.join(body_rows)` — no embedded newlines anywhere, so no whitespace-only "blank line" can ever appear between them. **The fix, and the rule going forward:** any HTML fragments that get concatenated together before being handed to `st.markdown(unsafe_allow_html=True)` must be built as single continuous lines (like `body_rows`/`cells` already were) — never as indented multi-line f-strings — since a blank/whitespace-only line anywhere in the joined result silently breaks HTML-block parsing from that point on.

## Scripts (`scripts/`)

All are standalone CLI entrypoints (`sys.path.insert` a project-root hack
at the top so they run without installing the package) using
`get_service_client()`:

- **`run_refresh.py --mode=intraday|eod|fundamentals|screener|all [--daemon]`** — the main scheduled job, called by `.github/workflows/refresh_prices.yml` (one-shot per mode) or run standalone with `--daemon` for an APScheduler loop.
- **`fetch_nifty50_constituents.py`** — re-applies a hardcoded `CURRENT_CONSTITUENTS` dict (kept in sync with `seed.sql` by hand) and reconciles which symbols are no longer current.
- **`seed_mock_data.py`** — backfills ~400 days of synthetic prices/fundamentals/dividends and ~60 days of daily snapshots using the mock providers, regardless of the configured env provider. This is the fastest way to get a fully populated local/dev environment.

  **Clean up mock rows before/when switching a project to a real provider.** `price_history` and `dividend_events` are additive/upserted per `(symbol, trade_date)` or `(symbol, ex_date, amount_per_share)` — a real provider refresh only overwrites rows for dates it actually fetches (`refresh_service`'s EOD lookback is 90 days), so mock rows for older dates, and *any* mock dividend event (dividends aren't overwritten by date at all, only deduplicated by exact amount), silently persist alongside real data forever unless removed. This actually happened on this project's own Supabase instance: a leftover mock dividend row inflated one stock's TTM dividend yield ~27x (1.13% shown vs. ~0.04% actual) until it was found and deleted. If you ever seed mock data into a project that will later go live, run something like this before trusting the numbers:
  ```python
  client.table("dividend_events").delete().eq("source", "mock").execute()
  client.table("price_history").delete().eq("source", "mock").execute()
  ```
  then re-run `run_refresh.py --mode=screener` to recompute. `fundamental_snapshots` doesn't need this — its upsert key is `(symbol, as_of_date)`, so a same-day real fetch fully replaces that day's mock row.
- **`import_screener_csv.py`** — converts a screener.in "Export screen results" CSV into `fundamental_snapshots`/`dividend_events` rows, with fuzzy column-name matching since the export's exact columns depend on what the user chose to include on screener.in.

## Edge Functions (`supabase/functions/`)

`manual-refresh/` backs the Dashboard's "Manual refresh" button
(`src/services/edge_refresh.py` calls it over HTTP). It exists because a
real fetch-and-write needs the Supabase service-role key, which cannot
live in Streamlit page code (Streamlit Cloud runs that code inside every
logged-in user's own browser session) — an Edge Function runs
server-side inside Supabase's own infrastructure instead, so it's safe to
give it the key there. This is a fundamentally different runtime from
the rest of this project: Supabase Edge Functions run **Deno/TypeScript**,
not Python.

- **`calculations.ts`** — a direct port of `src/calculations/*.py` plus
  `fundamentals_repo.py::carry_forward_fields`, same function names/shape
  translated to camelCase specifically so the two are easy to diff against
  each other. **This is a second copy of business logic living in a
  different language, with no automated check that it stays in sync with
  the Python originals** — if you change a rule in `src/calculations/`
  (a threshold direction, what counts as stale, etc.), mirror the change
  here too. `calculations.test.ts` mirrors the same boundary cases as
  `tests/test_calculations_classification.py` (exactly-at-threshold,
  missing-vs-confirmed-zero, PEG's reversed `<=` direction) — run with
  `deno test supabase/functions/manual-refresh/calculations.test.ts`.
- **`yahoo.ts`** — `fetchChartData()` (price history + dividend events,
  one Yahoo endpoint, no auth needed) and `fetchFundamentals()` (PE/PEG/
  EPS/market cap/52-week high/52-week low, a *different* Yahoo endpoint
  that needs a session cookie + "crumb" token obtained via a separate
  handshake — real added fragility beyond what Python's `yfinance`
  package already manages for the cron-refresh side of this project; see
  README "Limitations"). The 52-week high/low come off the same
  `summaryDetail` module already being requested for PE/market cap
  (`fiftyTwoWeekHigh.raw`/`fiftyTwoWeekLow.raw`) — no extra API call
  needed. Both endpoint shapes were confirmed with live `curl` requests
  before this was written, not assumed from documentation (there isn't
  any — both are unofficial).
- **`index.ts`** — the HTTP handler: verifies the caller's JWT (any
  logged-in user may trigger this — it refreshes shared data, not
  anything per-user), checks a 5-minute cooldown against
  `provider_fetch_log` (`provider_name = 'manual_edge'`, `fetch_type =
  'all'` — `'all'` had to be added to that column's CHECK constraint in
  `0005_add_manual_refresh_fetch_type.sql`, since none of the existing
  per-mode values fit a single combined refresh; `week_52_high`/
  `week_52_low`/`criterion_52w_high`/`criterion_52w_low` columns were
  added later in `0006_add_52week_high_low.sql`, mirroring the same
  columns added to `fundamental_snapshots`/`daily_screener_snapshots` on
  the Python side), then processes
  constituents in concurrency-limited batches of 8, and logs one summary
  row plus returns `{succeeded, failed, total, symbolsFailed}` as JSON.
  One symbol's failure doesn't abort the batch (each symbol's pipeline is
  wrapped in try/catch, mirroring `refresh_service.py`'s per-symbol
  error handling).
- Not using `supabase gen types typescript` (no generated Database
  schema type), so `supabase-js` clients are typed as `any` deliberately
  (see the `AnyClient` alias in `index.ts`) rather than fighting the
  library's default `never`-row inference for an ungenerated schema.

**Deploying/updating this function requires the Supabase CLI** (see
README "On-demand refresh" for the exact commands) — unlike the SQL
migrations elsewhere in this project, the Edge Functions Dashboard editor
is a much rougher way to manage a multi-file TypeScript function with
imports. `SUPABASE_URL`/`SUPABASE_ANON_KEY`/`SUPABASE_SERVICE_ROLE_KEY`
are auto-injected into every function's environment by Supabase; no
manual secret configuration is needed for this function to run.

Deno was installed locally at `~/.deno/bin/deno.exe` specifically to
test-and-typecheck this code before ever deploying it (`deno test`,
`deno check`) — there is no way to deploy to or invoke a live Supabase
project's Edge Functions from this development environment directly, so
`deno test`/`deno check` are as far as verification goes without the
user actually deploying and clicking the button themselves.

`fo-refresh/` backs the Dashboard's "📊 F&O Data Refresh" button, same
reasoning and runtime as `manual-refresh/` above (real writes need the
service-role key, must run server-side). Structurally it's a check-then-
maybe-ingest, not an unconditional refresh:

- **`bhavcopy.ts`** — `bhavcopyUrl(isoDate)`, `fetchBhavcopyText(isoDate)`
  (null on 404, mirroring the Python provider's walk-back-friendly
  contract), `findLatestAvailableBhavcopy(onOrBefore, maxLookback=7)`, and
  `parseFoBhavcopy(csvText, universe)` — a TypeScript port of
  `src/data_providers/nse_fo_provider.py`'s parsing (same column mapping,
  same STF/STO instrument-type filter, same universe filter). **The zip
  extraction is hand-rolled**, not via a library: Deno's Edge Runtime has
  no zip module built in, and the bhavcopy is always a single-entry
  archive, so `extractFirstZipEntry()` reads the ZIP's End-Of-Central-
  Directory + Central-Directory records (robust regardless of whether the
  local file header used a trailing "data descriptor", which makes the
  *local* header's own size fields unreliable — the central directory's
  are always authoritative) to locate the one entry's compressed bytes,
  then decompresses with the Web Streams API's native
  `DecompressionStream("deflate-raw")` — zero external dependencies for a
  format this constrained. `bhavcopy.test.ts` round-trips a hand-built
  synthetic zip through the extractor (proving the container-parsing logic
  independent of any real file) and separately verifies the CSV parsing
  against the same fixture rows `tests/test_nse_fo_provider.py` uses in
  Python. **Beyond the unit tests, this was also run against a real, live
  NSE bhavcopy** (not just the synthetic fixture) during development,
  confirming it correctly extracted and parsed all 50 symbols' futures and
  options from an actual current-day file before being considered done.

  **A real bug hit in production, worth understanding if this fails
  again**: the very first deploy failed with `"Could not reach NSE: Not a
  valid zip file (End Of Central Directory record not found)"`, even
  though the identical URL/logic had just been verified working from a
  normal dev machine. Root cause: `fetchBhavcopyText`'s original request
  sent only a `User-Agent` header, while the working Python provider
  (`nse_fo_provider.py::_BROWSER_HEADERS`) also sends `Accept` and
  `Accept-Language` — and NSE's bot-detection served a 200-status HTML
  challenge/block page (not the zip) to requests from Supabase's Edge
  Runtime network origin, which is a different source than a dev
  machine's. The thin header set made this function look more
  bot-like, and the (still-passing) `buf.length < 1000` size guard didn't
  catch it since a full HTML page is easily over 1000 bytes — so it fell
  through to `extractFirstZipEntry`, which correctly failed on genuinely-
  not-zip bytes but with no way to tell "blocked" from "corrupted
  download". Fixed two ways: (1) `REQUEST_HEADERS` now matches Python's
  header set exactly; (2) defense in depth — `fetchBhavcopyText` checks
  the response's `content-type` via `looksLikeZipContentType()` (confirmed
  live: a real bhavcopy is `application/zip`) *before* attempting to parse
  it, and on either a bad content-type or a zip-parse failure, throws an
  error that includes the HTTP status, content-type, byte length, and a
  text snippet of the actual body — so a future failure is
  self-diagnosing from the Streamlit page's error message alone, without
  needing `supabase functions logs`.

  **A second real bug, right after the first was fixed**: on the very
  next live click, the Streamlit app's tab spun indefinitely and needed a
  full app reboot to recover — worse than a clean error, because
  `fetchBhavcopyText`'s `fetch()` call had **no timeout at all**.
  `findLatestAvailableBhavcopy()` walks back up to `MAX_LOOKBACK_DAYS` (7)
  days looking for the latest published bhavcopy; if even one of those
  requests to NSE hangs (its bot-detection layer, already known from the
  first bug to behave unusually toward this function's network origin,
  could plausibly stall a connection rather than cleanly rejecting it),
  the whole Edge Function invocation blocks indefinitely — far longer
  than `edge_refresh.py`'s own client-side timeout was originally sized
  for, and apparently long enough that whatever came back (or didn't) left
  the Streamlit process itself stuck rather than surfacing a clean
  exception. Fixed by giving every NSE request a bounded
  `AbortSignal.timeout(FETCH_TIMEOUT_MS)` (15s — generous headroom over
  the sub-second response times seen in all live testing), and by
  treating a timed-out/failed fetch **the same as a 404** (return `null`,
  let the walk-back try the previous day) rather than throwing — so one
  bad day (most likely today's not-yet-published file, checked first)
  can't block discovery of an already-available earlier day, and the
  loop's total worst-case runtime is now bounded at
  `maxLookback * FETCH_TIMEOUT_MS` (~105s) instead of unbounded.
  `edge_refresh.py::FO_TIMEOUT_SECONDS` was raised from 120s to 180s to
  keep comfortable headroom above that new, now-real worst case (walk-back
  time plus ingest time for a genuinely new day). `bhavcopy.test.ts`
  covers this by monkey-patching `globalThis.fetch` to reject like a hung
  connection would, confirming `fetchBhavcopyText` returns `null` rather
  than throwing, and confirming an `AbortSignal` is actually passed to
  `fetch()`.
- **`index.ts`** — same auth/cooldown pattern as `manual-refresh/index.ts`
  (`provider_name = 'fo_edge'`, `fetch_type = 'fo'` — added to
  `provider_fetch_log`'s CHECK constraint by
  `0008_add_fo_fetch_type.sql`, same pattern as `0005` did for `'all'`).
  The distinguishing step: before doing any work, it reads
  `max(trade_date)` from `futures_daily_prices` (the "already loaded"
  watermark) and compares it against `findLatestAvailableBhavcopy()`'s
  result (the "NSE's latest" watermark) — if NSE has nothing newer, it
  returns `{updated: false, message, latestAvailable, latestLoaded}`
  immediately, with zero writes. Only when NSE's date is strictly newer
  does it parse and upsert into all four F&O tables (chunked at 500 rows,
  matching `fo_repo.py`'s Python chunk size) and re-derive `is_open` via
  the same expiry-vs-today logic as `fo_repo.refresh_open_flags`, then
  returns `{updated: true, tradeDate, futuresRows, optionRows}`.
- On the Streamlit side, `edge_refresh.py::trigger_fo_refresh()` is the
  HTTP client (same shape as `trigger_manual_refresh`, reusing
  `ManualRefreshError` rather than a parallel exception type, since the
  calling convention — cooldown/4xx/5xx handling — is identical); the
  Dashboard button shows a distinct message depending on `updated: true`
  vs `false` vs an error, rather than treating "nothing new" as a failure.

## Tests (`tests/`)

Run with `pytest` (config in `pytest.ini`; `-m "not integration"` is the
default, since there are no `@pytest.mark.integration` tests currently —
everything either mocks external state or is a pure function, so the
whole suite runs with zero network access). One file per module under
test, named `test_<module>.py`. If you add a new pure function to
`src/calculations/` or `src/services/`, it should get a same-pattern test
file — boundary cases (exactly-at-threshold, missing data) are the ones
that matter most given how the spec is written. The same applies inside
otherwise I/O-heavy repository modules: `fundamentals_repo.py`'s actual
carry-forward logic is factored out into a standalone pure function
(`carry_forward_fields()`) specifically so it has a direct test
(`test_fundamentals_repo.py`) without needing to mock a Supabase client —
prefer that split over testing repo logic through a mocked client.

## Common changes, step by step

**Add a new market-data or fundamentals vendor**: implement
`PriceDataProvider` or `FundamentalsDataProvider` in a new file under
`src/data_providers/`, add a branch in `factory.py`, add the new literal
value to `src/config.py`'s `Settings.market_data_provider` /
`fundamentals_provider` type.

**Add a new alert type**: add the value to `AlertType` in
`src/models/enums.py`, add a branch in `alert_service.evaluate_alert()`,
add the matching `config` fields to the alert-creation UI in
`pages/2_Stock_Detail.py` and `pages/3_Alerts.py`, add the CHECK constraint
value in a new migration altering `alerts.alert_type`.

**Add a new Streamlit page**: create `pages/N_Name.py`, start it with
`require_login()`, use `get_user_client_cached()` for all data access
(never `get_service_client()`), add a `st.page_link(...)` to it from
`app.py`.

**Add a new table**: write a new numbered migration in
`supabase/migrations/`, add RLS policies for it (per-user tables need
`auth.uid() = user_id` policies; shared tables need an `authenticated`
read-only policy — see `0002_rls_policies.sql` for the pattern), add a
matching Pydantic model in `src/models/`, add a repository module in
`src/repositories/`.

**Change a calculation rule**: everything lives in `src/calculations/`.
Change the function, then update/add the corresponding test in
`tests/test_calculations_*.py` — these tests are the executable spec, so
a rule change without a test change is a red flag on review.
