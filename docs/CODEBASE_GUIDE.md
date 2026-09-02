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
  4_Settings.py                    Per-user thresholds, alert CRUD + notification history, Data Provider (broker connect/sync), theme, change password, sign out
  5_Options.py                     F&O: futures term structure + 5% CSP/CC breakdown per stock
  7_My_Trades.py                     Holdings + positions grouped by underlying into Stock/Index/Other Trades
  8_My_Holdings.py                   Equity holdings, ETFs & Mutual Funds / Stocks split, identical columns
  9_My_Positions.py                  Per-leg F&O positions, split into Stock Options / Index Options / Others
  11_My_CSP.py                       Every position leg from a "CSP"-tagged Trade, + underlying LTP/1D/5D/20D
  12_My_Portfolio_Trades.py          Every "Portfolio "-prefixed Trade, one row per Trade: Stock Holding + up to
                                     4 option legs (formerly My CC, Covered-Call only)
  13_My_Other_Trades.py               My Trades' own tables, filtered to neither "CSP" nor Portfolio-prefixed
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
  functions/manual-refresh/         Edge Function (Deno/TypeScript) behind Stock/Fundamental Data Refresh (mode param)
  functions/fo-refresh/              Edge Function (Deno/TypeScript) behind Bhavcopy Refresh (exchange param)
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
- `user_settings` — thresholds, theme, and (migration `0028`) `data_provider`: `Literal["dhan", "yfinance_bhavcopy"]`, default `"yfinance_bhavcopy"` — which live source prices this account's own stock LTP wherever it's shown (Dashboard, Stock Detail, the portfolio pages). Set via Settings' "Data Provider" section (`src/utils/data_provider_settings.py`); fundamentals and the full F&O chain are never provider-branched, only stock LTP. Zerodha used to be a third option here until it was removed entirely (see the Portfolio pages section's "Removed: Zerodha" note below). See the Streamlit app section's `4_Settings.py` bullet below.
- `saved_filters` — named filter presets
- `user_positions` — entry/target/stop-loss/notes per symbol
- `alerts` — alert configs
- `notification_log` — alert-fired history, deduped via a unique `dedupe_key`
- `portfolio_holdings` — broker-synced holdings (migration `0012`, `portfolio_name` added in `0014`), keyed `(user_id, portfolio_name, broker, raw_name)`. The schema still allows multiple independently-named portfolios to coexist per user, but there is no longer any UI that creates a second one: CSV upload (the only way that used to happen) was dropped entirely along with `pages/6_My_Broker.py`, and the Dhan portfolio sync (connecting in Settings, or the Portfolio Refresh button) now always targets the account's one resolved name (`portfolio_repo.get_or_default_portfolio_name` — see the Portfolio pages section below). `symbol` is nullable and deliberately **not** FK'd to `companies` -- a resolved symbol may not exist there yet (an ETF/fund or non-Nifty50 stock the screener doesn't otherwise track); see the Portfolio pages section below for how it gets registered.
- `portfolio_positions` — broker-synced F&O positions (migration `0016`), same keying/RLS shape as `portfolio_holdings`. `symbol`/`expiry_date`/`strike_price`/`option_type` are nullable together (an undecoded instrument format), and `qty` keeps its broker-reported sign (negative = short). `raw_name` (part of the primary key) is not guaranteed unique straight from a broker's API -- see `dhan_positions_from_api`'s dedup pass in the Portfolio pages section's My Positions subsection. See that subsection for the full table.
- `broker_connections` — saved Dhan API credentials, one row per `(user_id, broker)` (migration `0017`; `api_secret` + nullable `access_token` added by `0022`, originally for Zerodha, now permanently unused -- see the Portfolio pages section's "Removed: Zerodha" note; `portfolio_name` dropped and the primary key narrowed from `(user_id, portfolio_name, broker)` to `(user_id, broker)` by migration `0029`, once the Data Provider choice became account-wide rather than per-portfolio) -- an account's one resolved portfolio (see `portfolio_holdings` above) syncs holdings/positions directly from a broker's API through this credential. Credentials are stored as entered, protected only by this table's own RLS policy -- no application-level encryption. See the Portfolio pages section's "Connect Dhan account" subsection.
- `user_live_prices` — per-user cache of live stock LTPs pulled from Dhan (migration `0030`), keyed `(user_id, symbol)`, columns `latest_price`/`fetched_at`. Written by the "Stock & Option Data Refresh" button (`src/utils/refresh_bar.py`) only when `data_provider` is `"dhan"`; read by Dashboard/Stock Detail as an override on top of the shared `daily_screener_snapshots` value (`snapshot_repo.get_user_live_prices`/`upsert_user_live_prices`). See the Streamlit app section's `1_Dashboard.py`/`2_Stock_Detail.py` bullets below.
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

**A recurring gotcha, hit twice**: `0002`'s "shared tables are SELECT-only,
writes go through the service-role key" assumption breaks the moment a
*Streamlit page itself* needs to write shared/log data -- there's no
service-role path from Streamlit (that key must never live in page code,
see the Edge Functions section's own reasoning). `provider_fetch_log` was
exactly this: `0034` added an authenticated INSERT policy (narrowly scoped
by *value*, since the table has no `user_id` to scope by --
`fetch_type='portfolio_sync'`) after the new Portfolio Refresh button hit
`42501` logging its own sync; `0036` had to add a near-identical policy for
`fetch_type='dhan_instrument_master'` after the *same* class of error hit
again once migration `0035`'s instrument-master loaders started logging
their own downloads from inside a user's session. If a future change makes
a Streamlit page write to any table whose RLS policy predates it, check
whether that table's policies actually cover an ordinary authenticated
write, not just the service-role path -- `0002`'s policies don't, by
design, for every shared table that existed when it was written.

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
- **`portfolio_service.py`** — translates Dhan's raw API response into a common holdings/positions shape (`dhan_holdings_from_api`/`dhan_positions_from_api`), cross-broker merging (`merge_holdings`), and live valuation (`compute_portfolio_view`); plus the *positions* side's own valuation (`compute_positions_view`, migration `0016`). **CSV upload was dropped entirely** (`pages/6_My_Broker.py` deleted along with it) -- there is no CSV parsing anywhere in this module anymore: `parse_zerodha_csv`, `parse_dhan_csv`, `parse_zerodha_positions_csv`, `parse_dhan_positions_csv`, `parse_dhan_position_name`, `match_symbol`, `_normalize_name`, and `_to_float` were all deleted (and their tests removed), since a broker's API response already carries the exact symbol/contract fields those functions used to reconstruct from free text or a fuzzy name match. Zerodha's own live-sync path (`zerodha_holdings_from_api`/`zerodha_positions_from_api`/`parse_zerodha_option_instrument`, its F&O tradingsymbol decoder) was removed entirely along with the broker itself -- see the Portfolio pages section's "Removed: Zerodha" note. `resolve_tracked_symbols` is the pure diff both refresh paths call to register newly-seen portfolio symbols; `looks_like_etf_name` is the real-display-name-based ETF/fund classifier those same paths apply to it before upserting (migration `0015`). See the Portfolio pages section below and the Futures & Options section's "A follow-up problem 0013 itself introduced" paragraph for the full ETF story.

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
positions (Settings' "Data Provider" section's "Connect Dhan account"
flow, `src/utils/data_provider_settings.py`) have real
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

**On-demand refresh**: clicking **Bhavcopy Refresh** (Settings only,
`src/utils/refresh_bar.py`, see the Utils section below) fires
two concurrent POST calls to the *same* second Edge Function,
`supabase/functions/fo-refresh/` (see the Edge Functions section below),
one parameterized `{"exchange": "NSE"}` and one `{"exchange": "BSE"}` — a
TypeScript port of the same bhavcopy fetch+parse, but only for the single most
recent day and only if that exchange has actually published something
newer than what's already loaded for it (checked via the greater of
`max(trade_date)` in `futures_daily_prices` and `option_daily_prices`,
scoped by a `source` prefix -- see the Edge Functions section for why
this scoping was necessary, not optional, and why both tables must be
checked), so a call when nothing's new is a cheap read-only no-op
rather than a silent re-fetch. This F&O half always runs regardless of
the account's Data Provider setting (Dhan/YFinance+Bhavcopy) --
Dhan's API doesn't expose a bhavcopy-equivalent full options chain, so
F&O ingestion stays bhavcopy-sourced for every account. NSE has no external zip-library dependency — see the
Edge Functions section for why; BSE needs no zip handling at all. Its
`universe` set gets the identical Index-row + portfolio-symbol widening as
`fetch_fo_data.py` above (mirrored in TypeScript via the same
`resolveTrackedSymbols`/`portfolioSymbols.ts` helper `manual-refresh`
already uses), so a symbol newly tracked from a portfolio sync starts
getting its F&O data via the next Bhavcopy Refresh click too, not just
a manual backfill run.

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
- `manual-refresh`/`fo-refresh` Edge Functions — behind Stock Data
  Refresh (every page) and Bhavcopy Refresh (Settings only), respectively
  (`src/utils/refresh_bar.py`). These can't call the Python implementation
  (no service-role key in Streamlit), so the calculation is ported to
  TypeScript too: `supabase/functions/_shared/dashboardMetrics.ts`
  (`cspFivePct`/`ccFivePct`/`recomputeDashboardMetrics`, tested in
  `dashboardMetrics.test.ts`) — the same duplicated-business-logic
  tradeoff `calculations.ts`/`bhavcopy.ts` already accept, for the same
  reason (a truly instant on-demand path). If you change the CSP/CC
  calculation in `fo_service.py`, mirror it here too.

**Live F&O override for Dhan accounts (migration `0032`)**: the cache
above is still what's *stored* -- it stays bhavcopy-sourced, updated once
per Bhavcopy Refresh -- but for an account whose Data Provider is Dhan,
the Dashboard now overlays a live premium on top of it at render time.
`src/utils/refresh_bar.py::_dhan_fo_universe` builds the set of contracts
worth live-pricing: every futures/option leg the account's own
`portfolio_positions` actually holds, plus the exact CSP-strike/CC-strike
legs `dashboard_fo_metrics` already cached (not the full chain -- there's
no cached concept of "every strike" to widen from). `_refresh_user_live_prices`
resolves each contract to a Dhan `security_id` (`DhanProvider.get_fo_quotes`,
`resolve_fo_security_id` against a *second*, F&O-filtered instrument-master
download -- `_load_fo_instrument_master`, separate from the equity-only one
`resolve_security_id` already used) and caches the live premium in

**Instrument-master caching (migration `0035`)**: both instrument-master
downloads (`_load_instrument_master`/`_load_fo_instrument_master`) used to
independently re-download the same ~211,742-row Dhan CSV
(`images.dhan.co/api-data/api-scrip-master.csv`) into a per-process
`@lru_cache(maxsize=1)` -- confirmed live as the dominant cost of a "Stock &
Option Data Refresh" click, doubly so right after any Streamlit Cloud cold
start (the `@lru_cache` doesn't survive a process restart). Both loaders now
take an optional `client: Client | None = None`: with a client, they check a
day-keyed in-memory dict first, then the shared `dhan_equity_instruments`/
`dhan_fo_instruments` tables (freshness via `provider_fetch_log`,
`fetch_type="dhan_instrument_master"`, `provider_name="equity"`/`"fo"`), only
falling back to a real download (now `_download_equity_master`/
`_download_fo_master`, the original parse/filter logic unchanged, but now
taking an already-downloaded `raw_df` -- see below) when neither has
today's IST-calendar-day data -- a fresh download is persisted back via
the new `src/repositories/dhan_instrument_repo.py` (delete-then-insert,
same convention as `fo_repo.clear_dashboard_fo_metrics`/
`upsert_dashboard_fo_metrics`) before being cached in-memory, so the *next*
process/user skips the download too. **This freshness-check-then-download
behavior was later removed** (see "Decoupling instrument-master downloads
from Stock & Option Data Refresh entirely" further below) -- a
`client`-backed call now never downloads at all, only ever reads whatever
`refresh_dhan_instrument_master` last persisted. `client=None` keeps the exact old
in-memory-only behavior (`factory.py::get_price_provider`'s plain-cron path
never has a Supabase client to give a generic `PriceDataProvider`). Hit the
same RLS gap described above: `0035` itself granted `dhan_equity_instruments`/
`dhan_fo_instruments` their own authenticated write policies, but the
`provider_fetch_log` insert those loaders also make needed a *separate*
follow-up (`0036`) -- confirmed live, same `42501` as `0034`'s.
`DhanProvider.__init__` gained one new optional kwarg, `supabase_client`,
threaded through to both loaders from `get_quotes`/`get_fo_quotes`/
`get_historical_daily`; `refresh_bar.py::_refresh_user_live_prices` and
`portfolio_page.py::load_live_dhan_prices` both now pass their own Supabase
`client` through.

The two loaders also share **one** raw download instead of each
downloading the full CSV independently -- confirmed live as still slow
even after the DB cache above landed, since a cache-cold refresh triggered
two full downloads of the same large file. `_get_raw_instrument_master()`
downloads the raw, unfiltered CSV at most once per IST day, holding
`_master_cache_lock` for the *entire* download (not just the cache check).
`_download_equity_master`/`_download_fo_master` were correspondingly
changed from "download and filter" to "filter" -- each takes the
already-downloaded `raw_df` as a parameter. This raw-CSV cache is
deliberately **not** persisted to Supabase like the two derived tables --
it's mostly rows neither loader wants (currency, commodity, other
segments), so there's nothing worth storing beyond what
`dhan_equity_instruments`/`dhan_fo_instruments` already keep.
`_get_raw_instrument_master` always returns a fresh `.copy()`, never the
cached object itself -- pandas is **not** thread-safe for concurrent reads
of one shared DataFrame instance (lazy internal caching -- e.g. block
consolidation on first column access -- mutates C-level state even on
operations that look read-only, like `.rename()` or `.str.upper()`), and
an earlier version that skipped the `.copy()` silently corrupted results
when both loaders happened to run at the same time (see below for why
that "at the same time" stopped being possible).

**The equity and F&O legs of `_refresh_user_live_prices` run
sequentially, not concurrently** -- this was tried as a 2-worker
`ThreadPoolExecutor` (`_refresh_dhan_equity_leg`/`_refresh_dhan_fo_leg`,
same pattern `_run_bhavcopy_refresh` uses for NSE/BSE) and reverted after
two rounds of real, silent production breakage, both worth understanding
if this is ever revisited:

1. **Shared-DataFrame corruption.** The first concurrent version handed
   the *same* raw CSV DataFrame to both legs. A live account's next
   refresh dropped from 61/61 equities + 333+/351 F&O resolving to
   **15/61 + 0/348** -- no exception anywhere, just quietly wrong results,
   which is what made it worth documenting rather than just patching.
   Root cause was the pandas thread-safety issue described above; fixed
   by the `.copy()`.
2. **Shared Supabase client corruption.** Even with (1) fixed, both legs
   also make calls through `_refresh_user_live_prices`'s own `client` --
   `snapshot_repo.upsert_user_live_prices`/`upsert_user_live_fo_prices`,
   `_dhan_fo_universe`'s reads, and the instrument-master loaders' own
   `provider_fetch_log`/`dhan_*_instruments` calls -- while running on two
   different threads. This crashed with `httpx.RemoteProtocolError`
   (confirmed live, full traceback landing in `fetch_log_repo.log_fetch`
   -> `postgrest` -> `httpx`) -- Supabase's cached client's underlying
   connection isn't safe for two threads to use concurrently; it gets
   corrupted rather than queued.

`_run_bhavcopy_refresh`'s own `ThreadPoolExecutor` (NSE + BSE) is **not**
affected by either issue and is unchanged -- it calls
`edge_refresh.trigger_fo_refresh`, which POSTs to an Edge Function with a
fresh `httpx.post()` per call, never touching a shared pandas object or
the cached Supabase `client`. The distinction that matters: threading is
safe here for independent, self-contained network calls, but not for
sharing a live pandas object or a cached Supabase client across threads
-- copy (or don't share) the former, and don't call the latter
concurrently, full stop. Since the two genuinely expensive shared
resources in the Dhan refresh path -- the CSV download and every Dhan API
call (`_throttle`'s global rate gate) -- are already serialized by locks
regardless of threading, going sequential here gives up only the overlap
of the two legs' own network *wait* time.

**A third concurrency bug, this one across separate sessions rather than
two threads in one call**: going sequential above only serializes the
equity/F&O legs *within* one `_refresh_user_live_prices` call -- it does
nothing about two *different* users (or two tabs of the same user) each
independently finding today's `dhan_equity_instruments`/
`dhan_fo_instruments` cache cold and racing to repopulate the same shared
table at once, which is exactly what these tables are for (migration
`0035`'s whole point). `dhan_instrument_repo.replace_equity_instruments`/
`replace_fo_instruments` used to `delete()` the whole table then
`insert()` the fresh rows chunk-by-chunk -- two overlapping callers'
inserts can interleave against the same primary key (`security_id`),
confirmed live as `duplicate key value violates unique constraint
"dhan_equity_instruments_pkey"` (`postgrest.exceptions.APIError`,
`23505`) surfacing all the way up through `get_quotes`/`get_fo_quotes` to
the "Stock & Option Data Refresh" button. Fixed by switching the write
half from `insert()` to `upsert(chunk, on_conflict="security_id")` --
same convention already used by every other replace-semantics repo in
this codebase (`companies_repo`, `fo_repo`, `portfolio_repo`, ...); a
colliding chunk from a racing writer now just overwrites instead of
erroring, since both writers downloaded the same Dhan CSV and would
write identical data for that `security_id` anyway. The `delete()` half
is untouched and doesn't need the same fix -- two concurrent deletes of
an already-empty table are naturally idempotent, unlike two concurrent
inserts targeting the same primary key. `tests/test_dhan_instrument_repo.py`
regression-locks this with a fake client that enforces the same
primary-key uniqueness Postgres does, including one test that confirms a
plain `.insert()` *would* reproduce the original error on this fake
client -- so a future revert back to `insert()` fails the suite the same
way it failed in production.

**A fourth bug, this one making resolution itself unreliable rather than
crashing anything**: `dhan_instrument_repo.get_equity_instruments`/
`get_fo_instruments` -- the DB-cache-read path `_load_instrument_master`/
`_load_fo_instrument_master` take once a `provider_fetch_log` entry
already exists for today -- used a plain, unpaginated `.select().execute()`.
This is the exact same PostgREST-caps-a-response-at-1000-rows bug
`fo_repo._paginate` already exists to fix (see the Futures & Options
section's "A real bug this surfaced" above) -- confirmed live here too:
`dhan_equity_instruments` had 9,854 rows, the unpaginated SELECT returned
exactly 1,000, and every `trading_symbol` sorting past that page --
RELIANCE, TCS, HDFCBANK, SBIN, and the large majority of the Nifty50 --
resolved as `no Dhan security_id found`, indistinguishable from a
genuinely unlisted symbol. `dhan_fo_instruments` (85,000+ rows) was even
more exposed -- under 2% of contracts resolvable through this path.

**Why this looked like intermittent Dhan-side flakiness rather than a
deterministic bug, across several rounds of live debugging**: the
*first* instrument-master load of an IST day is cache-cold, so it builds
its DataFrame straight from the Dhan CSV download and caches that
complete DataFrame in-memory (`_equity_master_cache`/`_fo_master_cache`)
-- it never round-trips through this SELECT at all. Any later call in
that *same* long-lived Streamlit process hits the warm in-memory cache
and also sees full resolution. Only a call in a *different* process (a
separate Streamlit Cloud worker, or the same worker after a restart)
that finds today's cache already warm goes through
`get_equity_instruments`/`get_fo_instruments` and hit the 1,000-row
truncation -- so whether a given "Stock & Option Data Refresh" click
resolved 58/61 symbols or 3/61 depended on which process happened to
handle it, not on Dhan's API or the time of day, even though the
symptom (wildly different resolution counts minutes apart) looked
exactly like upstream flakiness during live investigation. Fixed by
giving `dhan_instrument_repo` its own local `_paginate` helper (same
implementation as `fo_repo`'s) and routing both `get_*_instruments`
functions through it. `tests/test_dhan_instrument_repo.py`'s
`TestGetEquityInstruments`/`TestGetFoInstruments` regression-lock this
with a fake client that mimics PostgREST's own `.range()` paging, so a
future regression back to a plain `.select()` fails the same way.

**A fifth bug, specific to BSE-listed index legs**: `DhanProvider.get_fo_quotes`
resolved every contract's Dhan `security_id` correctly (`resolve_fo_security_id`
was never the problem) but then queried **all** of them under a single
hardcoded `"NSE_FNO"` segment in the `get_ltp_by_security_id` call. SENSEX/
BANKEX are the only F&O legs allowed to resolve cross-exchange (migration
`0031`'s stock-legs-are-NSE-only rule doesn't apply to index legs -- see
`_download_fo_master`'s docstring), so their `security_id` genuinely lists
on `BSE_FNO`, not `NSE_FNO` -- Dhan's LTP endpoint silently returns nothing
for a security_id queried under the wrong segment, indistinguishable in
the "Stock & Option Data Refresh" summary's `fo_missing` list from "Dhan
has no matching contract, or it's simply not trading" for those exact
contracts (confirmed live: 2 of 254 F&O contracts stuck, both SENSEX
strikes, every NSE-listed contract unaffected). Fixed by having
`_download_fo_master` keep (not just filter on and discard) each row's
own exchange in a new `exchange` column -- persisted through
`dhan_fo_instruments` via migration `0037` (`text not null default 'NSE'
check (exchange in ('NSE', 'BSE'))`, the default only a bridge for
existing rows until the next wholesale replace) and through
`dhan_instrument_repo.get_fo_instruments`'s `SELECT` -- and having
`get_fo_quotes` split resolved `security_id`s into `NSE_FNO`/`BSE_FNO`
lists by that column before calling `get_ltp_by_security_id`, instead of
one hardcoded list.

**A sixth bug, hit repairing the fifth live**: `dhan_instrument_repo`'s
`replace_equity_instruments`/`replace_fo_instruments` cleared their table
with one unbounded `.delete().neq("security_id", "")` before upserting.
Once `dhan_fo_instruments` grew to ~85,000 rows, that single `DELETE`
statement hit Postgres's own `statement_timeout` (`57014 canceling
statement due to statement timeout`) and rolled back **entirely** --
confirmed live while force-refreshing the table to backfill migration
`0037`'s new `exchange` column: the whole replace failed, leaving every
row (stale `exchange='NSE'` default included) completely untouched,
not partially cleared. Fixed with a new `_delete_all(client, table_name)`
helper: selects up to `_CHUNK` (500) existing `security_id`s, deletes just
that batch via `.delete().in_("security_id", ids)`, and loops until
nothing's left -- no single statement scales with the table's full size
regardless of how large it grows. `tests/test_dhan_instrument_repo.py`'s
`TestDeleteAll` regression-locks this against a table spanning more than
one batch.

**Decoupling instrument-master downloads from Stock & Option Data
Refresh entirely.** Every fix above still left one thing unresolved: a
`client`-backed `_load_instrument_master`/`_load_fo_instrument_master`
call would download+persist a fresh Dhan CSV inline, on request, whenever
it judged the DB cache "not fresh today" (`provider_fetch_log` freshness
check). That made "Stock & Option Data Refresh" unpredictably slow --
whichever process/worker's click happened to find the cache stale paid
for the full ~211,742-row download right there, while every other click
that day stayed fast. On request, this was decoupled outright rather than
tuned further: both loaders now, when given a `client`, **never**
download -- they read `dhan_equity_instruments`/`dhan_fo_instruments`
verbatim, however old, and raise a `ProviderError` pointing at a new
Settings button if the table has never been populated at all (no more
freshness check or "stale means re-download" branch — see their own
docstrings). A new module-level function,
`dhan_provider.refresh_dhan_instrument_master(client)`, is now the
*only* thing that downloads Dhan's CSV and persists both slices --
shares one raw download between the equity/F&O filters (same
`_get_raw_instrument_master` sharing the old cache-cold refresh path
used), persists both, logs both `provider_fetch_log` entries, and
critically also clears/repopulates this process's own in-memory caches
immediately (not just the DB ones), so a resolve call in the *same*
process right after the click sees the fresh data too rather than
serving whatever was cached for "today" until the process restarts.
Exposed as **"Refresh Instrument Master - Dhan"**
(`src/utils/data_provider_settings.py::_render_dhan_instrument_master_refresh`),
shown in Settings' Data Provider section only when the account's
provider is Dhan, right below the connect/credentials form -- needs no
broker connection or token of its own, since the instrument master
(`images.dhan.co/api-data/api-scrip-master.csv`) is public Dhan
reference data. `client=None` (the plain-cron `factory.py::get_price_provider`
path, or a script/test with no Supabase wiring) is untouched and keeps
the old real-download-on-miss behavior, since there's no DB cache to read
there at all. `to_ist` and the `fetch_log_repo.get_last_successful_fetch`
freshness check are no longer used by either loader (`get_last_successful_fetch`
is now only called from `_render_dhan_instrument_master_refresh`, for
that button's own "Last instrument master refresh: ..." caption).

`user_live_prices`, widened by migration `0032` from an equity-only
`(user_id, symbol)` table to `(user_id, symbol, expiry_date, strike_price,
option_type)` -- `option_type='EQ'` (default) for the pre-existing equity/
ETF rows, `'FUT'`/`'CE'`/`'PE'` for the new F&O rows, sharing one table
rather than adding a second. `pages/1_Dashboard.py` then recomputes
`csp_pct`/`cc_pct` from the live premium using the exact same formulas
`fo_service.csp_5pct_for_rows`/`cc_5pct_for_rows` already document (`put_price
/ strike * 100`, `premium / spot * 100`) -- only the premium is live, the
cached strike/spot are left as-is. `src/utils/portfolio_page.py::load_positions`
applies the same override to every position leg's own `ltp` (clearing
`ltp_as_of` on a live hit, same "never stale" rule the equity override
already follows), which is what makes My Positions/My Trades/My CSP/
Analyse Trade all pick it up for free -- they all read positions through
that one shared loader.

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
`option_contracts` rows were left alone at the time -- believed harmless
once excluded from this one cache's computation. **That assumption was
wrong, confirmed by a sixth bug below**: the same stale rows were still
being read, unfiltered, by every *other* consumer of
`latest_option_chain_view`.

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

**A sixth bug, reported "once again" some time after `0027` shipped**:
the Dashboard's "Options month" dropdown showed a stock's month
duplicated once more, even though `dashboard_metrics_rows`' BSE
exclusion (above) was still correctly in place and had never regressed.
Root cause: the stale pre-restriction BSE `option_contracts`/
`option_daily_prices` rows `0027` deliberately left alone were never
actually deleted -- `fo_repo.refresh_open_flags` (run on every ingest)
only compares `expiry_date` to today, with no concept of source or
symbol type, so it kept reaffirming `is_open = true` for these rows
forever, exactly as `0027`'s own docstring already predicted ("still
`is_open = true` as long as their own expiry date hasn't passed"). Fixing
only `dashboard_metrics_rows`' own computation was necessary but not
sufficient a second time: `latest_option_chain_view` itself still served
the garbage row to everyone else -- `fo_repo.get_option_chain` (My CSP's
fallback LTP, Analyse Trade's fallback path), `fo_repo.list_option_expiries`
(queries `option_contracts` directly, bypassing the view and its
`source` column entirely), and the Options page's own expiry picker all
read it unfiltered. There was no single Python-side choke point left to
patch -- the fix had to move into the data itself.

Migration `0031_stock_options_nse_only.sql` does two things instead of
one more downstream filter: (1) a one-time cleanup, deleting every
surviving BSE-sourced `option_contracts`/`option_daily_prices` row whose
symbol isn't a genuine Index (`companies.company_type = 'Index'`) --
`option_contracts` itself carries no `source` column, so it's matched
via its corresponding `option_daily_prices` row's source, and deleted
*before* those price rows are removed out from under it; and (2) the
same "BSE row only passes if its symbol is a genuine Index" guard baked
directly into `latest_option_chain_view`'s own `WHERE` clause (appending
`source` in `0027` already exposed the column this needs) -- so every
current *and future* consumer of the view is protected permanently, not
just whichever one happens to remember to filter it. `refresh_open_flags`
itself is deliberately left unchanged (still a pure date comparison,
still blind to source) -- it's not what's wrong; the two fixes above
mean it no longer matters that it can't tell a stale row from a real one,
since a stale one can no longer exist in the first place.

This was also the point where two *different* expiry dates could still
look identical in the UI: `available_expiries` (the dropdown's
`format_func`) rendered every date as `"%b %Y"` (month + year only) --
so a BSE date a few days off its symbol's real NSE expiry, both falling
in the same calendar month, were genuinely indistinguishable entries
before `0031` even existed to fix the data, which is part of why this
took a live "Aug 2026 twice" report to notice at all rather than being
caught by inspection. Fixed on the same request: the dropdown (and
`pages/5_Options.py`'s futures chart title, the only other
month-only-formatted date in the app -- everywhere else already showed
`%d %b %Y`) now renders `"%b %Y (%d-%b-%y)"`, e.g. `"Aug 2026
(25-Aug-26)"` -- two entries in the same month are now visibly distinct
even if the underlying data problem were ever to recur.

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
pages = {
    "Market": [
        st.Page("pages/1_Dashboard.py", title="Screener", default=True),
        st.Page("pages/2_Stock_Detail.py", title="Equity"),
        st.Page("pages/5_Options.py", title="Options"),
    ],
    "My Portfolio": [
        st.Page("pages/8_My_Holdings.py", title="Holdings"),
        st.Page("pages/9_My_Positions.py", title="Positions"),
        st.Page("pages/14_Trade_History.py", title="Trade History"),
    ],
    "My Trades": [
        st.Page("pages/7_My_Trades.py", title="All Trades"),
        st.Page("pages/11_My_CSP.py", title="CSP"),
        st.Page("pages/12_My_Portfolio_Trades.py", title="Portfolio Trades"),
        st.Page("pages/13_My_Other_Trades.py", title="Other Trades"),
        st.Page("pages/10_Analyse_Trade.py", title="Analyse Trade", visibility="hidden"),
    ],
    "Settings": [
        # url_path pinned explicitly (not left to Streamlit's filename-derived
        # default) so this page's URL stays stable if this file is ever
        # renamed -- originally needed as a stable Kite Connect "Redirect URL"
        # for Zerodha's OAuth login, which no longer exists (Zerodha was
        # removed entirely, see the Portfolio pages section's "Removed:
        # Zerodha" note), but there's no reason to unpin it now either.
        st.Page("pages/4_Settings.py", title="Settings", url_path="Settings"),
    ],
}
st.navigation(pages).run()
```

**The dict form (rather than a flat list)** groups pages under a labeled
sidebar section header -- confirmed via `st.navigation`'s own docstring:
in `position="sidebar"` mode (the default, unchanged here) every key
renders as a header above its pages, so Settings gets a section of its
own single page purely because the dict form requires every page to
belong to one (the `""`-key trick the docstring mentions for hiding a
header only applies to `position="top"`). This is what gives "Market"
(Screener/Equity/Options), "My Portfolio" (Holdings/Positions/Trade
History), and "My Trades" (All Trades/CSP/Portfolio Trades/Other Trades,
plus the hidden Analyse Trade) their nested sub-page groupings ("Portfolio
Trades" was "CC" until `12_My_CC.py` was renamed and rebuilt into
`12_My_Portfolio_Trades.py`) -- Streamlit has no other native notion
of a page belonging "under" another page. **The section/page labels here
are a second, independent naming layer on top of this doc's own prose**
(which keeps calling each page by its file-derived feature name -- "My
Trades", "My CSP", "My Holdings", etc. -- matching the filename and each
page's own on-page `st.title()`/`st.set_page_config()`) -- e.g. the
sidebar shows "All Trades" for what this doc, the filename
(`7_My_Trades.py`), and the page's own header all still call "My
Trades"; only the `title=` argument passed to `st.Page` here changed,
requested and renamed once already since these pages first shipped.
Verified with an `AppTest.from_file`
smoke pass over every page plus `app.py` itself (no exceptions); a real
logged-in sidebar screenshot wasn't captured -- `require_login()` gates
every page before any sidebar-adjacent content renders, and this
sandbox has no real Supabase credentials to sign in with.

**The My Broker page (`pages/6_My_Broker.py`) was deleted entirely**, along
with CSV upload -- there is no CSV parsing anywhere in the app anymore
(see the `portfolio_service.py` bullet above and the Portfolio pages
section below). Its "Connect Dhan account"
(the latter since removed entirely along with Zerodha itself) credential
flows moved to Settings' new "Data Provider" section
(`src/utils/data_provider_settings.py::render_data_provider_section`),
simplified along the way: no more per-portfolio tabs, no "+ New
portfolio" creation flow, and `broker_connections` collapsed to one row
per `(user_id, broker)` account-wide (migration `0029`) instead of one
per `(user_id, portfolio_name, broker)` -- see the Portfolio pages
section below for the full story.

**`visibility="hidden"` on Analyse Trade** (confirmed supported in the
installed Streamlit version, 1.59.1 -- `st.Page`'s own `visibility`
parameter): the page is excluded from the sidebar nav menu but stays
reachable via `st.switch_page`, which is exactly what My Trades needs --
selecting a row and clicking "Analyse Trade" stashes
`st.session_state["analyse_trade_id"]`/`["analyse_trade_portfolio"]` and
calls `st.switch_page("pages/10_Analyse_Trade.py")`, landing on a real
page (its own URL, its own script) that simply never appears as its own
sidebar link alongside My Trades/My Holdings/My Positions/My CSP/My
CC/My Other Trades.

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

- **`1_Dashboard.py`** — loads `latest_screener_view` via `snapshot_repo.get_latest_screener()`, applies the signed-in user's thresholds via `threshold_override.apply_user_thresholds()`, renders metric cards (also usable as quick filters, wired through `st.session_state["status_filter"]`), sidebar filters, and the screener table. The Status sidebar filter is a `st.multiselect` over `ALL_STATUSES = ["Green", "Amber", "Red", "Unavailable"]` — `status_filter` is always a *list* (any combination, not one-or-all), and the final row filter is a single `df["status"].isin([...])`, so selecting all four is equivalent to no filter at all. Saved filter presets normalize old single-string `"status"` values (from before this was a multiselect) into a list on load for backward compatibility. The "Minimum dividend yield" / "Minimum PEG" sidebar filters default to `0.0`, **not** `user_settings.dividend_yield_threshold`/`peg_threshold` — they're a separate display filter from the criterion A/C pass/fail thresholds, and defaulting them to the threshold value silently hid every stock below it on first load (a real bug, since fixed). Keep these two concepts distinct if you touch this page: the Settings-page thresholds decide Green/Amber/Red/Unavailable; these sidebar inputs just additionally hide rows below a value the user dials in themselves, and should default to "show everything." Right after the title/disclaimer, `render_stock_refresh_button(client, user_id, user_settings.data_provider)` (`src/utils/refresh_bar.py`, see the Utils section below) renders **Stock Data Refresh** (YFinance/Bhavcopy) or three Dhan-specific buttons (see the Utils section's `refresh_bar.py` bullet) by Data Provider setting — this used to be a single bundled **"🔄 Market Data Refresh"** button firing every fetch (stock, fundamentals, F&O bhavcopy, broker live prices) at once; it's now five independent, narrower buttons across Settings and the other pages (see the Utils section's `refresh_bar.py` bullet for the full breakdown). `render_stock_refresh_button` is called identically from every page except Settings (Dashboard, Stock Detail, Options, My Trades, My Holdings, My Positions, My CSP, My Portfolio Trades, My Other Trades, Analyse Trade), so refreshing stock data never requires navigating back here specifically. Below the title, a "Data sources" caption reads `user_settings.data_provider` (the signed-in account's own Settings > Data Provider choice — `"dhan"`/`"yfinance_bhavcopy"`, migration `0028`) for stock prices, `get_settings().fundamentals_provider` for PE/PEG/dividends (this one stays an app-wide `.env`-driven setting -- fundamentals are never provider-branched per account, see the `refresh_bar.py` bullet below), and states the options/F&O source as a fixed string, "NSE + BSE Bhavcopy (end-of-day) — always, regardless of Data Provider" — there's no configurable per-account F&O provider (Dhan's API doesn't expose a bhavcopy-equivalent full options chain, see the Futures & Options section). The header's "Data freshness" line covers stock refresh only now (`last_fetch_at`, still needed for `get_market_state()`'s staleness check); the per-exchange F&O refresh timestamps live in the shared refresh bar's own captions instead of a Dashboard-only line. "Latest NSE Bhavcopy: <date>" / "Latest BSE Bhavcopy: <date>" are still Dashboard-specific, each from its own `fo_repo.get_latest_fo_trade_date(client, source_prefix=...)` call (`"nse_fo_bhavcopy"` / `"bse_fo_bhavcopy"` -- same `source`-prefix scoping the fo-refresh Edge Function's own watermark query uses, and for the same reason: NSE and BSE publish on the same trading days, so one combined "latest bhavcopy" figure couldn't tell you which exchange's file was actually newest, and a single shared line was exactly what made a real BSE-side false-success bug easy to miss -- see the Edge Functions section's "A third real bug, once BSE was added"). Each is deliberately **not** that exchange's own last-successful-fetch timestamp: a bhavcopy is published for a specific trading day and a run on a non-trading day finds nothing new, so a refresh can succeed today while the loaded data is still from a prior session -- these lines surface that distinction. Wrapped in the same `except APIError: None` degrade as the rest of this page's optional F&O reads, for a deployment that hasn't applied migration `0007` yet.

  **Live price override (migration `0030`, `user_live_prices`)**: once `df` is built from `latest_screener_view`, if `user_settings.data_provider != "yfinance_bhavcopy"` the page calls `snapshot_repo.get_user_live_prices(client, user_id, df["symbol"].tolist())` and overwrites `df["latest_price"]` for any symbol that call returns — the account's own cached live LTP from whichever broker "Stock & Option Data Refresh" last pulled it from (see the `refresh_bar.py` bullet below), taking priority over the shared, possibly-Yahoo-delayed `daily_screener_snapshots` value. A symbol this account hasn't live-priced yet (no broker connected, an unwatched symbol, an expired token) simply keeps its snapshot value — same `{**shared, **live}` merge pattern `src/utils/portfolio_page.py::load_live_broker_prices` already established for the portfolio pages. This same `live_prices` lookup is also consulted by the "(as of <date>)" stale-fallback marker described below — a row with a live-overridden LTP skips that suffix even if the *snapshot* row backing its 52W/returns/PEG columns is stale, since the LTP itself is fresh.

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
- **`2_Stock_Detail.py`** — Plotly candlestick (falls back to a line chart if OHLC is incomplete) with volume subplot, moving averages, dividend timeline, classification-history chart, and inline alert creation. It still fetches this symbol's `UserPosition` (`settings_repo.get_user_position`) purely to draw entry/target/stop-loss reference lines on the chart if one was saved previously — the **form** that let a user create/edit that position ("Your position notes": entry/target/stop-loss/holding-period/notes + a risk/reward-ratio metric) was removed on request, since the Portfolio page now owns real holdings and will eventually own position-style tracking too; there is currently no UI anywhere to create a *new* `UserPosition` row, only this page's read-only chart overlay of one saved previously. The Fundamentals column is rendered via `render_stat_grid()` instead of stacked `st.markdown` lines; the alert list uses `render_alert_row()` (see below) instead of printing the alert's raw Python `config` dict; the "Create a new alert" expander's inputs are wrapped in an `st.form`, matching the same pattern `4_Settings.py`'s Alerts section uses. A "📊 View F&O / options" button hands the current symbol to `5_Options.py` via `st.session_state["fo_symbol"]` + `st.switch_page`. `symbol_options` (the "Select a stock" picker) is `companies_repo.list_current_constituents(client)` unioned with `portfolio_repo.list_portfolio_symbols(client, user_id)` -- the signed-in user's own resolved portfolio symbols (ETFs, non-Nifty50 stocks) -- so a portfolio-only stock becomes viewable here the moment it's tracked, not just on the Dashboard. Tolerant of `portfolio_holdings` not existing yet (migration `0012`), degrading to Nifty50-only exactly as before this widening existed. **Live price override**, same mechanism and reasoning as the Dashboard's own (`user_live_prices`, migration `0030`, see that page's bullet above): once the selected symbol's `ScreenerRow` is loaded, if `user_settings.data_provider != "yfinance_bhavcopy"` the page calls `snapshot_repo.get_user_live_prices(client, user_id, [symbol])` and, if it returns a value for this symbol, replaces `row.latest_price` with it (`row.model_copy(update=...)`) before anything below renders — so the header price, chart, and every downstream calculation on this page see the live-overridden LTP, not the possibly-stale snapshot one.
- **`4_Settings.py`** — per-user thresholds, an "Alerts" section, notification channels, account, theme, change-password. The Alerts section (formerly its own `3_Alerts.py` page, folded in on request between "Screening thresholds" and "Notification channels") is the same three pieces that page always had: "Your alerts" (list + active toggle + delete), an "➕ Create a new alert" expander (alert CRUD including portfolio-wide alerts, `symbol IS NULL`), and a "Notification history" expander (the latter two are now expanders rather than always-open sections, to keep this now-longer page scannable — a purely presentational change, no behavior difference). Alert rows use `render_alert_row()` (shared with Stock Detail — one formatting implementation, two call sites) instead of a raw dict dump. Notification history stays `st.dataframe`-only on every viewport, deliberately not given a Tailwind mobile-card alternative — see the design-system note under Utils for why. The three permanently-disabled Email/Telegram/Slack notification checkboxes were collapsed into a single row of `render_pill()` "coming soon" badges next to the one real (In-app) checkbox, removing dead-weight disabled UI for unimplemented channels.
- **`5_Options.py`** — the F&O / Options screen for one stock (see the Futures & Options section above for the data pipeline). Symbol selector defaults to `st.session_state["fo_symbol"]` (set by selecting a row on the Dashboard's table or clicking Stock Detail's own "View F&O / options" button), falling back to `selected_symbol`. `fo_symbols` (the options list) is `fo_repo.list_fo_symbols(client)` -- already every symbol with an open futures contract, regardless of Nifty50 status -- falling back to current constituents if that's empty, then unioned with `portfolio_repo.list_portfolio_symbols(client, user_id)` so a portfolio-only stock is at least selectable even with zero F&O data (handled gracefully below, same as any Nifty50 stock with none). Renders: an expiry selector (drives the summary tiles and, indirectly, which expiry's chain gets reused rather than re-fetched in the CSP/CC tables below); summary tiles (spot / ATM strike / total CE OI / total PE OI / Put-Call ratio) via `render_stat_grid`, sourced from `fo_service.option_chain_summary(chain_rows)` for the selected expiry; a futures term-structure table (near/next/far, with basis vs spot) + a near-month daily-close Plotly chart; and two sections below that, **"5% CSP"** and **"5% CC"**, showing the actual calculation for the selected symbol rather than just the final Dashboard-column percentage. (There used to be a classic CE | Strike | PE option chain table between the futures chart and these two sections -- it was dropped on request; `chain_rows`/`option_chain_summary` are still fetched/used for the summary tiles and CSP's near-expiry row, but the pivoted per-strike display itself, and the now-unused `fo_service.shape_option_chain` pivot helper + its tests, were removed rather than left as dead code.):
  - **5% CSP** is a **near/next/far month table** (`fo_service.csp_5pct_for_rows`, one call per expiry — the same term-structure shape the Futures section above already uses), columns Term / Expiry / Spot / Strike / Put Premium / **Trade Date** / 5% CSP. The near row reuses the already-fetched `chain_rows` when the expiry selector above happens to be on the near expiry; next/far are fetched separately via `fo_repo.get_option_chain`. The Trade Date column is what actually surfaces a stale quote to the user — see `_freshest_rows`'s docstring above for why a strike's "latest" row can silently be weeks old.
  - **5% CC** is also a **near/next/far month table** (`fo_service.cc_5pct_for_rows`, one call per expiry, mirroring 5% CSP's own loop exactly), columns Term / Expiry / Strike (lowest ≥5% above spot) / Premium / Trade Date / **Net Investment** / 5% CC / **Assignment Profit** (`(strike / net_investment - 1) * 100`, `None`/"N/A" if `net_investment` is zero or negative -- premium ≥ spot). This originally only showed the nearest expiry as a stat-grid breakdown (via `fo_service.cc_5pct_map`, which itself just restricts to the nearest expiry and delegates to `cc_5pct_for_rows`) -- changed on request to match 5% CSP's table shape once a live user actually wanted to see next/far month CC yields too, not just the near month `dashboard_fo_metrics` already caches for the Dashboard. "Net Investment" and "Assignment Profit" only appear here, not on the Dashboard, which only ever caches/displays `cc_pct`.
  Both loops share one `_chain_rows_for(exp)` helper (reuses the already-fetched `chain_rows` when `exp` happens to be the expiry selected above, otherwise fetches that expiry's chain separately) and both use the cash-market spot for every expiry, not just the near one -- so **every** row of both tables, not just the near-month one, matches what the Dashboard would compute for that same expiry. Shaping is done by `fo_service.option_chain_summary`/`futures_term_structure`/`csp_5pct_for_rows`/`cc_5pct_for_rows`, not in the page.
  - **Portfolio CC** -- a third near/next/far table, shown *only* when the signed-in user actually holds this stock in at least one of their own saved portfolios (`portfolio_repo.list_holdings(client, user_id)`, filtered to this symbol; silently absent otherwise, unlike 5% CSP/CC above which always render). Computed via `fo_service.covered_call_for_holding` (avg-buy-price-vs-LTP-dependent target, nearest-strike, not 5% CC's fixed-5%-OTM floor filter) -- this used to mirror My Holdings' own "CC ROI"/"CC Assignment ROI" columns, but those were removed by request (see the "Per-holding covered-call suggestion" bullet in the Portfolio pages section below), making this the only place in the app showing this figure now. If the same portfolio name holds this symbol across multiple brokers, `portfolio_service.merge_holdings` combines them into one row first; if the stock is held in more than one *named* portfolio, one table renders per portfolio (each with its own qty/avg price subheading), since different portfolios can have different cost bases and thus different target strikes. Columns: Term / Expiry / Strike / Premium / Trade Date / Invested Amount / CC ROI / CC Assignment ROI.

  **A real bug found here, right after this section first shipped**: the CSP/CC breakdown's spot value (CC was still "ITM PMCC" at the time, but the bug and fix applied identically) was initially taken from `option_chain_summary(near_chain_rows)["spot"]` — the F&O bhavcopy's own `underlying_price` column — while the Dashboard's two columns (now the `dashboard_fo_metrics` cache, see above) use the cash-market `latest_price` from `latest_screener_view`. These two prices aren't the same value, so this page's numbers didn't match the Dashboard's for the same stock (confirmed live: ADANIENT showed 5% CSP = 0.54% on the Dashboard but 0.45% here, since a different spot picked a different nearest-5%-below strike, 3040 vs 3020). Fixed by fetching `snapshot_repo.get_latest_screener_row(client, symbol).latest_price` and using that as the spot for both calculations here too, instead of the chain's `underlying_price` — the top-of-page "Spot"/"ATM strike" summary tiles are unaffected and deliberately still use the chain's own `underlying_price` (correct for highlighting the ATM row in the actual option-chain data being displayed there). If you add another F&O-derived calculation to either screen, source spot the same way this one now does — from the screener, not the chain — to keep the two screens' numbers in agreement.

- **`7_My_Trades.py` / `8_My_Holdings.py` / `9_My_Positions.py` / `11_My_CSP.py` / `12_My_Portfolio_Trades.py` / `13_My_Other_Trades.py` / `10_Analyse_Trade.py`** — seven pages (`10_Analyse_Trade.py` hidden from the sidebar, see the "Streamlit app" section above) replacing what used to be one combined `6_Portfolio.py` (sidebar label "My Portfolio", retired). `pages/6_My_Broker.py`, an earlier page in this family, was later deleted entirely -- CSV upload dropped along with it, and its connect/sync UI moved into Settings' "Data Provider" section (see the Streamlit app section above and the dedicated Portfolio pages section below for the full story). `12_My_Portfolio_Trades.py`/`13_My_Other_Trades.py` are the newest two, though they've since diverged: `13_My_Other_Trades.py` is still exactly `7_My_Trades.py`'s own trades-table code with one extra `trade_type` filter applied before the bucket split (`portfolio_service.is_other_trade_type`, next to `is_csp_trade_type`), not a new grouping/analytics engine, but `12_My_CC.py` (as it was originally called) was rebuilt into a per-leg table matching `11_My_CSP.py`'s own depth, then rebuilt again into `12_My_Portfolio_Trades.py` -- one row **per Trade**, not per leg, covering every "Portfolio "-prefixed strategy rather than Covered Call only (see the dedicated Portfolio pages section below for the sync → save → refresh-registration pipeline and the My Trades/Analyse Trade/My CSP/My Portfolio Trades/My Other Trades grouping). Each page reads every one of the signed-in user's saved rows across every portfolio and broker via `src/utils/portfolio_page.py`'s shared cached loaders; My Holdings/My Positions/My Trades/My CSP/My Portfolio Trades/My Other Trades each render one `st.tabs` entry per distinct `portfolio_name` (union of holdings' and positions' names — a portfolio can exist on positions alone; in practice this is a single tab for almost every account now, since the live sync flow only ever targets one resolved name — see the Portfolio pages section's "One portfolio per account" note below) scoping `portfolio_service.merge_holdings`/`compute_portfolio_view` (LTP via `snapshot_repo.get_latest_prices`, a direct `daily_screener_snapshots` query, deliberately **not** `latest_screener_view` — see below for why — overridden by a live broker quote wherever `portfolio_page.load_live_broker_prices` finds one, on request; see the LTP Underlying and Holdings sections below) and `compute_positions_view` to just that portfolio's own rows. Every table on these pages is a plain `st.dataframe` — see below for why, and for how row selection replaced the per-row 🔍 button.

## Portfolio pages

The signed-in user's own broker holdings and F&O positions (not the
Nifty50 screener universe) span seven pages -- `pages/7_My_Trades.py`
(holdings + positions grouped by underlying into Trades),
`pages/8_My_Holdings.py` (equity holdings, valued live against the app's
own market data), `pages/9_My_Positions.py` (per-leg F&O positions split
into Stock Options/Index Options/Others tables, valued against each
file's own LTP -- see the Positions subsection near the end of this
section for why), `pages/11_My_CSP.py` (every position leg from a
"CSP"-tagged Trade, with the underlying's own LTP/1D/5D/20D change -- see
its own subsection below), `pages/12_My_Portfolio_Trades.py` (formerly
`12_My_CC.py`, Covered-Call only -- every "Portfolio "-prefixed Trade,
one row per Trade: the covered stock's own Holding/Avg Price/Invested/
LTP/Momentum alongside up to 4 option legs -- see its own subsection
below, right after My CSP's), `pages/13_My_Other_Trades.py`
(My Trades' own
Stock/Index/Other tables, filtered to neither "CSP" nor Portfolio-prefixed --
see its own subsection further below), and `pages/10_Analyse_Trade.py`
(one Trade's
detail, registered `visibility="hidden"` in `app.py` so it's reachable
via `st.switch_page` but never shows as its own sidebar link). There used
to be a sixth page here, `pages/6_My_Broker.py` (CSV upload, connect
Dhan/Zerodha, create/delete portfolios) -- it was deleted entirely once
CSV upload was dropped; connecting a broker now happens in Settings'
"Data Provider" section instead (`src/utils/data_provider_settings.py`,
see the "Connect Dhan account" subsection
below), and there is no more portfolio create/delete UI at all (see "One
portfolio per account now, in practice" further down). Before My Broker
existed, this was one combined page (`pages/6_Portfolio.py`, retired) --
most of the mechanics below are unchanged, just relocated; the split
itself, and the My Trades/Analyse Trade grouping, are covered in their
own subsections further down. All seven pages share one module,
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
`companies`** — at sync time a correctly-resolved symbol (an ETF, a
fund, a non-Nifty50 stock) may not have a `companies` row yet at all;
forcing the FK would make saving a freshly-synced portfolio fail until
some *other* process happened to register that symbol first.

**Multiple portfolios per user, all coexisting at the schema level**
(`0014` — a real request after `0012` shipped: the first cut only ever
supported one implicit portfolio per user, sync-only). `0014` adds
`portfolio_name text not null default 'Portfolio 1'` (the default
backfills existing rows so they stay visible under a real tab
post-migration) and widens the primary key from `(user_id, broker,
raw_name)` to `(user_id, portfolio_name, broker, raw_name)` — the same
broker + raw instrument name can now be saved once per distinct
portfolio without colliding. `PortfolioHolding` gained a required
`portfolio_name: str` field to match; a deployment that's applied `0012`
but not yet `0014` fails **every** row with a Pydantic `ValidationError`
(`portfolio_name` missing), not a `postgrest.APIError` — every Portfolio
page catches both around its own `load_holdings` call
(`src/utils/portfolio_page.py`) and shows one combined "apply 0012 and
0014, in that order" message (confirmed live
against the deployed project, which had `0012` but not yet `0014` at the
time this shipped: `list_holdings` raised exactly this `ValidationError`,
not an `APIError`).

**One portfolio per account now, in practice** — a later simplification,
once `broker_connections` became account-wide (migration `0029`) rather
than scoped to an individual named portfolio, and `pages/6_My_Broker.py`
(the only place a user could ever type a new `portfolio_name` or pick
which one to sync into) was deleted. The multi-portfolio *schema* above
is unchanged and still theoretically supports several coexisting
`portfolio_name`s per user, but there's no UI left anywhere that creates
a second one: `portfolio_repo.get_or_default_portfolio_name(client,
user_id) -> str` (plus a `DEFAULT_PORTFOLIO_NAME = "My Portfolio"`
constant) is what the Dhan portfolio sync flow
(`_sync_dhan` in `src/utils/data_provider_settings.py`,
see the "Connect Dhan account" subsection
below) calls instead of taking a free-text portfolio-name input — it
returns the account's existing `portfolio_name` if `list_holdings`/
`list_positions` share exactly one, the alphabetically-first one
(deterministic, not dependent on set-iteration order) if legacy data
from before this change still has more than one, or the default constant
for a brand-new account with nothing saved yet. Existing multi-portfolio
data created back when My Broker's "+ New portfolio" tab existed is left
completely untouched by this — just no longer reachable from a live
sync, since there's nowhere left in the UI to pick a different name. The
per-`portfolio_name` `st.tabs()` rendering on My Trades/My Holdings/My
Positions/My CSP (below) is unchanged code either way — it just now
naturally renders a single tab for almost every account, since sync only
ever targets one resolved name.

**`portfolio_repo.replace_broker_holdings(client, user_id,
portfolio_name, broker, holdings)`** is still the one function that
writes holdings, called by `_sync_dhan` against the one
resolved `portfolio_name` above: a delete-then-insert scoped to
`(user_id, portfolio_name, broker)` — full sync, not a merge, so a
holding no longer in the broker's response disappears rather than
lingering. Nothing about this function itself changed; what changed is
that every caller now always resolves to the same one name, so in
practice it only ever "updates" the account's single portfolio rather
than ever spontaneously creating a second one from a user-typed name (as
the old "+ New portfolio" flow used to allow).

**The per-portfolio tab-tracking/create/delete machinery described in
earlier versions of this doc lived entirely on `pages/6_My_Broker.py`
and no longer exists** — that page (its `st.tabs(...,
key="broker_active_tab", on_change="rerun")` active-tab tracking, its
"+ New portfolio" tab, its locked-Broker-field selectbox, and several
real Streamlit bugs fixed along the way, e.g. a `StreamlitAPIException`
from writing `st.session_state["broker_active_tab"]` from inside an
already-instantiated tab) was deleted wholesale once CSV upload was
dropped and the connect/sync flow moved to Settings' "Data Provider"
section (see the "Connect Dhan account"
subsections below). The other four (now five, with the addition of My
CSP) Portfolio pages were never part of that machinery — each still
renders the exact same plain, unkeyed `st.tabs(portfolio_names)` it
always did (no "+ New portfolio" tab, no active-tab tracking, since none
of them could ever create or delete a portfolio), and that code is
completely unaffected by My Broker's removal. In practice this now
renders a single tab for almost every account, since the sync flow only
ever resolves to one `portfolio_name` (see "One portfolio per account
now, in practice" above).

**Deleting a portfolio** (`portfolio_repo.delete_portfolio(client,
user_id, portfolio_name)`) — an unconditional delete of every row for
`(user_id, portfolio_name)`, every broker within it (plus every Trade
grouping/metadata row -- `portfolio_trade_groups`/`portfolio_trade_meta`,
see My Trades below). This function itself is untouched and still works
exactly as before, but **it currently has no caller anywhere in the
app** -- the two-step-confirmation `st.expander("🗑️ Delete ...")` UI that
used to invoke it lived on My Broker's per-tab layout and was deleted
along with the rest of that page's tab machinery, and nothing on
Settings' "Data Provider" section replaced it (that section only offers
"Disconnect", which removes the saved credential via
`delete_broker_connection`, not the synced holdings/positions
themselves). Worth knowing if a future request asks for a way to clear
out a portfolio again -- the repo-layer function is already there,
tested, and correct; it just needs a UI call site.

**Dhan only now, no CSV.** Holdings/positions now only ever come from
Dhan's own API — `src/services/portfolio_service.py` has no
CSV parsing left at all (`parse_zerodha_csv`, `parse_dhan_csv`,
`parse_zerodha_positions_csv`, `parse_dhan_positions_csv`,
`parse_dhan_position_name`, `match_symbol`, `_normalize_name`, and
`_to_float` were all deleted, along with their tests, when
`pages/6_My_Broker.py` was removed). `dhan_holdings_from_api`/
`dhan_positions_from_api` translate Dhan's raw JSON response
into the same broker-agnostic dict shape the old CSV parsers used to
produce, so every downstream function (`holdings_to_records`/
`positions_to_records`/`compute_portfolio_view`/`compute_positions_view`)
and the rendered tables are unaffected by which source a row came from
(a second broker's translation function, plumbed through the exact same
downstream functions, is how Zerodha's own support briefly worked --
removed entirely along with the broker, see "Removed: Zerodha" below).
Dhan's holdings/positions responses already carry the exact NSE
symbol (`tradingSymbol`) rather than a
free-text company name, so there's no equivalent of the old
`match_symbol` fuzzy name-matching left to do at all -- see "Connect Dhan
account" below for exactly what its
translation functions do and the real bugs found building them
(`drvOptionType` spelling, pledged-quantity holdings, etc.).

**The API response's own valuation fields are still never trusted** —
`compute_portfolio_view`/`compute_positions_view` always recompute
`cur_val`/`pnl`/`pnl_pct` from the app's own live/cached LTP, the same
"never trust the source's own P&L math" rule the old CSV path followed
for the file's LTP/Cur. val/P&L columns, now applied to whatever
market-value figure the broker's own API response happens to include.

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
raw ticker itself -- Dhan's own `tradingSymbol` field is the exact NSE
symbol, with no separate display name available).
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
plain `" (as of <date>)"` suffix appended to its `"LTP"` cell string --
unless that row's symbol got a live-price override from `user_live_prices`
(migration `0030`, see the Pages section's `1_Dashboard.py` bullet's "Live
price override" paragraph above): that symbol's LTP is fresh even when
the snapshot row backing its other columns isn't, so the suffix is
skipped for it specifically.
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

**Positions, decoded to a common shape** (`src/services/portfolio_service.py`)
— Dhan's API response already embeds the exact NSE underlying symbol or
the fields needed to derive it (no company-name fuzzy matching needed,
and no CSV involved anymore -- see "Two brokers, no CSV" above, now
really "one broker"):

- *Zerodha used to be decoded here too* (`zerodha_positions_from_api` /
  `parse_zerodha_option_instrument`, two regexes for Kite Connect's
  weekly -- `NIFTY2681123000PE` -- and monthly -- `NIFTY26AUG23100PE` --
  tradingsymbol formats, the latter's expiry computed via a
  `_last_thursday` helper) until it was removed entirely along with the
  broker -- see "Removed: Zerodha" above.
- **Dhan** (`dhan_positions_from_api`) — needs no regex decoding at all:
  `GET /v2/positions` carries `drvExpiryDate`/`drvStrikePrice`/
  `drvOptionType` directly on each row, so expiry/strike/type are read
  straight off the response. Only the underlying symbol itself still
  needs deriving from `tradingSymbol` (`_dhan_underlying_symbol`, a
  right-anchored split by trailing-token count for a real derivative, the
  bare tradingSymbol verbatim for a plain equity/ETF position) — see
  "Connect Dhan account" below for the full translation, including the
  real `drvOptionType`-spelling bug and the M&M-truncation bug found
  building it. (The old CSV path's
  `parse_dhan_positions_csv`/`parse_dhan_position_name` — which decoded
  Dhan's space-separated `Name` column, e.g. `"ONGC 25 AUG 230 PUT"`, via
  a from-scratch year-inference heuristic — was deleted along with the
  rest of CSV upload; the API's own structured fields make all of that
  unnecessary.)

**`raw_name` collisions are disambiguated, not assumed unique** —
`dhan_positions_from_api` ends with a
`Counter`-based dedup pass over the batch before returning. `raw_name` is
Dhan's tradingSymbol, and `portfolio_positions`' primary key is
`(user_id, portfolio_name, broker, raw_name)` — confirmed live via
`postgrest.exceptions.APIError` `23505` (`duplicate key value violates
unique constraint "portfolio_positions_pkey"`): the same tradingSymbol can
legitimately appear twice in one sync (Dhan allows the same contract held
under two `productType`s at once, e.g. an INTRADAY trade alongside a
carried-forward CNC/NRML position). The dedup is exhaustive, not just a `productType`
suffix — a second live account hit a collision with **no**
intraday/overnight overlap at all, meaning Dhan can apparently return
same-symbol rows sharing one `productType` too — so it falls back through
`productType`/`product` → `securityId` → a bare ordinal `#2`/`#3` suffix,
guaranteeing a unique `raw_name` regardless of cause. Only the *colliding*
rows get renamed (e.g. `"RELIANCE (INTRADAY)"`); an account with no overlap
keeps today's exact `raw_name`, so existing `trade_date`/`stop_loss`/
trade-group links (all keyed on `(broker, raw_name)`, see My CSP/Analyse
Trade below) survive the next sync unchanged.

**P&L is recomputed, not trusted from Dhan's own response** —
`compute_positions_view`: `pnl = (ltp - avg_price) * qty`, which is
direction-correct for both a long (positive qty) and a short (negative
qty) position without an `if` — originally cross-checked against every
row of a sample CSV export this feature was first built
against (e.g. Dhan's "HINDALCO 25 AUG 860 PUT", qty -700, avg 2.55, ltp
1.05: `(1.05 - 2.55) * -700 = 1050`, matching the file's own reported
"1,050.00" exactly), a verification that still holds now that Dhan is
synced live via its API instead, since the formula itself didn't change.
`pnl_pct = pnl / (avg_price * abs(qty)) * 100` — against the *premium*
notional, since there's no equivalent of a holding's "investment" for a
written option. Both are `None` (→ "N/A") when `ltp` is missing. Dhan's
own reported percentage field was checked and rejected as a
data source back when this was built against sample exports -- not
reliably direction-aware -- so it's never treated as authoritative;
`pnl`/`pnl_pct` are always recomputed the same way instead.

**Option-leg LTP is resolved once at sync time, not fetched live on
every render** — there's no equivalent *live* source for options the way
there is for equities: `nse_fo_provider.py` is EOD-only for every
underlying, index or stock (see "This is end-of-day data" above), so a
position's premium can only ever be as fresh as whatever Dhan's
own API returned at sync time, or (when its own quote is
unavailable) this app's own EOD F&O bhavcopy as a fallback -- see
"Connect Dhan account" below for the full
`get_ltp_by_security_id` + `apply_fallback_option_ltp` chain, including
index options as of migration `0018`.

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
`dhan_positions_from_api`'s direct
`drvExpiryDate`/`drvStrikePrice`/`drvOptionType` read always sets
`option_type` and `symbol` together from one decode-or-nothing result;
there's no row with one set and not the other. The Stock/Index Options tables share identical columns
(**Instrument** — the broker's raw contract string; **Underlying** — the
decoded symbol; **Expiry**; **Strike**; **Type**; **Qty**; **Avg Price**;
**P&L**; **P&L %**); Others drops the four option-specific columns since
they don't apply (**Instrument** — here just `raw_name`, since `symbol` is
always `None` in this bucket; **Qty**; **Avg Price**; **P&L**; **P&L %**).
The page-level "Total P&L" stat above the three tables still sums across
all of them, unchanged.

**Connect Dhan account (`broker_connections`, migration `0017`; account-wide
since migration `0029`)** — selecting "Dhan" as the Data Provider in
Settings (`src/utils/data_provider_settings.py::_render_dhan_connect_section`,
the CSV-upload-era `pages/6_My_Broker.py` this section used to live on
having been deleted) reveals a credential form; saving a Client ID +
Access Token (`upsert_broker_connection`) and clicking "Save & Sync"
drives the sync (`_sync_dhan`, targeting the account's one resolved
portfolio -- see "One portfolio per account now, in practice" above).
Once already connected, re-syncing happens via the **Portfolio Refresh**
button on My Trades/My Holdings/My Positions/My CSP instead (Settings no
longer has its own standalone sync button -- see the `refresh_bar.py`
bullet below), calling the same `_sync_dhan` through
`sync_broker_portfolio`. This reuses
`src/data_providers/dhan_provider.py`'s existing `DhanProvider` class —
already present in this codebase as an alternative live-price source for
the main screener pipeline (`Settings.market_data_provider == "dhan"`,
`src/config.py`'s app-wide, `.env`-driven, cron-facing setting — a
**different** "provider" concept from `UserSettings.data_provider` above,
which is per-user and only affects what this signed-in account itself
sees; don't confuse the two if you touch either) — rather than a separate
module, since the auth/header/throttle mechanics are identical; only the
credentials differ (per-account here, one row per `(user_id, "Dhan")`,
vs. one app-wide pair from `.env`/`Settings` for the cron price
pipeline). Methods added to that same class: `get_holdings()` (`GET
/v2/holdings`), `get_positions()` (`GET /v2/positions`),
`get_ltp_by_security_id()` (`POST /v2/marketfeed/ltp`, generalized to
arbitrary exchange segments like `NSE_FNO`/`IDX_I`, unlike the existing
`get_quotes()` which is hardcoded to `NSE_EQ` for the equity pipeline),
and `get_trade_history(from_date, to_date)` (`GET
/v2/trades/{from-date}/{to-date}/{page}`, paginating from page `0` until
an empty page comes back — feeds the Trade History page's Realized
P&L/journal, see the Portfolio pages section below). These deliberately skip the
`@retry`-decorated, auto-backoff `_post()` the price pipeline uses — a
manual sync click should fail fast (especially on an expired token)
rather than silently retrying for up to ~20s — and instead go through a
plain `_request()` that raises `DhanAuthError` (a `ProviderError`
subclass) specifically on a 401, so the page can show "your token expired"
instead of a generic error.

`portfolio_service.dhan_holdings_from_api`/`dhan_positions_from_api`
translate Dhan's raw JSON rows into this app's own broker-agnostic dict
shape (the same shape the now-deleted CSV parsers used to produce), so
every downstream function
(`holdings_to_records`/`positions_to_records`/`compute_portfolio_view`/
`compute_positions_view`) and the rendered tables are identical regardless
of source. Two things were notably *easier* to build here than the old
CSV path had been: `tradingSymbol` in the holdings response is already
the exact NSE symbol (no fuzzy name-matching needed, unlike the old
CSV path's `match_symbol()`), and the positions response
carries `drvExpiryDate`/`drvStrikePrice`/`drvOptionType` directly (no
regex instrument-name decoding needed) — only the underlying symbol itself
is extracted from `tradingSymbol` (`_dhan_underlying_symbol`), confirmed
against a real account's positions to look like
`"NIFTY-Aug2026-23000-PE"` / `"HDFCBANK-Aug2026-700-PE"` for a real
derivative, or the bare underlying itself with no suffix at all for a
plain equity/ETF position (e.g. `"SILVERBEES"`). This originally matched
a leading run of `[A-Z]+` and stopped at the first non-letter character —
confirmed live as a real bug once an account held an **M&M** (Mahindra &
Mahindra) option: the `&` broke the match, truncating the underlying to
just `"M"`, a symbol that doesn't exist, so that leg's live LTP/Momentum/
1D/5D/20D all silently came back blank on My CSP/My Portfolio Trades. Fixed by keying
off this row's own `is_derivative` (a real `drvExpiryDate`, not absent/
the no-expiry sentinel below) rather than the tradingSymbol's shape, and
splitting from the **right** by a known trailing-token count (3 for an
option, 2 for a future) instead of matching from the left — the same
right-anchored approach `dhan_provider.py`'s `_underlying_from_trading_symbol`
already uses for the F&O instrument-master's own trading symbols, and for
the same reason: the underlying can contain a non-letter character (`M&M`)
or its own hyphen (`NAM-INDIA`, `BAJAJ-AUTO`), so neither a leading-letters
regex nor a naive split-on-first-hyphen holds. Keying off `is_derivative`
specifically (rather than "does the tradingSymbol contain a hyphen")
matters because a plain hyphenated equity/ETF symbol (e.g. an intraday
`"NAM-INDIA"` stock trade) is structurally indistinguishable from a
genuine 2-token futures suffix otherwise. One thing *isn't* as documented: `drvOptionType` comes back
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
`ltp_as_of` on `PortfolioPosition` (`None` = live/as-given — a
successful Dhan Market Quote call; a date = this
fallback fired). `apply_fallback_option_ltp`
now returns `(price, trade_date)` pairs from its lookup table instead of
just `price`, and only ever sets `ltp_as_of` on the same branch that
already only-ever-fills-a-gap (never touches a position whose `ltp`
Dhan already supplied). `positions_to_records` reads it via
`p.get("ltp_as_of")`, not `p["ltp_as_of"]`, since a live-supplied
`ltp` never sets that key at all —
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

**Renewing an active token in place ("Renew Token (+24h)").** Added
after a real pain point: on mobile and away from a laptop, an expired
token meant the app was simply unusable until you could reach
`web.dhan.co` and paste a fresh one. Dhan's own `POST /v2/RenewToken`
(`DhanProvider.renew_access_token`, `_renew_dhan_token` in
`src/utils/data_provider_settings.py`) extends an **already-active**
token by another 24 hours in place — no `web.dhan.co` visit, works from
any device the app itself is reachable from. Two things make it safe to
build on:

- It reuses the exact same bearer token already sitting in
  `broker_connections.access_token` — the renewal call just posts that
  token back with a `dhanClientId` header (camelCase; this is the one
  Dhan v2 endpoint that wants that header name instead of the
  `client-id` every other call in this class uses) and gets a new token
  string back (`accessToken` in the response body) to overwrite it with
  via the same `upsert_broker_connection` path Save & Sync uses. No new
  credential type, no new risk surface beyond what already exists.
- It's documented to work **only on a token that hasn't expired yet** —
  renewing an already-expired one 401s exactly like a sync attempt
  would, mapped to the same `DhanAuthError` → "paste a fresh one below"
  message. There's no way around that once the 24 hours are actually up;
  this only helps if you renew *before* it lapses (e.g. right after the
  "renew it now" ~10-hour warning fires -- deliberately early, well ahead
  of the ~24-hour expiry, so there's a wide safe window even if the app
  isn't opened again for a while, e.g. on mobile).

Deliberately **not** built on Dhan's other two token-issuing flows, both
of which genuinely can mint a brand-new token even past expiry, entirely
headlessly: a `dhanClientId` + PIN + TOTP endpoint, and a 12-month
API-key/secret pair. Both were ruled out for the same reason — either
one means storing the account's actual login credential (a PIN, or a
TOTP seed capable of generating valid codes indefinitely) at rest in
`broker_connections`, which is a different, larger category of risk than
a revocable, 24-hour bearer token: a leaked bearer token is a ticking
clock; a leaked PIN/TOTP seed lets an attacker log into the account
itself, no expiry. See `tests/test_dhan_provider.py`'s
`TestRenewAccessToken` for the header/response-shape/error-mapping
coverage.

**Removed: Zerodha.** This section used to document a full second
broker-connect flow -- Kite Connect's OAuth-style login (`login_url()`/
`generate_session()`, a `sha256(api_key + request_token + api_secret)`
checksum exchange, `app.py`'s `url_path="Settings"` pin existing partly
to keep that OAuth Redirect URL stable), a fixed-daily-6am-IST session
expiry check (`_zerodha_token_is_fresh`) distinct from Dhan's rolling
24-hour one, and `zerodha_positions_from_api`/
`parse_zerodha_option_instrument` reusing the same weekly/monthly
tradingsymbol regex the old CSV importer used. All of it -- code, tests,
and this account's own Zerodha-synced database rows -- was removed
entirely once that account's Zerodha session had drifted too far out of
sync to be worth maintaining, not merely disconnected. Two real,
worth-remembering bugs were found and fixed while this flow existed
(preserved here since the underlying lessons are still broadly useful):
a "Connected" state could show as true immediately after saving
credentials, before ever completing login, because a fresh
`broker_connections` row's `token_saved_at not null default now()`
column default fired on the credentials-only `INSERT` regardless of
whether a real session existed yet -- fixed by requiring
`access_token is not None` alongside the freshness check, not freshness
alone; and Kite's holdings `quantity` field reports only the *free*
(non-pledged) portion, silently dropping any fully-pledged holding
(GILT5YBEES, LIQUIDCASE, LTGILTCASE, NIFTYBEES on a real account) from a
naive `if not qty: continue` guard -- fixed by summing `quantity +
t1_quantity + collateral_quantity` to reconstruct the true owned
quantity. `broker_connections.api_secret` (added by migration `0022`
specifically for this flow) is left in the schema, permanently unused,
rather than dropped in a migration. See git history for the full
narrative and code if it's ever needed as a reference.

**A third real bug, this one caused by the removal itself, found
live**: an account that still had `user_settings.data_provider =
'zerodha'` saved from before the removal got a hard
`pydantic.ValidationError` on **every single page** -- `require_login()`
calls `settings_repo.get_user_settings()` before any page body runs, and
`'zerodha'` was dropped from `src/models/user.py`'s `DataProvider`
Literal type along with everything else. The `user_settings.data_provider`
CHECK constraint (migration `0028`) was DB-level only, so nothing at the
database layer caught this -- it surfaced purely as an application-level
Pydantic validation failure. Fixed on both ends: migration
`0040_zerodha_data_provider_cleanup.sql` updates any stored `'zerodha'`
row to `'yfinance_bhavcopy'` and tightens the CHECK constraint to match
(same drop/recreate pattern as `0033`/`0037`/`0039`); `settings_repo.get_user_settings`
also now catches this specific `ValidationError` (checking
`exc.errors()` for a `data_provider`-scoped failure specifically, not a
blanket `except ValidationError`) and degrades to the same default in
code, so an account is never blocked on the migration having been
applied yet -- any other validation failure on that row still raises
normally. See `tests/test_settings_repo.py` for the covered cases
(no row at all, a valid stored value, the legacy `'zerodha'` value, and
confirming an unrelated bad row still raises).

**Holdings split by `company_type` (ETFs & Mutual Funds vs Stocks)** — the
old single "My Holdings" table became two, `_render_holdings_table` (a
small helper factored out of what was previously an inline block in
`_render_holdings_tab` (`pages/8_My_Holdings.py`), so both tables share
the exact same columns, `column_config`, and single-row-selection "Open
in Stock Detail"/"Open in Options" behavior, just keyed with a different
`key_suffix` so their widgets don't collide). The split itself is a pure
filter over `companies.company_type` (loaded via the already-cached
`src/utils/portfolio_page.py::load_all_companies`, shared by every
Portfolio page that needs a symbol's `company_type` for bucketing --
My Trades, My Positions, My CSP, Analyse Trade, and this split):
`ETF`/`Fund` symbols go to "ETFs & Mutual Funds",
everything else (`Equity`, `Index`, and any holding with no resolved
symbol at all -- there's no company_type to check for one of those) goes
to "Stocks". The Total Investment/Cur Val/P&L/P&L% stat grid above both
tables is untouched -- it still aggregates across every holding regardless
of which table it lands in.

**LTP (and everything derived from it -- Cur Val, P&L, P&L%) prefers a
live broker quote over `daily_screener_snapshots`, on the same request
that added it to My CSP's LTP Underlying**: `_render_holdings_tab` calls
`load_latest_prices` for the base value, then overrides it with
`load_live_broker_prices(client, user_id, symbols,
cache_bust)` for any symbol a connected Dhan broker
actually quotes -- falling back to the snapshot value for the rest (no
broker connected, an expired token, or a symbol Dhan's feed
doesn't cover). `load_live_broker_prices` dropped its own `portfolio_name`
parameter once `broker_connections` became account-wide (migration
`0029`) -- one connected Dhan account now covers every portfolio
the account has, so there's no per-portfolio credential lookup left to
scope by. Same merge pattern as My CSP: `{**ltp_by_symbol,
**live_ltp_by_symbol}`, so only symbols the live call actually priced get
overridden.

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

`src/utils/portfolio_page.py::build_trade_legs(client, user_id,
cache_bust, holdings_for_portfolio,
positions_for_portfolio)` is the shared leg builder both
`7_My_Trades.py` and `10_Analyse_Trade.py` call (My CSP also calls it, to
build the same unmerged leg list before filtering down to CSP-tagged
Position legs — see below), so their grouping/labels never drift apart:
holding rows are priced via `compute_portfolio_view` (called on the
**unmerged** per-broker dicts, not `merge_holdings`'s cross-broker-
combined rows -- Trades need leg- level identity, the same `(broker,
raw_name)` natural key `portfolio_trade_groups` is keyed by) and
re-tagged `leg_type="Holding"` by zipping the input list against
`compute_portfolio_view`'s output rows (that function builds a
fixed-key dict per row, so any caller-supplied extra key like `broker`
doesn't survive through it and has to be reattached afterwards);
position rows go through the existing `compute_positions_view`, which
*does* pass extra keys through (`{**p, "pnl": ..., "pnl_pct": ...}`), so
tagging `leg_type="Position"` before calling it is enough.

**`user_id` was added to this signature on request** ("apply the same
[live-broker-quote] logic to My Trades, My Holdings, My Positions and
Analyse Trade" -- following the same fix on My CSP's LTP Underlying
column, see further down). A Holding leg's price now goes through the
identical two-step lookup My CSP's `ltp_by_symbol` uses: `load_latest_prices`
(the `daily_screener_snapshots` base value) then overridden by
`load_live_broker_prices(client, user_id, holding_symbols, cache_bust)`
wherever a connected broker actually quotes that symbol. Since this
function is the single shared leg builder, that one change automatically
reaches every page that calls it — My Trades' Total P&L, Analyse Trade's
legs table LTP/P&L, and (for the Holding legs it discards before
filtering to CSP Positions only) My CSP. `portfolio_name` was originally
added to this same signature alongside `user_id` (both needed for the
per-portfolio `load_live_broker_prices` lookup at the time), then
**dropped again later** once `broker_connections` became account-wide
(migration `0029`) and `load_live_broker_prices` itself lost its own
`portfolio_name` parameter — a caller-supplied `portfolio_name` had
nothing left to scope by. `build_trade_legs`'s callers still pre-filter
`holdings_for_portfolio`/`positions_for_portfolio` to one
`portfolio_name` before calling it, exactly as before -- only the
now-unnecessary parameter itself is gone. Position-leg pricing is
untouched by this — it was already resolved once at sync time (live
broker quote, or this app's own F&O bhavcopy as fallback, see "Connect
Dhan account" above), not recomputed per-render, so **My Positions
needed no code change at all** — it only ever reads `p["ltp"]` as
already-resolved.

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

**Auto-classified `trade_type` (Holding/CSP/Portfolio CC/Strangle/Jade
Lizard/Twisted Sister/IC)** -- until now `trade_type` was purely free text
the user typed on Analyse Trade (default `"Trade"`, no strategy
semantics at all). `portfolio_service.classify_trade_type(legs)` reads a
trade's current legs (`leg_type`, `option_type`, signed `qty`) and
returns one of those strings, or `None` if the shape doesn't match any
of them:
- **Holding**: one or more Holding legs and **zero Position legs** --
  just a stock/ETF holding, no options (or futures) involved at all.
  Added per an explicit user request, distinct from the plain `"Trade"`
  default a trade with no saved meta row falls back to display-side --
  this one is actually detected and auto-saved, same as every other
  strategy below.
- **CSP**: exactly one Position leg, a short PE, **and zero Holding
  legs** (a CSP is specifically *uncovered* -- a holding present rules it
  out, deliberately, even if everything else matches).
- **Portfolio CC** (Covered Call): at least one Holding leg plus exactly
  one Position leg, a short CE. Always returned as `"Portfolio CC"`,
  unconditionally -- renamed from the old bare `"Covered Call"` per an
  explicit user request, using the same "Portfolio " convention as the
  four below, except there's no bare "CC" variant to fall back to: the
  rule itself already requires a Holding leg to fire at all.
- **Strangle**: exactly one PE and one CE Position leg, both short or
  both long (mismatched direction doesn't count); a Holding leg may or
  may not also be present.
- **Jade Lizard / Twisted Sister**: three or more Position legs with
  exactly one long and the rest all short -- the long leg's side decides
  which name: a bought CE -> Jade Lizard, a bought PE -> Twisted Sister
  (confirmed with the user: this is the only way to tell the two apart
  from leg shape alone, since both are otherwise identical "1 long + 2
  short" structures).
- **IC (Iron Condor)**: exactly four Position legs, two long and two
  short -- a pure leg-count-and-direction rule, deliberately with no
  separate PE/CE-split requirement the way Strangle has one (confirmed
  with the user: "2 buy legs and 2 sell legs" was the literal spec, so a
  same-side 2-long-2-short combination also matches IC even though a
  textbook Iron Condor is specifically two verticals, one on each side).
- Any Position leg with `option_type is None` (undecoded contract, or a
  futures leg) present anywhere in the trade -> bails to `None` rather
  than guessing on incomplete information.

**"Portfolio " prefix for these last four when a Holding is present**
(added per an explicit user request -- a Strangle/Jade Lizard/Twisted
Sister/IC sitting alongside a stock holding is a materially different
risk profile from the same option legs on their own, even though the leg
*shape* that triggers detection is identical either way): `holdings and
positions` still decide *whether* one of these four fires, exactly as
above, but the returned string becomes `"Portfolio Strangle"`,
`"Portfolio Jade Lizard"`, `"Portfolio Twisted Sister"`, or `"Portfolio
IC"` whenever at least one Holding leg is present alongside the matched
Position legs. CSP never takes this prefix -- its own rule already
requires zero Holding legs. `trade_type_mismatch` (see `group_into_trades`
below) picks this up for free too -- a trade saved as plain `"Strangle"`
that later gains a holding leg will now detect as `"Portfolio Strangle"`,
disagree with the saved label, and get flagged exactly like any other
shape-changed trade. Same mechanism covers the Covered Call rename
itself: any trade already saved as the old `"Covered Call"` string will
now disagree with the freshly-detected `"Portfolio CC"` and surface a
`trade_type_mismatch` ⚠️ until re-saved.

**`is_portfolio_trade_type(trade_type)`** replaced the narrower,
now-deleted `is_covered_call_trade_type` -- a case-insensitive/trimmed
**prefix** match on `"portfolio "`, not an exact match against one fixed
string. This is what let `pages/12_My_CC.py` generalize into
`pages/12_My_Portfolio_Trades.py`: it needed to catch every
Portfolio-prefixed type (CC/Strangle/Jade Lizard/Twisted Sister/IC), not
just Covered Call, per an explicit user request to broaden the page from
"Covered Call only" to "any trade pairing a stock holding with option
legs." `is_other_trade_type` was updated to exclude
`is_portfolio_trade_type` rather than the old narrower check too, so
none of the five double up on both My Portfolio Trades and My Other
Trades. Being a plain string convention (like every other `is_*_trade_type`
check here), it's still just as blind to a trade's *actual* legs as the
narrower check was: a hand-typed custom label that also carries a
holding (e.g. `"Hedged"`, `"Batman"` -- both seen on a real account) won't
match unless renamed to start with `"Portfolio "`.

Two call sites, deliberately asymmetric (confirmed with the user -- a
trade the user has already typed a label for is never silently
overwritten):
1. **New trades get auto-classified and saved.**
   `data_provider_settings.py::_auto_classify_new_trades` runs at the end
   of `_sync_dhan` (same spot
   `_default_new_position_trade_dates` already runs). Re-reads the *full*
   current holdings+positions for the portfolio across **every** broker
   (not just the one just synced -- `portfolio_repo.list_holdings`/
   `list_positions`, filtered to `portfolio_name`), not the pricing-heavy
   `build_trade_legs` My Trades/Analyse Trade use, since classification
   only needs `leg_type`/`option_type`/`qty`. Groups via the existing
   `assign_trade_ids`, then for every `trade_id` with **no**
   `portfolio_trade_meta` row yet (checked via `list_trade_meta`) --
   meaning this account has never visited Analyse Trade for it, whether
   the trade is brand new this sync or has existed untouched for a
   while -- calls `classify_trade_type` and, if it returns a type,
   `portfolio_repo.set_trade_meta(...)`. A `None` result writes nothing,
   identical to today's untouched "Trade" default. Re-reading all brokers
   (not just the synced one) matters for real: a stock held via one
   broker with a call written via another still correctly classifies as
   one Portfolio CC, not two unrelated single-broker fragments -- this
   was genuinely exercised back when Zerodha was also supported.
2. **Already-classified trades are validated, not touched, at read time.**
   `group_into_trades` gained one more computed field per trade:
   `trade_type_mismatch` -- `True` only when a `trade_meta` row exists
   for this `trade_id` (however it got there) **and**
   `classify_trade_type(trade_legs)` returns a value that
   case-insensitively *disagrees* with the saved `trade_type`. A `None`
   detection (shape matches no known strategy) is never treated as a
   mismatch -- a custom label like "Earnings Play" isn't flagged just for
   not looking like an options strategy. Surfaced in two places, both
   already consuming `group_into_trades`' output: `pages/7_My_Trades.py`
   appends `" ⚠️"` to the "Trade Type" cell (plus one `st.caption`
   per table explaining the mark, shown only if at least one row in that
   table is flagged); `pages/10_Analyse_Trade.py` shows an `st.warning`
   above the edit form naming both the saved type and what
   `classify_trade_type` currently detects, right where the user can
   actually act on it by editing "Trade Type" below.

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
   contract, a previously-grouped leg's `portfolio_trade_groups` override
   row simply stops matching any current leg and silently falls back to
   its default per-underlying Trade. This also happened historically for
   any account that had been on the old CSV-upload flow and then switched
   to "Connect Dhan account" once that existed -- the API path derives
   `raw_name` from `tradingSymbol` rather than the CSV's own `Name`
   column, so it doesn't match a grouping saved under the old raw names
   (that migration path no longer applies to new accounts, since CSV
   upload is gone, but any grouping saved under it before this
   architecture shipped would have hit exactly this). Nothing errors or
   crashes; the manual grouping for that one leg just needs to be redone.
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
already lets you rename to anything ("Portfolio CC", "Aug Iron Condor",
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
width), then `Max Credit`, `LTP`, `P&L`, `Target P&L`, `Stop Loss`,
`Breakeven`, `LTP Underlying`, `Momentum`, `1D`, `5D`, `20D`. `Trade
Date` leads (everything else on the row can depend on it); `Max Credit`
sits right after `Avg Price` on request (it's `Avg Price * |Qty|`, so
reads naturally as "what Avg Price actually adds up to"); `Target
P&L`/`Stop Loss` sit right after `P&L` since they're the other
P&L-shaped numbers; `Momentum` sits just before `1D`/`5D`/`20D` (the
returns it's computed from) — see the dict literal in `_render_csp_tab`
for the exact order, which `pd.DataFrame` preserves as column order.

**Analyse Trade's own legs table was later rebuilt to match these
columns** (`Trade Date`/`Underlying`/`Expiry`/`Strike`/`Qty`/`Avg
Price`/`Credit`/`LTP`/`P&L`/`Target P&L`/`Stop Loss`/`Breakeven`/`LTP
Underlying`, renamed `Max Credit` → `Credit` along the way), replacing
its original, much plainer `Type`/`Broker`/`Instrument`/`Expiry`/
`Strike`/`Option Type`/`Qty`/`Avg Price`/`LTP`/`P&L` shape. Unlike My CSP
(Position-only, pre-filtered to CSP-tagged trades), Analyse Trade has to
work for *any* Trade shape and must still show every leg — Holding legs
included — since the merge/split controls below the table select rows by
position in `trade_legs`, unfiltered. `Credit`/`Target P&L`/`Stop Loss`
(`csp_max_credit`/`csp_target_pnl`/`csp_stop_loss`, computed and
persisted exactly like My CSP) only populate for a leg that's a
genuine **short** option (`leg_type == "Position"`, `option_type` and
`strike_price` both set, `qty < 0`) — a short call qualifies too (this
math is generic to any short option leg, put or call -- My Portfolio
Trades, formerly My CC, doesn't reuse it any more for its own summary
row, but Analyse Trade's own per-leg computation here is unaffected by
that), but a future
(`option_type` is `None`) or a long option shows `None`/"—" for all
three. `Breakeven` is narrower still — it's specifically
`csp_breakeven_price` (`Strike - Avg Price`), the textbook *put*
breakeven, so it only populates when the leg is additionally
`option_type == OptionType.PE`; a short call passes the "short option"
gate above for `Credit`/`Target P&L`/`Stop Loss` but still shows "—" for
`Breakeven`.

**A Holding leg gets its own `Target P&L` instead, on request** — since
there's no option premium to decay toward a target on a plain stock
holding, `leg["leg_type"] == "Holding"` takes a separate `elif` branch:
`target_pnl = 0.05 * leg["avg_price"] * leg["qty"]`, a flat 5% of the
holding's own investment (`qty` is always positive for a Holding, unlike
a short leg's negative `qty`, so no `abs()` is needed the way
`csp_max_credit` needs one), computed inline here rather than via a
shared `portfolio_service` function -- My Portfolio Trades (formerly My
CC) doesn't carry an equivalent "Target Stock P&L" concept in its
current, rebuilt form, so there's nothing left to keep this in sync
with; this is purely Analyse Trade's own per-leg math now. Feeding this
into the same `_fmt_pnl(pnl, pnl_pct, target_pnl, stop_loss)` call every
other leg type already uses means a Holding's `P&L` cell picks up the
same "✅ once it clears Target P&L" marker for free, with no separate
formatting path needed — `stop_loss` stays `None` for a Holding either
way, so there's no equivalent "❌ once it falls through" case for a
stock, only the ✅ one. `Credit`/`Stop Loss`/`Breakeven` all still stay
`None`/"—" for a Holding — there's no premium collected or strike to
compute those against, target P&L aside.

`LTP Underlying` applies to *every* leg with a resolved `symbol`,
Holdings included, since it describes the underlying itself rather than
the specific leg's own instrument — the same
`load_latest_prices`/`load_live_broker_prices` calls My CSP/My Portfolio
Trades already make, just over the distinct symbol set across this one
Trade's legs instead of a whole CSP/Portfolio Trades bucket.

**`Momentum`/`1D`/`5D`/`20D` were pulled back out of the table again, on
request, into their own section right above it** — one tile per distinct
underlying `symbol` across `trade_legs` (`load_returns_and_pe` over that
same symbol set), styled exactly like `2_Stock_Detail.py`'s (the Equity
page's) own "B · Momentum" scorecard tile: `st.metric(f"{symbol} -- B ·
Momentum", "1D/5D/20D")`, a markdown line spelling out the three
percentages (`format_pct`), then `pass_fail_badge(criterion_b(...))` —
the same ✅ Pass/❌ Fail/N/A text Stock Detail's own scorecard uses, not
the compact `pass_fail_icon` symbol My CSP/My Portfolio Trades' table
cells use. Laid
out via `st.columns(len(leg_symbols))`, one column per symbol (almost
always exactly one, since a Trade is normally single-underlying, but a
manually-merged Trade could span more) — this is a fact about the
underlying, not something that varies leg to leg, so repeating it as four
more columns on every leg row was redundant once there was somewhere
better to put it.

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

`LTP Underlying` is the underlying stock's own current price, base value
from `snapshot_repo.get_latest_prices` (`load_latest_prices` — the same
call My Holdings uses for its Cur Val column — a direct
`daily_screener_snapshots` query, not `latest_screener_view`, so it still
resolves for a portfolio-only symbol not in `nifty50_constituents`), then
**overridden with a live Dhan quote, where available** — on request, since
`daily_screener_snapshots` is only as fresh as whatever "🔄 Market Data
Refresh" last pulled (Yahoo-sourced for an account on the default
YFinance + Bhavcopy Data Provider, ~15-20min delayed, and only as current
as the last manual click), while a user whose Settings > Data Provider is
set to Dhan (see the "Connect Dhan account" subsection above) already has a live-data source
sitting right there — this LTP Underlying override, unlike the Dashboard/
Stock Detail's `user_live_prices` cache (migration `0030`, see the Pages
section above), fetches live on every render rather than reading a
cached value, the same as every other portfolio-page broker-live LTP.
After the snapshot lookup, `_render_csp_tab` calls
`portfolio_page.load_live_broker_prices(client, user_id, symbols,
cache_bust)`, which checks this account's Dhan connection:
`portfolio_repo.get_broker_connection(client, user_id,
"Dhan")` — if one exists with an `access_token`,
`load_live_dhan_prices(client_id, access_token, symbols, cache_bust)`
calls `DhanProvider(client_id, access_token).get_quotes(symbols)` (the
marketfeed/ltp endpoint, `NSE_EQ` segment — the same method the main
price pipeline uses when `Settings.market_data_provider=dhan`, just invoked here
with a per-user token instead of the app-wide one). (A second broker,
Zerodha, used to be checked and merged in here too -- `dict.update` with
Zerodha's value winning on a symbol both quoted, an arbitrary tie-break
-- until it was removed entirely, see "Removed: Zerodha" above.)

The result is spread over `ltp_by_symbol` so only symbols Dhan
actually priced get overridden; the `daily_screener_snapshots` value for
every other symbol (no broker connected at all, an expired
session/token, or a symbol Dhan's feed doesn't cover)
survives untouched — the same fallback chain the user asked for: broker
live data first, Yahoo-sourced snapshot as the fallback.
`load_live_dhan_prices` swallows its own
provider's auth/generic errors (`DhanAuthError`/
`ProviderError`) and returns `{}` on any failure — deliberately silent,
since `ltp_by_symbol`'s snapshot fallback already covers it and one bad
quote (or an outage) shouldn't take down the whole tab. It's cached with the same `ttl=60`
convention as `load_latest_prices` right above it;
`load_live_broker_prices` itself is deliberately *not*
`@st.cache_data`-wrapped, so its `get_broker_connection` lookup always
sees the latest saved connection (e.g. right after a Portfolio Refresh
bumps `portfolio_cache_bust`) even though the provider call it delegates
to is cached individually.

**Option-leg LTP already had this broker-first-bhavcopy-fallback shape
before this fix, unchanged here**: `leg["ltp"]` (the option premium
itself, not the underlying) is resolved once at sync time, not on every
My CSP render —
`_sync_dhan`/`_fetch_fallback_option_chains`/`portfolio_service.apply_fallback_option_ltp`
in `src/utils/data_provider_settings.py` (formerly `pages/6_My_Broker.py`,
before that page's deletion) fall back to this app's own F&O bhavcopy data
(`latest_option_chain_view`) for any Dhan position whose Market Quote
call came back empty (commonly a Dhan account without the separate "Data
APIs" subscription). `1D`/`5D`/`20D` are that stock's own `return_1d`/`return_5d`/`return_20d`
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
  **`src/utils/data_provider_settings.py`'s `_default_new_position_trade_dates`**
  (called from `_sync_dhan`, right after
  `replace_broker_positions`; formerly on `pages/6_My_Broker.py` before
  that page's deletion) defaults every just-synced leg with no
  `trade_date` yet to `date.today()` — a real request: without this, a
  brand-new CSP synced today would show a blank Target P&L until the
  user separately remembered to visit the Trade Date form. Looks up
  every leg's current `trade_date` via `portfolio_repo.list_position_meta`
  first and only calls `set_position_trade_date` for a leg that has
  none — never overwrites one already set (whether entered by the user
  or defaulted by an earlier sync). Now wired into the live sync
  flow (`_sync_dhan`) unconditionally -- there's no
  CSV-upload path left to have excluded it from anymore.
- `portfolio_service.csp_target_pnl(max_credit, trade_date, expiry_date,
  as_of=None)` — `max(max_credit * 0.5, min(max_credit * 0.95,
  max_credit * (duration_held / duration_to_expiry) * 1.2))` (`as_of`
  defaults to `date.today()`, explicit for testability, same convention
  `screener_service`/`refresh_service` use). **Three terms, on
  request** — the accelerated term (`* 1.2`) runs 20% faster than plain
  linear, changing every day as `duration_held` grows, so it reads as
  "is today's decay running ahead of or behind a
  slightly-faster-than-linear expectation"; the `0.95 * max_credit` term
  is a hard ceiling the accelerated term will eventually exceed (past
  `duration_held / duration_to_expiry` ≈ 0.79), at which point `min()`
  locks the target at 95% of max credit for the rest of the position's
  life, including well past expiry if it's still open — chasing the
  last 5% isn't worth the assignment/gamma risk of holding to the very
  end. The outer `max(max_credit * 0.5, ...)` is a floor added on
  request ("the target should never be below 50% of max credit") — early
  in a trade the accelerated term starts near 0 and would otherwise let
  the target sink toward nothing; the floor keeps it pinned at half the
  premium collected until the accelerated term itself climbs past that
  point (`duration_held / duration_to_expiry` ≈ 0.417, since `0.417 *
  1.2 ≈ 0.5`). `None` until a Trade Date is entered (nothing to compute
  a duration against), or if `duration_to_expiry` isn't positive (expiry
  on/before the trade date).
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

**My Other Trades (`pages/13_My_Other_Trades.py`)** — unlike My CSP/My
Portfolio Trades, this one adds **no** new analytics; it's a
byte-for-byte copy of `7_My_Trades.py`'s own
`_render_trades_table`/`_render_trades_tab` (same `build_trade_legs` +
`group_into_trades` call, same `Underlying Instrument`/`Trade
Type`/`Legs`/`Total P&L` columns, same "⚠️" `trade_type_mismatch` marker
+ caption, same row-select → `st.switch_page("pages/10_Analyse_Trade.py")`
flow, same Stock Trades/Index Trades/Other Trades bucket split) with
exactly one line added: the `trades` list is filtered to
`is_other_trade_type(trade_type)` (`not is_csp_trade_type(trade_type)
and not is_portfolio_trade_type(trade_type)`) before the bucket split,
so the default `"Trade"` label, a bare Strangle/Jade Lizard/Twisted
Sister/IC with no stock holding, or any other free-text Trade Type all
land here. This was a deliberate scope choice, not an oversight — My
Other Trades' arbitrary multi-leg strategies don't have a single
well-defined breakeven/credit/target shape, so inventing one wasn't
requested; it stays at My Trades' own summary-table depth (per-trade
Total P&L, not per-leg breakeven/target/stop-loss) until a real formula
is asked for.

**My Portfolio Trades (`pages/12_My_Portfolio_Trades.py`, formerly My
CC)** — renamed and rebuilt per an explicit user request: generalizes
from "Covered Call only" to **every** Trade whose `trade_type` starts
with `"Portfolio "` (`is_portfolio_trade_type`) -- Portfolio CC,
Portfolio Strangle, Portfolio Jade Lizard, Portfolio Twisted Sister,
Portfolio IC, or a hand-typed label using the same convention. Unlike
the old design (one row per short-call Position leg -- a Covered Call
only ever has one), this renders **one row per Trade**, since a Trade
here can carry up to 4 option legs at once. Same Stock/Index/Other
bucket split as My Trades.

Six column groups per row, built by `_render_portfolio_trades_table`:
1. **Trade Details** -- `Underlying`/`Trade Type` (same "⚠️"
   `trade_type_mismatch` suffix My Trades uses)/`Total P&L`/`Option
   P&L`. The latter two both come straight off `group_into_trades`'
   own per-trade dict (`total_pnl`/`option_pnl` -- `option_pnl` is a new
   field added specifically for this page: the same sum as `total_pnl`,
   but restricted to Position legs, so the option side's own
   contribution is visible separately from the stock's).
2. **Stock Holding** -- `Stock Avg Price`/`Stock Qty`/`Stock
   Invested`/`Stock LTP` (summed qty, investment-weighted avg price
   across the trade's own Holding leg(s), same aggregation the old My CC
   used) plus `Momentum`/`1D`/`5D`/`20D` (`criterion_b`, the same
   broker-live-first LTP/returns resolution every other Portfolio page
   uses via `load_latest_prices`/`load_live_broker_prices`/
   `load_returns_and_pe`). All blank if the trade has no Holding leg at
   all -- a hand-typed "Portfolio ..." label that doesn't match its own
   legs, the same class of caveat the old page had for a naked short
   call mislabeled "Covered Call".
3-6. **PE Sell Leg / PE Buy Leg / CE Sell Leg / CE Buy Leg** --
   `Strike`/`Expiry`/`Avg Price`/`Qty`/`LTP` for whichever single
   Position leg matches that slot. `_LEG_SLOTS` (a
   `(name, OptionType, is_long)` tuple list) and `_slot_legs` do the
   matching: a leg's `option_type` plus the sign of its `qty` (negative
   = short/sell, positive = long/buy) decide which of the 4 slots it
   goes in; a leg with `option_type is None` (undecoded contract, or a
   futures leg) matches none of them and is simply left out of these 4
   blocks (still counted in Total P&L/Option P&L above). An option leg's
   own `LTP`/`ltp_as_of` come straight from `build_trade_legs`/
   `compute_positions_view`'s already-resolved fields -- no fresh lookup
   needed, unlike the Stock LTP above.

**If more than one leg matches the same slot** (verified against a real
account's own 4 Portfolio-prefixed trades while building this -- none
of them hit this case, since Strangle/CC/Jade Lizard/Twisted Sister
naturally produce at most one leg per slot, but a Jade Lizard/Twisted
Sister with more than 3 legs, or an IC with a same-side 2-long-2-short
shape, theoretically could), only the first leg found is shown in that
slot (`legs_here[0]`) and a table-wide caption flags it
(`has_unshown_legs`), pointing at My Trades/Analyse Trade for the full
leg list -- strike/expiry can't be meaningfully combined into one cell
the way qty/avg price can (an investment-weighted average strike isn't a
real number anyone would trade against), so this is surfaced rather than
silently aggregated or guessed at.

**Dropped entirely, on purpose, in this rebuild**: Trade Date/Target
Option P&L/Stop Loss/Credit (`csp_max_credit`/`csp_target_pnl`/
`csp_stop_loss`) and the whole `portfolio_position_meta`-backed ratchet
this page used to drive for its one short call. That machinery is
CSP/CC-specific (whose target, whose stop, on a 4-leg spread?) and
wasn't part of the explicit spec for this rebuild, so nothing was
invented to replace it — Analyse Trade's own independent per-leg version
of the same math (see above) is completely unaffected and still computes
for any short option leg regardless of what this page shows.

`app.py` groups all five of `7_My_Trades.py`/`11_My_CSP.py`/
`12_My_Portfolio_Trades.py`/`13_My_Other_Trades.py`/`10_Analyse_Trade.py`
(hidden) under one `st.navigation` dict section, `"My Trades"` — see the
"Streamlit app" section above for why the dict form (rather than a flat
`st.Page(...)` list) was needed to get My CSP/My Portfolio Trades/My
Other Trades to render nested under a "My Trades" sidebar header instead
of as top-level, unrelated-looking sidebar entries.

**Trade History (`pages/14_Trade_History.py`)** — Dhan only, and the one
portfolio page that doesn't belong in the `st.navigation` groupings
above: it's registered under `"My Portfolio"` (alongside Holdings/
Positions) in `app.py`, not `"My Trades"`, since it shares neither
`build_trade_legs`/`group_into_trades` nor Stock & Option Data Refresh/
Portfolio Refresh with that family — **a real gap this caught**: the page
was written and fully tested before it occurred that `app.py`'s explicit
`st.Page(...)` registration (this repo doesn't use file-based page
auto-discovery — see the "Streamlit app" section) means a new
`pages/*.py` file is completely unreachable, no error, no 404, just
absent from the sidebar, until it's added there by hand.

Built from `portfolio_trade_fills` (migration
`0038_portfolio_trade_fills.sql`) instead of holdings/positions — one row
per executed fill, synced via Settings' **"Sync Trade History from
Dhan"** button (`data_provider_settings.py`'s
`_render_dhan_trade_history_sync`, calling the new
`DhanProvider.get_trade_history(from_date, to_date)`, which paginates
`GET /v2/trades/{from-date}/{to-date}/{page}` — page starts at `0`, Dhan
returns an empty list once exhausted). Deliberately a separate, explicit
sync from `_sync_dhan`/`sync_broker_portfolio`'s holdings/positions
snapshot, same "read from cache vs. download and persist" decoupling
reasoning as `_render_dhan_instrument_master_refresh` — pulling a
potentially long date range of history is a different cost/cadence than
a fast current-state sync, and a first-time backfill is a one-off,
occasional action (the render function shows an `st.date_input`
defaulting to 365 days ago only on the very first sync; every sync after
that continues automatically from `portfolio_repo.latest_trade_fill_date`
+ 1 day). Logs a new `FetchType.TRADE_HISTORY_SYNC` fetch, needing its
own `provider_fetch_log` CHECK-constraint migration (`0039`, same
drop/recreate pattern as `0033`/`0037`) plus its own narrow-by-value
INSERT policy (same RLS gap as `0034`/`0036` — `provider_fetch_log` has
no `user_id` column to scope by).

`portfolio_repo.upsert_trade_fills` is the one place this whole feature
departs from `replace_broker_holdings`/`replace_broker_positions`'s
delete-then-insert "replace" semantics: fills are **upserted**, keyed on
`(user_id, portfolio_name, broker, exchange_trade_id)` — so historical
fills are never lost just because a later sync's date range doesn't
happen to include them again. This single difference is called out
explicitly in both the table's migration comment and the repo function's
docstring, since it's easy to reflexively copy the holdings/positions
pattern here and silently delete history.

**A real bug in Dhan's own API, found before `dhan_trade_fills_from_api`
was even written** — by design, per its own docstring, the first
implementation step was inspecting one real live page of `/v2/trades`
rather than guessing at the shape. That inspection (2026-08-27, 400 real
fills) found `exchangeTradeId` — documented as the exchange's
unique-per-fill trade id, and this table's originally-intended natural
key — comes back the literal string `"0"` on *every single fill*,
regardless of order/symbol/time. Completely unusable as a key.
`dhan_trade_fills_from_api` builds a synthetic composite instead
(`orderId:exchangeOrderId:exchangeTime:tradedQuantity:tradedPrice`,
stored in the same `exchange_trade_id` column despite not being Dhan's
own field of that name) — stable across re-syncs since none of those
inputs change once a trade has settled, so `upsert_trade_fills`' dedup
still works correctly. The same inspection also settled the two open
questions from the plan: `/v2/trades` *does* carry structured
`drvExpiryDate`/`drvStrikePrice`/`drvOptionType` fields directly, read
the same way `dhan_positions_from_api` does (same
`_DHAN_NO_EXPIRY_SENTINEL`/`_DHAN_OPTION_TYPES`) — but `customSymbol` is
a *different* format from positions/holdings' hyphen-joined
`tradingSymbol` (space-separated and human-readable, e.g. `"GOLD 31 AUG
135000 PUT"` rather than a `"GOLD-31AUG-135000-PUT"`-style string), so
`_dhan_underlying_symbol` does NOT apply here — for a **derivative** fill
the underlying is simply `customSymbol.split()[0]`.

**A second round of live testing (2026-08-31) found the equity/ETF branch
guessed above was wrong** — the first inspection only ever saw options,
so a plain equity/ETF fill's handling was "inferred by analogy," flagged
explicitly as unconfirmed in both the function's docstring and this
guide. It turned out `customSymbol` for a `productType == "CNC"` fill
(stock/ETF/fund) is a free-text *display name*, not a ticker at all:
`"Coal India"`, `"Oil & Natural Gas Corporation"`, `"Nippon Nifty 50 ETF
(NIFTYBEES)"` — and inconsistently formatted (some funds carry no ticker
in parentheses at all, e.g. `"LIC Nifty 10 Year G-Sec ETF"`).
`customSymbol.split()[0]` on these gives nonsense (`"Coal"`, `"Oil"`,
`"Nippon"`) — a real, confirmed-live bug that had already written wrong
`symbol` values into `portfolio_trade_fills` for every one of this
account's 321 CNC rows before it was caught. Fixed by adding a
`symbol_by_security_id: dict[str, str] | None` parameter to
`dhan_trade_fills_from_api` — for a non-derivative fill, the real ticker
is resolved via `securityId` against `dhan_equity_instruments`' own
`trading_symbol` (built by the caller, `_render_dhan_trade_history_sync`,
from `dhan_instrument_repo.get_equity_instruments(client)` — the same
table "Refresh Instrument Master - Dhan" populates), falling back to the
raw, unresolved display name only if that lookup misses (e.g. the
instrument master hasn't been refreshed) — same "still saved and shown,
just unresolved" convention as every other `*_from_api` function.

The same round also found `/v2/trades` uses a **different "no real
expiry" sentinel than `/v2/positions`**: `"1970-01-01"`
(`_DHAN_TRADE_NO_EXPIRY_SENTINEL`), not `"0001-01-01"`
(`_DHAN_NO_EXPIRY_SENTINEL`, positions' own convention). Missing this
meant every stock/ETF/fund fill's `is_derivative` check evaluated `True`
(since `"1970-01-01" != "0001-01-01"`), silently storing a fake
`expiry_date` of 1970-01-01 on every one of them. Both sentinels are now
checked. Also confirmed harmless without any special-casing needed:
`drvOptionType` comes back the literal string `"NA"` (not null) and
`drvStrikePrice` `0.0` (not null) on a non-derivative fill — both already
fall through to `None` via the existing falsy-value/unmapped-dict-key
checks.

**If the equity/ETF display-name bug is ever hit again on an account that
already synced before this fix landed**: the wrong `symbol`/`expiry_date`
values are stuck in already-stored rows — `upsert_trade_fills`' upsert
only overwrites a row when a sync's date range covers it again, and the
incremental (post-first-sync) flow only ever fetches *forward* from the
latest stored date. The fix for already-bad data is a fresh full
backfill: clear `portfolio_trade_fills` (e.g. `delete from
portfolio_trade_fills where broker = 'Dhan';`) and re-run "Sync Trade
History from Dhan" from the first-sync date-picker flow.

`portfolio_service.compute_realized_pnl` is the analytical core — a
pure, thoroughly-tested FIFO lot-matcher (see
`tests/test_portfolio_service.py::TestComputeRealizedPnl`, covering full
close, partial close, multiple-opens-closed-oldest-first, a position
*flip* — a closing fill larger than every open lot opens a new lot in the
opposite direction rather than erroring — and confirming two different
contracts sharing a symbol never cross-match). Grouped by contract
identity (`symbol`, `expiry_date`, `strike_price`, `option_type` — a
plain equity fill has the latter three all `None`, itself a valid,
distinct group key), it emits one dict per closed lot with charges
pro-rated by qty from both the opening and closing fill's own
`taxes_and_charges`/`brokerage` (each converted to a per-unit rate since
one fill can be partially consumed across several separate closes). Any
quantity never closed isn't emitted at all — it's exactly what's already
visible as a current `portfolio_holding`/`portfolio_position` row
elsewhere. Guards against float drift on repeated subtraction with a
`1e-9` epsilon rather than exact-zero comparisons, since real money math
over many partial fills can otherwise leave a lot "open" at
`1e-13` instead of `0`.

`DhanProvider.get_trade_history` also carries a `max_pages=500` safety
cap (raising `ProviderError` if hit, already handled by the sync
button's existing `except ProviderError` branch) — not a documented Dhan
limit, but a guard against the pagination loop never terminating if the
`page` segment were ever silently ignored server-side (every page
echoing the same non-empty batch forever). Added after exactly that
symptom showed up while live-testing the inspection script by hand — the
first attempt looked hung with zero output for a long stretch, which
turned out to just be many real pages (20 fills/page) being walked
silently with no progress indication at all, not an actual infinite
loop; the cap plus per-page logging in the (throwaway,
not-part-of-the-repo) inspection script fixed both the real risk and the
UX problem of a legitimately-slow operation looking identical to a hang.

**Not attempted at all**: linking a closed lot back to My Trades/Analyse
Trade's manual "Trade" grouping (`group_into_trades`/`trade_id`) — that
grouping only ever sees currently-open holdings/positions rows, with no
notion of a closed position, so Realized P&L groups by raw contract
identity instead, independent of any Trade Type label. Revisit only if
that turns out to be insufficient in practice. **Also explicitly out of
scope**: mutual funds. MF investments settle through allotment, not a
real-time exchange trade, so they never appear on `/v2/trades` at all —
covering them would need a completely separate Dhan API this app doesn't
integrate with.

Both of the page's sections render **grouped by underlying symbol**
(requested after the first version shipped as one flat table each) — one
`st.expander` per symbol, sorted alphabetically, each holding its own
mini `st.dataframe`s, `Symbol` dropped from the displayed columns since
it's already the expander's own header. Realized P&L's expander header
shows that symbol's own net P&L and closed-lot count inline (`f"{symbol}
— {format_inr(symbol_net)} ({len} closed lot(s))"`); the net-P&L-by-
symbol bar chart stays above the expanders as a quick visual summary
before drilling into any one symbol.

**Unrealised P&L replaced what used to be a flat "Trade Journal" browse
of every fill** (a second real request after first use of the shipped
page). Deliberately does **not** derive "what's currently held" from
trade fills at all — it reuses the exact same
`load_holdings`/`load_positions` (`src/utils/portfolio_page.py`) +
`portfolio_service.merge_holdings` +
`load_latest_prices`/`load_live_broker_prices` +
`compute_portfolio_view`/`compute_positions_view` calls
`pages/8_My_Holdings.py`/`pages/9_My_Positions.py` already make, combined
into one list of rows tagged `Kind: "Holding"`/`"Position"` and grouped
by symbol the same way Realized P&L is. **Why not derive it from trade
fills, given the page already has them**: a real, unfixable data-source
gap (below) means trade-fill-derived quantity can under-count a real
holding — the *unrealized P&L number itself* has to come from the same
authoritative holdings/positions data every other portfolio page already
trusts, regardless of what trade history does or doesn't cover.

Each symbol's expander also renders **"Trades leading to this
holding"** — `portfolio_service.compute_open_lots(fills)` filtered to
that symbol, sorted oldest-first (build-up order). This is where the
trade-fill data still earns its keep for Unrealised P&L: it shows which
actual fills accumulated into the current position. The section compares
`sum(open lot quantities)` against the actual current quantity from the
summary row(s) above (`_EPS = 1e-6` float tolerance) and shows a caption
when they don't match instead of silently under-reporting.

**A real, unfixable data-source gap, confirmed live**: `GET /v2/trades`
only returns trades that actually executed on an exchange. Shares
**transferred in from another broker** (an off-market DP transfer) never
did — the user's own Dhan account showed several ETF buys in Dhan's
"Transactions" report that aren't marked "Exchange traded transactions"
and, correspondingly, never come back from `get_trade_history` at all. No
change to `dhan_trade_fills_from_api` can fix this — there is nothing to
parse, since Dhan's trade-history API itself never received these. The
qty-mismatch caption above is the intended handling: surface the gap
plainly rather than pretend trade history is complete.

**Instrument column, added to every per-fill/per-lot table on this
page** (Realized P&L's closed-lot table, Unrealised P&L's "trades leading
to this holding" table) once grouping-by-symbol made the old flat
Expiry/Strike/Type columns insufficient to tell rows within one symbol's
expander apart (a "GOLD" group can span many different strikes/
expiries). Plain `raw_name` — Dhan's own descriptive string, already
human-readable for both an option (`"GOLD 31 AUG 135000 PUT"`) and an
equity/ETF (`"Nippon Nifty 50 ETF (NIFTYBEES)"`) — positioned right after
that table's own date/time column (`Exit`/`Entry` respectively). Existing
Expiry/Strike/Type columns stay alongside it, still useful for sorting.

`compute_realized_pnl`/`compute_open_lots` now share a private
`_fifo_walk(fills) -> (closed_lots, open_lots)` engine rather than
duplicating the FIFO matching logic — `compute_realized_pnl` returns just
the closed half (unchanged public behavior, `TestComputeRealizedPnl`
untouched), `compute_open_lots` returns the leftover fragments never
matched to a close, one dict per fragment (NOT aggregated — a symbol can
have several, e.g. two separate still-open buys at different prices),
each carrying that fragment's own `qty` (FIFO's signed convention),
`price`, `traded_at`, and `raw_name`. A closed lot's `raw_name` is the
*closing* fill's own (not the opening one's) — see
`TestComputeRealizedPnl::test_raw_name_on_a_closed_lot_is_the_closing_fills_own`.
`TestComputeOpenLots` mirrors `TestComputeRealizedPnl`'s edge cases
(fully open, partially closed, fully closed via `compute_realized_pnl`
instead, a flip) plus one cross-check: for any given fill sequence,
`sum(closed qty) + sum(open qty)` must always equal the total quantity
involved — nothing should ever be double-counted or dropped between the
two functions.

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

- **`session.py`** — all Supabase Auth + `st.session_state` handling: `sign_in`/`sign_up`/`sign_out`, `request_password_reset`/`verify_recovery_code`/`set_new_password`, `require_login()` (the gate every page calls), `get_user_client_cached()`. **A real bug fixed here**: sessions were getting silently, permanently kicked not long after login. `get_user_client_cached()` calls `supabase_client.get_user_client(access_token, refresh_token)`, whose `auth.set_session(...)` call transparently exchanges an expired access token for a new one via the refresh token — but every page render builds a *brand new* `Client` (there's no client caching across Streamlit reruns despite the function's name), and the refreshed token pair was previously just discarded once that one call returned. Supabase also rotates refresh tokens on every use — the old one is invalidated the instant a new one is issued — so the very next rerun after the access token's ~1 hour expiry retried that same, now-already-consumed refresh token from `st.session_state` and failed. That failure used to be swallowed by a bare `except Exception: pass` inside `get_user_client()` itself, so nothing ever surfaced it: `is_logged_in()` only checks that `sb_access_token`/`sb_user_id` are *present* in session_state, not that they still work, so the page kept rendering as if logged in while every actual Postgres call 401'd — indistinguishable from the app just "disconnecting." Fixed on both ends: `get_user_client()` no longer swallows that exception (it now propagates), and `get_user_client_cached()` wraps the call in its own `try/except` — on success it reads the (possibly just-refreshed) session straight back off the client via `auth.get_session()` and re-saves both tokens into `st.session_state`, so a session now survives as long as the refresh token itself stays valid (Supabase's default is a sliding 30 days) instead of breaking on the first click past the first hour; on failure (the stored refresh token was itself rejected — e.g. already consumed by another tab racing the same session) it calls `sign_out()` and `st.rerun()` so the user cleanly lands back on the login form instead of a wall of silent 401s. `get_user_client()`'s `refresh_token` param is still optional/best-effort only in the sense that a missing refresh token just skips `set_session()` entirely (falls back to whatever raw access token was passed in) — see `tests/test_session.py` for the three behaviors covered (tokens persisted, left alone when `get_session()` returns `None`, sign-out-and-reraise on an invalid refresh token). Note this is purely a code-side fix for the rotation bug; the *initial* access-token lifetime (default 3600s) is a Supabase project setting (Dashboard → Authentication → Sessions), not something this file controls.
- **`formatting.py`** — Indian-numbering-system currency formatting (`format_inr`, lakh/crore grouping), `format_pct`, `direction_arrow`, `pass_fail_badge` (✅ Pass/❌ Fail/N/A, with text), `pass_fail_icon` (✅/❌/—, symbol only — used throughout the Dashboard table's Momentum/Dividend yield/PEG/Fundamentals columns; `pass_fail_badge` is kept for spots that still want the text, e.g. Stock Detail's scorecard). `alert_type_label()`/`summarize_alert_config()` — pure functions turning an `AlertType` + its raw `config` dict into human-readable text (e.g. "Price crosses above ₹1,000.00"), replacing what used to be a literal `f"config={a.config}"` Python-dict dump shown on both Stock Detail and the Alerts screen (now folded into Settings); the exact `config` keys each branch reads (`level`/`direction`, `period`/`direction`, `threshold`/`direction`, `entry_price`, `target_price`/`stop_loss`) must stay in sync with whatever keys the alert-creation forms in `2_Stock_Detail.py`/`4_Settings.py` actually write. **A real bug found here**: `format_inr`/`format_crores`/`format_pct`/`direction_arrow` all checked `value is None`, but `pages/1_Dashboard.py`'s `pd.DataFrame([r.model_dump() for r in rows])` silently converts a Pydantic model's correct `None` into `float('nan')` for any column that has real float values elsewhere in the same column (confirmed directly: a mixed-value column comes back `float64` dtype with `None` cells as `nan`, `nan is None` is `False`) — a genuinely-missing `return_1d` rendered as the literal string `"nan%"` on screen instead of `"—"`. All four formatters now route through a shared `_is_missing(value)` helper that also checks `math.isnan()`.
- **`timezones.py`** — `now_ist()`/`to_ist()`/`format_ist()`, thin wrappers around `pytz`.
- **`refresh_bar.py`** — five independent, targeted refresh renderers, replacing what used to be one bundled **"🔄 Market Data Refresh"** button (`render_global_refresh_bar`/`_run_all`, which fired every fetch concurrently regardless of what the page actually needed). Each renderer fetches independently -- no shared cooldown or thread pool across buttons, since each is now its own single-purpose click:
  - `render_fundamental_and_bhavcopy_refresh(client)` -- Settings only, a new "Data Refresh" `st.subheader`. **Fundamental Data Refresh** calls `edge_refresh.trigger_manual_refresh(token, mode="fundamentals")` (fresh Yahoo fundamentals only, no price/screener writes); **Bhavcopy Refresh** fires `trigger_fo_refresh(token, "NSE")` and `"BSE"` concurrently via a 2-worker `ThreadPoolExecutor` (the one place concurrency is still used, since it's one button covering two independent exchanges).
  - `render_stock_refresh_button(client, user_id, data_provider)` -- every page except Settings; shows exactly one of **Stock Data Refresh** (`data_provider == "yfinance_bhavcopy"`, calls `trigger_manual_refresh(token, mode="price")` -- price/dividends + a screener recompute using carried-forward fundamentals), or, for `data_provider == "dhan"`, three buttons side by side (`_render_dhan_stock_option_refresh_buttons`, `st.columns(3)`):
    - **Stock & Option Data Refresh from Dhan** -- the combined refresh (relabeled "... from Dhan" to disambiguate from the other two, otherwise byte-identical to before): `_refresh_user_live_prices(client, user_id, "Dhan")` loads the account's `broker_connections` row, builds the full watched-symbol universe (Nifty50 constituents ∪ this account's own `portfolio_repo.list_portfolio_symbols` ∪ every tracked ETF), calls `_refresh_dhan_equity_leg`/`_refresh_dhan_fo_leg` sequentially, writes `user_live_prices` (migration `0030`) and `user_live_fo_prices` (migration `0032`).
    - **Stock Data Refresh from Dhan** -- `_refresh_dhan_stock_only`, the equity/ETF leg alone (`_refresh_dhan_equity_leg` via the same `_dhan_equity_etf_universe` helper, a duplicate of the combined function's own universe-building rather than a shared call, so the combined path never has to change to support this). No F&O call at all; its own summary caption never mentions futures/option contracts.
    - **Option Data Refresh from Dhan** -- `_refresh_dhan_option_only`, `_refresh_dhan_fo_leg` alone. No equity/ETF call at all.

    All three write to independent `st.session_state` keys (`_refresh_bar_live_prices`/`_refresh_bar_dhan_stock_only`/`_refresh_bar_dhan_option_only`) and render through their own summary function, so clicking one never clobbers or depends on either of the others.
  - `render_portfolio_refresh_button(client, user_id, data_provider)` -- My Trades/My Holdings/My Positions/My CSP only; no-ops for `yfinance_bhavcopy` (no broker to sync from). Otherwise renders **Portfolio Refresh**, calling `data_provider_settings.sync_broker_portfolio` -- the same `_sync_dhan` Settings' "Save & Sync"/"Update credentials" forms call, now also reachable without visiting Settings. Settings no longer has its own standalone "Sync now" button.

  Every click still ends with a blanket `st.cache_data.clear()` -- not a page-local cache-bust counter -- before `st.rerun()`, same reasoning as before: a refresh triggered from, say, the Options page must also invalidate the Dashboard's cached screener rows. Each button's own `_render_*_summary()` function is stashed in `st.session_state` and rendered on the *next* script run (the "can't render across an `st.rerun()`" pattern this module has always used): `_render_stock_summary`/`_render_fundamentals_summary`/`_render_fo_summary`/`_render_live_prices_summary`. `_universe_breakdown()` is unchanged, still the same "(X stocks, Y ETFs/funds)" message logic. `_last_fetch_caption()` is unchanged too, now called with a `"portfolio_sync"` fetch_type (migration `0033`) for the new Portfolio Refresh caption, in addition to its existing `"price"`/`"fundamentals"`/`"fo"` uses.
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

`fo-refresh/` backs **Bhavcopy Refresh** (Settings only) -- one
Edge Function, not two, selected via the POST body's `exchange` field
(called twice per click, once per exchange, concurrently with the stock
half -- see `refresh_bar.py`'s bullet above) -- same reasoning and runtime as `manual-refresh/`
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
