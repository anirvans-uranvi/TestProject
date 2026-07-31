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
.streamlit/config.toml          Streamlit's own [theme] (light base, slate-navy primaryColor) + toolbarMode
app.py                          Pure st.navigation() router, no visible content of its own -- see below
pages/
  1_Dashboard.py                 Screener table, metric cards, filters, CSV export -- sidebar label "Screener"
  2_Stock_Detail.py               Price/volume/dividend charts, scorecard, alerts, position notes -- sidebar label "Equity"
  3_Alerts.py                     Alert CRUD + notification history
  4_Settings.py                    Per-user thresholds, theme, change password, sign out
  5_Options.py                     F&O: futures term structure + 5% CSP/CC breakdown per stock
  6_Portfolio.py                    Upload Zerodha/Dhan holdings, live-valued against app market data
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
(`0001` → `0015`). Eighteen tables, in three groups (`0008` doesn't add a
table -- it just extends `provider_fetch_log.fetch_type`'s CHECK
constraint with `'fo'`, for the `fo-refresh` Edge Function's logging;
`0010`/`0011` drop and recreate `dashboard_fo_metrics` with a different
key/columns rather than adding a new table -- see "Dashboard cache"
below; `0012` adds `portfolio_holdings`; `0013` only redefines
`latest_screener_view`, no new table; `0014` only adds a column +
widens `portfolio_holdings`' primary key, no new table; `0015` only adds
a column to `companies` (`is_etf`) + redefines `latest_screener_view`
again, no new table):

**Reference data** (written by `scripts/fetch_nifty50_constituents.py` /
`seed.sql`, read-only to the app):
- `nifty50_constituents` — which symbols are in the index and when (supports historical reconstitution tracking)
- `companies` — name/sector/industry per symbol, plus `is_etf` (migration `0015`) -- see the Futures & Options section's "A follow-up problem 0013 itself introduced" paragraph for what this excludes and why

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
- `portfolio_holdings` — broker-CSV-uploaded holdings (migration `0012`, `portfolio_name` added in `0014`), keyed `(user_id, portfolio_name, broker, raw_name)` -- a user can maintain multiple independently-named portfolios that all coexist. `symbol` is nullable and deliberately **not** FK'd to `companies` -- a resolved symbol may not exist there yet (an ETF/fund or non-Nifty50 stock the screener doesn't otherwise track); see the Portfolio section below for how it gets registered.

Two generated helpers, defined in `0003_views_functions.sql` (and patched
in `0004`):
- `latest_screener_view` — one joined row per current constituent (companies + its latest daily_screener_snapshot), plus (as of `0013`) every symbol the *viewing* user holds in their own `portfolio_holdings`, minus (as of `0015`) any row with `is_etf = true`. This is what the Dashboard queries in a single call instead of joining client-side. `0004` added `coalesce(status, 'unavailable')` / `coalesce(data_quality, '{}')` here because a constituent with no snapshot yet would otherwise return `NULL` for those columns, which fails Pydantic validation on the `ScreenerRow` model. `0006` added `week_52_high`/`week_52_low`/`criterion_52w_high`/`criterion_52w_low` — **a real deploy-time error hit here**: `create or replace view` can only *append* new output columns; inserting them positionally in the middle of the existing `select` list (as the first draft of `0006` did) makes Postgres think you're renaming the columns that got pushed down a slot, and it fails with `42P16: cannot change name of view column ... HINT: Use ALTER VIEW ... RENAME COLUMN ... instead`. The fix is to always append new columns at the very end of the `select` list in any future `create or replace view` migration, never insert them mid-list — column *order* doesn't matter to the app since every read is by name (`ScreenerRow.model_validate(dict)`), so this costs nothing. `0013` made two further changes, both **real production bugs found after the Portfolio feature shipped** (see the Portfolio section's "Screener fallback" note below for the full story): the per-symbol lateral join now prefers the most recent snapshot row that actually has a price (falling back across days when today's fetch failed) instead of always taking literally the latest date regardless of whether it has data, and the join went from `nifty50_constituents` inner-join to `left join ... where nc.is_current or exists (select 1 from portfolio_holdings where symbol = c.symbol and user_id = auth.uid())` — `security_invoker = true` means `auth.uid()` here is the actual querying user, so this stays correctly scoped per user (reinforced by `portfolio_holdings`' own RLS policy on top).
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
backoff and a client-side request-rate throttle. `yfinance_provider.py`
also has one function outside either ABC, `fetch_display_name(symbol)` —
a best-effort real-name lookup (`longName`/`shortName`) used only once,
at portfolio-symbol registration time, to classify a new symbol as an
ETF/fund (see the Futures & Options section's ETF-exclusion paragraph);
not part of `FundamentalsDataProvider` since it's a one-off the other
three providers have no equivalent for.

## Repositories (`src/repositories/`)

One module per table/concern (`companies_repo.py`, `price_repo.py`,
`fundamentals_repo.py`, `dividends_repo.py`, `snapshot_repo.py`,
`settings_repo.py`, `alerts_repo.py`, `notification_repo.py`,
`fetch_log_repo.py`, `portfolio_repo.py`), plus `supabase_client.py` for
client construction.

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
- **`refresh_service.py`** — fetch (via a provider) → normalize → persist raw/normalized records, with retry + `provider_fetch_log` auditing. Intraday price upserts only include the columns actually fetched (`close`/`adjusted_close`) so a same-day EOD upsert filling `open`/`high`/`low` later isn't clobbered, and vice versa (PostgREST's upsert only sets columns present in the request body). **A real bug was found and fixed here**: `refresh_intraday_prices`/`refresh_eod_prices`/`refresh_fundamentals` each loop over an *alphabetically sorted* symbol list (`run_refresh.py`'s `sorted(set(symbols) | set(portfolio_symbols))`), catching only `ProviderError` per symbol — but a bad upsert raises `postgrest.exceptions.APIError` instead, a different exception type, which propagated out uncaught and silently aborted every symbol after the failing one for the rest of that day's run. Root cause: `yfinance` derives some fundamentals ratios itself (e.g. `trailingPE = price / trailingEps`) rather than passing through a value Yahoo's API already computed, so a newly listed stock with `trailingEps == 0.0` (seen for VAML — Vedanta Aluminium Metal Limited, added via portfolio tracking) surfaces `trailingPE = Infinity`; `json.dumps` renders that as the bare token `Infinity`, which isn't valid JSON, so PostgREST rejects the upsert. Fixed two ways: `yfinance_provider.py`'s new `_finite()` helper sanitizes every fundamentals field to `None` if it isn't a finite number before it ever reaches `FundamentalSnapshot`, and all three loops in `refresh_service.py` now also catch `APIError` so a future bad value for one symbol degrades to that symbol failing, not the whole batch. The TypeScript Edge Function (`supabase/functions/manual-refresh/index.ts`) isolates each symbol's failure already (`refreshOneSymbol` wraps its entire fetch+upsert chain in one try/catch, returning `{symbol, ok: false, error}` rather than throwing past the loop) so it can't cascade the same way — but it turned out to hit the *exact same* underlying problem on its own: Yahoo's `quoteSummary` endpoint can't put a real `Infinity` in JSON either, so for VAML it sent the **string** `"Infinity"` for `trailingPE.raw` instead of a number. That string passed the existing `!== null` check in `index.ts` and reached the `numeric(10,4)` column directly, and Postgres rejected it outright (`numeric field overflow ... cannot hold an infinite value` — confirmed by reproducing the exact upsert directly against the live table). Every "Manual refresh" click was failing specifically on VAML for this reason (see `provider_fetch_log` rows with `provider_name='manual_edge', error_message='1 symbol(s) failed: VAML'`). Fixed with the same pattern: `yahoo.ts`'s new `finiteOrNull()` (covers both a real `Infinity` number and Yahoo's `"Infinity"`/`"-Infinity"`/`"NaN"` string forms) sanitizes all six fundamentals fields in `requestFundamentals()` before they ever reach `index.ts`'s upsert payload — mirroring `_finite()` on the Python side, deliberately kept in sync the same way every other calculation is ported between the two.
- **`alert_service.py`** — `evaluate_alert(alert, current_snapshot, previous_snapshot, stock_name, now)` is pure (no I/O) and covers all ten `AlertType` values, cooldown (`last_triggered_at` + `cooldown_minutes`), and a stable SHA-256 `dedupe_key` (same alert+symbol+day always produces the same key, so a DB-level unique constraint on `notification_log.dedupe_key` is the final backstop against double-firing). Callers persist the returned `NotificationEvent`s via `notifications/inapp_adapter.py`.
- **`market_calendar.py`** — NSE trading-day/market-state logic. The holiday list (`NSE_HOLIDAYS`) is hardcoded **per calendar year** and needs a manual update every year; falls back to weekday-only for years not listed.
- **`threshold_override.py`** — `daily_screener_snapshots` is computed server-side against *default* thresholds (the stable audit trail); a signed-in user can configure their own thresholds in Settings, so pages re-run `build_classification()` client-side against the row's stored raw inputs (which are threshold-independent) to reflect that choice, without a server-side recompute per user. Also recomputes `is_stale` from `data_quality.stale_minutes` against the user's own `stale_data_threshold_minutes` when available.
- **`explanation.py`** — `explain_classification(row)` builds the plain-English sentence shown on Stock Detail, branching on which criteria passed/failed/are missing.
- **`portfolio_service.py`** — CSV parsing for the two supported broker exports (`parse_zerodha_csv`, `parse_dhan_csv`), name-to-symbol matching (`match_symbol`, normalized-substring containment against `companies.name`), cross-broker merging (`merge_holdings`), and live valuation (`compute_portfolio_view`). `resolve_tracked_symbols` is the pure diff both refresh paths call to register newly-seen portfolio symbols; `looks_like_etf_name` is the real-display-name-based ETF/fund classifier those same paths apply to it before upserting (migration `0015`). See the Portfolio section below and the Futures & Options section's "A follow-up problem 0013 itself introduced" paragraph for the full ETF story.

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
against the real calendar once per run by `fo_repo.refresh_open_flags` --
every newly-upserted contract row defaults to `is_open: true` at insert
time (both the Python parser and its TypeScript port), so this
finalization step is what actually closes out anything whose expiry has
since passed.

**A real incident this caused**: `scripts/fetch_fo_data.py --days 60`
calls `refresh_open_flags` exactly once, *after* its whole oldest→newest
ingest loop finishes -- so a run that dies partway through (confirmed
live: a transient Cloudflare 502 from Supabase's own REST endpoint,
mid-backfill) never reaches it at all. The already-ingested days aren't
lost (each `ingest_fo_day` call commits independently), but every contract
that run inserted stays `is_open: true` forever, including ones for
expiries the backfill deliberately reached back through that have since
passed (e.g. a 60-day backfill run in late July surfaces April/May/June
expiries, all of which are stale by then) -- so the Options screen's
futures/option-chain tables start showing long-expired contracts
alongside the real current ones, with no error anywhere to flag it. Fixed
by simply calling `fo_repo.refresh_open_flags(client, date.today())`
directly (safe to run any time, fully idempotent) rather than re-running
the whole backfill. If you see stale expiries on the Options screen after
a `fetch_fo_data.py` run, check whether it actually completed (its final
log line is `F&O ingest complete: ...`) before assuming it's a data bug.

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
  day is ~9k option rows), `refresh_open_flags`,
  `delete_expired_dashboard_fo_metrics` (see "A real bug this caused"
  below), and reads off the views.
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
  OTM call whose strike is the **lowest one still at or above 5% above
  spot** (`target = spot * 1.05`, the mirror image of "5% CSP"'s `target
  = spot * 0.95` search) — no ITM leg, no PE leg, just the one call.
  **This is a strike filter, not a nearest-match**: a strike below the 5%
  line never wins even if it happens to be closer to `target` in absolute
  distance than every strike that actually clears it (e.g. spot 1000 →
  target 1050: a 1040 strike loses to a 1080 strike, even though 1040 is
  numerically closer to 1050) — falls back to the single highest
  available strike only if none reach the 5% line at all. `cc_5pct_for_rows`
  returns, beyond the strike/premium themselves:
  `gross_investment` = spot (the cost of buying 1 share at the current
  price); `net_investment` = `gross_investment − premium` (the premium
  collected up front reduces the real capital outlay); `cc_pct` = premium
  ÷ `gross_investment` × 100 (the covered-call yield on the stock's own
  price — this is the value both the Dashboard and the Options screen
  label "5% CC"); and `assignment_profit_pct` = (strike ÷ `net_investment`
  − 1) × 100 — if assigned, the seller receives `strike` per share for a
  position that only cost `net_investment` per share, so this is the
  *total return of the whole covered-call trade if called away*, shown on
  the Options screen only as "Assignment Profit" (`None` when
  `net_investment` is zero or negative — premium ≥ spot — rather than
  dividing by zero or a negative number). This replaced an earlier,
  simpler formula (`assignment_profit_pct = premium ÷ (strike − spot) ×
  100`, and strike selection was pure nearest-by-absolute-distance to
  `target` with no "must actually be ≥5% OTM" filter) on request — if you
  find code or docs describing that version, or an even earlier "5% ITM
  PMCC" (poor-man's-covered-call: buy an ITM call, sell a PE at the same
  strike, sell a further OTM call, net-credit ÷ ITM strike, exposed as
  `itm_pmcc_5pct_map`/`itm_pmcc_for_rows`), neither exists anymore.
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
  Its `universe` (which symbols the bhavcopy parse keeps) starts as the
  current Nifty50 constituents, then widens with every distinct resolved
  symbol across all users' `portfolio_holdings` -- same pattern
  `scripts/run_refresh.py` already uses for cash-market data (registering
  a minimal `companies` row for any not seen before), applied here too so
  a portfolio-only stock like Hindustan Zinc actually gets its
  futures/options ingested, not just its equity LTP. Tolerant of
  `portfolio_holdings` not existing yet (migration `0012`).

**On-demand refresh**: the Dashboard's "📊 F&O Data Refresh" button hits a
second Edge Function, `supabase/functions/fo-refresh/` (see the Edge
Functions section below) — a TypeScript port of the same bhavcopy
fetch+parse, but only for the single most recent day and only if NSE has
actually published something newer than what's already loaded (checked via
`max(trade_date)` in `futures_daily_prices`), so a click when nothing's
new is a cheap read-only no-op rather than a silent re-fetch. It has no
external zip-library dependency — see the Edge Functions section for why.
Its `universe` set gets the identical portfolio-symbol widening as
`fetch_fo_data.py` above (mirrored in TypeScript via the same
`resolveTrackedSymbols`/`portfolioSymbols.ts` helper `manual-refresh`
already uses), so a symbol newly tracked from a portfolio upload starts
getting its F&O data via this button too, not just a manual backfill run.

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

**A real bug this caused**: `recompute_dashboard_metrics` (and its
TypeScript port) only ever *upserted* -- `dashboard_metrics_rows` emits a
symbol's *current* up-to-3 nearest expiries each run, but a month that
just expired simply stops being emitted; nothing ever deleted its old
cached row. Left unchecked, the Dashboard's "Options month" dropdown
(built from every distinct `expiry_date` still present in the table)
kept offering already-expired months forever -- confirmed live: the
dropdown still listed "Jul 2026" a day after that expiry had passed,
even though the Options screen (which reads live contracts, gated on
`is_open`, not this cache) had already stopped showing it. Fixed by
`fo_repo.delete_expired_dashboard_fo_metrics(client, as_of)` (and its
TypeScript mirror in `dashboardMetrics.ts`), called at the end of every
`recompute_dashboard_metrics` run -- same finalization pattern as
`refresh_open_flags` above, just a straight delete rather than a
two-way flag flip since this cache has no `is_open` column of its own.

**A second, more subtle real bug found right after fixing the above**:
even after fixing the stale-row issue, a portfolio-only symbol (e.g.
Hindustan Zinc) still never got a `dashboard_fo_metrics` row at all --
confirmed live on the Options screen (which reads live contracts
directly) but permanently "N/A" on the Dashboard. Root cause:
`recompute_dashboard_metrics` sourced spot prices from
`snapshot_repo.get_latest_screener()` (`latest_screener_view`), whose
per-user portfolio-symbol widening (migration 0013, see the Portfolio
section) keys off `auth.uid()` -- but every caller of
`recompute_dashboard_metrics` runs under the **service-role** client
(cron, `fetch_fo_data.py`, both on-demand refresh Edge Functions), where
`auth.uid()` is null. So the view silently fell back to only the 50
Nifty50 constituents regardless of whose portfolio a symbol came from
-- confirmed directly: `latest_screener_view` returned exactly 50 rows
under a service-role query even though `daily_screener_snapshots` had a
real, current row for Hindustan Zinc. Fixed by sourcing spot prices from
`snapshot_repo.get_latest_prices(client, all_company_symbols)` instead
-- the exact same fix that function's own docstring already describes
for the Portfolio page's identical problem -- mirrored in the TypeScript
port as `fetchSpotBySymbol`. **If you add another `recompute_*`-style
function that runs under the service-role client, do not read spot/price
data from `latest_screener_view`** -- reach for `get_latest_prices` (or
a raw `daily_screener_snapshots` query) instead; the view's widening is
correct only when the caller's own `auth.uid()` is the relevant user.

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

`app.py` has no content of its own -- it's a pure router:

```python
pages = [
    st.Page("pages/1_Dashboard.py", title="Screener", default=True),
    st.Page("pages/2_Stock_Detail.py", title="Equity"),
    st.Page("pages/5_Options.py", title="Options"),
    st.Page("pages/6_Portfolio.py", title="Portfolio"),
    st.Page("pages/3_Alerts.py", title="Alerts"),
    st.Page("pages/4_Settings.py", title="Settings"),
]
st.navigation(pages).run()
```

This replaced the legacy `pages/`-directory auto-discovery convention
(where the sidebar label and display order were both derived from each
file's name/numeric prefix, and the entrypoint script itself always
appeared as its own nav entry, labeled "app" from `app.py`) -- there used
to be a real landing-page screen here (welcome text + quick links + a
"data sources" info card reading `settings.market_data_provider`); it was
dropped on request, along with that screen from the nav entirely, in
favor of landing directly on a real page (`default=True` on the
Dashboard/"Screener" entry). `st.Page`'s `title=` is independent of the
underlying filename, which is why the sidebar shows "Screener"/"Equity"
for files still named `1_Dashboard.py`/`2_Stock_Detail.py` -- nothing
else about those files (including every `st.switch_page("pages/...")`
call elsewhere that references them by path) needed to change. Order in
the sidebar is just this list's order, independent of the files' numeric
prefixes (which now only affect directory sort order, not app UI) --
Alerts/Settings are listed last on request.

The sign-out control used to live in `app.py`'s own sidebar (`with
st.sidebar: st.button("Sign out")`) -- since that screen no longer
exists, it moved into `4_Settings.py`'s Account section, right after the
"Signed in as" line, as a plain `st.button()` (not a sidebar element;
Settings has no sidebar content of its own to put it in).

Every page in `pages/` still starts with `require_login()` (from
`src.utils.session`), which either lets the page proceed (a valid
session exists) or renders the Sign in / Create account / Forgot
password tabs and `st.stop()`s -- unaffected by which script (legacy
auto-discovery vs. `st.navigation`) actually invoked it. Likewise, each
page's own `st.set_page_config(page_title=..., page_icon=...)` call
(browser-tab metadata) is unaffected too -- it's independent of `st.Page`'s
`title=` (sidebar label).

- **`1_Dashboard.py`** — loads `latest_screener_view` via `snapshot_repo.get_latest_screener()`, applies the signed-in user's thresholds via `threshold_override.apply_user_thresholds()`, renders metric cards (also usable as quick filters, wired through `st.session_state["status_filter"]`), sidebar filters, and the screener table. The Status sidebar filter is a `st.multiselect` over `ALL_STATUSES = ["Green", "Amber", "Red", "Unavailable"]` — `status_filter` is always a *list* (any combination, not one-or-all), and the final row filter is a single `df["status"].isin([...])`, so selecting all four is equivalent to no filter at all. Saved filter presets normalize old single-string `"status"` values (from before this was a multiselect) into a list on load for backward compatibility. The "Minimum dividend yield" / "Minimum PEG" sidebar filters default to `0.0`, **not** `user_settings.dividend_yield_threshold`/`peg_threshold` — they're a separate display filter from the criterion A/C pass/fail thresholds, and defaulting them to the threshold value silently hid every stock below it on first load (a real bug, since fixed). Keep these two concepts distinct if you touch this page: the Settings-page thresholds decide Green/Amber/Red/Unavailable; these sidebar inputs just additionally hide rows below a value the user dials in themselves, and should default to "show everything." The header has two on-demand refresh buttons, each hitting its own Edge Function (see [Edge Functions](#edge-functions-supabasefunctions) below): "🔄 Stock Data Refresh" (cash market, `manual-refresh`) and "📊 F&O Data Refresh" (futures/options, `fo-refresh` — a no-op with an "already up to date" message if NSE hasn't published anything newer than what's loaded). Below the title, a "Data sources" caption reads `get_settings().market_data_provider`/`fundamentals_provider` directly (e.g. "Stock prices: `yfinance`") and states the options/F&O source as a fixed string, "NSE Bhavcopy (end-of-day)" — there's no configurable F&O provider setting to read (`src/config.py` has no such field; F&O ingestion is always the NSE bhavcopy). The header's "Data freshness" column also gets a second line, "Latest Bhavcopy: <date>", from a new `fo_repo.get_latest_fo_trade_date()` — the most recent `trade_date` actually present in `futures_daily_prices`, deliberately **not** `last_fo_fetch_at` (`fetch_log_repo`'s "when did the ingestion job last run" timestamp): a bhavcopy is published for a specific trading day and a run on a non-trading day finds nothing new, so the job can run successfully today while the loaded data is still from a prior session — this line surfaces that distinction, `last_fo_fetch_at` doesn't. Wrapped in the same `except APIError: None` degrade as the rest of this page's optional F&O reads, for a deployment that hasn't applied migration `0007` yet.

  **Metric cards**: seven buttons in a row (`st.columns(7)`) — `Total stocks`, `🟢 Green`/`🟠 Amber`/`🔴 Red` (each sets the status filter to just that one status), and `Yield > threshold`/`All momentum +ve`/`PEG ≤ threshold` (each sets `criterion_filter` instead). There is deliberately no `Unavailable` button — the status itself is still fully selectable via the sidebar's Status multiselect and still counts toward `ALL_STATUSES`, but it wasn't considered a useful one-click quick filter and was dropped to declutter the row (a purely cosmetic trim, not a behavior change to filtering).

  **Screener table columns**, left to right: `Stock` (the NSE ticker symbol, e.g. `ADANIENT` — not the full company name; no separate `#`/`Symbol` key -- see below for why), `LTP` (latest price — renamed from "Latest price" for column-width economy), `52W High`/`52W Low` (value + `pass_fail_icon` for `criterion_52w_high`/`criterion_52w_low` — display-only proximity checks, **not** part of Green/Amber/Red; see `classification.py` above), `1D`/`5D`/`20D` (arrow + percentage only), `Momentum` (a single `pass_fail_icon(criterion_b)` — despite the name it's specifically criterion B, not a combined A/B/C view), two F&O-derived columns (see below), and `Dividend`/`PE`/`PEG` last (`Dividend` — renamed from "Dividend yield" — and `PEG` both carry a `pass_fail_icon` for criteria A and C respectively; `PE` is plain, not a criterion). There used to be a third F&O-derived column, the near-month future's price (header e.g. `Jul Future`, backed by `fo_service.near_month_futures_map`/`near_month_column_label`) — it was dropped on request, and those two now-unused `fo_service` functions (and their tests) were deleted along with it rather than left as dead code; futures data is no longer fetched on this page at all (only options, for the two columns below).

  **The two F&O-derived columns** (between `Momentum` and `Dividend`) come from a *separate* data source than the rest of the table — `dashboard_fo_metrics` (migration `0011`), the Dashboard's precomputed F&O cache, not `latest_screener_view` — joined in by symbol after filtering, rather than being part of `ScreenerRow`. `_load_dashboard_fo_metrics` returns the *raw* row list (up to 3 per symbol, one per near/next/far expiry); an **"Options month" selectbox** lists the distinct `expiry_date`s actually present (formatted `"%b %Y"`, e.g. "Jul 2026") and defaults to the nearest one via `st.session_state["dashboard_options_month"]`. Picking a month is a pure re-render over already-cached rows -- `filtered["csp_5pct"]`/`filtered["cc_5pct"]` are built by filtering `dashboard_fo_metrics_rows` to that one `expiry_date` and mapping by symbol, no new fetch or recomputation triggered. This page only ever reads the final `csp_pct`/`cc_pct` key out of each row; see the Futures & Options section's "Dashboard cache" paragraph above for the full pipeline (`fo_service.dashboard_metrics_rows`/`recompute_dashboard_metrics`, and every refresh path that keeps it current) and `csp_5pct_for_rows`/`cc_5pct_for_rows`'s docs for the underlying formulas, which are also what the Options screen's "5% CSP"/"5% CC" breakdown sections use directly (unchanged, live, for a single symbol):
  - **`5% CSP`** — a cash-secured-put yield: for the strike nearest 5% below spot (the screener's `latest_price`, not the option chain's `underlying_price`, kept consistent both here and on the Options screen — see the bug note on `5_Options.py` below), the selected expiry's put premium as a percentage of that strike (`put_price / strike * 100`). **Deliberately not divided by exchange margin** — SPAN margin isn't available from NSE as a simple downloadable per-contract figure (it's a licensed CME Group multi-scenario risk calculation, confirmed via a live search of NSE's actual report index turning up nothing), so this uses the strike itself (the full notional a cash-secured-put seller sets aside) as the yield's denominator instead; `strike * lot_size` cancels out of both the premium and this ratio, so lot size never needs to appear in the formula at all.
  - **`5% CC`** — a covered-call yield: sell 1 lot of the lowest call strike still at or above 5% above spot (a floor filter, not a nearest-match -- see `fo_service.py`'s bullet above), expressed as a percentage of **gross investment** (spot, the cost of buying 1 share) -- `premium / spot * 100`. (The Options screen additionally shows "Net Investment" and "Assignment Profit" -- `(strike / net_investment - 1) * 100`, where `net_investment = spot - premium` -- but the Dashboard column only ever surfaces `cc_pct`.) This replaced an earlier three-leg "5% ITM PMCC" (poor-man's-covered-call) calculation on request; see `fo_service.py`'s bullet above for what that used to compute.

  **A real bug this surfaced**: `fo_repo.get_all_open_options()` initially had no pagination, and PostgREST caps a single response at a server-configured max (1000 rows on this project) regardless of how many rows actually match — against live data (~5,053 open PE legs across 50 symbols) this silently truncated to exactly 1000 rows, and whichever symbols fell outside that window (most of the universe, including RELIANCE/TCS/HDFCBANK) were missing from the 5% CSP column with **no error anywhere** — confirmed live (`PE rows: 1000` before, `5053` after). Fixed by a generic `fo_repo._paginate(query_builder, page_size=1000)` helper that `get_all_open_futures()`/`get_all_open_options()` go through, looping `.range()` calls until a page comes back short (this is still exercised by `recompute_dashboard_metrics`, which calls `get_all_open_options()` at refresh time, and by its TypeScript port's own paginated fetch). `tests/test_fo_repo.py` covers the pagination boundary cases (multi-page accumulation, an exact-multiple-of-page-size input not looping forever, empty results).

  The cache read (`fo_repo.get_dashboard_fo_metrics`) is cached the same way `_load_screener_rows`/`_load_last_fetch` are (`@st.cache_data(ttl=60)`, keyed on `dashboard_cache_bust`) — a handful of rows, not the thousands-of-rows option query this used to be (see "Dashboard cache" above for why that moved off this page's request path entirely). Both refresh buttons bump `dashboard_cache_bust` and `st.cache_data.clear()`, so a click always shows this session's own just-recomputed cache rows rather than stale 60s-old ones. Like `pages/5_Options.py`, a missing F&O schema (migration `0007`/`0011` not yet applied) degrades to "N/A" in both columns via the same `except APIError` pattern (and an empty/disabled month dropdown), rather than crashing the whole Dashboard.

  There is deliberately no dedicated `Status` column — it duplicated the per-criterion tick/cross columns already on screen without adding information. The underlying `status` field is still sortable/filterable (sidebar multiselect), just not rendered as its own column. `status_badge()` (colored text badge, e.g. "🟢 Green") is still used standalone on Stock Detail's header, where the status needs to stand alone rather than sit in a row with other context.

  **The table itself is a plain `st.dataframe`, not hand-rendered HTML.** It didn't start that way -- read this before adding another interactive element to the table, since the history explains a real constraint of this app (session-only auth) that's easy to re-break.

  The original design called for clickable column headers, implemented as `<a href="?sort=...">` links inside a hand-rendered `render_screener_table()` HTML table. This went through three rounds of real bugs, each one only visible by actually clicking the deployed app: (1) an untargeted link inside `st.markdown(unsafe_allow_html=True)` gets `target="_blank"` forced onto it by Streamlit's own markdown renderer, popping a new browser tab; (2) adding `target="_self"`/`target="_top"` stopped the popup but broke sorting outright, because Streamlit Community Cloud wraps the deployed app in its own outer routing context that those target values navigate past; (3) even once the right target was found, clicking the header **logged the user out** — the actual, unfixable root cause: `src/utils/session.py`'s own docstring already states the Supabase auth session lives only in `st.session_state`, never a cookie/localStorage, and **any real browser navigation, regardless of `target=`, starts a brand-new WebSocket session with empty `st.session_state`** — Streamlit's own multipage sidebar nav and `st.page_link`/`st.switch_page` avoid this because those are React components with a JS click handler wired into Streamlit's own client-side router (no real page load happens), but a raw `<a href>` inside injected HTML has no such handler and is just a plain link to the browser. This is the same underlying fact the [Auth](#auth-a-non-obvious-quirk) section documents for password reset — read that section's mechanism explanation if you're tempted to add another `<a href>` inside any hand-rendered HTML block in this app. The first fix was to drop links entirely: a plain `st.selectbox("Sort By", ...)` + `st.checkbox("Descending", ...)` pair rendered above the table, with `render_screener_table()`'s headers showing nothing but a ▲/▼ arrow for visual feedback -- a real native widget doing the actual sorting server-side, in Python.

  That was later replaced with what's here now: a plain `st.dataframe`, whose column headers are natively clickable/sortable in the browser (no Python round-trip, all client-side) -- exactly like the Futures table on the Options screen and the Portfolio page's own holdings table. This is strictly better than the `st.selectbox`/`st.checkbox` workaround (real click-to-sort instead of a side dropdown) without reintroducing the `<a href>` problem, since `st.dataframe`'s sorting is handled entirely inside Streamlit's own React component, never a raw hyperlink.

  That native client-side sort is exactly why the per-row "open in Stock Detail" 🔍 button (previously a real `st.button()` in a narrow `st.columns([30, 1])` sliver beside the table, one per row) had to go too: those buttons were positioned by their *pre-sort* Python row index, so clicking a header to reorder the table in the browser would leave them pointing at the wrong row once the visual order no longer matched Python's. Row selection (`on_select="rerun"`, `selection_mode="single-row"`) replaces it -- Streamlit maps a click back to the correct index in the original (pre-sort) data regardless of how the table is currently sorted, so `display_rows[selected_rows[0]]["Stock"]` always resolves to the row the user actually clicked. Selecting a row reveals two buttons below the table, "Open `<symbol>` in Stock Detail" and "Open `<symbol>` in Options" (the latter sets `st.session_state["fo_symbol"]`) -- which also let the separate "Open in Stock Detail →"/"Open in Options →" selectbox pair that used to sit below the table get removed entirely as redundant.
- **`2_Stock_Detail.py`** — the most feature-dense page: Plotly candlestick (falls back to a line chart if OHLC is incomplete) with volume subplot, moving averages, entry/target/stop-loss lines, dividend timeline, classification-history chart, position notes form, and inline alert creation. The Fundamentals column is rendered via `render_stat_grid()` instead of stacked `st.markdown` lines; the alert list uses `render_alert_row()` (see below) instead of printing the alert's raw Python `config` dict; the "Create a new alert" expander's inputs are now wrapped in an `st.form` (previously plain buttons), bringing it to parity with `3_Alerts.py`'s create-alert form, which already used this pattern. A "📊 View F&O / options" button hands the current symbol to `5_Options.py` via `st.session_state["fo_symbol"]` + `st.switch_page`. `symbol_options` (the "Select a stock" picker) is `companies_repo.list_current_constituents(client)` unioned with `portfolio_repo.list_portfolio_symbols(client, user_id)` -- the signed-in user's own resolved portfolio symbols (ETFs, non-Nifty50 stocks) -- so a portfolio-only stock becomes viewable here the moment it's tracked, not just on the Dashboard. Tolerant of `portfolio_holdings` not existing yet (migration `0012`), degrading to Nifty50-only exactly as before this widening existed.
- **`3_Alerts.py`** — alert CRUD (including portfolio-wide alerts, `symbol IS NULL`) and notification history. Alert rows use `render_alert_row()` (shared with Stock Detail — one formatting implementation, two call sites) instead of a raw dict dump. Notification history stays `st.dataframe`-only on every viewport, deliberately not given a Tailwind mobile-card alternative — see the design-system note under Utils for why.
- **`4_Settings.py`** — per-user thresholds, theme, change-password. The three permanently-disabled Email/Telegram/Slack notification checkboxes were collapsed into a single row of `render_pill()` "coming soon" badges next to the one real (In-app) checkbox, removing dead-weight disabled UI for unimplemented channels.
- **`5_Options.py`** — the F&O / Options screen for one stock (see the Futures & Options section above for the data pipeline). Symbol selector defaults to `st.session_state["fo_symbol"]` (set by selecting a row on the Dashboard's table or clicking Stock Detail's own "View F&O / options" button), falling back to `selected_symbol`. `fo_symbols` (the options list) is `fo_repo.list_fo_symbols(client)` -- already every symbol with an open futures contract, regardless of Nifty50 status -- falling back to current constituents if that's empty, then unioned with `portfolio_repo.list_portfolio_symbols(client, user_id)` so a portfolio-only stock is at least selectable even with zero F&O data (handled gracefully below, same as any Nifty50 stock with none). Renders: an expiry selector (drives the summary tiles and, indirectly, which expiry's chain gets reused rather than re-fetched in the CSP/CC tables below); summary tiles (spot / ATM strike / total CE OI / total PE OI / Put-Call ratio) via `render_stat_grid`, sourced from `fo_service.option_chain_summary(chain_rows)` for the selected expiry; a futures term-structure table (near/next/far, with basis vs spot) + a near-month daily-close Plotly chart; and two sections below that, **"5% CSP"** and **"5% CC"**, showing the actual calculation for the selected symbol rather than just the final Dashboard-column percentage. (There used to be a classic CE | Strike | PE option chain table between the futures chart and these two sections -- it was dropped on request; `chain_rows`/`option_chain_summary` are still fetched/used for the summary tiles and CSP's near-expiry row, but the pivoted per-strike display itself, and the now-unused `fo_service.shape_option_chain` pivot helper + its tests, were removed rather than left as dead code.):
  - **5% CSP** is a **near/next/far month table** (`fo_service.csp_5pct_for_rows`, one call per expiry — the same term-structure shape the Futures section above already uses), columns Term / Expiry / Spot / Strike / Put Premium / **Trade Date** / 5% CSP. The near row reuses the already-fetched `chain_rows` when the expiry selector above happens to be on the near expiry; next/far are fetched separately via `fo_repo.get_option_chain`. The Trade Date column is what actually surfaces a stale quote to the user — see `_freshest_rows`'s docstring above for why a strike's "latest" row can silently be weeks old.
  - **5% CC** is also a **near/next/far month table** (`fo_service.cc_5pct_for_rows`, one call per expiry, mirroring 5% CSP's own loop exactly), columns Term / Expiry / Strike (lowest ≥5% above spot) / Premium / Trade Date / **Net Investment** / 5% CC / **Assignment Profit** (`(strike / net_investment - 1) * 100`, `None`/"N/A" if `net_investment` is zero or negative -- premium ≥ spot). This originally only showed the nearest expiry as a stat-grid breakdown (via `fo_service.cc_5pct_map`, which itself just restricts to the nearest expiry and delegates to `cc_5pct_for_rows`) -- changed on request to match 5% CSP's table shape once a live user actually wanted to see next/far month CC yields too, not just the near month `dashboard_fo_metrics` already caches for the Dashboard. "Net Investment" and "Assignment Profit" only appear here, not on the Dashboard, which only ever caches/displays `cc_pct`.
  Both loops share one `_chain_rows_for(exp)` helper (reuses the already-fetched `chain_rows` when `exp` happens to be the expiry selected above, otherwise fetches that expiry's chain separately) and both use the cash-market spot for every expiry, not just the near one -- so **every** row of both tables, not just the near-month one, matches what the Dashboard would compute for that same expiry. Shaping is done by `fo_service.option_chain_summary`/`futures_term_structure`/`csp_5pct_for_rows`/`cc_5pct_for_rows`, not in the page.
  - **Portfolio CC** -- a third near/next/far table, shown *only* when the signed-in user actually holds this stock in at least one of their own saved portfolios (`portfolio_repo.list_holdings(client, user_id)`, filtered to this symbol; silently absent otherwise, unlike 5% CSP/CC above which always render). Reuses the exact same per-holding formula behind the Portfolio page's own "CC ROI"/"Assignment ROI" columns (`fo_service.covered_call_for_holding` -- avg-buy-price-vs-LTP-dependent target, nearest-strike, not 5% CC's fixed-5%-OTM floor filter), so the numbers here always agree with that page for this exact stock. If the same portfolio name holds this symbol across multiple brokers, `portfolio_service.merge_holdings` combines them into one row first (same as the Portfolio page); if the stock is held in more than one *named* portfolio, one table renders per portfolio (each with its own qty/avg price subheading), since different portfolios can have different cost bases and thus different target strikes. Columns: Term / Expiry / Strike / Premium / Trade Date / Invested Amount / CC ROI / Assignment ROI.

  **A real bug found here, right after this section first shipped**: the CSP/CC breakdown's spot value (CC was still "ITM PMCC" at the time, but the bug and fix applied identically) was initially taken from `option_chain_summary(near_chain_rows)["spot"]` — the F&O bhavcopy's own `underlying_price` column — while the Dashboard's two columns (now the `dashboard_fo_metrics` cache, see above) use the cash-market `latest_price` from `latest_screener_view`. These two prices aren't the same value, so this page's numbers didn't match the Dashboard's for the same stock (confirmed live: ADANIENT showed 5% CSP = 0.54% on the Dashboard but 0.45% here, since a different spot picked a different nearest-5%-below strike, 3040 vs 3020). Fixed by fetching `snapshot_repo.get_latest_screener_row(client, symbol).latest_price` and using that as the spot for both calculations here too, instead of the chain's `underlying_price` — the top-of-page "Spot"/"ATM strike" summary tiles are unaffected and deliberately still use the chain's own `underlying_price` (correct for highlighting the ATM row in the actual option-chain data being displayed there). If you add another F&O-derived calculation to either screen, source spot the same way this one now does — from the screener, not the chain — to keep the two screens' numbers in agreement.

- **`6_Portfolio.py`** — see the dedicated Portfolio section below for the full upload → match → save → refresh-registration pipeline, and for the multiple-coexisting-portfolios design (`portfolio_name`, migration `0014`). On the page itself: `_load_holdings` reads every one of the signed-in user's saved rows across every portfolio and broker; one `st.tabs` entry is rendered per distinct `portfolio_name`, each scoping `portfolio_service.merge_holdings`/`compute_portfolio_view` (LTP via `snapshot_repo.get_latest_prices`, a direct `daily_screener_snapshots` query, deliberately **not** `latest_screener_view` — see below for why) to just that portfolio's own rows. The holdings table is a plain `st.dataframe` — see below for why, and for how row selection replaced the per-row 🔍 button.

## Portfolio

`pages/6_Portfolio.py` shows the signed-in user's own broker holdings
(not the Nifty50 screener universe), valued live against the app's own
market data. This is a separate, self-contained subsystem from the
screener, similar in spirit to the F&O section above: its own table, its
own service module, its own refresh-pipeline hook — nothing here changes
`nifty50_constituents`, `latest_screener_view`, or any existing page.

**Schema (migrations `0012_portfolio_holdings.sql` and
`0014_portfolio_holdings_multi_portfolio.sql`)**: one table,
`portfolio_holdings`, RLS-scoped to `auth.uid() = user_id` like every
other per-user table. `symbol` is nullable and **deliberately not FK'd to
`companies`** — at upload time a correctly-resolved symbol (an ETF, a
fund, a non-Nifty50 stock) may not have a `companies` row yet at all;
forcing the FK would make saving a freshly-uploaded portfolio fail until
some *other* process happened to register that symbol first.

**Multiple portfolios per user, all coexisting** (`0014` — a real request
after `0012` shipped: the first cut only ever supported one implicit
portfolio per user, sync-only). `0014` adds `portfolio_name text not null
default 'Portfolio 1'` (the default backfills existing rows so they stay
visible under a real tab post-migration) and widens the primary key from
`(user_id, broker, raw_name)` to `(user_id, portfolio_name, broker,
raw_name)` — the same broker + raw instrument name can now be saved once
per distinct portfolio without colliding. `PortfolioHolding` gained a
required `portfolio_name: str` field to match; a deployment that's
applied `0012` but not yet `0014` fails **every** row with a Pydantic
`ValidationError` (`portfolio_name` missing), not a `postgrest.APIError`
— `pages/6_Portfolio.py` catches both around `_load_holdings` and shows
one combined "apply 0012 and 0014, in that order" message (confirmed live
against the deployed project, which had `0012` but not yet `0014` at the
time this shipped: `list_holdings` raised exactly this `ValidationError`,
not an `APIError`).

**One repo function serves both "update an existing portfolio" and
"create a new one"**: `portfolio_repo.replace_broker_holdings(client,
user_id, portfolio_name, broker, holdings)` is a delete-then-insert
scoped to `(user_id, portfolio_name, broker)` — full sync, not a merge,
so a position no longer in the file disappears rather than lingering.
For a `portfolio_name` that's never been used before, the delete simply
matches nothing (no rows to remove), so calling this with a brand-new
name *is* how a new portfolio gets created — there's no separate
"replace everything" function, and deliberately isn't one anymore: an
earlier version of this feature had the "new portfolio" flow wipe the
user's *entire* portfolio (every broker) before inserting the fresh
upload, which was corrected on request — creating a new portfolio must
never touch any existing one.

`pages/6_Portfolio.py` renders one `st.tabs` entry per distinct
`portfolio_name` the user has (`sorted({h.portfolio_name for h in
saved_holdings})`), each showing that portfolio's own holdings table
(`_render_portfolio_tab`) plus its own upload section scoped to that
`portfolio_name` — uploading a broker's file there only ever calls
`replace_broker_holdings` for *this* `(portfolio_name, broker)` pair, so
every other tab is untouched no matter what you upload. A permanent
"+ New portfolio" tab at the end takes a portfolio name (`st.text_input`,
defaulting to `f"Portfolio {len(portfolio_names) + 1}"` if left blank)
and a broker; it refuses to proceed (shows `st.error`, no uploader) if
the name collides with an existing tab, to avoid silently merging into
what the user probably meant as a distinct portfolio. When the user has
no portfolios at all yet, there's nothing to make tabs out of, so the
page skips `st.tabs` entirely and renders the same name+broker+uploader
creation flow directly. Both call sites share one `_render_upload_section()`
helper for the parse → preview → manual-symbol-form → save sequence
(parameterized by `portfolio_name`/`broker`/`key_prefix`/`save_label`, so
the ~50 lines of shared logic isn't duplicated three times).

**Opening the tab you just acted on** (`st.tabs(..., key="portfolio_active_tab",
on_change="rerun")`): by default `st.tabs()` doesn't expose which tab is
selected to server-side code at all, and a plain `st.rerun()` (e.g. right
after a save) reset the view back to the first tab regardless of which
one the user had open or had just created — confirmed live: creating
"Dhan Corporate" while "Zerodha Personal" already existed landed back on
whichever tab was first alphabetically, not the new one. `key=` +
`on_change="rerun"` (added to `st.tabs()` in a Streamlit release recent
enough that it wasn't available when this page was first written --
worth checking `st.tabs()`'s own signature if this ever looks unsupported
again) makes Streamlit track the active tab through
`st.session_state["portfolio_active_tab"]`.

**A second real bug, found immediately after the first fix**: the "+ New
portfolio" save path's `on_saved` callback originally wrote
`st.session_state["portfolio_active_tab"] = created_name` directly —
which raised `StreamlitAPIException: st.session_state.portfolio_active_tab
cannot be modified after the widget with key portfolio_active_tab is
instantiated` (confirmed live: creating "Portfolio 2" saved successfully,
then crashed the very next line). The cause: `on_saved` runs from
*inside* one of the tabs' content, which only executes *after*
`st.tabs(..., key="portfolio_active_tab")` already ran earlier in that
same script execution — Streamlit forbids writing to a widget's own
`session_state` key once that widget has been instantiated in the
current run, full stop, even from code that logically "belongs" to one
of its children. The fix: never write `"portfolio_active_tab"` directly
from inside a tab. Instead, stash the request in a plain (non-widget)
`"portfolio_pending_active_tab"` key and call `st.rerun()`; right before
`st.tabs(...)` is instantiated on the *next* run — i.e. before it exists
for that run — a short block pops the pending key and promotes it into
`"portfolio_active_tab"` (empty string `""` means "clear", used by the
delete path so a deleted portfolio's now-stale name doesn't linger and
`st.tabs()` falls back to the first remaining tab; any other string
means "select this one"). Reproduced and confirmed both the crash (a
direct write) and the fix (the pending-key indirection) in an isolated
scratch script before shipping, mirroring exactly what the real page
does. One side effect worth knowing about the `on_change="rerun"` choice
itself: it also opts into lazy per-tab execution (a plain round trip on
every tab click, and only the open tab's own code actually runs) instead
of every tab's content always computing regardless of visibility -- a
reasonable trade since this page
already reruns on every other interaction anyway, and it avoids
computing every portfolio's LTP lookups on every load.

**A real bug found here**: right after successfully creating a portfolio
from the "+ New portfolio" tab, that same tab immediately (and on the
face of it, correctly) flagged the name it had *just* created as already
existing — confirmed live: creating "Dhan Corporate" succeeded (the tab
appeared), but the "+ New portfolio" tab then showed `A portfolio named
"Dhan Corporate" already exists`. The cause: `st.text_input("Portfolio
name", key="portfolio_new_name", ...)` keeps its typed value in
`st.session_state` across Streamlit's `st.rerun()`, so after the save the
widget still held the name just used, and `portfolio_names` on the next
render now legitimately included it too — the collision check
(correctly!) matched. Fixed with an optional `on_saved` callback on
`_render_upload_section()`, run right before its `st.rerun()`; the "+ New
portfolio" call site passes `on_saved=lambda:
st.session_state.pop("portfolio_new_name", None)`, so the field resets to
blank on the next render instead of re-showing what was just created. The
`_render_portfolio_tab()`/first-portfolio call sites don't need this —
their name isn't user-editable text that could re-collide with itself.

**Deleting a portfolio** (`portfolio_repo.delete_portfolio(client,
user_id, portfolio_name)`) — an unconditional delete of every row for
`(user_id, portfolio_name)`, every broker within it, leaving every other
portfolio untouched. Rendered inside each tab as a collapsed
`st.expander("🗑️ Delete \"<name>\"")` at the very bottom, below the
upload section, so it's out of the way of the normal update flow: a
warning, an `st.checkbox` the user must tick ("I understand -- permanently
delete ..."), and an `st.button(..., disabled=not confirm)` that only
becomes clickable once that box is checked -- a deliberate two-step
confirmation, since there's no undo for this one.

**Two broker CSV formats, one broker-agnostic shape after parsing**
(`src/services/portfolio_service.py`):
- **Zerodha** (`parse_zerodha_csv`) — the `Instrument` column is already
  the exact NSE trading symbol, so it's trusted directly with no name
  matching at all.
- **Dhan** (`parse_dhan_csv`) — the `Name` column is a free-text company
  name, and numbers are quoted with Indian-style grouping (e.g.
  `"6,42,438.40"`), handled by the same tolerant `_to_float` pattern
  `scripts/import_screener_csv.py` already established (strip
  `,`/`%`/quotes, treat `-`/`NA`/`""` as missing rather than zero).
  Symbol resolution goes through `match_symbol(raw_name, companies)`:
  both the raw name and every known `companies.name` are normalized
  (uppercase, strip non-alphanumerics, strip a trailing `LTD`/`LIMITED`),
  and a match requires exactly one company whose normalized name
  contains (or is contained by) the normalized raw name — zero or
  ambiguous matches are left unresolved (`symbol = None`) rather than
  guessed. The Portfolio page lets the user type the correct symbol in
  for any unresolved row before saving.

Both parsers deliberately **ignore the file's own LTP/Cur. val/P&L
columns** — those are always recomputed live from the app's own data, per
the feature's original spec, never trusted from the export.

**Valuation** (`compute_portfolio_view`): `cur_val = qty * ltp`,
`pnl = cur_val - investment`, `pnl_pct = pnl / investment * 100`, all
`None` (→ "N/A" in the UI) when `ltp` is unavailable — either because the
row's symbol is still unresolved, or because it's resolved but the app
has no market data for it yet. Portfolio-level totals sum `investment`
across every row but sum `cur_val`/`pnl`/`pnl_pct` only over rows with a
known LTP, with an `unpriced_count` the page uses to caption a partial
total rather than silently showing a number that excludes some holdings.

**Why LTP is read via `snapshot_repo.get_latest_prices`, not
`latest_screener_view`**: at the time this page was built, the view
(`0004_fix_constituents_fk_and_view_defaults.sql`) inner-joined
`nifty50_constituents.is_current`, so it would have silently excluded
any portfolio-only symbol (an ETF/fund or non-Nifty50 stock) even after
that symbol had been registered and priced. `get_latest_prices` instead
queries `daily_screener_snapshots` directly for exactly the symbols
asked for, keeping the newest *priced* row per symbol in Python.
`0013_screener_fallback_and_portfolio_symbols.sql` later taught
`latest_screener_view` itself to include the viewing user's portfolio
symbols too (see below) and to do the same "fall back to the last row
with a real price" logic — but this page still uses its own
`get_latest_prices` rather than switching to the view, since it needs
only `symbol`/`latest_price` for a caller-supplied symbol list (not a
join against `companies`/`nifty50_constituents` for every column the
Dashboard needs), and doesn't need the view's per-user `auth.uid()`
scoping since the caller already knows exactly which symbols it's
pricing. `daily_screener_snapshots`'s own RLS policy (`authenticated
read ... using (true)`, `0002_rls_policies.sql`) has no gate at the
table level either way.

**Getting a new symbol tracked** (the "N/A until the next refresh" flow):
neither `scripts/run_refresh.py` nor the `manual-refresh` Edge Function
originally looked past `nifty50_constituents.is_current` when building
their symbol universe — the Streamlit page's own client can't fix this
itself, since `companies`' RLS policy is `authenticated`-**read-only**
(`0002_rls_policies.sql`); only the service-role key these two refresh
paths already hold can insert into it. Both were extended identically:
read the distinct `(symbol, raw_name)` pairs across **every** user's
`portfolio_holdings`, diff against `companies` via the pure
`resolve_tracked_symbols` (Python) / `resolveTrackedSymbols` (TypeScript,
`supabase/functions/_shared/portfolioSymbols.ts` — same
dual-implementation tradeoff as `dashboardMetrics.ts`/`calculations.ts`,
for the same reason: the Edge Function can't call the Python code),
upsert a minimal `companies` row (symbol + best-known name) for anything
new, then union those symbols into the existing Nifty50 symbol list
before the normal intraday/EOD/fundamentals/screener fetch loops — no
other change to those loops, which already tolerate sparse fundamentals
(ETFs/funds lack PE/EPS) via existing `is_stale`/`data_quality` handling.
`nifty50_constituents` itself is **never** written by this path, so
portfolio-only symbols get `companies` + `daily_screener_snapshots` rows
without ever becoming an official constituent. Both paths are tolerant
of the `portfolio_holdings` migration not being applied yet
(catch-and-skip, same degrade-gracefully precedent as the F&O cache
recompute).

**Screener fallback (`0013_screener_fallback_and_portfolio_symbols.sql`)**:
shipping the above surfaced two real bugs once actually used against a
live account. First, `latest_screener_view`'s lateral join always took
the single most recent `daily_screener_snapshots` row per symbol, even
on a day its price fetch failed (`latest_price` null, `status =
'unavailable'`) — so a stock priced fine yesterday showed blank "--"
on the Dashboard today instead of its last known value, even though
`get_latest_prices` (above) already had the more resilient "most recent
row that actually has a price" behavior for the Portfolio page. Second,
because `nifty50_constituents` is deliberately never written for
portfolio-only symbols, they never appeared on the Dashboard at all —
which is what the original design intended, but the user explicitly
asked for uploaded non-Nifty50
holdings like Hindustan Zinc/IndusInd Bank to show up on the Dashboard
screener too, once tracked. `0013` fixes both in the view itself: the
lateral join now orders by `(latest_price is not null) desc,
snapshot_date desc` instead of just `snapshot_date desc`, and the join
from `companies` to `nifty50_constituents` changed from an inner join to
`left join ... where nc.is_current or exists (select 1 from
portfolio_holdings where symbol = c.symbol and user_id = auth.uid())` —
`security_invoker = true` on the view means `auth.uid()` resolves to
the actual querying user, so each user only ever sees their *own*
uploaded symbols added to their Dashboard, on top of the shared 50
Nifty50 constituents everyone sees. One consequence: "Total stocks" /
"Screener (X of Y stocks)" on the Dashboard is no longer necessarily 50
— both already read `len(df)`/`len(filtered)` dynamically
(`pages/1_Dashboard.py`), so no page code changed, only the view.

**A follow-up problem `0013` itself introduced**: it puts *every*
tracked portfolio symbol on the Dashboard, including ETFs and gilt/liquid
funds (NIFTYBEES, GILT5YBEES, LIQUIDCASE, LTGILTCASE) -- a
momentum/dividend/PEG stock screener doesn't make sense for these (PE,
PEG, and dividend criteria are all meaningless for a fund), and a live
user asked for them to stay off this specific list (Stock
Detail/Options/Portfolio still show them fine; this is a
Dashboard-screener-only exclusion). Fixed in
`0015_add_is_etf_to_companies.sql`: a new `companies.is_etf boolean not
null default false` column, filtered out of `latest_screener_view` with
a plain `where not c.is_etf` (Nifty50 constituents are always real
stocks, so this never affects them).

The hard part was classification, not the column. yfinance's own
`quoteType` field is **unreliable for Indian-listed ETFs**: checked live
against every ETF/fund this app tracks and Yahoo returns `"EQUITY"` for
all of them (confirmed for NIFTYBEES, GILT5YBEES, LIQUIDCASE,
LTGILTCASE -- a real Yahoo data-quality quirk for this market, not
something wrong on this app's side). The real display name is the
reliable signal instead -- every one of those four literally contains
"ETF" (e.g. yfinance's `longName` for NIFTYBEES is "Nippon India ETF
Nifty 50 BeES") -- so `portfolio_service.looks_like_etf_name(name)` is
just `"etf" in name.lower()`, checked against the real display name, not
`companies.name` (which for a portfolio-only symbol is often just the
raw ticker itself -- Zerodha's CSV export uses the exact NSE symbol as
its own "Instrument" field, with no separate display name available).
`src/data_providers/yfinance_provider.py::fetch_display_name(symbol)` is
a one-off, best-effort `longName`/`shortName` lookup (returns `None` on
any failure rather than raising) used only at company-registration time
-- deliberately **not** added to the `FundamentalsDataProvider` ABC,
since that interface is the per-refresh PE/PEG/EPS/market-cap contract
every provider (Dhan, manual, mock, yfinance) implements, and this is a
one-off lookup only the yfinance-backed registration path needs.

Both Python registration paths (`scripts/run_refresh.py`,
`scripts/fetch_fo_data.py` -- they run independently on cron, so either
could register a given symbol first) call `fetch_display_name` +
`looks_like_etf_name` for each brand-new symbol from
`resolve_tracked_symbols` before upserting, same TS mirror
(`fetchDisplayName` in `manual-refresh/yahoo.ts`, `looksLikeEtfName` in
`_shared/portfolioSymbols.ts`) wired into `manual-refresh/index.ts`'s
own portfolio-widening block. `resolve_tracked_symbols`/
`resolveTrackedSymbols` themselves stay pure, network-free diffs, same
as before -- classification is entirely the caller's job, applied to the
`Company`/`NewCompany` objects just before the upsert. **One known gap**:
`fo-refresh/index.ts` also registers new portfolio symbols (for F&O
universe widening) but has no Yahoo Finance access at all, so it can't
classify there -- a symbol this path registers first stays `is_etf =
false` until one of the other three paths next sees it. Accepted as
narrow in practice: real ETFs/funds don't have listed derivatives, so
this path being the *first* to register one would be unusual. Migration
`0015` also backfills the four ETFs already tracked at the time it was
written, verified via a live yfinance `longName` check against every
non-Nifty50 symbol tracked at the time (all four straightforwardly
matched; HINDZINC/INDUSINDBK/INDHOTEL/VAML correctly did not).

Once the fallback could silently show a stale price, the Dashboard
needed a way to say so: `pages/1_Dashboard.py` computes
`df["snapshot_date"].max()` once per load (the newest date *any* symbol
in the batch actually got refreshed to) and, per row, compares that
against the row's own `snapshot_date`. A row that falls short gets a
plain `" (as of <date>)"` suffix appended to its `"LTP"` cell string.
(This used to be `<br>` + a raw-HTML muted `<span>` via
`render_muted_note()`, back when the table itself was hand-rendered HTML
that could embed arbitrary markup per cell -- now that the table is a
plain `st.dataframe`, which displays cell values as literal text, the
note had to become plain text too; `render_muted_note()` had no other
caller left at that point, so it was deleted along with
`render_screener_table()` itself -- see the Pages section's
`1_Dashboard.py` bullet above for the full story.) Guarded on
`pd.notna(latest_price)` too, so a symbol with no price at all
(genuinely never fetched) still just shows "—", never a dangling "as
of" with nothing to date.

**Per-holding covered-call suggestion ("CC ROI" / "Assignment ROI"
columns)**: distinct from the Dashboard/Options "5% CC" figure
(`cc_5pct_for_rows`, always spot-based, fixed 5% OTM, floor-filtered
strike) -- this one is `fo_service.covered_call_for_holding(ce_rows,
avg_price, ltp, qty, expiry_date)`, keyed off the *holding's own* avg
buy price, not just the spot price:

- If `avg_price > ltp` (a loss so far), the target is 3% above
  `avg_price` -- writing a call struck near the original cost basis
  rather than the current (lower) price, so it isn't implicitly locking
  in the loss at assignment. Otherwise (`ltp >= avg_price`), the target
  is 5% above `ltp`, matching the app's usual 5% OTM convention.
- The strike is the single one *nearest* that target (either side), not
  floor-filtered like the Dashboard/Options "5% CC" -- "about 3%/5%
  above" here is an approximate target, not a floor the strike must
  clear.
- `invested_amount` = `avg_price * qty` (the real, full cost basis) but
  `premium_collected` = premium per share × `lot_size` (a covered call is
  written per-lot, not scaled to however many shares are actually held)
  -- these two deliberately use different scales. `cc_roi_pct` =
  `premium_collected / invested_amount * 100`. `assignment_roi_pct` =
  `(premium_collected + strike * qty - invested_amount) / invested_amount
  * 100` -- the total return if the *entire* position (not just the 1
  lot the premium came from) were closed out at the strike, plus that
  lot's premium, relative to the original cost basis.
- Returns `None` (→ "N/A" in the UI) if `qty` is 0, `avg_price`/`ltp`
  isn't a positive number (an unpriced holding has no LTP to compare
  against), or there's no priceable CE strike at all (no F&O for that
  symbol -- ETFs/funds, or a non-Nifty50 stock with no listed
  derivatives, like VAML).

`pages/6_Portfolio.py` renders one shared "Covered call expiry" selectbox
above the tabs, applying uniformly across every portfolio. Its options
are real expiry dates, not fixed "Near/Next/Far" labels: it unions
`fo_repo.list_option_expiries(client, symbol)` across every symbol held
anywhere in the account, sorts the distinct dates, and keeps the nearest
3 -- formatted `%b %Y` (e.g. "Jul 2026") the same way the Dashboard's
"Options month" selectbox already does, so the choices always reflect
whatever NSE's current monthly contracts actually are instead of drifting
out of sync as months roll over. The selected date is matched against
each row's *own* expiry list by actual date (not position index) before
`fo_repo.get_option_chain(client, symbol, expiry_date)` supplies the CE
rows `covered_call_for_holding` needs -- a symbol missing a contract for
that exact date (or with no F&O data at all) just gets "N/A" for that
row. Both lookups are wrapped in `_load_covered_calls`, cached via
`st.cache_data` and tolerant of `APIError` (F&O tables not migrated yet
degrades the whole feature to "N/A" rather than crashing the page) the
same way every other Portfolio-page query already is.

**The holdings table itself is a plain `st.dataframe`**, same as the
Dashboard's screener table -- see the Pages section's `1_Dashboard.py`
bullet above for the full history of why (a hand-rendered HTML table's
headers can only ever show a ▲/▼ arrow next to a separate
`st.selectbox`("Sort By")/`st.checkbox`("Descending") pair, since a real
`<a href="?sort=...">` sort link would force a browser navigation, which
logs the user out under this app's `st.session_state`-only auth -- see
[Auth](#auth-a-non-obvious-quirk)). A native `st.dataframe` sidesteps
this entirely: Streamlit's own frontend handles header-click sorting
client-side, no Python round-trip needed, exactly like the Options
screen's Futures term-structure table (`pages/5_Options.py`) already
does.

That native client-side sort is why the per-row 🔍 "open in Stock
Detail" button (a real `st.button()` beside the table, the same pattern
the Dashboard used to use too) had to go here as well: those buttons are
positioned by their *pre-sort* Python row index, so clicking a header to
reorder the table in the browser would leave them pointing at the wrong
row once the visual order no longer matches Python's. Row selection
(`on_select="rerun"`, `selection_mode="single-row"`) replaces it instead
-- Streamlit maps a click back to the correct index in the original
(pre-sort) data regardless of how the table is currently sorted, so
`rows[selected_rows[0]]` always resolves to the row the user actually
clicked. Selecting a row reveals two buttons below the table -- "Open
`<symbol>` in Stock Detail" and "Open `<symbol>` in Options" (the latter
sets `st.session_state["fo_symbol"]`, the same key the Options screen's
own symbol selector reads) -- both gated on `symbol` being resolved,
with a caption instead for an unresolved (unmatched-name) row. No
separate constituent check needed for either: every resolved row here is
by construction one of *this* signed-in user's own portfolio symbols, and
both Stock Detail's and Options' own pickers union in exactly that set
(see their bullets under "Pages" above), so it's always selectable on
both pages.

This was verified against real production data via Streamlit's
`AppTest` harness rather than a live browser click: this sandbox's
browser tab reports `document.visibilityState === "hidden"`, and
`st.dataframe`'s grid (glide-data-grid, canvas-based) never mounts a
canvas at all on a hidden tab, so no click can land on it. `AppTest` sets
`st.session_state["portfolio_table_<slug>"] = {"selection": {"rows": [i],
...}}` directly (the same schema a real click produces) and reruns the
script -- confirming `rows[i]["symbol"]` resolves to the right stock and
the right button renders, without needing the canvas to paint.

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
- **`ui.py`** — shared fragments: `status_badge()` (colored HTML span with text, e.g. Stock Detail's header), `market_state_label()`, `buy_sell_label()` (Green→"Model Buy Watch" etc., per the spec's no-guarantee wording), `render_disclaimer()`, `plotly_template()`, `inject_tailwind()`, plus the design-system layer described below: `ACCENT` (the "Classic Institutional" slate/navy palette constants), `inject_global_styles()`/`inject_design_system()`, `_surface_classes()`, `render_card()`, `render_pill()`, `render_stat_tile()`/`render_stat_grid()`, `render_alert_row()`.
- **`logging.py`** — `get_logger(name)`, configures `logging.basicConfig` once from `Settings.log_level`.

**Tailwind CSS — how it's actually wired in, and why not the obvious way.** Streamlit renders its own native widgets (buttons, inputs, `st.dataframe`, columns, sidebar) through its own internal React components with no supported hook for external CSS frameworks to target them — Tailwind only styles HTML we hand-render ourselves via `st.markdown(html, unsafe_allow_html=True)` (the disclaimer banner, the design-system components below). Within that scope, there's a second, less obvious trap: Tailwind's current CDN distribution (the "Play CDN") is a `<script>` that scans the DOM at runtime and injects styles as it goes — but `st.markdown(unsafe_allow_html=True)` inserts HTML via `innerHTML`, and browsers never execute `<script>` tags inserted that way (a standard, deliberate DOM security behavior, not a Streamlit quirk). Loading the Play CDN script this way silently does nothing; there's no error, the styles just never apply. `inject_tailwind()` in `ui.py` instead loads the older, fully-precompiled Tailwind **v2** static stylesheet via a `<link rel="stylesheet">` tag, which — unlike `<script>` — *is* honored via `innerHTML`. Call it once near the top of any page before rendering Tailwind-classed HTML (every page already does).

**Historical note, in case a future table goes back to hand-rendered HTML**: the Dashboard's screener table and the Portfolio page's holdings table both used to be hand-rendered this way (`render_screener_table()`, since removed as dead code once both pages moved to a native `st.dataframe` -- see the Pages section's `1_Dashboard.py`/`6_Portfolio.py` bullets above for why). It rendered the same data twice into one HTML blob — a normal `<table>` wrapped `hidden md:block` (visible only ≥768px) and a stacked list of cards wrapped `md:hidden` (visible only below that) — a pure-CSS responsive switch, no JS, fixing a real mobile problem the previous plain `df.to_html()` table had (no responsive handling at all, overflowing or squeezing unreadably on a phone). Because the static Tailwind v2 build has no `dark:` variant available, its light/dark table colors were chosen explicitly in Python (a small `theme`-branching helper, the same pattern `_surface_classes()` below still uses) from the same `user_settings.theme` that already drives `plotly_template()`. If a future table needs this dual-block technique again (`st.dataframe` can't do it -- see the note on Alerts' notification history below), this is the pattern to reach for.

**The design system — combining Tailwind with a global CSS override for native widgets.** Until this pass, Tailwind reached exactly one surface in the whole app: the Dashboard's screener table. Every other screen (landing page, login/signup/forgot-password, Stock Detail, Alerts, Settings) was 100% unstyled native Streamlit, since `inject_tailwind()` was called on every page but nothing on those pages actually used a Tailwind class. Tailwind *can't* reach native widgets at all (buttons, inputs, forms, sidebar, tabs, `st.metric`, `st.dataframe`, `st.expander` are React components Streamlit renders itself, with no exposed hook for an external CSS framework) — a Tailwind `<div>` can never wrap a native `st.button`/`st.form`, since hand-rendered HTML and native widgets are DOM siblings, not parent/child (each Streamlit element call appends its own separate node; one `st.markdown()` call's HTML can't "contain" a later `st.button()` call's output).

The fix is a second, complementary mechanism: `inject_global_styles(theme)` injects a global `<style>` block (plain CSS, not Tailwind classes) that reskins native widgets — border-radius, colors, focus states — using the same `ACCENT` palette Tailwind-classed HTML uses (`ACCENT[900]` == Tailwind's own `slate-900` hex value, so `bg-slate-900` and `var(--accent-900)` are visually identical from one source of truth). `inject_design_system(theme)` calls both `inject_tailwind()` and `inject_global_styles(theme)` together and is what every page actually calls now (via `require_login()`, plus each page re-injecting with its own loaded `user_settings.theme` right after — see the Auth section above for why `require_login()` is the enforcement point).

**The palette itself: "Classic Institutional."** `ACCENT` is Tailwind's `slate` scale (50→900), used as the app's one dominant/branding color — `kind="primary"` buttons and selected tabs/headings use `slate-900` (`#0F172A`), `kind="secondary"` buttons use a filled `slate-100`/`slate-800` pairing, cards are pure white on a `slate-50` page background (`.streamlit/config.toml`'s `backgroundColor`), and borders/dividers are `slate-200` throughout — deliberately flat and undersaturated, avoiding the bright/neon look a consumer app might use. `render_pill(text, tone, theme)` has four tones: `"accent"` (slate, for neutral branding-adjacent labels like alert types), `"neutral"` (gray, e.g. Settings' "coming soon" badges), and `"positive"`/`"negative"` (emerald/red, reserved *exclusively* for financial gains/losses and destructive actions — never used for branding or a primary CTA, per the palette's own rule). `STATUS_STYLE` (the Green/Amber/Red/Unavailable buy-signal badges) independently uses `emerald-600`/`amber-500`/`red-600`/`slate-400` hex values for the same reason — those colors are domain-meaningful classification, never touched by the `ACCENT` swap above, but chosen to be visually consistent with the same institutional palette. In dark mode, the primary/secondary button pairing inverts (a light `slate-100` button reads as "primary" against the dark `slate-900` page background) since a `slate-900`-on-`slate-900` button would have no contrast — see `_GLOBAL_CSS_DARK`'s `button[kind="primary"]` rule.

Every CSS selector in `_GLOBAL_CSS_LIGHT`/`_GLOBAL_CSS_DARK` is `data-testid`/ARIA-role/`kind`-attribute based (`[data-testid="stForm"]`, `button[kind="primary"]`, `[data-testid="stTab"][aria-selected="true"]`, etc.), confirmed via live DOM inspection against the actually-installed Streamlit version (1.59.1) at implementation time — **never** target Streamlit's own `st-emotion-cache-*` class names, which are content-hashed and change across builds/versions; testids and ARIA attributes are the only part of Streamlit's generated markup that's stable to target. If you bump Streamlit's version and native widgets stop looking styled, re-verify these selectors the same way (a scratch script + browser devtools `[data-testid]` inspection) rather than guessing.

The dark branch additionally overrides `[data-testid="stAppViewContainer"]`/`stMain`/`stHeader`/`stSidebar` backgrounds, since `.streamlit/config.toml`'s `[theme]` section (added alongside this, for Streamlit's own officially-supported BaseWeb theming — focus rings, checkbox tick color, `kind="primary"` buttons) can only express one static base theme (`light`); without the dark CSS branch also recoloring those top-level containers, "dark" would leave dark-styled widgets floating on Streamlit's own light page background. `[client] toolbarMode = "minimal"` in that same file hides Streamlit's own built-in theme picker, so there's exactly one theme control in the app (Settings → Chart theme), not two competing ones.

New reusable Tailwind-HTML components in `ui.py`, all following the same explicit-branch-on-`Theme` pattern (`_surface_classes(theme)`, since the static v2 build has no `dark:` variant to rely on): `render_card(inner_html, theme)` — bordered/padded/shadowed wrapper for **static content only**, per the DOM-siblings constraint above; `render_pill(text, tone, theme)` — small badge, used for alert-type labels and Settings' "coming soon" tags; `render_stat_tile()`/`render_stat_grid()` — responsive (`grid-cols-1 md:grid-cols-N`) stat cards, replacing Stock Detail's previously-stacked-markdown Fundamentals column; `render_alert_row()` — formatted alert summary (pill + `summarize_alert_config()` text), replacing the raw dict dump on both Stock Detail and Alerts.

**The join-bug rule applies to every one of these**: joining multi-line indented f-string fragments leaves a whitespace-only line between them, which Streamlit's markdown parser treats as ending the current HTML block — every `render_*` function returns a single continuous-line string, never a multi-line indented literal, and this must hold for any future addition too. The one deliberate exception is the CSS `<style>` block itself: `<style>`/`<script>`/`<pre>` are CommonMark "HTML block type 1," terminated only by their closing tag, not by blank lines — so `_GLOBAL_CSS_LIGHT`/`_GLOBAL_CSS_DARK` are safe to write as ordinary multi-line triple-quoted strings, same as `inject_tailwind()`'s single `<link>` call always was.

**Notification history (`3_Alerts.py`) deliberately stays `st.dataframe`-only**, with no Tailwind mobile-card alternative -- same as every other table in the app now (Dashboard, Portfolio, Options' Futures table). The dual-block hand-rendered-HTML technique described above only ever worked because both the table and the card list were Tailwind `<div>`s the code fully controlled and could tag with `hidden md:block`/`md:hidden`; `st.dataframe` is one opaque native React subtree with no reliable way to attach a scoped class to just that one call without brittle DOM-adjacency assumptions that could break on a future Streamlit version. `st.dataframe` already has native horizontal scroll — an acceptable, if not ideal, mobile experience.

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
  any — both are unofficial). `fetchDisplayName()` (migration `0015`)
  requests the `price` module of the same `quoteSummary` endpoint for a
  symbol's real `longName`/`shortName` — used only once, when
  `index.ts`'s portfolio-widening block below registers a brand-new
  symbol, to classify it as an ETF/fund (see the Futures & Options
  section's ETF-exclusion paragraph). Deliberately swallows every
  failure and returns `null` rather than throwing, unlike
  `fetchFundamentals()`'s own fresh-crumb retry -- this is a one-off,
  best-effort classification, not a critical-path fetch.
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
(never `get_service_client()`), add an `st.Page("pages/N_Name.py",
title="...")` entry to `app.py`'s `pages` list (this is what actually
puts it in the sidebar now -- the file's numeric prefix no longer
matters for that, only the list's own order does).

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
