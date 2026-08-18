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
classifies each as **Green / Amber / Red / Unavailable** based on two
factors: Momentum (1/5/20-day price momentum) and Fundamentals (dividend
yield or PEG ratio clearing its threshold). Users sign in (Supabase Auth),
configure their own thresholds, set alerts, and browse per-stock detail
pages with charts.

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
  2_Stock_Detail.py               Price/volume/dividend charts, scorecard, per-stock alerts -- sidebar label "Equity"
  4_Settings.py                    Per-user thresholds, alert CRUD + notification history, theme, change password, sign out
  5_Options.py                     F&O: futures term structure + 5% CSP/CC breakdown per stock
  6_My_Broker.py                    Upload/connect Zerodha/Dhan holdings + F&O positions, create/delete portfolios
  7_My_Trades.py                     Holdings + positions grouped by underlying into Stock/Index/Other Trades
  8_My_Holdings.py                   Equity holdings, ETFs & Mutual Funds / Stocks split, identical columns
  9_My_Positions.py                  Per-leg F&O positions, split into Stock Options / Index Options / Others
  11_My_CSP.py                       Every position leg from a "CSP"-tagged Trade, + underlying LTP/1D/5D/20D
  10_Analyse_Trade.py                 One Trade's legs -- correct underlying/trade type, merge/split (hidden page)
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
  functions/fo-refresh/              Edge Function (Deno/TypeScript) behind "NSE/BSE F&O Data Refresh" (exchange param)
tests/                             Pytest suite -- almost entirely calculations/services, no network
```

## Database schema

All migrations live in `supabase/migrations/`, applied in numeric order
(`0001` → `0019`). Twenty tables, in three groups (`0008` doesn't add a
table -- it just extends `provider_fetch_log.fetch_type`'s CHECK
constraint with `'fo'`, for the `fo-refresh` Edge Function's logging;
`0010`/`0011` drop and recreate `dashboard_fo_metrics` with a different
key/columns rather than adding a new table -- see "Dashboard cache"
below; `0012` adds `portfolio_holdings`; `0013` only redefines
`latest_screener_view`, no new table; `0014` only adds a column +
widens `portfolio_holdings`' primary key, no new table; `0015` only adds
a column to `companies` (`is_etf`) + redefines `latest_screener_view`
again, no new table; `0016` adds `portfolio_positions`; `0017` adds
`broker_connections`; `0018` replaces `is_etf` with `company_type`, seeds
three `Index` rows, and redefines `latest_screener_view` a third time;
`0019` seeds a fourth `Index` row (BANKEX) -- neither adds a new table;
`0020` adds `portfolio_trade_groups`):

**Reference data** (written by `scripts/fetch_nifty50_constituents.py` /
`seed.sql`, read-only to the app):
- `nifty50_constituents` — which symbols are in the index and when (supports historical reconstitution tracking)
- `companies` — name/sector/industry per symbol, plus `company_type` (`Equity`/`ETF`/`Index`/`Fund`, migration `0018`, replacing the `is_etf` boolean from `0015`) -- see the Futures & Options section's "A follow-up problem 0013 itself introduced" paragraph for what this excludes and why

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
- `portfolio_holdings` — broker-CSV-uploaded holdings (migration `0012`, `portfolio_name` added in `0014`), keyed `(user_id, portfolio_name, broker, raw_name)` -- a user can maintain multiple independently-named portfolios that all coexist. `symbol` is nullable and deliberately **not** FK'd to `companies` -- a resolved symbol may not exist there yet (an ETF/fund or non-Nifty50 stock the screener doesn't otherwise track); see the Portfolio pages section below for how it gets registered.
- `portfolio_positions` — broker-CSV-uploaded F&O positions (migration `0016`), same keying/RLS shape as `portfolio_holdings`. `symbol`/`expiry_date`/`strike_price`/`option_type` are nullable together (an undecoded instrument format), and `qty` keeps its broker-reported sign (negative = short). See the Portfolio pages section's My Positions subsection.
- `broker_connections` — saved Dhan/Zerodha API credentials per `(user_id, portfolio_name, broker)` (migration `0017`, `api_secret` + nullable `access_token` added by `0022` for Zerodha), letting a portfolio sync holdings/positions directly from a broker's API instead of a CSV upload. Credentials are stored as entered, protected only by this table's own RLS policy -- no application-level encryption. See the Portfolio pages section's "Connect Dhan account" / "Connect Zerodha account" subsections.
- `portfolio_trade_groups` — manual "Trade" grouping overrides for one holding or F&O position leg (migration `0020`), keyed `(user_id, portfolio_name, broker, raw_name)` -- the leg's own natural identity, not a `portfolio_holdings`/`portfolio_positions` row id, so it survives those tables' delete-then-insert replace semantics. See the Portfolio pages section's My Trades subsection.
- `portfolio_trade_meta` — trade-*level* underlying-label/trade-type overrides (migration `0021`), keyed `(user_id, portfolio_name, trade_id)` -- a different grain from `portfolio_trade_groups` above (many legs share one `trade_id`). See the Portfolio pages section's My Trades subsection.

Two generated helpers, defined in `0003_views_functions.sql` (and patched
in `0004`):
- `latest_screener_view` — one joined row per current constituent (companies + its latest daily_screener_snapshot), plus (as of `0013`) every symbol the *viewing* user holds in their own `portfolio_holdings`, filtered (as of `0018`, originally `0015`'s `where not is_etf`) to `company_type = 'Equity'` -- excluding ETF/Index/Fund rows by construction rather than a separate flag per category. This is what the Dashboard queries in a single call instead of joining client-side. `0004` added `coalesce(status, 'unavailable')` / `coalesce(data_quality, '{}')` here because a constituent with no snapshot yet would otherwise return `NULL` for those columns, which fails Pydantic validation on the `ScreenerRow` model. `0006` added `week_52_high`/`week_52_low`/`criterion_52w_high`/`criterion_52w_low` — **a real deploy-time error hit here**: `create or replace view` can only *append* new output columns; inserting them positionally in the middle of the existing `select` list (as the first draft of `0006` did) makes Postgres think you're renaming the columns that got pushed down a slot, and it fails with `42P16: cannot change name of view column ... HINT: Use ALTER VIEW ... RENAME COLUMN ... instead`. The fix is to always append new columns at the very end of the `select` list in any future `create or replace view` migration, never insert them mid-list — column *order* doesn't matter to the app since every read is by name (`ScreenerRow.model_validate(dict)`), so this costs nothing. `0013` made two further changes, both **real production bugs found after the Portfolio feature shipped** (see the Portfolio pages section's "Screener fallback" note below for the full story): the per-symbol lateral join now prefers the most recent snapshot row that actually has a price (falling back across days when today's fetch failed) instead of always taking literally the latest date regardless of whether it has data, and the join went from `nifty50_constituents` inner-join to `left join ... where nc.is_current or exists (select 1 from portfolio_holdings where symbol = c.symbol and user_id = auth.uid())` — `security_invoker = true` means `auth.uid()` here is the actual querying user, so this stays correctly scoped per user (reinforced by `portfolio_holdings`' own RLS policy on top).
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
- `UserPosition` no longer has a `risk_reward_ratio` property — it was a computed `(target - entry) / (entry - stop_loss)` used only by Stock Detail's now-removed "Your position notes" form (see that page's bullet below); deleted as dead code once its one caller went away, rather than left unused.

## Calculation engine (`src/calculations/`)

No I/O, no Streamlit, no Supabase imports — every function here takes
plain values and returns plain values, which is what makes them cheap to
test exhaustively.

- **`returns.py`**: `pct_return(latest, base)` and `return_1d/5d/20d(latest_price, historical_closes)`. `historical_closes` must be ordered oldest→newest and must NOT include the day `latest_price` came from — see the note on `screener_service.py` under [Services](#services-srcservices) for a real bug this exact boundary caused.
- **`dividends.py`**: `ttm_dividend_sum`/`ttm_dividend_yield(events, as_of_date, latest_price)`. An empty `dividend_events` list sums to `0.0` (a confirmed-zero yield), not `None` — missing-vs-zero is a distinction the *caller* (the provider/repo layer) is responsible for, based on whether a fundamentals fetch actually succeeded.
- **`classification.py`**: `criterion_a/b/c()` each return `bool | None` (`None` = missing input, never a fail). `criterion_a`/`criterion_b` pass strictly *above* their threshold; `criterion_c` (PEG) passes *at or below* its threshold — the direction is deliberately reversed for PEG, since a lower PEG is the conventionally desirable side. `criterion_fundamentals(a, c)` is `a or c` — `None` unless *both* `a` and `c` are known (so a single missing PEG/dividend can't silently resolve the `or` to a "fail"). `classify(momentum, fundamentals, is_stale)` short-circuits to `UNAVAILABLE` if `is_stale` or either input is `None`, before ever checking pass/fail counts — this ordering is the whole point of the "missing is never a failure" rule; overall status is driven by Momentum (`b`) and Fundamentals, **not** the raw `a`/`b`/`c` triple, so a stock can be Green with `criterion_a` failing as long as `criterion_c` passes (or vice versa). `build_classification(...)` is the one-stop version that computes `a`/`b`/`c`/`fundamentals` and also assembles the `DataQuality` record — `criterion_a` and `criterion_c` are still returned individually (the Dashboard's **Dividend** and **PEG** columns each show their own pass/fail tick), just no longer fed straight into `classify()`. `criterion_52w_high(latest_price, week_52_high)`/`criterion_52w_low(latest_price, week_52_low)` are separate, **display-only** functions — deliberately *not* threaded into `build_classification`/`classify`, so they have zero effect on Green/Amber/Red status. `criterion_52w_high` passes when price is below 90% of the 52-week high (`latest_price < 0.9 * week_52_high`); `criterion_52w_low` passes when price is above 110% of the 52-week low (`latest_price > 1.1 * week_52_low`). Both return `None` (not a fail) when either input is missing.
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
- **`portfolio_service.py`** — CSV parsing for the two supported broker holdings exports (`parse_zerodha_csv`, `parse_dhan_csv`), name-to-symbol matching (`match_symbol`, normalized-substring containment against `companies.name`), cross-broker merging (`merge_holdings`), and live valuation (`compute_portfolio_view`); plus the two broker *positions* exports (`parse_zerodha_positions_csv`/`parse_zerodha_option_instrument`, `parse_dhan_positions_csv`/`parse_dhan_position_name`) and their own valuation (`compute_positions_view`, migration `0016`). `resolve_tracked_symbols` is the pure diff both refresh paths call to register newly-seen portfolio symbols; `looks_like_etf_name` is the real-display-name-based ETF/fund classifier those same paths apply to it before upserting (migration `0015`). See the Portfolio pages section below and the Futures & Options section's "A follow-up problem 0013 itself introduced" paragraph for the full ETF story.

## Notifications (`src/notifications/`)

`base.py` defines `NotificationAdapter.send(event) -> bool`. Only
`inapp_adapter.py` is implemented (writes to `notification_log`, surfaced
via the Alerts section on `4_Settings.py`). `email_adapter.py`, `telegram_adapter.py`,
`slack_adapter.py` are stubs — each raises `NotImplementedError` with a
docstring describing exactly what to wire up (credentials needed, what
API call to make). Extending notifications means implementing one of
these, not touching `alert_service.py`.

## Futures & Options (F&O) data

A separate, self-contained subsystem for NSE **and BSE** derivatives —
futures + option chains — feeding the Options screen
(`pages/5_Options.py`). It does **not** go through the
`PriceDataProvider`/`FundamentalsDataProvider` ABCs; F&O has its own shape.

**Data source — and why these are the only viable ones** (settled
empirically):
- **yfinance carries no NSE/BSE derivatives** — `Ticker("RELIANCE.NS").options`
  is empty. Yahoo does not list Indian options/futures.
- **NSE's live option-chain API** (`/api/option-chain-equities`) returns
  HTTP 200 with hollow JSON (`expiryDates: None`) to non-interactive
  sessions — its anti-bot layer. Unusable from a script. BSE has no
  equivalent live API at all to even try.
- **Each exchange's own UDiFF bhavcopy** — the reliable source for both.
  Same SEBI-mandated schema (Unified Distilled File Format) on both
  exchanges -- same column names, same `FinInstrmTp` instrument codes --
  confirmed live for BSE too, which is what made adding it as a second
  source cheap (`src/data_providers/udiff_bhavcopy.py` holds the one
  parsing routine both `nse_fo_provider.py` and `bse_fo_provider.py`
  call; see "Two exchanges, one parser" below). NSE: one **zip** per
  trading day at
  `https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip`
  (the `nsearchives` host; the older `archives.nseindia.com` is now
  bot-blocked and serves a PDF). BSE: one **plain CSV** per trading day
  (no zip) at
  `https://www.bseindia.com/download/Bhavcopy/Derivative/BhavCopy_BSE_FO_0_0_0_YYYYMMDD_F_0000.CSV`.
  Both need just a browser User-Agent, no cookie handshake. Each row is
  one contract's full trading day: OHLC, LTP, prev close, settlement,
  underlying (spot), open interest + change, volume, turnover, trades,
  expiry, strike, CE/PE, lot size. Instrument types: `STF` = stock
  future, `STO` = stock option, `IDO` = index option (NIFTY/BANKNIFTY on
  NSE; SENSEX/BANKEX on BSE -- migrations `0018`/`0019`, see the "Index
  F&O" paragraph below). All four codes exist in both exchanges' bhavcopy
  files, but **BSE ingestion only ever keeps `IDO`** -- BSE's own
  stock-level F&O liquidity is negligible, so its `STF`/`STO` rows are
  parsed out even though they're present in the file; NSE is the sole
  source for every stock future/option (see "Two exchanges, one parser"
  below for exactly where this allow-list lives). `IDF` (index future) is
  ignored on both exchanges -- no Index position on this app needs a
  futures LTP today.
  **This is end-of-day data** (published after close) — "latest price"
  means the most recent close/settlement, never an intraday live quote.
  There is no free live/intraday F&O feed for either exchange.

**Two exchanges, one parser.** `src/data_providers/udiff_bhavcopy.py`
owns `parse_udiff_bhavcopy(csv_text, *, source_name, futures_types,
option_types, trade_date, universe)` -- the full CSV-to-model parsing
logic, identical for both exchanges since the schema is identical.
`nse_fo_provider.py` and `bse_fo_provider.py` are both thin wrappers
around it: each owns only its exchange-specific URL template, HTTP fetch
(NSE unzips a `.csv.zip`; BSE decodes the response body directly, no zip
library needed), and its own `SOURCE_NAME` (`"nse_fo_bhavcopy"` /
`"bse_fo_bhavcopy"`, stamped onto every price row for traceability).
`_FUTURES_TYPES`/`_OPTION_TYPES` were originally identical between the
two (`{"STF"}` / `{"STO", "IDO"}`), kept as separate module-level
constants rather than shared specifically because "there's no guarantee
the two exchanges' in-scope instrument sets stay identical forever" --
and they since have diverged: BSE's own stock-level F&O liquidity turned
out to be negligible in practice, so `bse_fo_provider.py` now uses
`_FUTURES_TYPES = set()` / `_OPTION_TYPES = {"IDO"}` (index options only
-- SENSEX, BANKEX), while `nse_fo_provider.py` keeps the original
`{"STF"}` / `{"STO", "IDO"}` and remains the sole source for every stock
future/option. The TypeScript mirror (`supabase/functions/fo-refresh/bhavcopy.ts`)
takes the same approach in one file: `bhavcopyUrl`/`fetchBhavcopyText`
branch on an `Exchange = "NSE" | "BSE"` parameter (BSE skips the
zip-specific content-type check and unzip step entirely), and
`parseFoBhavcopy` now also takes that same `exchange` parameter (in
addition to an explicit `source` argument rather than a module
constant), deriving its allow-list per call via
`futuresTypesFor`/`optionTypesFor` instead of the single pair of
module-level `Set`s it originally had -- those two functions encode the
identical NSE/BSE split as the two Python providers.

**Gotcha: narrowing an exchange's allow-list doesn't retroactively clean
up rows it already wrote.** `futures_daily_prices`/`option_daily_prices`
are keyed by `(symbol, expiry_date[, strike_price, option_type],
trade_date)` (migration `0007`) -- **no exchange/source column in the
key** -- and `latest_futures_view`/`latest_option_chain_view` pick
`distinct on (contract) order by trade_date desc`, i.e. whichever
exchange wrote the newest `trade_date` for that exact contract wins,
regardless of which exchange it was. When `bse_fo_provider.py` was
narrowed to index-options-only, that only stopped *future* BSE writes --
rows BSE had already written for stock contracts stayed in place, and for
any contract where NSE's bhavcopy hadn't listed that exact strike again
since (routine for a less-active strike, since NSE's file only includes
contracts that actually traded that session), the old BSE row kept
winning "latest" indefinitely. Confirmed live: 36 option contracts across
20 symbols (including HDFCBANK's 680 CE/PE and 820 PE) and 20 futures
contracts across 18 symbols were still showing stale BSE prices days
after the restriction shipped. Fixed with a one-time manual cleanup
(deleting `option_daily_prices`/`futures_daily_prices` rows where
`source LIKE 'bse_fo_bhavcopy%'` and, for options, the symbol isn't
SENSEX/BANKEX) rather than a code change -- the ingestion code itself was
already correct going forward. **If this restriction is ever loosened or
another exchange's scope is narrowed the same way, repeat this cleanup**
-- the code has no automatic mechanism to purge rows an exchange is no
longer allowed to write, only to stop writing new ones.

**The contract dimension tables need the same cleanup, separately.**
`futures_contracts`/`option_contracts` have no `source` column at all --
they just record that a contract (symbol + expiry[, strike, type])
exists, independent of which exchange's bhavcopy last mentioned it. BSE
had, in some cases, listed an expiry date NSE never uses for that symbol
at all (observed live: BSE carried a spurious `2026-08-27` expiry for
HDFCBANK and 19 other symbols, and `2026-09-24`/`2026-10-29` for a
handful more, none of which NSE's own bhavcopy has ever listed). Deleting
the stale *price* rows (above) left these *contract* rows behind as
orphans -- zero price history, but still a real row, so `fo_repo.
list_option_expiries` (and the Options page's near/next/far expiry
picker) kept listing that phantom date with every column showing N/A.
Fixed the same way: for each symbol touched by the price cleanup above,
diff its contract-table expiry dates against its price-table expiry
dates and delete whichever `(symbol, expiry_date)` combination in
`option_contracts`/`futures_contracts` has zero matching rows in
`option_daily_prices`/`futures_daily_prices` (36 + 20 rows respectively,
the exact 20/18-symbol scope as the price-row cleanup). **Whenever the
price-row cleanup above is repeated, repeat this contract-row check too**
-- the two tables can drift out of sync independently, and deleting only
the prices leaves a silently-broken phantom expiry behind rather than
just making the symbol's chain look normal again.

**Greeks / implied volatility are intentionally NOT stored** — not in the
bhavcopy (or any free source), and computing them was scoped out. The
tables can gain those columns + a `greeks.py` later without reshaping.

**Index F&O (migrations `0018_company_type.sql` / `0019_add_bankex.sql` /
`0023_add_more_nse_indices.sql`)** — added so Dhan-synced index option
positions (`pages/6_My_Broker.py`'s "Connect Dhan account") have real
F&O data to fall back on for LTP instead of a blanket N/A (see the
Portfolio pages section's "Connect Dhan account" subsection). Seven
symbols are seeded into `companies` as `company_type = 'Index'`: `NIFTY`,
`BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`, `NIFTYNXT50` (all NSE-listed),
`SENSEX`, `BANKEX` (both BSE-listed -- `0019` added BANKEX after
`bse_fo_provider.py` confirmed it live alongside SENSEX in a real BSE
bhavcopy response; `0023` added the other three NSE indices after a real
Dhan-synced FINNIFTY option position showed up misclassified as a
"stock" on My Positions -- `portfolio_service.classify_position_bucket`
has no fallback beyond `companies.company_type`, so an underlying with no
seeded row there silently defaults to "stock"). `option_contracts`/`option_daily_prices`
themselves are unchanged -- an index option row is stored identically to
a stock option row, just e.g. `symbol = 'SENSEX'`, so no schema change
was needed there, only widening which bhavcopy rows get kept
(`_OPTION_TYPES` gained `IDO` on both providers) and which symbols
`scripts/fetch_fo_data.py`/`fo-refresh/index.ts` include in their
ingestion universe (every `companies` row with `company_type = 'Index'`,
same widening pattern as the existing portfolio-symbols widening below --
passed unfiltered to *either* exchange's provider, since each one's own
bhavcopy will only ever contain the symbols actually listed there). Index
*futures* (`IDF` bhavcopy rows) stay out of scope on both exchanges -- no
Index position on this app needs a futures LTP today, only options. New
F&O rows only appear once `fetch_fo_data.py` or the **NSE/BSE F&O Data
Refresh** buttons actually run against a universe that includes these
symbols; the on-demand Edge Function only fetches the *latest* bhavcopy
per exchange and skips entirely if it's already loaded (see "A real
incident this caused" below for the backfill-completion analog, and the
Edge Functions section for the per-exchange watermark-scoping fix), so
widening the universe alone doesn't retroactively backfill days already
ingested for stocks -- a one-off `scripts/fetch_fo_data.py --exchange
nse` / `--exchange bse` run (idempotent upserts) is what actually
backfills index history immediately rather than waiting for the next
scheduled refresh to naturally pick it up.

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
- `src/data_providers/udiff_bhavcopy.py` — the shared `FOBhavcopy`
  dataclass and `parse_udiff_bhavcopy(...)`, the one CSV-to-model parser
  both exchange providers call (see "Two exchanges, one parser" above).
- `src/data_providers/nse_fo_provider.py` / `bse_fo_provider.py` — each
  has its own `fetch_fo_bhavcopy(trade_date, universe)` (download + parse)
  and thin `parse_fo_bhavcopy(csv_text, ...)` wrapper around the shared
  parser above, unit-tested against inline fixtures with no network
  (`tests/test_nse_fo_provider.py`, `tests/test_bse_fo_provider.py`).
- `src/data_providers/mock_provider.py::MockFOProvider` — synthetic
  futures (3 monthly expiries) + option chains (strikes stepped around a
  spot), shaped as the same `FOBhavcopy` object, so the ingest path,
  Options screen and tests run offline.
- `src/repositories/fo_repo.py` — natural-key upserts (chunked, since one
  day is ~9k option rows), `refresh_open_flags`,
  `clear_dashboard_fo_metrics` (see "A real bug this caused"
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
- `scripts/fetch_fo_data.py` — service-role backfill, `--exchange nse`
  (default) or `--exchange bse` selects `nse_fo_provider`/`bse_fo_provider`
  from a small `_PROVIDERS` dict (`--days 60` default, `--date`, `--mock`
  -- `--mock` ignores `--exchange`, `MockFOProvider` is exchange-agnostic
  synthetic data), run by the operator (like the other seed scripts);
  processes oldest→newest then calls `refresh_open_flags(today)`.
  `scripts/seed_mock_data.py` also seeds ~30 mock F&O days for local dev.
  Its `universe` (which symbols the bhavcopy parse keeps) starts as the
  current Nifty50 constituents, adds every `companies` row with
  `company_type = 'Index'` (migrations `0018`/`0019`), then widens with
  every distinct resolved symbol across all users' `portfolio_holdings` --
  same pattern `scripts/run_refresh.py` already uses for cash-market data
  (registering a minimal `companies` row for any not seen before), applied
  here too so a portfolio-only stock like Hindustan Zinc actually gets its
  futures/options ingested, not just its equity LTP. The same merged
  universe is passed to whichever exchange's provider was selected,
  unfiltered -- each exchange's own bhavcopy naturally only contains the
  symbols actually listed there, so there's no need to split the set by
  exchange. Tolerant of `portfolio_holdings` not existing yet (migration
  `0012`).

**On-demand refresh**: the **📊 NSE F&O Data Refresh** / **📊 BSE F&O Data
Refresh** buttons both hit the *same* second Edge Function,
`supabase/functions/fo-refresh/` (see the Edge Functions section below),
parameterized by a POST body `{"exchange": "NSE" | "BSE"}` — a TypeScript
port of the same bhavcopy fetch+parse, but only for the single most
recent day and only if that exchange has actually published something
newer than what's already loaded for it (checked via the greater of
`max(trade_date)` in `futures_daily_prices` and `option_daily_prices`,
scoped by a `source` prefix -- see the Edge Functions section for why
this scoping was necessary, not optional, and why both tables must be
checked), so a click when nothing's new is a cheap read-only no-op
rather than a silent re-fetch. NSE has no external zip-library dependency — see the
Edge Functions section for why; BSE needs no zip handling at all. Its
`universe` set gets the identical Index-row + portfolio-symbol widening as
`fetch_fo_data.py` above (mirrored in TypeScript via the same
`resolveTrackedSymbols`/`portfolioSymbols.ts` helper `manual-refresh`
already uses), so a symbol newly tracked from a portfolio upload starts
getting its F&O data via either button too, not just a manual backfill run.

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
`is_open`, not this cache) had already stopped showing it. Fixed (at the
time) by `fo_repo.delete_expired_dashboard_fo_metrics(client, as_of)`
(and its TypeScript mirror in `dashboardMetrics.ts`), called at the end
of every `recompute_dashboard_metrics` run -- same finalization pattern
as `refresh_open_flags` above, just a straight delete rather than a
two-way flag flip since this cache has no `is_open` column of its own.
**This expiry-only prune was later replaced by a full clear-then-insert
(`fo_repo.clear_dashboard_fo_metrics`) -- see the BSE-exclusion bug
further below for why an expiry-only prune wasn't general enough.**

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

**A third bug, this one in the Dashboard page itself rather than the
recompute pipeline**: `recompute_dashboard_metrics` sources `all_symbols`
from `companies_repo.list_all_companies`, which includes the seeded
Index rows (NIFTY, BANKNIFTY, BANKEX, ...; see the `company_type`
section above) alongside the 50 Nifty50 stocks -- so `dashboard_fo_metrics`
can carry rows for those index symbols too, sourced from the BSE
index-options feed (`bse_fo_provider`, restricted to index options only
-- see "BSE F&O provider" below). The Dashboard's "Options month"
dropdown originally built its choices from every distinct `expiry_date`
across the **whole** `dashboard_fo_metrics` table, with no symbol
filter -- so an index's own BSE expiry (e.g. BANKEX's, which need not
line up with any NSE monthly stock-options expiry) leaked into the
dropdown as an extra entry, even though the screener below only ever
lists Nifty50 stocks and not one of them had a `dashboard_fo_metrics`
row for that month. Fixed in `pages/1_Dashboard.py` by restricting
`available_expiries` to rows whose `symbol` is one of the screener's own
`df["symbol"]` values, before taking the distinct set of expiry dates --
the same "the cache spans a wider universe than what this specific view
displays" shape as the Hindustan Zinc bug above, just filtering down
instead of widening up.

**That symbol filter turned out necessary but not sufficient**: the
dropdown still showed duplicate same-month entries afterward, confirmed
live -- e.g. Aug 2026 twice, Sep 2026 twice. The remaining source wasn't
an index symbol at all; it was a **stale, pre-restriction BSE contract
for a real Nifty50 stock**. `bse_fo_provider.py` was restricted to index
options only (see "BSE provider: restrict to index options only"
below), but that restriction only governs *new* ingestion -- any stock-
symbol `option_contracts` row BSE wrote before the restriction stays
`is_open = true` for as long as its own (real, once-valid) expiry date
hasn't passed, since `fo_repo.refresh_open_flags` only re-derives
`is_open` against the calendar, with no concept of "still a valid source
for this symbol". So `latest_option_chain_view` kept serving these
alongside the correct NSE leg for the same stock and month -- two
distinct `expiry_date`s a few days apart (BSE's own monthly expiry
convention isn't the same day as NSE's), both `dashboard_fo_metrics`
rows for a symbol that legitimately belongs in the screener, so the
earlier symbol filter couldn't tell them apart.

Fixed at the source instead of the display layer: migration `0027`
adds `source` to `latest_option_chain_view` (previously selected
everything *except* that column, even though the underlying
`option_daily_prices.source` has always distinguished
`"nse_fo_bhavcopy"`/`"nse_fo_bhavcopy_edge"` from
`"bse_fo_bhavcopy"`/`"bse_fo_bhavcopy_edge"` -- see
`get_latest_fo_trade_date`'s `source_prefix` param above). With `source`
now visible, `dashboard_metrics_rows` (and its TypeScript mirror in
`dashboardMetrics.ts`) drops any leg whose `source` starts with
`"bse_fo_bhavcopy"` before grouping legs by symbol -- BSE has no
legitimate role in this cache at all now, since `dashboard_fo_metrics`
is Dashboard-only (no other page reads `get_dashboard_fo_metrics`) and
the Dashboard is purely a Nifty50 *stock* screener. Rows with no
`source` key at all (older fixtures, or the mock provider's single
`"mock_fo"` source used for local dev) pass through unaffected -- only
an explicit BSE prefix is excluded. The underlying stale BSE
`option_contracts` rows are left alone (harmless once excluded here, and
still legitimately readable by other consumers of the view, e.g. a
portfolio's own BANKEX position).

**A fifth bug, confirmed live immediately after applying migration
`0027` and re-syncing**: the dropdown still showed all 5 entries
(including the duplicates) even though `dashboard_metrics_rows` was now
correctly excluding BSE legs from any *newly computed* row. Root cause:
the write path was still `upsert_dashboard_fo_metrics` (new rows) +
`delete_expired_dashboard_fo_metrics` (rows whose `expiry_date` had
already passed) -- and the stale BSE row's `expiry_date` is a real,
future BSE monthly expiry, so it was never "expired" by that prune's
definition. The BSE-exclusion fix stops the row from being *re-upserted*
each run, but nothing was left to *remove* a row that a previous run
(before the fix existed) had already written. Since this is a pure
derived cache with no history worth preserving (the same reasoning
migrations `0010`/`0011` already used to justify a wholesale
drop-and-recreate on schema changes), `recompute_dashboard_metrics`
was changed to a true replace: `fo_repo.clear_dashboard_fo_metrics`
(deletes every row, not just expired ones) now runs *before*
`upsert_dashboard_fo_metrics` on every call, and
`delete_expired_dashboard_fo_metrics` was deleted outright (superseded,
not left as dead code) -- mirrored in `dashboardMetrics.ts` as
`clearDashboardMetrics`, replacing `deleteExpiredDashboardMetrics`. This
also forecloses the whole class of bug: any *future* reason
`dashboard_metrics_rows` might exclude a leg no longer needs its own
matching prune query, since every recompute now starts from an empty
table.

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
    st.Page("pages/6_My_Broker.py", title="My Broker"),
    st.Page("pages/7_My_Trades.py", title="My Trades"),
    st.Page("pages/8_My_Holdings.py", title="My Holdings"),
    st.Page("pages/9_My_Positions.py", title="My Positions"),
    st.Page("pages/10_Analyse_Trade.py", title="Analyse Trade", visibility="hidden"),
    st.Page("pages/4_Settings.py", title="Settings"),
]
st.navigation(pages).run()
```

**`visibility="hidden"` on Analyse Trade** (confirmed supported in the
installed Streamlit version, 1.59.1 -- `st.Page`'s own `visibility`
parameter): the page is excluded from the sidebar nav menu but stays
reachable via `st.switch_page`, which is exactly what My Trades needs --
selecting a row and clicking "Analyse Trade" stashes
`st.session_state["analyse_trade_id"]`/`["analyse_trade_portfolio"]` and
calls `st.switch_page("pages/10_Analyse_Trade.py")`, landing on a real
page (its own URL, its own script) that simply never appears as a 5th
sidebar link alongside My Broker/My Trades/My Holdings/My Positions.

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
Settings is listed last on request. There used to be a separate
`3_Alerts.py` entry between Portfolio and Settings; its entire content
(alert CRUD + notification history) was folded into `4_Settings.py`
instead (see that bullet below) and the file/nav entry were removed
rather than kept as a thin redirect -- there was nothing left in it to
redirect to.

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

- **`1_Dashboard.py`** — loads `latest_screener_view` via `snapshot_repo.get_latest_screener()`, applies the signed-in user's thresholds via `threshold_override.apply_user_thresholds()`, renders metric cards (also usable as quick filters, wired through `st.session_state["status_filter"]`), sidebar filters, and the screener table. The Status sidebar filter is a `st.multiselect` over `ALL_STATUSES = ["Green", "Amber", "Red", "Unavailable"]` — `status_filter` is always a *list* (any combination, not one-or-all), and the final row filter is a single `df["status"].isin([...])`, so selecting all four is equivalent to no filter at all. Saved filter presets normalize old single-string `"status"` values (from before this was a multiselect) into a list on load for backward compatibility. The "Minimum dividend yield" / "Minimum PEG" sidebar filters default to `0.0`, **not** `user_settings.dividend_yield_threshold`/`peg_threshold` — they're a separate display filter from the criterion A/C pass/fail thresholds, and defaulting them to the threshold value silently hid every stock below it on first load (a real bug, since fixed). Keep these two concepts distinct if you touch this page: the Settings-page thresholds decide Green/Amber/Red/Unavailable; these sidebar inputs just additionally hide rows below a value the user dials in themselves, and should default to "show everything." Right after the title/disclaimer, `render_global_refresh_bar(client)` (`src/utils/refresh_bar.py`, see the Utils section below) renders the three on-demand refresh buttons — this used to be two Dashboard-only buttons hand-rolled in this file, now a shared component called identically from every page (Dashboard, Stock Detail, Options, My Broker, My Trades, My Holdings, My Positions, Settings), so refreshing data never requires navigating back here specifically. Below the title, a "Data sources" caption reads `get_settings().market_data_provider`/`fundamentals_provider` directly (e.g. "Stock prices: `yfinance`") and states the options/F&O source as a fixed string, "NSE + BSE Bhavcopy (end-of-day)" — there's no configurable F&O provider setting to read (`src/config.py` has no such field; F&O ingestion always means these two bhavcopy sources, see the Futures & Options section). The header's "Data freshness" line covers stock refresh only now (`last_fetch_at`, still needed for `get_market_state()`'s staleness check); the per-exchange F&O refresh timestamps live in the shared refresh bar's own captions instead of a Dashboard-only line. "Latest NSE Bhavcopy: <date>" / "Latest BSE Bhavcopy: <date>" are still Dashboard-specific, each from its own `fo_repo.get_latest_fo_trade_date(client, source_prefix=...)` call (`"nse_fo_bhavcopy"` / `"bse_fo_bhavcopy"` -- same `source`-prefix scoping the fo-refresh Edge Function's own watermark query uses, and for the same reason: NSE and BSE publish on the same trading days, so one combined "latest bhavcopy" figure couldn't tell you which exchange's file was actually newest, and a single shared line was exactly what made a real BSE-side false-success bug easy to miss -- see the Edge Functions section's "A third real bug, once BSE was added"). Each is deliberately **not** that exchange's own last-successful-fetch timestamp: a bhavcopy is published for a specific trading day and a run on a non-trading day finds nothing new, so a refresh can succeed today while the loaded data is still from a prior session -- these lines surface that distinction. Wrapped in the same `except APIError: None` degrade as the rest of this page's optional F&O reads, for a deployment that hasn't applied migration `0007` yet.

  **Metric cards**: seven buttons in a row (`st.columns(7)`) — `Total stocks`, `🟢 Green`/`🟠 Amber`/`🔴 Red` (each sets the status filter to just that one status), and `Yield > threshold`/`All momentum +ve`/`PEG ≤ threshold` (each sets `criterion_filter` instead). There is deliberately no `Unavailable` button — the status itself is still fully selectable via the sidebar's Status multiselect and still counts toward `ALL_STATUSES`, but it wasn't considered a useful one-click quick filter and was dropped to declutter the row (a purely cosmetic trim, not a behavior change to filtering).

  **Screener table columns**, left to right: `Stock` (the NSE ticker symbol, e.g. `ADANIENT` — not the full company name; no separate `#`/`Symbol` key -- see below for why), `LTP` (latest price — renamed from "Latest price" for column-width economy), `52W High`/`52W Low` (value + `pass_fail_icon` for `criterion_52w_high`/`criterion_52w_low` — display-only proximity checks, **not** part of Green/Amber/Red; see `classification.py` above), `1D`/`5D`/`20D` (arrow + percentage only), `Momentum` (a single `pass_fail_icon(criterion_b)` — despite the name it's specifically criterion B, not a combined view), two F&O-derived columns (see below), and `Dividend`/`PEG`/`Fundamentals` last (`Dividend` — renamed from "Dividend yield" — and `PEG` each carry a `pass_fail_icon` for criteria A and C respectively; `Fundamentals` is `pass_fail_icon(criterion_fundamentals(criterion_a, criterion_c))`, i.e. A or C, and is what actually feeds the overall Green/Amber/Red status alongside Momentum — see `classification.py` above). `PE` (`pe_ratio`) is **not** shown on this table — it's a fundamentals input, not one of A/B/C/Fundamentals, and stayed too noisy for the Dashboard; it's still shown on Stock Detail's pass/fail scorecard and Fundamentals panel. There used to be a third F&O-derived column, the near-month future's price (header e.g. `Jul Future`, backed by `fo_service.near_month_futures_map`/`near_month_column_label`) — it was dropped on request, and those two now-unused `fo_service` functions (and their tests) were deleted along with it rather than left as dead code; futures data is no longer fetched on this page at all (only options, for the two columns below).

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
- **`2_Stock_Detail.py`** — Plotly candlestick (falls back to a line chart if OHLC is incomplete) with volume subplot, moving averages, dividend timeline, classification-history chart, and inline alert creation. It still fetches this symbol's `UserPosition` (`settings_repo.get_user_position`) purely to draw entry/target/stop-loss reference lines on the chart if one was saved previously — the **form** that let a user create/edit that position ("Your position notes": entry/target/stop-loss/holding-period/notes + a risk/reward-ratio metric) was removed on request, since the Portfolio page now owns real holdings and will eventually own position-style tracking too; there is currently no UI anywhere to create a *new* `UserPosition` row, only this page's read-only chart overlay of one saved previously. The Fundamentals column is rendered via `render_stat_grid()` instead of stacked `st.markdown` lines; the alert list uses `render_alert_row()` (see below) instead of printing the alert's raw Python `config` dict; the "Create a new alert" expander's inputs are wrapped in an `st.form`, matching the same pattern `4_Settings.py`'s Alerts section uses. A "📊 View F&O / options" button hands the current symbol to `5_Options.py` via `st.session_state["fo_symbol"]` + `st.switch_page`. `symbol_options` (the "Select a stock" picker) is `companies_repo.list_current_constituents(client)` unioned with `portfolio_repo.list_portfolio_symbols(client, user_id)` -- the signed-in user's own resolved portfolio symbols (ETFs, non-Nifty50 stocks) -- so a portfolio-only stock becomes viewable here the moment it's tracked, not just on the Dashboard. Tolerant of `portfolio_holdings` not existing yet (migration `0012`), degrading to Nifty50-only exactly as before this widening existed.
- **`4_Settings.py`** — per-user thresholds, an "Alerts" section, notification channels, account, theme, change-password. The Alerts section (formerly its own `3_Alerts.py` page, folded in on request between "Screening thresholds" and "Notification channels") is the same three pieces that page always had: "Your alerts" (list + active toggle + delete), an "➕ Create a new alert" expander (alert CRUD including portfolio-wide alerts, `symbol IS NULL`), and a "Notification history" expander (the latter two are now expanders rather than always-open sections, to keep this now-longer page scannable — a purely presentational change, no behavior difference). Alert rows use `render_alert_row()` (shared with Stock Detail — one formatting implementation, two call sites) instead of a raw dict dump. Notification history stays `st.dataframe`-only on every viewport, deliberately not given a Tailwind mobile-card alternative — see the design-system note under Utils for why. The three permanently-disabled Email/Telegram/Slack notification checkboxes were collapsed into a single row of `render_pill()` "coming soon" badges next to the one real (In-app) checkbox, removing dead-weight disabled UI for unimplemented channels.
- **`5_Options.py`** — the F&O / Options screen for one stock (see the Futures & Options section above for the data pipeline). Symbol selector defaults to `st.session_state["fo_symbol"]` (set by selecting a row on the Dashboard's table or clicking Stock Detail's own "View F&O / options" button), falling back to `selected_symbol`. `fo_symbols` (the options list) is `fo_repo.list_fo_symbols(client)` -- already every symbol with an open futures contract, regardless of Nifty50 status -- falling back to current constituents if that's empty, then unioned with `portfolio_repo.list_portfolio_symbols(client, user_id)` so a portfolio-only stock is at least selectable even with zero F&O data (handled gracefully below, same as any Nifty50 stock with none). Renders: an expiry selector (drives the summary tiles and, indirectly, which expiry's chain gets reused rather than re-fetched in the CSP/CC tables below); summary tiles (spot / ATM strike / total CE OI / total PE OI / Put-Call ratio) via `render_stat_grid`, sourced from `fo_service.option_chain_summary(chain_rows)` for the selected expiry; a futures term-structure table (near/next/far, with basis vs spot) + a near-month daily-close Plotly chart; and two sections below that, **"5% CSP"** and **"5% CC"**, showing the actual calculation for the selected symbol rather than just the final Dashboard-column percentage. (There used to be a classic CE | Strike | PE option chain table between the futures chart and these two sections -- it was dropped on request; `chain_rows`/`option_chain_summary` are still fetched/used for the summary tiles and CSP's near-expiry row, but the pivoted per-strike display itself, and the now-unused `fo_service.shape_option_chain` pivot helper + its tests, were removed rather than left as dead code.):
  - **5% CSP** is a **near/next/far month table** (`fo_service.csp_5pct_for_rows`, one call per expiry — the same term-structure shape the Futures section above already uses), columns Term / Expiry / Spot / Strike / Put Premium / **Trade Date** / 5% CSP. The near row reuses the already-fetched `chain_rows` when the expiry selector above happens to be on the near expiry; next/far are fetched separately via `fo_repo.get_option_chain`. The Trade Date column is what actually surfaces a stale quote to the user — see `_freshest_rows`'s docstring above for why a strike's "latest" row can silently be weeks old.
  - **5% CC** is also a **near/next/far month table** (`fo_service.cc_5pct_for_rows`, one call per expiry, mirroring 5% CSP's own loop exactly), columns Term / Expiry / Strike (lowest ≥5% above spot) / Premium / Trade Date / **Net Investment** / 5% CC / **Assignment Profit** (`(strike / net_investment - 1) * 100`, `None`/"N/A" if `net_investment` is zero or negative -- premium ≥ spot). This originally only showed the nearest expiry as a stat-grid breakdown (via `fo_service.cc_5pct_map`, which itself just restricts to the nearest expiry and delegates to `cc_5pct_for_rows`) -- changed on request to match 5% CSP's table shape once a live user actually wanted to see next/far month CC yields too, not just the near month `dashboard_fo_metrics` already caches for the Dashboard. "Net Investment" and "Assignment Profit" only appear here, not on the Dashboard, which only ever caches/displays `cc_pct`.
  Both loops share one `_chain_rows_for(exp)` helper (reuses the already-fetched `chain_rows` when `exp` happens to be the expiry selected above, otherwise fetches that expiry's chain separately) and both use the cash-market spot for every expiry, not just the near one -- so **every** row of both tables, not just the near-month one, matches what the Dashboard would compute for that same expiry. Shaping is done by `fo_service.option_chain_summary`/`futures_term_structure`/`csp_5pct_for_rows`/`cc_5pct_for_rows`, not in the page.
  - **Portfolio CC** -- a third near/next/far table, shown *only* when the signed-in user actually holds this stock in at least one of their own saved portfolios (`portfolio_repo.list_holdings(client, user_id)`, filtered to this symbol; silently absent otherwise, unlike 5% CSP/CC above which always render). Computed via `fo_service.covered_call_for_holding` (avg-buy-price-vs-LTP-dependent target, nearest-strike, not 5% CC's fixed-5%-OTM floor filter) -- this used to mirror My Holdings' own "CC ROI"/"CC Assignment ROI" columns, but those were removed by request (see the "Per-holding covered-call suggestion" bullet in the Portfolio pages section below), making this the only place in the app showing this figure now. If the same portfolio name holds this symbol across multiple brokers, `portfolio_service.merge_holdings` combines them into one row first; if the stock is held in more than one *named* portfolio, one table renders per portfolio (each with its own qty/avg price subheading), since different portfolios can have different cost bases and thus different target strikes. Columns: Term / Expiry / Strike / Premium / Trade Date / Invested Amount / CC ROI / CC Assignment ROI.

  **A real bug found here, right after this section first shipped**: the CSP/CC breakdown's spot value (CC was still "ITM PMCC" at the time, but the bug and fix applied identically) was initially taken from `option_chain_summary(near_chain_rows)["spot"]` — the F&O bhavcopy's own `underlying_price` column — while the Dashboard's two columns (now the `dashboard_fo_metrics` cache, see above) use the cash-market `latest_price` from `latest_screener_view`. These two prices aren't the same value, so this page's numbers didn't match the Dashboard's for the same stock (confirmed live: ADANIENT showed 5% CSP = 0.54% on the Dashboard but 0.45% here, since a different spot picked a different nearest-5%-below strike, 3040 vs 3020). Fixed by fetching `snapshot_repo.get_latest_screener_row(client, symbol).latest_price` and using that as the spot for both calculations here too, instead of the chain's `underlying_price` — the top-of-page "Spot"/"ATM strike" summary tiles are unaffected and deliberately still use the chain's own `underlying_price` (correct for highlighting the ATM row in the actual option-chain data being displayed there). If you add another F&O-derived calculation to either screen, source spot the same way this one now does — from the screener, not the chain — to keep the two screens' numbers in agreement.

- **`6_My_Broker.py` / `7_My_Trades.py` / `8_My_Holdings.py` / `9_My_Positions.py` / `11_My_CSP.py` / `10_Analyse_Trade.py`** — six pages (`10_Analyse_Trade.py` hidden from the sidebar, see the "Streamlit app" section above) replacing what used to be one combined `6_Portfolio.py` (sidebar label "My Portfolio", retired). See the dedicated Portfolio pages section below for the full upload → match → save → refresh-registration pipeline, the multiple-coexisting-portfolios design (`portfolio_name`, migration `0014`), and the My Trades/Analyse Trade/My CSP grouping. Each page reads every one of the signed-in user's saved rows across every portfolio and broker via `src/utils/portfolio_page.py`'s shared cached loaders; My Holdings/My Positions/My Trades/My CSP each render one `st.tabs` entry per distinct `portfolio_name` (union of holdings' and positions' names — a portfolio can exist on positions alone) scoping `portfolio_service.merge_holdings`/`compute_portfolio_view` (LTP via `snapshot_repo.get_latest_prices`, a direct `daily_screener_snapshots` query, deliberately **not** `latest_screener_view` — see below for why) and `compute_positions_view` to just that portfolio's own rows. Every table on these pages is a plain `st.dataframe` — see below for why, and for how row selection replaced the per-row 🔍 button.

## Portfolio pages

The signed-in user's own broker holdings and F&O positions (not the
Nifty50 screener universe) span six pages -- `pages/6_My_Broker.py`
(upload/connect/create/delete), `pages/7_My_Trades.py` (holdings +
positions grouped by underlying into Trades), `pages/8_My_Holdings.py`
(equity holdings, valued live against the app's own market data),
`pages/9_My_Positions.py` (per-leg F&O positions split into Stock Options/
Index Options/Others tables, valued against each file's own LTP -- see
the Positions subsection near the end of this section for why),
`pages/11_My_CSP.py` (every position leg from a "CSP"-tagged Trade, with
the underlying's own LTP/1D/5D/20D change -- see its own subsection
below), and `pages/10_Analyse_Trade.py` (one Trade's detail,
registered `visibility="hidden"` in `app.py` so it's reachable via
`st.switch_page` but never shows as its own sidebar link). This used to
be one combined page (`pages/6_Portfolio.py`, retired) -- most of the
mechanics below are unchanged, just relocated; the split itself, and the
new My Trades/Analyse Trade grouping, are covered in their own
subsections further down. All six pages share one module,
`src/utils/portfolio_page.py` (cached `@st.cache_data` loaders, the
`portfolio_cache_bust` counter, `build_trade_legs`) -- since these are
plain module-level functions rather than redefined per page, a cache hit
in one page is a cache hit in another. This is a separate,
self-contained subsystem from the screener, similar in spirit to the F&O
section above: its own tables, its own service module, its own
refresh-pipeline hook — nothing here changes `nifty50_constituents`,
`latest_screener_view`, or any existing page.

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
— every Portfolio page catches both around its own `load_holdings` call
(`src/utils/portfolio_page.py`) and shows one combined "apply 0012 and
0014, in that order" message (confirmed live
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

`pages/6_My_Broker.py` renders one `st.tabs` entry per distinct
`portfolio_name` the user has (`sorted({h.portfolio_name for h in
saved_holdings} | {p.portfolio_name for p in saved_positions})`), each
showing that portfolio's own upload section (`_render_broker_tab`)
scoped to that `portfolio_name` — uploading a broker's file there only
ever calls `replace_broker_holdings`/`replace_broker_positions` for
*this* `(portfolio_name, broker)` pair, so every other tab is untouched
no matter what you upload. A permanent "+ New portfolio" tab at the end
takes a portfolio name (`st.text_input`, defaulting to `f"Portfolio
{len(portfolio_names) + 1}"` if left blank) and a broker; it refuses to
proceed (shows `st.error`, no uploader) if the name collides with an
existing tab, to avoid silently merging into what the user probably
meant as a distinct portfolio. When the user has no portfolios at all
yet, there's nothing to make tabs out of, so the page skips `st.tabs`
entirely and renders the same name+broker+uploader creation flow
directly. Every call site shares one `_render_upload_section()` helper
for the parse → preview → manual-symbol-form → save sequence
(parameterized by `portfolio_name`/`broker`/`key_prefix`/`save_label`, so
the ~50 lines of shared logic isn't duplicated three times). **This
tab-tracking/create/delete machinery lives only on My Broker** -- the
other four Portfolio pages each render a plain, unkeyed `st.tabs(portfolio_names)`
(no "+ New portfolio" tab, no active-tab tracking), since none of them
can create or delete a portfolio.

**An existing portfolio's Broker field is locked, not re-selectable.**
Early on, `_render_broker_tab` gave every tab its own free `st.selectbox("Broker",
BROKERS, ...)`, defaulting to whichever broker sorted first -- so opening
"Dhan Corporate" could show "Zerodha" pre-selected in the dropdown, with
nothing stopping an upload there from silently mixing a second broker's
data into a portfolio whose name implied one specific account. Fixed by
`_portfolio_broker(portfolio_name)`, which derives the portfolio's one
real broker from its own saved holdings/positions (`{h.broker for h in
saved_holdings if h.portfolio_name == portfolio_name} | {p.broker for p
in saved_positions if ...}`) and renders it as a disabled single-option
`st.selectbox` -- shown, but not changeable. (A pre-existing portfolio
that somehow already mixed two brokers before this lock existed falls
back to picking one deterministically, sorted alphabetically, rather than
depending on set-iteration order.) The save button's label also now
distinguishes the two flows: **"Update Portfolio"** on an existing tab
(`_render_broker_tab`) vs. **"Create Portfolio"** on the "+ New portfolio"
tab and the first-portfolio flow, where the Broker dropdown stays fully
open since nothing has been saved under that name yet.

**Opening the tab you just acted on** (`st.tabs(..., key="broker_active_tab",
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
`st.session_state["broker_active_tab"]`.

**A second real bug, found immediately after the first fix**: the "+ New
portfolio" save path's `on_saved` callback originally wrote
`st.session_state["broker_active_tab"] = created_name` directly —
which raised `StreamlitAPIException: st.session_state.broker_active_tab
cannot be modified after the widget with key broker_active_tab is
instantiated` (confirmed live: creating "Portfolio 2" saved successfully,
then crashed the very next line). The cause: `on_saved` runs from
*inside* one of the tabs' content, which only executes *after*
`st.tabs(..., key="broker_active_tab")` already ran earlier in that
same script execution — Streamlit forbids writing to a widget's own
`session_state` key once that widget has been instantiated in the
current run, full stop, even from code that logically "belongs" to one
of its children. The fix: never write `"broker_active_tab"` directly
from inside a tab. Instead, stash the request in a plain (non-widget)
`"broker_pending_active_tab"` key and call `st.rerun()`; right before
`st.tabs(...)` is instantiated on the *next* run — i.e. before it exists
for that run — a short block pops the pending key and promotes it into
`"broker_active_tab"` (empty string `""` means "clear", used by the
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
computing every portfolio's upload-form state on every load.

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
`_render_broker_tab()`/first-portfolio call sites don't need this —
their name isn't user-editable text that could re-collide with itself.

**Deleting a portfolio** (`portfolio_repo.delete_portfolio(client,
user_id, portfolio_name)`) — an unconditional delete of every row for
`(user_id, portfolio_name)`, every broker within it (plus every Trade
grouping/metadata row -- `portfolio_trade_groups`/`portfolio_trade_meta`,
see My Trades below), leaving every other portfolio untouched. Rendered
inside each tab as a collapsed `st.expander("🗑️ Delete \"<name>\"")` at
the very bottom, below the upload section, so it's out of the way of the
normal update flow: a warning, an `st.checkbox` the user must tick ("I
understand -- permanently delete ..."), and an `st.button(...,
disabled=not confirm)` that only becomes clickable once that box is
checked -- a deliberate two-step confirmation, since there's no undo for
this one.

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
stocks, so this never affects them). `0018_company_type.sql` later
replaced that boolean with a proper category column,
`company_type` (`Equity`/`ETF`/`Index`/`Fund`, default `Equity`), and the
view's filter became `where c.company_type = 'Equity'` -- functionally
the same exclusion for ETFs, but it now also covers `Index` (three rows
`0018` seeds: NIFTY, BANKNIFTY, SENSEX -- see the Futures & Options
section's "Index F&O" paragraph for why) without a second flag, and
leaves room for a future `Fund` category with no code change to the view
itself. `looks_like_etf_name`'s classifier is unchanged -- it only ever
produces `ETF`, never `Fund` -- so LIQUIDCASE/GILT5YBEES/LTGILTCASE (debt/
gilt funds, not equity ETFs) stay classified as `ETF` for now rather than
being split out; `Fund` is reserved for a future pass at that distinction.

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
classify there -- a symbol this path registers first stays
`company_type = 'Equity'` until one of the other three paths next sees
it. Accepted as narrow in practice: real ETFs/funds don't have listed
derivatives, so this path being the *first* to register one would be
unusual. Migration `0015` also backfills the four ETFs already tracked
at the time it was written, verified via a live yfinance `longName`
check against every non-Nifty50 symbol tracked at the time (all four
straightforwardly matched; HINDZINC/INDUSINDBK/INDHOTEL/VAML correctly
did not) -- `0018` carries that same data forward (`ETF` rows stay
`ETF`) as part of its `is_etf` → `company_type` backfill.

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

**Per-holding covered-call suggestion ("CC ROI" / "CC Assignment ROI",
the Options page's "Portfolio CC" table)**: distinct from the
Dashboard/Options "5% CC" figure (`cc_5pct_for_rows`, always spot-based,
fixed 5% OTM, floor-filtered strike) -- this one is
`fo_service.covered_call_for_holding(ce_rows, avg_price, ltp, qty,
expiry_date)`, keyed off the *holding's own* avg buy price, not just the
spot price. **This used to also drive a pair of columns on My Holdings**
(`_load_covered_calls`, a page-wide "Covered call expiry" selectbox, an
`include_cc` gate on `_render_holdings_table`) -- all of that was removed
by request once the Stocks table was made to share the ETFs & Mutual
Funds table's exact columns; `covered_call_for_holding` itself is
unchanged and now only ever called from `pages/5_Options.py`'s Portfolio
CC table, which needs no separate expiry selector at all -- it just
reuses that page's own already-computed near/next/far expiry list (the
same one 5% CSP/CC above it uses) for the stock currently being viewed:

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
`st.session_state["holdings_table_<slug>"] = {"selection": {"rows": [i],
...}}` directly (the same schema a real click produces) and reruns the
script -- confirming `rows[i]["symbol"]` resolves to the right stock and
the right button renders, without needing the canvas to paint.

**F&O positions (`portfolio_positions`, migration `0016`)** — a sibling
table to `portfolio_holdings`, same per-user RLS shape
(`auth.uid() = user_id`), same delete-then-insert replace semantics
(`portfolio_repo.replace_broker_positions`), same primary key shape
(`user_id, portfolio_name, broker, raw_name`). `symbol`/`expiry_date`/
`strike_price`/`option_type` are nullable together: a row whose
instrument string didn't decode is still saved (`raw_name` always is)
but shown with no contract detail — same "save it anyway, degrade the
display" precedent as an unresolved holding, except there's no manual-
symbol-override form here, since a position's contract identity (unlike
a holding's free-text company name) is either decodable from the string
or it isn't; there's nothing for the user to correct. `qty` keeps its
broker-reported sign (negative = short). Deliberately no FK to
`option_contracts` — even with `0018`/`0019`'s Index-option widening
(NSE's `_FUTURES_TYPES`/`_OPTION_TYPES` keep `STF`/`STO`/`IDO`; BSE's
narrow to `IDO`-only, see "Two exchanges, one parser" above), a stock
option whose underlying only trades on BSE (never true for a Nifty50
constituent, but possible for a portfolio-only stock), a strike/expiry
this app hasn't ingested yet, or a plain CSV-parsing gap can all leave a
decoded position's contract missing from that table, so a hard FK would
be too strict. `pages/9_My_Positions.py` (and My Trades, which also reads
positions) degrades gracefully (an `st.info` pointing at the migration,
no `st.stop()`) if `portfolio_positions` doesn't exist yet -- unlike the
holdings-table load, which every Portfolio page `st.stop()`s on if
`0012`/`0014` aren't applied (there's nothing useful to show any of these
pages without holdings at all, but a missing `portfolio_positions` table
is a narrower, tolerable gap).

**Two broker position-export formats, decoded to a common shape**
(`src/services/portfolio_service.py`) — unlike holdings, *both* brokers'
positions exports already embed the exact NSE underlying symbol (no
company-name fuzzy matching needed for either):

- **Zerodha** (`parse_zerodha_positions_csv` /
  `parse_zerodha_option_instrument`) — the `Instrument` column is
  Zerodha's own F&O tradingsymbol, decoded via two regexes tried in
  order:
  - **Weekly** (`_ZERODHA_WEEKLY_OPTION_RE`), e.g. `NIFTY2681123000PE`:
    underlying (`[A-Z]+`, greedy — safe since NSE symbols are pure
    alphabetic, so the first digit unambiguously ends it), 2-digit year,
    a single month character (`1`-`9` for Jan-Sep, `O`/`N`/`D` for
    Oct/Nov/Dec), 2-digit day, strike, `CE`/`PE`. Today, only indices
    (NIFTY, BANKNIFTY, SENSEX, ...) have weekly expiries.
  - **Monthly** (`_ZERODHA_MONTHLY_OPTION_RE`), e.g. `NIFTY26AUG23100PE`
    or `SBIN25AUG970PE`: underlying, 2-digit year, 3-letter month
    abbreviation, strike, `CE`/`PE` — no day-of-month in the symbol at
    all, since NSE monthly F&O always expires the last Thursday of that
    month (`_last_thursday`, the same convention
    `src/data_providers/mock_provider.py`'s synthetic data already uses).
    **A real bug this fixed**: this format used to be deliberately left
    unparsed (there was no confirmed real sample, just a guessed shape) —
    a Zerodha-synced NIFTY monthly option's `Instrument` came back with
    `symbol=None`, so My Trades showed the raw tradingsymbol as the
    "Underlying Instrument" and sorted the trade into Other Trades
    instead of Index Trades. A real portfolio's synced data confirmed
    the guessed shape was exactly right, for both index and stock
    underlyings, so it's now decoded for both. **Caveat inherited from
    that same guess**: `_last_thursday` doesn't account for an exchange
    holiday landing on the natural last Thursday (which shifts the real
    expiry a day or more earlier) — fine for grouping/display, but this
    `expiry_date` isn't guaranteed to be the exact contract date the way
    the weekly format's is (that one is unambiguous, no calendar guess
    needed).
  
  A row matching neither regex (a futures contract, or a genuinely
  malformed instrument string) comes back with `symbol=None` and is
  saved undecoded rather than guessed.
- **Dhan** (`parse_dhan_positions_csv` / `parse_dhan_position_name`) —
  the `Name` column is Dhan's own space-separated format, e.g.
  `"ONGC 25 AUG 230 PUT"`, used identically for monthly stock options and
  weekly index options (no format difference between the two, unlike
  Zerodha). It carries no year at all, so the year is inferred as the
  nearest occurrence of that day/month on or after `as_of` (defaults to
  `date.today()`, explicit for testability, same `as_of` convention as
  `screener_service`/`refresh_service`) — a currently-open position's
  expiry can't be in the past, so if that day/month has already passed
  this year, it rolls to next year. Numbers are quoted with Indian-style
  grouping, same tolerant `_to_float` reuse as `parse_dhan_csv`.

**P&L is recomputed, not trusted from either file, and doesn't use
`ltp`+`avg_price` alone from Zerodha's own P&L column** —
`compute_positions_view`: `pnl = (ltp - avg_price) * qty`, which is
direction-correct for both a long (positive qty) and a short (negative
qty) position without an `if` — cross-checked against every row of both
sample export files this feature was built against (e.g. Dhan's
"HINDALCO 25 AUG 860 PUT", qty -700, avg 2.55, ltp 1.05: `(1.05 - 2.55) *
-700 = 1050`, matching the file's own reported "1,050.00" exactly).
`pnl_pct = pnl / (avg_price * abs(qty)) * 100` — against the *premium*
notional, since there's no equivalent of a holding's "investment" for a
written option. Both are `None` (→ "N/A") when `ltp` is missing. The two
sample brokers' own percentage columns were checked and rejected as a
data source: Dhan's "% Change" is direction-aware (matches this
`pnl_pct` formula exactly), but Zerodha's "Chg." is a raw, non-direction-
aware price change (`(ltp - avg_price) / avg_price * 100`) — the same
short HINDALCO-style position would show oppositely-signed numbers
depending on which broker's own column you trusted, so neither is
treated as authoritative; both are always recomputed the same way
instead.

**LTP itself, unlike holdings, is trusted from the uploaded file** rather
than fetched live. Holdings can always be revalued live because every
tracked equity symbol has a `daily_screener_snapshots` row from this
app's own refresh pipeline; there's no equivalent *live* source for
options at all -- `nse_fo_provider.py` is EOD-only for every underlying,
index or stock (see "This is end-of-day data" above) -- so "wait for the
next refresh" (the holdings playbook for an unpriced symbol) never
produces a live intraday quote for a CSV-uploaded position the way it
does for a holding. The file's own LTP is the only live-like number
available for the CSV-upload path, so it's used as-is; only the
*derived* P&L/P&L% are recomputed, per the point above. (A Dhan API-synced
position is different -- see "Connect Dhan account" below, which can
fall back to this app's own EOD F&O data, including index options as of
migration `0018`, when Dhan's own live quote is unavailable.)

**My Positions renders three tables per portfolio tab — Stock Options,
Index Options, Others** (`pages/9_My_Positions.py`), replacing an earlier
single flat table. `portfolio_service.classify_position_bucket(option_type,
symbol, company_type_by_symbol)` decides which: `option_type is None` →
`"other"` (an undecoded F&O row, a futures position, or a plain stock/ETF
bought/sold as a *position* rather than a holding — none of which this
page has ever separately supported valuing beyond raw qty/avg_price/pnl);
otherwise the *decoded* option's underlying sorts into `"index"`
(`company_type = 'Index'` only -- same Index-only rule
`classify_underlying_bucket` uses for My Trades, see that section for the
ETF/Fund bug this deliberately avoids) or `"stock"` (everything else,
including ETF/Fund). This is safe
to key off `option_type` alone (rather than checking `symbol` too) because
every position-parsing path — `parse_zerodha_option_instrument`,
`parse_dhan_position_name`, `dhan_positions_from_api`,
`zerodha_positions_from_api` — always sets `option_type` and `symbol`
together from one decode-or-nothing result; there's no row with one set
and not the other. The Stock/Index Options tables share identical columns
(**Instrument** — the broker's raw contract string; **Underlying** — the
decoded symbol; **Expiry**; **Strike**; **Type**; **Qty**; **Avg Price**;
**P&L**; **P&L %**); Others drops the four option-specific columns since
they don't apply (**Instrument** — here just `raw_name`, since `symbol` is
always `None` in this bucket; **Qty**; **Avg Price**; **P&L**; **P&L %**).
The page-level "Total P&L" stat above the three tables still sums across
all of them, unchanged.

**Connect Dhan account (`broker_connections`, migration `0017`)** — an
alternative to CSV upload, Dhan only. `pages/6_My_Broker.py` offers a
CSV-vs-API toggle whenever the selected broker is Dhan; picking "Connect
Dhan account" saves a Client ID + Access Token (`upsert_broker_connection`)
and a "Sync now" button drives the sync (`_sync_dhan`). This reuses
`src/data_providers/dhan_provider.py`'s existing `DhanProvider` class —
already present in this codebase as an alternative live-price source for
the main screener pipeline (`settings.market_data_provider == "dhan"`,
using one app-wide credential pair) — rather than a separate module,
since the auth/header/throttle mechanics are identical; only the
credentials differ (per-user/per-portfolio here, vs. one pair from
`.env`/`Settings` for the price pipeline). Three new methods were added to
that same class: `get_holdings()` (`GET /v2/holdings`), `get_positions()`
(`GET /v2/positions`), and `get_ltp_by_security_id()` (`POST
/v2/marketfeed/ltp`, generalized to arbitrary exchange segments like
`NSE_FNO`/`IDX_I`, unlike the existing `get_quotes()` which is hardcoded
to `NSE_EQ` for the equity pipeline). These deliberately skip the
`@retry`-decorated, auto-backoff `_post()` the price pipeline uses — a
manual "Sync now" click should fail fast (especially on an expired token)
rather than silently retrying for up to ~20s — and instead go through a
plain `_request()` that raises `DhanAuthError` (a `ProviderError`
subclass) specifically on a 401, so the page can show "your token expired"
instead of a generic error.

`portfolio_service.dhan_holdings_from_api`/`dhan_positions_from_api`
translate Dhan's raw JSON rows into the exact same dict shapes the CSV
parsers produce, so every downstream function
(`holdings_to_records`/`positions_to_records`/`compute_portfolio_view`/
`compute_positions_view`) and the rendered tables are identical regardless
of source. Two things are notably *easier* here than the CSV path:
`tradingSymbol` in the holdings response is already the exact NSE symbol
(no `match_symbol()` fuzzy matching needed), and the positions response
carries `drvExpiryDate`/`drvStrikePrice`/`drvOptionType` directly (no
regex instrument-name decoding needed) — only the underlying symbol itself
is extracted from `tradingSymbol` by a best-effort leading-alphabetic-run
regex (`_dhan_underlying_symbol`), confirmed against a real account's
positions (`tradingSymbol` looks like `"NIFTY-Aug2026-23000-PE"` /
`"HDFCBANK-Aug2026-700-PE"` — always letters up to the first `-`, so the
regex holds). One thing *isn't* as documented: `drvOptionType` comes back
as the full word (`"PUT"`/`"CALL"`), not the `CE`/`PE` code used elsewhere
in this app (`option_contracts.option_type`, Dhan's own CSV export) — the
docs excerpts pulled during planning didn't show a sample value, and this
was only caught by testing against a real connected account (all 27
positions came back with `option_type=None` before the fix). `_DHAN_OPTION_TYPES`
now accepts both spellings. Positions carry no LTP of their own, so
`get_ltp_by_security_id` is called separately for the distinct
`(exchangeSegment, securityId)` pairs before translating — in practice
this call needs Dhan's separate "Data APIs" subscription (distinct from
"Trading APIs"); without it, Dhan returns a 401 ("Data APIs not
Subscribed") that's caught as a `DhanAuthError` and degrades to no LTP
from Dhan for every position, rather than failing the whole sync.
`_sync_dhan` then calls `portfolio_service.apply_fallback_option_ltp`,
which fills any still-missing `ltp` from this app's own F&O data
(`option_daily_prices` via `latest_option_chain_view`,
`fo_repo.get_option_chain`) — matched on `(symbol, expiry_date,
strike_price, option_type)`, the previous trading day's close rather than
a live tick, but real data instead of a blanket N/A. Verified against a
real synced account (before `0018`'s Index widening): 21 of 27 positions
resolved this way; the 6 misses were all NIFTY index options, which this
app tracked no F&O data for at all at the time. `0018` closes most of
that gap -- NIFTY/BANKNIFTY option chains are now ingested too (see the
Futures & Options section's "Index F&O" paragraph) -- so those same 6
positions resolve once `fetch_fo_data.py` has actually run against the
widened universe; only a SENSEX position or a strike/expiry genuinely
outside the tracked chain would still fall back to N/A.

**A real bug this eventually caused (migration `0026_portfolio_positions_ltp_as_of.sql`)**:
a fallback LTP looked visually identical to a live one everywhere it was
shown — confirmed live: a JioFin CSP showed LTP 3.30 on My CSP (the
previous NSE F&O refresh's close) while Dhan's own app showed a live
4.40 for the exact same contract, with nothing in this app distinguishing
"live" from "yesterday's close" a full trading day apart. Fixed by
threading the fallback chain row's own `trade_date` through as
`ltp_as_of` on `PortfolioPosition` (`None` = live/as-given — Zerodha's
own `last_price`, a successful Dhan Market Quote call, or a CSV upload's
own LTP column; a date = this fallback fired). `apply_fallback_option_ltp`
now returns `(price, trade_date)` pairs from its lookup table instead of
just `price`, and only ever sets `ltp_as_of` on the same branch that
already only-ever-fills-a-gap (never touches a position whose `ltp` a
broker/CSV already supplied). `positions_to_records` reads it via
`p.get("ltp_as_of")`, not `p["ltp_as_of"]`, since every other
position-parsing path (CSV, Zerodha API) never sets that key at all —
same "missing key means live" convention the `None` default already
implies. My CSP's `LTP` column shows `"₹4.40"` for a live quote or
`"₹3.30 (as of 17 Aug 2026)"` for a fallback one (`_fmt_ltp`, same
"value (extra info)" string-column convention `_fmt_breakeven` already
uses on that page) — same idea as the Dashboard's own `"(as of <date>)"`
suffix for a stale screener price, just sourced from a different table.
My Positions doesn't need the equivalent treatment: it dropped its own
`LTP` column entirely on an earlier request (only used internally to
compute P&L/P&L%, never displayed), so there's no live-vs-fallback
distinction to surface there.

**Security trade-off, stated in the UI itself, not just here:** the
access token can also place trades — Dhan has no read-only scope for an
individual account — and it's stored in `broker_connections` as entered,
protected only by that table's RLS policy (`auth.uid() = user_id`), the
same protection model as every other per-user table in this app. No
application-level encryption is applied. This app's own code only ever
calls the read-only endpoints above. The token also expires after 24
hours (a Dhan platform limit, not a choice made here), so there is no
background/scheduled sync in this version — `token_saved_at` only tracks
when credentials were last saved, purely so the page can warn once it's
old enough to likely be expired.

**Connect Zerodha account (`broker_connections.api_secret`, migration
`0022`)** — Zerodha's Kite Connect API is architecturally nothing like
Dhan's: no self-service "generate a token" page, a paid per-app
subscription (₹2,000+GST/month, on Zerodha's side), and a proper
OAuth-style login redirect instead of a pasted credential.
`src/data_providers/zerodha_provider.py::ZerodhaProvider` is deliberately
**not** a `PriceDataProvider` (unlike `DhanProvider`, which doubles as
this app's own equity price-pipeline source) -- Zerodha isn't and isn't
planned to be that (`config.py`'s `market_data_provider` stays
`Literal["dhan", "yfinance", "mock"]`), so this class only implements
what the connect flow needs: `login_url()`, `generate_session()`,
`get_holdings()`, `get_positions()`.

The session exchange: `login_url()` sends the browser to
`kite.zerodha.com/connect/login?v=3&api_key=...`; Zerodha's own login
page handles the actual authentication (this app never sees the
password/TOTP) and redirects to whatever **Redirect URL is configured on
the Kite Connect app itself** (not something this app's code controls
per-request) with `?request_token=...&action=login&status=success`
appended. `generate_session(request_token)` computes
`sha256(api_key + request_token + api_secret)` and `POST`s it to
`/session/token` -- the checksum is what proves the exchange request
came from whoever holds `api_secret`, not just anyone who happened to
observe a `request_token` value (e.g. in browser history or a proxy
log). This is safe to do directly in `pages/6_My_Broker.py`'s own code
because Streamlit page scripts run **server-side** (unlike a browser
SPA), so `api_secret` is never sent to or exposed in the browser.

**`app.py` pins `url_path="My_Broker"`** on that page's `st.Page(...)`
entry specifically so the URL to register as the Kite Connect app's
Redirect URL (`{app_base_url}/My_Broker`) doesn't silently change if the
underlying filename is ever renamed -- Streamlit's default `url_path`
inference is filename-derived, which would otherwise be an easy way to
quietly break every existing user's Zerodha connection.

**Handling the redirect back is deliberately not session-state-
dependent.** `st.link_button` (used for "Log in to Zerodha") always
opens a **new browser tab** -- confirmed from Streamlit's own docstring:
"When clicked, a new tab will be opened to the specified URL. This will
create a new session for the user if directed within the app." That new
tab is a fresh Streamlit session, so `st.session_state["zerodha_connect_pending"]`
(set right before rendering the login link, remembering which
`portfolio_name` initiated it) is not guaranteed to survive into the tab
that lands back with `request_token`. Two consequences designed around
explicitly: (1) since `require_login()` gates every page purely on
`st.session_state`, a fresh session in the new tab means the user likely
has to sign back into *this app* (not Zerodha again) before reaching the
`request_token`-handling code -- annoying but not broken, since
`st.query_params` survives `require_login()`'s own internal reruns, so
the pending Zerodha exchange is still there once they're signed in; (2)
the `request_token`-handling block (near the top of
`pages/6_My_Broker.py`, before the tabs render) shows a **portfolio
picker**, defaulting to the remembered pending portfolio *if* it
survived, but never requiring it to -- selecting the wrong portfolio (or
one with no saved `api_key`/`api_secret` for Zerodha yet) just shows a
clear error pointing back to where to fix it, rather than silently
misattributing the connection. `st.query_params.clear()` runs right
after a successful exchange so a page refresh can't try to re-spend the
same (single-use) `request_token`.

**Kite Connect's access_token expires at a fixed daily time (~6am IST
the next day), not a rolling window** -- `_zerodha_token_is_fresh()`
compares `token_saved_at` against the most recent 6am IST boundary
(`src/utils/timezones.py::now_ist`/`to_ist`), not an "hours old" check
like Dhan's `_hours_since()`/23-hour warning. This is stated as an
approximation in its own docstring -- the exact invalidation time isn't
published to the minute, and (same caveat as the rest of this feature)
hasn't been verified against a real Kite Connect account, only against
Zerodha's own published documentation.

**Reuse worth noting**: `portfolio_service.zerodha_positions_from_api`
decodes each position's `tradingsymbol` via the *already-existing*
`parse_zerodha_option_instrument` (built for the CSV positions export) --
Kite Connect's own `tradingsymbol` field is in the exact same format, so
no new regex was needed, unlike Dhan's API/CSV paths which needed two
separate decoders (`_dhan_underlying_symbol` vs. the CSV path's own
column mapping). Kite's holdings/positions responses also both include
`last_price` directly, so unlike `dhan_positions_from_api` (which needs
a separate `get_ltp_by_security_id` call, and `apply_fallback_option_ltp`
as a fallback for accounts without Dhan's "Data APIs" subscription),
`zerodha_positions_from_api` needs neither -- `_sync_zerodha` is
correspondingly simpler than `_sync_dhan`.

`BrokerConnection.access_token` was relaxed to nullable (`0022`, same
migration that added `api_secret`) because Zerodha's flow legitimately
has an intermediate state Dhan's never does: `api_key`/`api_secret`
saved, but no `access_token` yet (before the first login completes).
`portfolio_repo.upsert_broker_connection`'s `model_dump(exclude_none=True)`
already made this safe without any repo changes -- omitting
`access_token` from a save's payload (because the model field is `None`)
means the upsert leaves any existing value untouched rather than nulling
it out, which is exactly "don't touch the token" for the
credentials-only save and "update the tabs' cached copy of api_key
without disturbing a still-valid session" for the "Update API Key /
Secret" form.

**A real bug found right after shipping**: the page showed "Connected --
session started 1 minute(s) ago" and a working-looking "Sync now" button
immediately after just saving API Key/Secret -- *before* ever clicking
"Log in to Zerodha" at all. Cause: `broker_connections.token_saved_at`
is `not null default now()` (migration `0017`); the credentials-only
save omits it from the upsert payload (no real session exists yet), but
since that's a fresh `INSERT` for this `(portfolio_name, broker)`,
Postgres's own column default stamps it "now()" regardless. The page's
"is this session still good" check
(`_zerodha_token_is_fresh(connection.token_saved_at)`) only looked at
that timestamp's freshness, not whether `access_token` itself was
actually present -- so a *just-saved-but-never-logged-in* connection
looked identical to a *freshly-logged-in* one. Fixed by requiring
`connection.access_token and _zerodha_token_is_fresh(...)` together, not
freshness alone.

**A second real bug, found against a live account with pledged
holdings**: several ETF holdings (GILT5YBEES, LIQUIDCASE, LTGILTCASE,
NIFTYBEES) were missing from My Holdings entirely after a real sync,
even though they were genuinely held. Cause: Kite's `quantity` field on
a holdings row is the *free* (non-pledged) quantity only -- confirmed
live, and visible in Kite's own web UI too (it shows "Qty. 0" plus a
separate "P: 7500"-style pledged-quantity badge for a fully-pledged
holding). `zerodha_holdings_from_api`'s `if not qty: continue` guard
(meant to skip a holding sold off entirely) was silently dropping every
*fully-pledged* holding too, since Kite reports its free quantity as
zero. Fixed by summing `quantity + t1_quantity + collateral_quantity` to
reconstruct the true total owned quantity -- verified against the same
live account that `average_price * (this sum)` matches Kite's own
displayed "Invested" figure exactly (GILT5YBEES: 64.30 × 7500 =
₹4,82,250.00, to the rupee). Zerodha's Positions API has no equivalent
pledging concept (F&O margin isn't posted via share pledging), so
`zerodha_positions_from_api` isn't affected by this.

**Holdings split by `company_type` (ETFs & Mutual Funds vs Stocks)** — the
old single "My Holdings" table became two, `_render_holdings_table` (a
small helper factored out of what was previously an inline block in
`_render_holdings_tab` (`pages/8_My_Holdings.py`), so both tables share
the exact same columns, `column_config`, and single-row-selection "Open
in Stock Detail"/"Open in Options" behavior, just keyed with a different
`key_suffix` so their widgets don't collide). The split itself is a pure
filter over `companies.company_type` (loaded via the already-cached
`src/utils/portfolio_page.py::load_all_companies`, the same loader the
Dhan CSV upload path -- now `pages/6_My_Broker.py` -- already used for
name-matching): `ETF`/`Fund` symbols go to "ETFs & Mutual Funds",
everything else (`Equity`, `Index`, and any holding with no resolved
symbol at all -- there's no company_type to check for one of those) goes
to "Stocks". The Total Investment/Cur Val/P&L/P&L% stat grid above both
tables is untouched -- it still aggregates across every holding regardless
of which table it lands in.

**The two Holdings tables' columns have gone back and forth a few times,
by user request each time -- currently identical again.** Originally
both tables shared the same base columns; then Stocks gained CC ROI/CC
Assignment ROI and ETFs & Mutual Funds gained 1D/5D/20D Change (plus a
TTM PE column that existed only briefly, sourced from the same
`returns_pe_by_symbol` dict's `pe_ratio` field, yfinance's `trailingPE`
-- removed along with the first round of CC columns); most recently, CC
ROI/CC Assignment ROI were dropped from Stocks entirely (see "Per-holding
covered-call suggestion" above -- that figure now lives only on the
Options page's Portfolio CC table) and 1D/5D/20D Change was added to
Stocks too, so `_render_holdings_table` no longer needs an `include_cc`
knob at all -- `returns_pe_by_symbol` (still sourced from
`snapshot_repo.get_latest_returns_and_pe`, still fetching `pe_ratio` even
though nothing displays it -- no reason to special-case it out of one
already-batched query for a field that costs nothing extra) is now a
required parameter, computed once per portfolio tab over *every* held
symbol and passed to both table calls unchanged.

Sourced by a bulk repo function, `snapshot_repo.get_latest_returns_and_pe`,
modeled directly on the existing `get_latest_prices` right above it in
the same file: queries `daily_screener_snapshots` directly (not
`latest_screener_view`, whose inner join on `nifty50_constituents.
is_current` would silently drop every portfolio-only ETF), takes each
symbol's single most recent row (`order by snapshot_date desc`, first
row per symbol wins, no cross-row carry-forward for a field that's null
in that latest row -- same simple convention Stock Detail's own PE
display already uses via `get_latest_screener_row`), and returns
`{symbol: {return_1d, return_5d, return_20d, pe_ratio}}`.

`daily_screener_snapshots` only stores *percentage* returns, not
historical closes, so the "1D/5D/20D Change" columns' rupee amounts are
derived, not read: `src/calculations/returns.py::value_change_from_pct
(current_value, return_pct)` is the algebraic inverse of the same file's
`pct_return` (`base = current/(1+pct/100)`, `change = current - base`,
which simplifies to `current * pct/(100+pct)`). Passing `cur_val`
(`qty * ltp`, the holding's *total* current value) rather than `ltp`
alone gives the position's whole-holding rupee change, matching how the
existing P&L column is already a rupee amount over the position, not a
per-share one -- the formula is linear in `current_value` so either
works, this just matches the table's existing convention. Formatted as
one combined string per cell (`_fmt_value_change`, e.g.
`"₹+588.24 (+2.00%)"`, or `"—"` when either half is missing), not split
across separate amount/percent columns like P&L/P&L% -- deliberately
different from that existing pair, since three periods as six columns
would be repetitive.

**My Trades (`pages/7_My_Trades.py`) + Analyse Trade (`pages/10_Analyse_Trade.py`)**
— holdings and F&O positions sharing an underlying, grouped into "Trades".
This used to be F&O-positions-only (the original `portfolio_trade_groups`
"Trades" feature, migration `0020`); My Trades widens the same mechanism
to holdings too and adds a real detail page instead of an inline
combine/split control under the positions table.

`src/utils/portfolio_page.py::build_trade_legs(client, cache_bust,
holdings_for_portfolio, positions_for_portfolio)` is the shared leg
builder both `7_My_Trades.py` and `10_Analyse_Trade.py` call (so their
grouping/labels never drift apart): holding rows are priced via
`compute_portfolio_view` (called on the **unmerged** per-broker dicts,
not `merge_holdings`'s cross-broker-combined rows -- Trades need leg-
level identity, the same `(broker, raw_name)` natural key
`portfolio_trade_groups` is keyed by) and re-tagged `leg_type="Holding"`
by zipping the input list against `compute_portfolio_view`'s output rows
(that function builds a fixed-key dict per row, so any caller-supplied
extra key like `broker` doesn't survive through it and has to be
reattached afterwards); position rows go through the existing
`compute_positions_view`, which *does* pass extra keys through
(`{**p, "pnl": ..., "pnl_pct": ...}`), so tagging `leg_type="Position"`
before calling it is enough.

`portfolio_service.classify_underlying_bucket(symbol, company_type_by_symbol)`
decides which of the three tables a leg's underlying belongs in **by
default**: `None` symbol → `"other"` (an undecoded F&O contract or
unmatched holding -- nothing to classify by); `company_type = 'Index'`
only → `"index"`; `company_type` `ETF`/`Fund` → `"other"` too (neither a
real equity trade nor a genuine index); anything else, including an
unknown symbol → `"stock"` (mirrors the fallback the old combined page's
`_is_etf_or_fund` helper used for the Holdings ETF/MF split). **Two real
bugs this fixed, in sequence**: ETF/Fund first got lumped in with Index,
so gilt/liquid/gold ETFs (BANKBEES, GILT5YBEES, GOLDBEES, LIQUIDCASE,
...) showed up in "Index Trades" alongside genuine index positions
(NIFTY, FINNIFTY) -- confirmed live against a real portfolio. Moving
ETF/Fund to `"stock"` instead (only a true Index company_type counts as
`"index"`) was a second live request's worth of correction -- an ETF
isn't a real equity trade either, so it now defaults to `"other"`
instead.

`portfolio_service.group_into_trades(legs, overrides, trade_meta,
company_type_by_symbol)` assigns `trade_id` via `assign_trade_ids`
(unchanged -- see above, it only ever touched symbol/raw_name/broker, so
it already worked for holding legs and position legs alike), groups legs
by it, and for each trade computes `bucket`: `trade_meta`'s
`bucket_override` if set (migration `0024` -- the user manually pinned
this trade to a table, e.g. an index ETF they deliberately want shown
alongside genuine Index Trades despite its `company_type` being `ETF`
not `Index`), else unanimous across the trade's own legs'
`classify_underlying_bucket`, else `"other"` when the legs' computed
buckets disagree (deliberate: a trade mixing a stock leg and an index
leg, via a manual merge, doesn't cleanly belong to either specific
table). Also computes a default underlying label (sorted, `" + "`-joined
distinct `symbol or raw_name` across the legs -- the same "Symbols"
summary format the old Trades table used), and `leg_count`/`total_pnl`
(summed over the trade's own priced legs). My Trades then just buckets
the returned list into three tables by `bucket`. `pages/9_My_Positions.py`'s
`classify_position_bucket` mirrors the same Index-only rule for
Stock/Index Options (see the Positions subsection), but has no
`bucket_override` equivalent -- there's no per-leg meta table for
positions the way `portfolio_trade_meta` exists for trades.

**Manual override (`10_Analyse_Trade.py`'s "Table (on My Trades)"
selectbox, migration `0024_portfolio_trade_meta_bucket_override.sql`)**
-- `_BUCKET_LABELS`/`_BUCKET_VALUES` map between the dropdown's four
options (`"Auto (based on underlying)"` → `None`, plus the three
table names → `"stock"`/`"index"`/`"other"`) and what's actually stored
in `portfolio_trade_meta.bucket_override`. Defaults to whatever's
already saved for this `trade_id` (`None` shows "Auto"), same pattern as
the underlying-label/trade-type fields in the same form; saved via the
same `set_trade_meta` upsert, just with one more keyword argument.

**`portfolio_trade_meta` (migration `0021`) — a new table, not an
extension of `portfolio_trade_groups`.** The user asked My Trades to
show a "Trade Type" (defaulting to "Trade", freely renameable) and to
let the underlying itself be corrected (free text, not constrained to a
known symbol -- e.g. a resolved "Tata Motors" corrected to the real
post-demerger underlying "Tata Motors Passenger Vehicle"). Neither of
these fits `portfolio_trade_groups`' key: that table is per-*leg*
`(portfolio_name, broker, raw_name) -> trade_id`, but a label/type
applies to the whole *trade*, so a second table keyed
`(portfolio_name, trade_id) -> {underlying_label, trade_type}` was
needed instead. `portfolio_repo.set_trade_meta` upserts one row (`on_conflict
="user_id,portfolio_name,trade_id"`); `underlying_label=None` clears
back to the auto-computed default while still writing a row (so a
previously-corrected label can be explicitly reset). `10_Analyse_Trade.py`'s
edit form only calls `set_trade_meta` with a non-`None` label when the
edited text differs from `trade["default_underlying_label"]` -- leaving
the field unchanged doesn't create an unnecessary override row.

**Merge and split, generalized to holdings + positions.** Analyse Trade
reuses the exact same `portfolio_repo.set_trade_group`/
`clear_trade_group_overrides` calls the old Trades feature already had
-- "Merge into this trade" (a multiselect of the portfolio's *other*
`trade_id`s) collects every leg across the selected source trades and
reassigns them all to the current trade's id; "Split" (multi-row-select
over *this* trade's own legs table) either clears their override (back
to the default per-underlying grouping) or assigns them a fresh
`trade_id`. Same keying rationale as before: `(portfolio_name, broker,
raw_name)` is a leg's natural identity, stable across
`replace_broker_holdings`/`replace_broker_positions`'s full
delete-then-insert on every upload/sync -- the exact same trick
`broker_connections` already relies on to survive a holdings re-upload,
now shared by two override tables instead of one.

Two sharp edges worth knowing, neither a bug:
1. If a broker ever changes how it formats `raw_name` for the same
   contract, or the same `(portfolio_name, broker)` pair starts being
   synced from a different source (e.g. switching a Dhan portfolio from
   CSV upload to "Connect Dhan account", which produces `raw_name` from
   `tradingSymbol` instead of the CSV's own `Name` column), a
   previously-grouped leg's `portfolio_trade_groups` override row simply
   stops matching any current leg and silently falls back to its default
   per-underlying Trade. Nothing errors or crashes; the manual grouping
   for that one leg just needs to be redone.
2. If a trade is renamed via merge (its `trade_id` changes to the target
   trade's id), any `portfolio_trade_meta` row for the *old* `trade_id`
   is simply left behind, unused -- no automatic carry-forward. This is
   deliberate, not an oversight: the target trade already has its own
   (possibly already-customized) label/type, and silently overwriting it
   with the source trade's would be more surprising than a clean
   "re-enter it if you still want it".

**A real bug this caused**: splitting legs out of a trade with a large
leg count threw `IndexError: list index out of range` on
`trade_legs[i]` -- confirmed live, splitting 2 legs out of a 10-leg
trade. Root cause: the legs table's row-selection state
(`st.dataframe(..., on_select="rerun", key=legs_table_key)`) is keyed by
widget key, not by the data it was computed against, so it survives the
`st.rerun()` a successful split triggers completely unchanged -- but
`trade_legs` is rebuilt from the now-smaller trade on that rerun, so a
previously-selected row index past the new (shorter) list's end
crashes `trade_legs[i]` on the very next render, even though the split
itself had already succeeded. Fixed two ways: `st.session_state.pop(legs_table_key,
None)` right before every `st.rerun()` that follows a merge or split
(both change `trade_legs`' composition, not just split), so a fresh
selection starts empty next render instead of carrying stale indices
forward; plus a defensive `[i for i in all_selected_rows if i <
len(trade_legs)]` filter where `selected_leg_rows` is computed, so any
future path that reruns this page without remembering to clear
`legs_table_key` degrades to "selection cleared" instead of crashing.

`10_Analyse_Trade.py` itself is reached only via `st.session_state["analyse_trade_id"]`/
`["analyse_trade_portfolio"]` + `st.switch_page` from My Trades' row
selection (see the "Streamlit app" section's `visibility="hidden"`
note above) -- landing on it directly with neither key set (a stale
bookmark, or the trade having been fully split away since) shows an
`st.info` pointing back to My Trades instead of crashing on a missing
lookup.

**My CSP (`pages/11_My_CSP.py`)** — a flat, un-grouped view of every
*position* leg belonging to a Trade whose `trade_type` is "CSP". There's
no dedicated boolean/tag column for this anywhere in the schema; it
deliberately reuses the same free-text `trade_type` field Analyse Trade
already lets you rename to anything ("Covered Call", "Aug Iron Condor",
...) — "CSP" is just a convention this one page happens to filter on.
`portfolio_service.is_csp_trade_type(trade_type)` does the match
(`trade_type.strip().lower() == "csp"`, so "CSP"/"csp"/" CSP " all
count). The page calls the exact same `build_trade_legs` +
`group_into_trades` pipeline My Trades/Analyse Trade use (so a leg's
`trade_type` here is identical to what those pages show), filters to
`is_csp_trade_type(t["trade_type"])` trades, then flattens to
`leg["leg_type"] == "Position"` legs only — a Holding leg has no
expiry/strike/option_type to show, so a Trade that somehow got renamed
"CSP" while only containing holdings (or a merge that pulled a holding
leg in alongside a short put) silently contributes nothing from that
leg rather than showing a row full of blanks. `bucket` (Stock/Index/
Other) is irrelevant here and ignored — a CSP is filtered purely by
its Trade Type, regardless of which My Trades table it'd otherwise
sort into.

**Column order, left to right (rearranged several times on request
since this page first shipped)**: `Trade Date`, then what My Positions
already shows (`Underlying`/`Expiry`/`Strike`/`Qty`/`Avg Price` --
**`Instrument` dropped on request**, redundant with
`Underlying`/`Expiry`/`Strike` for a single-leg CSP and just ate table
width; Analyse Trade's own legs table still shows it, since that's the
one spot it's still useful for telling legs apart), then `Max Credit`,
`LTP`, `P&L`, `Target P&L`, `Stop Loss`, `Breakeven`, `LTP Underlying`,
`Momentum`, `1D`, `5D`, `20D`. `Trade Date` leads (everything else on
the row can depend on it); `Max Credit` sits right after `Avg Price` on
request (it's `Avg Price * |Qty|`, so reads naturally as "what Avg
Price actually adds up to"); `Target P&L`/`Stop Loss` sit right after
`P&L` since they're the other P&L-shaped numbers; `Momentum` sits just
before `1D`/`5D`/`20D` (the returns it's computed from) — see the dict
literal in `_render_csp_tab` for the exact order, which `pd.DataFrame`
preserves as column order.

**`P&L%` was folded into `P&L` itself, on request** — there's no
separate `P&L%` column anymore. `_fmt_pnl(pnl, pnl_pct, target_pnl,
stop_loss)` renders one combined cell, `"₹1,234.56 (+12.34%)"`, the same
"value (pct%)" shape `Breakeven`/`Target P&L` already use — and prefixes
it with **✅** once `pnl > target_pnl` or **❌** once `pnl < stop_loss`
(both `>`/`<`, not `>=`/`<=`), no marker at all when neither threshold
is crossed, including whenever `target_pnl`/`stop_loss` themselves
aren't computable yet (no Trade Date, no saved Stop Loss row). Compares
against `new_stop_loss` — the freshly ratcheted value about to be shown
in the `Stop Loss` column and upserted this render — not the
`existing_stop_loss` read from the database, so the marker always
agrees with what's actually displayed. Similarly, **`Target P&L` now
shows what % of `Max Credit` it represents in parentheses**,
`_fmt_target_pnl(target_pnl, max_credit)` → `"₹4,275.00 (85.00%)"`, an
em dash before Trade Date is set. Both `_fmt_pnl`/`_fmt_target_pnl` are
page-local (not unit-tested, matching `_fmt_breakeven`/`_fmt_ltp`
alongside them) — coverage here is the `AppTest` import-level smoke
check plus manual verification, the same tradeoff every other
page-local formatter on this page already accepts.

`LTP Underlying` is the underlying stock's own current price (`snapshot_repo.get_latest_prices`,
the same call My Holdings uses for its Cur Val column — a direct
`daily_screener_snapshots` query, not `latest_screener_view`, so it still
resolves for a portfolio-only symbol not in `nifty50_constituents`);
`1D`/`5D`/`20D` are that stock's own `return_1d`/`return_5d`/`return_20d`
(`snapshot_repo.get_latest_returns_and_pe`, the same fields My Holdings'
"1D/5D/20D Change" columns are derived from — named without the
"Underlying" suffix those columns first shipped with, on request, since
every column on this page is already about the underlying except the
option-leg ones that come first) shown as a **plain percentage**, not
converted to a rupee amount the way My Holdings does via
`value_change_from_pct` — there's no "current value of the underlying"
concept to apply that conversion to here, only the stock's own price, so
the raw percentage is the more direct fit. `Momentum` reuses
`src.calculations.classification.criterion_b(return_1d, return_5d,
return_20d)` unchanged — the exact same "all three returns positive"
rule the Dashboard's screener classifies every stock on for its own
Momentum criterion — rendered with the same `pass_fail_icon` (✅/❌/—)
the Dashboard uses, so a CSP's underlying always shows the identical
Momentum verdict it would on the Dashboard. All of `LTP Underlying`/
`1D`/`5D`/`20D`/`Momentum` are `None`/`—` for a leg with no resolved
`symbol` (an undecoded contract), same as everywhere else on these
pages.

**Breakeven** is a combined price+percentage string, same "value (pct%)"
convention as My Holdings' 1D/5D/20D Change columns (`_fmt_value_change`
there, `_fmt_breakeven` here) — plain text, not a `NumberColumn`, since
it packs two numbers into one cell: `"₹23,455.00 (-2.27%)"`. Two pure
functions build it:
- `portfolio_service.csp_breakeven_price(strike_price, avg_price)` — the
  textbook CSP breakeven price, `strike_price - avg_price`: the premium
  collected for writing the put (`avg_price`, the price it was sold at)
  reduces the effective cost basis below the strike by that much.
- `portfolio_service.csp_breakeven_pct(breakeven_price, underlying_ltp)`
  — `(breakeven_price / underlying_ltp - 1) * 100`, how far that
  breakeven price sits from the underlying's *current* price (`LTP
  Underlying`, not the option leg's own `LTP` — the option premium is
  far too small relative to the strike for a direct comparison there to
  mean anything). Negative means breakeven sits below the current
  price — there's cushion, the underlying would need to fall by that %
  before the position starts losing money past the premium collected;
  positive means the underlying has already fallen through breakeven.

Kept as two separate functions (rather than one, or inlined in the page)
so each half is independently unit-testable without a live Supabase
connection, same reasoning every other calculation on these pages
follows.

**Trade Date / Target P&L / Stop Loss (migration
`0025_portfolio_position_meta.sql`, new table `portfolio_position_meta`)**
— a real request after CSP tracking existed: gauge whether a CSP is
"ahead of schedule" on theta decay, and auto-manage a trailing stop.
Deliberately a **separate table from `portfolio_trade_meta`, keyed
per-LEG** `(portfolio_name, broker, raw_name)` rather than per-*trade*
`(portfolio_name, trade_id)`: a Trade's default grouping is one Trade
per underlying symbol (`assign_trade_ids`), so two CSPs on the same
underlying at different strikes/expiries/entry dates would otherwise
collide on one `trade_id` and be forced to share a single trade date and
stop loss — keying per-leg instead matches My CSP's own row granularity
exactly (`src/utils/portfolio_page.py::load_position_meta`,
`portfolio_repo.list_position_meta`/`set_position_trade_date`/
`set_position_stop_loss`, and `delete_portfolio` extended to also clear
this table).

- `portfolio_service.csp_max_credit(avg_price, qty)` — `avg_price *
  abs(qty)`. `abs()` because `qty` is signed (negative for a short
  position), but "credit received" is inherently positive.
- **Trade Date** is user-entered on **Analyse Trade**
  (`pages/10_Analyse_Trade.py`), not on My CSP itself — folded into the
  same `st.form` as the underlying/trade-type/table edits, as a single
  `st.date_input` placed right after "Underlying Instrument" (gated
  behind `if position_legs:` so a pure-Holding Trade shows none). One
  date represents the whole Trade: on Save, it's written via
  `portfolio_repo.set_position_trade_date` to **every** `Position` leg
  in the Trade, not just one — multi-leg Trades (e.g. a strangle) are
  near-always entered as one package on a single day, so per-leg
  precision isn't worth the extra UI. The field's own `value=` shows the
  first Position leg's already-saved `trade_date`, if any (an earlier
  version had per-leg dates disagree only in the edge case of a Trade
  built by merging legs that were dated separately before the merge —
  Save reconciles them to one value going forward). Originally lived
  directly below My CSP's table with its own instrument-picker sub-form,
  scoped to that one Trade's CSP legs; moved and simplified on request
  so it applies to any Trade being analysed (not just ones already
  tagged "CSP" — useful for setting it *before* renaming a Trade Type to
  "CSP") and needs one Save instead of a picker-then-form two-step.
  There's no broker export or API this app talks to that reliably
  carries the original entry date for an already-open position, so
  unlike every other value derived on these pages, this one simply can't
  be. `set_position_trade_date` only touches the `trade_date` column
  (any already-saved `stop_loss` for that leg is omitted from the
  payload entirely, not sent as `None` — same partial-upsert convention
  `upsert_broker_connection` already relies on, see its own docstring).
  **`pages/6_My_Broker.py`'s `_default_new_position_trade_dates`** (called
  from both `_sync_dhan`/`_sync_zerodha`, right after
  `replace_broker_positions`) defaults every just-synced leg with no
  `trade_date` yet to `date.today()` — a real request: without this, a
  brand-new CSP synced today would show a blank Target P&L until the
  user separately remembered to visit the Trade Date form. Looks up
  every leg's current `trade_date` via `portfolio_repo.list_position_meta`
  first and only calls `set_position_trade_date` for a leg that has
  none — never overwrites one already set (whether entered by the user
  or defaulted by an earlier sync), and deliberately **not** wired into
  the CSV-upload path (`_render_positions_upload_section`), only the two
  live "Sync now" flows.
- `portfolio_service.csp_target_pnl(max_credit, trade_date, expiry_date,
  as_of=None)` — `min(max_credit * 0.95, max_credit *
  (duration_held / duration_to_expiry) * 1.2)` (`as_of` defaults to
  `date.today()`, explicit for testability, same convention
  `screener_service`/`refresh_service` use). **Two terms, on request** —
  the accelerated term (`* 1.2`) runs 20% faster than plain linear,
  changing every day as `duration_held` grows, so it reads as "is
  today's decay running ahead of or behind a slightly-faster-than-linear
  expectation"; the `0.95 * max_credit` term is a hard ceiling the
  accelerated term will eventually exceed (past `duration_held /
  duration_to_expiry` ≈ 0.79), at which point `min()` locks the target
  at 95% of max credit for the rest of the position's life, including
  well past expiry if it's still open — chasing the last 5% isn't worth
  the assignment/gamma risk of holding to the very end. `None` until a
  Trade Date is entered (nothing to compute a duration against), or if
  `duration_to_expiry` isn't positive (expiry on/before the trade date).
- `portfolio_service.csp_stop_loss(existing_stop_loss, max_credit,
  pnl_pct)` — the one genuinely stateful calculation on these pages:
  called fresh on **every render** of My CSP, and whatever it returns is
  immediately saved back (`set_position_stop_loss`) so the *next*
  render's `existing_stop_loss` reflects it. A pure ratchet, tightens
  only, never loosens: no `existing_stop_loss` yet → `-max_credit`;
  `pnl_pct < 0` → unchanged; `25 <= pnl_pct < 50` →
  `max(existing_stop_loss, 0)`; `pnl_pct >= 50` →
  `max(existing_stop_loss, 0.5 * max_credit)`; `0 <= pnl_pct < 25` →
  unchanged (no rule specified for that band). Doesn't need a Trade
  Date — `max_credit` and `pnl_pct` (already computed by
  `compute_positions_view` for the P&L% column) are enough. The page
  only issues the upsert when the freshly-computed value actually
  differs from what's stored (a small float-tolerance check), so a
  render where nothing crossed a new band writes nothing.

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
- **`formatting.py`** — Indian-numbering-system currency formatting (`format_inr`, lakh/crore grouping), `format_pct`, `direction_arrow`, `pass_fail_badge` (✅ Pass/❌ Fail/N/A, with text), `pass_fail_icon` (✅/❌/—, symbol only — used throughout the Dashboard table's Momentum/Dividend yield/PEG/Fundamentals columns; `pass_fail_badge` is kept for spots that still want the text, e.g. Stock Detail's scorecard). `alert_type_label()`/`summarize_alert_config()` — pure functions turning an `AlertType` + its raw `config` dict into human-readable text (e.g. "Price crosses above ₹1,000.00"), replacing what used to be a literal `f"config={a.config}"` Python-dict dump shown on both Stock Detail and the Alerts screen (now folded into Settings); the exact `config` keys each branch reads (`level`/`direction`, `period`/`direction`, `threshold`/`direction`, `entry_price`, `target_price`/`stop_loss`) must stay in sync with whatever keys the alert-creation forms in `2_Stock_Detail.py`/`4_Settings.py` actually write. **A real bug found here**: `format_inr`/`format_crores`/`format_pct`/`direction_arrow` all checked `value is None`, but `pages/1_Dashboard.py`'s `pd.DataFrame([r.model_dump() for r in rows])` silently converts a Pydantic model's correct `None` into `float('nan')` for any column that has real float values elsewhere in the same column (confirmed directly: a mixed-value column comes back `float64` dtype with `None` cells as `nan`, `nan is None` is `False`) — a genuinely-missing `return_1d` rendered as the literal string `"nan%"` on screen instead of `"—"`. All four formatters now route through a shared `_is_missing(value)` helper that also checks `math.isnan()`.
- **`timezones.py`** — `now_ist()`/`to_ist()`/`format_ist()`, thin wrappers around `pytz`.
- **`refresh_bar.py`** — `render_global_refresh_bar(client)`, the 3-button "Stock Data Refresh" / "NSE F&O Data Refresh" / "BSE F&O Data Refresh" bar called at the same spot (right after the title/disclaimer) on every page (see the Pages section's `1_Dashboard.py` bullet for what it replaced there). Reads `st.session_state["sb_access_token"]` itself rather than taking it as a parameter, since every caller has already gone through `require_login()`. Each button's result is stashed in `st.session_state` and rendered on the next script run (the same "can't render across an `st.rerun()`" pattern the old Dashboard-only buttons used), and every click ends with a blanket `st.cache_data.clear()` -- not a page-local cache-bust counter -- specifically so a refresh triggered from, say, the Options page also invalidates the Dashboard's cached screener rows for whenever the user navigates there next. `_universe_breakdown()` is the same "(X stocks, Y ETFs/funds)" stock-refresh message logic that used to live in `pages/1_Dashboard.py` as `_load_universe_counts`, moved here so the message stays identical regardless of which page triggered the refresh.
- **`ui.py`** — shared fragments: `status_badge()` (colored HTML span with text, e.g. Stock Detail's header), `market_state_label()`, `buy_sell_label()` (Green→"Model Buy Watch" etc., per the spec's no-guarantee wording), `render_disclaimer()`, `plotly_template()`, `inject_tailwind()`, plus the design-system layer described below: `ACCENT` (the "Classic Institutional" slate/navy palette constants), `inject_global_styles()`/`inject_design_system()`, `_surface_classes()`, `render_card()`, `render_pill()`, `render_stat_tile()`/`render_stat_grid()`, `render_alert_row()`.
- **`logging.py`** — `get_logger(name)`, configures `logging.basicConfig` once from `Settings.log_level`.

**Tailwind CSS — how it's actually wired in, and why not the obvious way.** Streamlit renders its own native widgets (buttons, inputs, `st.dataframe`, columns, sidebar) through its own internal React components with no supported hook for external CSS frameworks to target them — Tailwind only styles HTML we hand-render ourselves via `st.markdown(html, unsafe_allow_html=True)` (the disclaimer banner, the design-system components below). Within that scope, there's a second, less obvious trap: Tailwind's current CDN distribution (the "Play CDN") is a `<script>` that scans the DOM at runtime and injects styles as it goes — but `st.markdown(unsafe_allow_html=True)` inserts HTML via `innerHTML`, and browsers never execute `<script>` tags inserted that way (a standard, deliberate DOM security behavior, not a Streamlit quirk). Loading the Play CDN script this way silently does nothing; there's no error, the styles just never apply. `inject_tailwind()` in `ui.py` instead loads the older, fully-precompiled Tailwind **v2** static stylesheet via a `<link rel="stylesheet">` tag, which — unlike `<script>` — *is* honored via `innerHTML`. Call it once near the top of any page before rendering Tailwind-classed HTML (every page already does).

**Historical note, in case a future table goes back to hand-rendered HTML**: the Dashboard's screener table and the old combined Portfolio page's holdings table both used to be hand-rendered this way (`render_screener_table()`, since removed as dead code once both pages moved to a native `st.dataframe` -- see the Pages section's `1_Dashboard.py` bullet and the Portfolio pages bullet above for why). It rendered the same data twice into one HTML blob — a normal `<table>` wrapped `hidden md:block` (visible only ≥768px) and a stacked list of cards wrapped `md:hidden` (visible only below that) — a pure-CSS responsive switch, no JS, fixing a real mobile problem the previous plain `df.to_html()` table had (no responsive handling at all, overflowing or squeezing unreadably on a phone). Because the static Tailwind v2 build has no `dark:` variant available, its light/dark table colors were chosen explicitly in Python (a small `theme`-branching helper, the same pattern `_surface_classes()` below still uses) from the same `user_settings.theme` that already drives `plotly_template()`. If a future table needs this dual-block technique again (`st.dataframe` can't do it -- see the note on Settings' notification history below), this is the pattern to reach for.

**The design system — combining Tailwind with a global CSS override for native widgets.** Until this pass, Tailwind reached exactly one surface in the whole app: the Dashboard's screener table. Every other screen (landing page, login/signup/forgot-password, Stock Detail, Alerts -- now folded into Settings, see the Pages section above -- Settings) was 100% unstyled native Streamlit, since `inject_tailwind()` was called on every page but nothing on those pages actually used a Tailwind class. Tailwind *can't* reach native widgets at all (buttons, inputs, forms, sidebar, tabs, `st.metric`, `st.dataframe`, `st.expander` are React components Streamlit renders itself, with no exposed hook for an external CSS framework) — a Tailwind `<div>` can never wrap a native `st.button`/`st.form`, since hand-rendered HTML and native widgets are DOM siblings, not parent/child (each Streamlit element call appends its own separate node; one `st.markdown()` call's HTML can't "contain" a later `st.button()` call's output).

The fix is a second, complementary mechanism: `inject_global_styles(theme)` injects a global `<style>` block (plain CSS, not Tailwind classes) that reskins native widgets — border-radius, colors, focus states — using the same `ACCENT` palette Tailwind-classed HTML uses (`ACCENT[900]` == Tailwind's own `slate-900` hex value, so `bg-slate-900` and `var(--accent-900)` are visually identical from one source of truth). `inject_design_system(theme)` calls both `inject_tailwind()` and `inject_global_styles(theme)` together and is what every page actually calls now (via `require_login()`, plus each page re-injecting with its own loaded `user_settings.theme` right after — see the Auth section above for why `require_login()` is the enforcement point).

**The palette itself: "Classic Institutional."** `ACCENT` is Tailwind's `slate` scale (50→900), used as the app's one dominant/branding color — `kind="primary"` buttons and selected tabs/headings use `slate-900` (`#0F172A`), `kind="secondary"` buttons use a filled `slate-100`/`slate-800` pairing, cards are pure white on a `slate-50` page background (`.streamlit/config.toml`'s `backgroundColor`), and borders/dividers are `slate-200` throughout — deliberately flat and undersaturated, avoiding the bright/neon look a consumer app might use. `render_pill(text, tone, theme)` has four tones: `"accent"` (slate, for neutral branding-adjacent labels like alert types), `"neutral"` (gray, e.g. Settings' "coming soon" badges), and `"positive"`/`"negative"` (emerald/red, reserved *exclusively* for financial gains/losses and destructive actions — never used for branding or a primary CTA, per the palette's own rule). `STATUS_STYLE` (the Green/Amber/Red/Unavailable buy-signal badges) independently uses `emerald-600`/`amber-500`/`red-600`/`slate-400` hex values for the same reason — those colors are domain-meaningful classification, never touched by the `ACCENT` swap above, but chosen to be visually consistent with the same institutional palette. In dark mode, the primary/secondary button pairing inverts (a light `slate-100` button reads as "primary" against the dark `slate-900` page background) since a `slate-900`-on-`slate-900` button would have no contrast — see `_GLOBAL_CSS_DARK`'s `button[kind="primary"]` rule.

Every CSS selector in `_GLOBAL_CSS_LIGHT`/`_GLOBAL_CSS_DARK` is `data-testid`/ARIA-role/`kind`-attribute based (`[data-testid="stForm"]`, `button[kind="primary"]`, `[data-testid="stTab"][aria-selected="true"]`, etc.), confirmed via live DOM inspection against the actually-installed Streamlit version (1.59.1) at implementation time — **never** target Streamlit's own `st-emotion-cache-*` class names, which are content-hashed and change across builds/versions; testids and ARIA attributes are the only part of Streamlit's generated markup that's stable to target. If you bump Streamlit's version and native widgets stop looking styled, re-verify these selectors the same way (a scratch script + browser devtools `[data-testid]` inspection) rather than guessing.

The dark branch additionally overrides `[data-testid="stAppViewContainer"]`/`stMain`/`stHeader`/`stSidebar` backgrounds, since `.streamlit/config.toml`'s `[theme]` section (added alongside this, for Streamlit's own officially-supported BaseWeb theming — focus rings, checkbox tick color, `kind="primary"` buttons) can only express one static base theme (`light`); without the dark CSS branch also recoloring those top-level containers, "dark" would leave dark-styled widgets floating on Streamlit's own light page background. `[client] toolbarMode = "minimal"` in that same file hides Streamlit's own built-in theme picker, so there's exactly one theme control in the app (Settings → Chart theme), not two competing ones.

New reusable Tailwind-HTML components in `ui.py`, all following the same explicit-branch-on-`Theme` pattern (`_surface_classes(theme)`, since the static v2 build has no `dark:` variant to rely on): `render_card(inner_html, theme)` — bordered/padded/shadowed wrapper for **static content only**, per the DOM-siblings constraint above; `render_pill(text, tone, theme)` — small badge, used for alert-type labels and Settings' "coming soon" tags; `render_stat_tile()`/`render_stat_grid()` — responsive (`grid-cols-1 md:grid-cols-N`) stat cards, replacing Stock Detail's previously-stacked-markdown Fundamentals column; `render_alert_row()` — formatted alert summary (pill + `summarize_alert_config()` text), replacing the raw dict dump on both Stock Detail and the Alerts screen (now folded into Settings).

**The join-bug rule applies to every one of these**: joining multi-line indented f-string fragments leaves a whitespace-only line between them, which Streamlit's markdown parser treats as ending the current HTML block — every `render_*` function returns a single continuous-line string, never a multi-line indented literal, and this must hold for any future addition too. The one deliberate exception is the CSS `<style>` block itself: `<style>`/`<script>`/`<pre>` are CommonMark "HTML block type 1," terminated only by their closing tag, not by blank lines — so `_GLOBAL_CSS_LIGHT`/`_GLOBAL_CSS_DARK` are safe to write as ordinary multi-line triple-quoted strings, same as `inject_tailwind()`'s single `<link>` call always was.

**Notification history (`4_Settings.py`'s Alerts section) deliberately stays `st.dataframe`-only**, with no Tailwind mobile-card alternative -- same as every other table in the app now (Dashboard, Portfolio, Options' Futures table). The dual-block hand-rendered-HTML technique described above only ever worked because both the table and the card list were Tailwind `<div>`s the code fully controlled and could tag with `hidden md:block`/`md:hidden`; `st.dataframe` is one opaque native React subtree with no reliable way to attach a scoped class to just that one call without brittle DOM-adjacency assumptions that could break on a future Streamlit version. `st.dataframe` already has native horizontal scroll — an acceptable, if not ideal, mobile experience.

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

`fo-refresh/` backs the **📊 NSE F&O Data Refresh** / **📊 BSE F&O Data
Refresh** buttons -- one Edge Function, not two, selected via the POST
body's `exchange` field -- same reasoning and runtime as `manual-refresh/`
above (real writes need the service-role key, must run server-side).
Structurally it's a check-then-maybe-ingest, not an unconditional refresh:

- **`bhavcopy.ts`** — `bhavcopyUrl(isoDate, exchange)`,
  `fetchBhavcopyText(isoDate, exchange)` (null on 404, mirroring the
  Python providers' walk-back-friendly contract; BSE skips the zip
  content-type check and unzip step entirely -- see below),
  `findLatestAvailableBhavcopy(onOrBefore, maxLookback=7, exchange)`,
  `sourceName(exchange)` (`"nse_fo_bhavcopy_edge"` / `"bse_fo_bhavcopy_edge"`),
  and `parseFoBhavcopy(csvText, universe, source, exchange)` — a
  TypeScript port of `src/data_providers/udiff_bhavcopy.py`'s shared
  parsing (same column mapping, same universe filter, same *per-exchange*
  instrument-type allow-list -- `futuresTypesFor`/`optionTypesFor` mirror
  `nse_fo_provider.py`'s `{"STF"}`/`{"STO", "IDO"}` vs
  `bse_fo_provider.py`'s `set()`/`{"IDO"}`) -- both `source` and `exchange`
  are explicit arguments here rather than module constants, precisely so
  one function can serve both exchanges without a stale hardcoded tag (or
  allow-list) leaking onto the wrong exchange's rows (a bug this file used
  to be structurally exposed to, before the multi-exchange
  parameterization). **The zip extraction is hand-rolled**, not via a
  library (NSE only -- BSE's bhavcopy is a plain CSV, no zip at all):
  Deno's Edge Runtime has
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

  **A third real bug, once BSE was added**: a BSE F&O refresh reported
  `"Loaded BSE F&O bhavcopy for <today>: 0 futures + 0 option rows"` as a
  *success* -- confirmed live, at the exact same time the identical
  URL/date returned a real, fully-populated bhavcopy from a normal dev
  machine. Same root cause as the very first NSE bug above (BSE's
  bot-detection serving a different response to Supabase's Edge Runtime
  network origin than to a dev machine) but a quieter failure mode: BSE's
  file is a plain CSV with no zip step and no reliable content-type
  signal to check (`application/octet-stream` for both the real file and
  whatever gets served instead), so the blocked response -- large enough
  to clear the `buf.length < 500` stub guard -- decoded and "parsed"
  cleanly, it just matched zero symbols in `universe`, producing a
  plausible-looking success with no error anywhere to flag it. Fixed the
  same way as NSE's content-type check, adapted to what BSE actually
  offers: a real bhavcopy always starts with the literal header row
  `TradDt,...`, so `fetchBhavcopyText` now checks for that exact prefix
  before accepting a BSE response, throwing the same kind of
  self-diagnosing error (status, byte length, a body snippet) as the NSE
  path does on a bad zip. Mirrored in
  `src/data_providers/bse_fo_provider.py::download_bhavcopy_csv` (raises
  `ProviderError`) even though the Python path hasn't been observed
  hitting this -- same dev-machine-vs-Edge-Runtime asymmetry could affect
  any deployment context, not just this one. `bhavcopy.test.ts` and
  `tests/test_bse_fo_provider.py` both cover a non-CSV body past the size
  guard raising instead of silently returning "0 rows".

  **A fourth real bug, once the third's fix had been live a few days**: a
  BSE F&O refresh started failing outright with `"Could not reach BSE: BSE
  did not return a bhavcopy CSV for <today> (likely blocked the
  request)..."`, even on days when BSE's actual data (from a few days
  earlier) was perfectly reachable. Root cause: `findLatestAvailableBhavcopy`
  always starts its walk-back from *today's* date, and BSE — unlike NSE,
  which cleanly 404s for a file that isn't published yet — instead
  redirects a request for today's not-yet-published bhavcopy to its own
  homepage with an HTTP 200 `text/html` response. The third bug's fix made
  `fetchBhavcopyText` correctly detect that this isn't real CSV and throw a
  diagnostic error — the right behavior for a *single day* — but
  `findLatestAvailableBhavcopy`'s loop let that throw propagate immediately
  instead of catching it and trying the previous day, so the walk-back
  aborted on day one and never reached the genuinely available bhavcopy
  from a few days back (the same file NSE's walk-back had no trouble
  reaching, since NSE's 404 for the same not-yet-published case returns
  `null` rather than throwing). Fixed by having the loop catch a single
  day's error, remember it, and keep walking back; the error is only
  re-thrown once the entire `maxLookback` window is exhausted with nothing
  found, so a genuine persistent block is still surfaced (not silently
  swallowed into a bare "not found"), while a routine same-day "not
  published yet" no longer blocks discovery of an earlier, real trading
  day. Mirrored in `src/data_providers/bse_fo_provider.py::
  latest_available_bhavcopy`, which has the identical bug shape (its
  `on_or_before` also defaults to today) — NSE's Python/TS providers were
  left alone here since neither raises a diagnostic error for a
  not-yet-published day today (NSE cleanly resolves to `null`/404 either
  way), so this specific failure mode doesn't apply to them.
  `bhavcopy.test.ts` and `tests/test_bse_fo_provider.py` both cover a
  blocked/HTML "today" not stopping the walk-back from finding an earlier
  real CSV, and the diagnostic error still surfacing once every day in the
  lookback window fails.
- **`index.ts`** — reads `exchange` from the request's JSON body
  (`{"exchange": "NSE"}` / `{"exchange": "BSE"}`; missing/unparseable body
  defaults to `"NSE"`, so an old caller with no body still works). Same
  auth/cooldown pattern as `manual-refresh/index.ts`, but `provider_name`
  is now derived per exchange (`providerName(exchange)` →
  `'fo_edge_nse'` / `'fo_edge_bse'`, renamed from the pre-multi-exchange
  `'fo_edge'` -- cooldown history is a rolling 5-minute window, so the
  reset was harmless), `fetch_type = 'fo'` for both (added to
  `provider_fetch_log`'s CHECK constraint by
  `0008_add_fo_fetch_type.sql`, same pattern as `0005` did for `'all'`).
  The distinguishing step: before doing any work, it reads
  `max(trade_date)` from **both** `futures_daily_prices` and
  `option_daily_prices`, **filtered by a `source` prefix** (`nse_fo_bhavcopy%`
  / `bse_fo_bhavcopy%`, via `.like()` -- covering both this Edge Function's
  own `..._edge`-suffixed rows and `scripts/fetch_fo_data.py`'s cron/backfill
  rows for the same exchange), and takes the newer of the two dates before
  comparing it against `findLatestAvailableBhavcopy(..., exchange)`'s
  result (that exchange's latest watermark) — if the exchange has nothing
  newer, it returns `{exchange, updated: false, message, latestAvailable,
  latestLoaded}` immediately, with zero writes. **The `source` filter is
  not optional** — an earlier version of this watermark query had no
  filter at all (fine when only NSE existed), and adding BSE without
  scoping it would have meant an NSE refresh today makes a same-day BSE
  refresh think it's already up to date, since both exchanges publish on
  the same trading days and would otherwise share one watermark. **Checking
  both tables is also not optional** — once BSE was restricted to index
  options only (`_FUTURES_TYPES = set()` in `bse_fo_provider.py`), a BSE
  run stopped writing any `futures_daily_prices` row at all, so a
  futures-only watermark froze on BSE's last pre-restriction futures date
  forever: every later BSE refresh kept "succeeding" against the exchange
  but comparing against that stale date, and the Dashboard's "Latest BSE
  Bhavcopy" (`fo_repo.get_latest_fo_trade_date`, same futures-only bug,
  fixed the same way) stuck on that date too even though new index-option
  rows were landing in `option_daily_prices`. Only
  when that exchange's date is strictly newer does it parse (stamping
  rows with `sourceName(exchange)`) and upsert into all four F&O tables
  (chunked at 500 rows, matching `fo_repo.py`'s Python chunk size) and
  re-derive `is_open` via the same expiry-vs-today logic as
  `fo_repo.refresh_open_flags`, then
  returns `{exchange, updated: true, tradeDate, futuresRows, optionRows}`.
- On the Streamlit side, `edge_refresh.py::trigger_fo_refresh(access_token,
  exchange="NSE")` is the HTTP client (same shape as
  `trigger_manual_refresh`, reusing `ManualRefreshError` rather than a
  parallel exception type, since the calling convention —
  cooldown/4xx/5xx handling — is identical), now sending `json={"exchange":
  exchange}` in the POST body; `src/utils/refresh_bar.py` calls it once
  with `"NSE"` and once with `"BSE"` for its two F&O buttons, each
  showing a distinct message depending on `updated: true` vs `false` vs
  an error, rather than treating "nothing new" as a failure.

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
`pages/2_Stock_Detail.py` and `pages/4_Settings.py`, add the CHECK constraint
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
