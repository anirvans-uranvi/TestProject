# Nifty 50 Momentum & Dividend Screener

A Streamlit + Supabase decision-support dashboard that screens all current
Nifty 50 constituents on Momentum and Fundamentals (dividend yield or PEG),
and classifies each as **Green / Amber / Red / Unavailable**.

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
- [On-demand refresh (the refresh bar)](#on-demand-refresh-the-refresh-bar)
- [Futures & Options (F&O) data](#futures--options-fo-data)
- [Portfolio pages](#portfolio-pages)
- [Docker](#docker)
- [Limitations](#limitations)

## Architecture

```
app.py                  Pure st.navigation() router -- no visible content of its
                        own; see "Navigation" below for the sidebar it builds
pages/                  Streamlit multipage app (each still its own script,
                        registered by app.py rather than auto-discovered)
  1_Dashboard.py         Screener table, metric cards, filters, CSV export -- sidebar label "Screener",
                              nested under the "Market" sidebar section
  2_Stock_Detail.py       Price/volume/dividend charts, scorecard, per-stock alerts -- sidebar label "Equity",
                              nested under "Market"
  4_Settings.py            Per-user thresholds, alert CRUD + notification history, notification channels,
                              Data Provider (Dhan/Zerodha/YFinance+Bhavcopy) + broker sync, sign out
  5_Options.py              F&O: futures term structure, 5% CSP / 5% CC breakdown -- nested under "Market"
  7_My_Trades.py              Holdings + positions grouped by underlying into Stock/Index/Other Trades --
                              sidebar label "All Trades", nested under the "My Trades" sidebar section
  8_My_Holdings.py            Equity holdings, split ETFs & Mutual Funds / Stocks, both with 1D/5D/20D Change --
                              sidebar label "Holdings", nested under the "My Portfolio" sidebar section
  9_My_Positions.py           Per-leg F&O positions, split into Stock Options / Index Options / Others (no
                              grouping -- see My Trades for that) -- sidebar label "Positions", nested under
                              "My Portfolio"
  11_My_CSP.py                Every Trade with Trade Type "CSP" -- one position leg per row, with underlying
                              LTP + 1D/5D/20D change -- sidebar label "CSP", nested under "My Trades"
  12_My_CC.py                 Every short-call leg from a "Covered Call"-tagged Trade, + covered stock's own
                              Holding/Avg Price/LTP and Combined P&L -- sidebar label "CC", nested under "My Trades"
  13_My_Other_Trades.py       Every Trade from My Trades whose Trade Type is neither "CSP" nor "Covered Call" --
                              sidebar label "Other Trades", nested under "My Trades"
  10_Analyse_Trade.py          One Trade's legs -- correct underlying, rename trade type, merge/split
                              (hidden from the sidebar -- reached only via All Trades'/CC's/Other Trades'
                              row selection)
src/
  config.py               Pydantic Settings (env-driven)
  data_providers/         PriceDataProvider / FundamentalsDataProvider + Dhan/mock/manual impls
  models/                 Pydantic domain models
  calculations/           Pure functions: returns, dividend yield, classification, moving averages
  services/                Orchestration: screener, refresh, alerts, market calendar, explanations, F&O
  repositories/            Supabase access layer (one module per table/concern)
  notifications/           NotificationAdapter interface + in-app implementation
  utils/                   Formatting, timezones, Streamlit session/UI helpers
scripts/
  fetch_nifty50_constituents.py   Refresh companies/nifty50_constituents
  seed_mock_data.py                Backfill synthetic prices/fundamentals/dividends/snapshots + mock F&O
  fetch_fo_data.py                  Backfill NSE or BSE F&O bhavcopy (--exchange nse|bse) into Supabase
  cleanup_mock_data.py               Delete leftover source='mock' rows (dry-run by default)
  run_refresh.py                    CLI entrypoint for cron/GitHub Actions/APScheduler
  import_screener_csv.py            Import a screener.in CSV export as PE/PEG/dividend-yield data
supabase/
  migrations/               Schema, RLS policies, views/functions
  seed.sql                   Current Nifty 50 constituents + companies (reference data only)
  functions/manual-refresh/  Edge Function behind Stock/Fundamental Data Refresh (mode param)
  functions/fo-refresh/       Edge Function behind Bhavcopy Refresh (one function, exchange param)
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

**Navigation**: `app.py` builds an explicit `st.navigation({section: [st.Page(...), ...], ...})`
dict and calls `.run()` -- there is no separate "app" home screen; the base
URL runs whichever page is marked `default=True` (currently
`1_Dashboard.py`). This replaced the legacy `pages/`-directory
auto-discovery convention (which derived both the sidebar label and the
display order from each file's name/numeric prefix) so the sidebar label
can differ from the filename (`1_Dashboard.py` shows as "Screener",
`2_Stock_Detail.py` as "Equity") and the order can be set independently
of the numeric prefixes (Settings deliberately listed last). The dict
form (rather than a flat list) groups pages under a labeled sidebar
section header -- Streamlit's only native notion of a "sub-page" -- giving
four sections: **Market** (Screener/Equity/Options), **My Portfolio**
(Holdings/Positions), **My Trades** (All Trades/CSP/CC/Other Trades,
plus the hidden Analyse Trade), and **Settings**. Settings gets a
section of its own single page since the dict form requires every page
to belong to one. Each page still keeps its
own `st.set_page_config()` call for its browser-tab title/icon --
unaffected by which script registers it. The former sign-out control
(previously only reachable from `app.py`'s own sidebar, now that there's
no such screen) moved to `4_Settings.py`'s Account section.

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
  constituent, plus the viewing user's own tracked portfolio symbols) and
  `get_classification_history(symbol, days)` back the Dashboard and Stock
  Detail pages respectively -- see `supabase/migrations/0003_views_functions.sql`
  and the fixes in `0004_fix_constituents_fk_and_view_defaults.sql` (adds the
  `nifty50_constituents -> companies` FK PostgREST needs for embedded
  queries, and defaults `status`/`data_quality` to Unavailable/`{}`
  instead of `NULL` for constituents with no snapshot yet),
  `0006_add_52week_high_low.sql` (adds 52-week high/low columns + the
  matching `criterion_52w_high`/`criterion_52w_low` display flags -- see
  its comments for a real `42P16` error hit while writing it: `create or
  replace view` can only append new columns, never insert them mid-list),
  `0013_screener_fallback_and_portfolio_symbols.sql` (falls back to
  the last snapshot row that actually has a price instead of always
  using today's, and folds in the viewing user's portfolio symbols --
  see [Portfolio pages](#portfolio-pages) for why), and
  `0015_add_is_etf_to_companies.sql` (added `companies.is_etf`, filtered
  out of this same view -- ETFs/funds tracked via a portfolio still
  showed up on this stock-focused screener otherwise) and
  `0018_company_type.sql` (replaced that boolean with a proper
  `company_type` category -- `Equity`/`ETF`/`Index`/`Fund` -- filtering
  the view on `company_type = 'Equity'` instead, so it now also excludes
  the Index rows the same migration seeds; see
  [Portfolio pages](#portfolio-pages) for the classification story).
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
20D returns all strictly > 0% · **C** = PEG <= threshold (default 1.0) ·
**Fundamentals** = A or C (dividend yield clears its threshold, or PEG
clears its threshold, or both). Note the direction flips for C: A and B
pass *above* their threshold (higher yield/returns are the desirable
side), while C passes *at or below* its threshold (a lower PEG is
conventionally the desirable side -- priced reasonably relative to
earnings growth). Exactly 0% return is neutral and fails B;
exactly-at-threshold PEG (e.g. 1.00 at the default threshold) *passes* C,
unlike A which fails at exactly-at-threshold. A criterion whose inputs are
missing evaluates to `None`, never `False`; Fundamentals is itself `None`
unless *both* A and C are known (so a missing PEG or dividend never
silently turns into a Fundamentals fail via the `or`). The overall status
is driven by **B (Momentum) and Fundamentals**, not the raw A/B/C triple
-- rows with either `None` are **Unavailable**, not Red. See
`src/calculations/classification.py` for the exact rules and
`tests/test_calculations_classification.py` for boundary coverage (exactly
0%, exactly-at-threshold, missing-vs-confirmed-zero, staleness).

| Status | Rule |
|---|---|
| Green | Momentum and Fundamentals both pass |
| Amber | exactly one of Momentum, Fundamentals passes |
| Red | neither Momentum nor Fundamentals passes |
| Unavailable | Momentum or Fundamentals has missing inputs, or data is stale beyond the configured threshold |

The Dashboard shows A and C individually as the **Dividend** and **PEG**
columns (each with its own pass/fail tick) plus a combined **Fundamentals**
column (✅/❌) for the OR of the two -- PE ratio is fetched and classified
the same as before but is no longer shown on the Dashboard table itself
(it's a fundamentals input, not one of A/B/C); it's still shown on the
Stock Detail page's fundamentals panel.

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

**52-week high/low (display-only, not part of Green/Amber/Red).** The
Dashboard's **52W High**/**52W Low** columns each show the fetched price
plus a pass/fail tick, using two separate proximity checks that are
deliberately *not* wired into the A/B/C classification engine above --
they don't affect a stock's overall status:

```
criterion_52w_high = latest_price < 0.90 x week_52_high   (pass = comfortably below the high)
criterion_52w_low  = latest_price > 1.10 x week_52_low    (pass = comfortably above the low)
```

Same missing-data rule as A/B/C: if either the price or the 52-week
figure is unavailable, the check evaluates to `None` (shown as `N/A`),
never a fail. See `criterion_52w_high`/`criterion_52w_low` in
`src/calculations/classification.py`.

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
   screener recompute, plus one **8pm IST** job that runs a full stock
   refresh (`--mode=all`) **and** NSE + BSE F&O bhavcopy
   (`scripts/fetch_fo_data.py --days 1`) -- the scheduled counterpart to
   the on-demand refresh buttons below, for every account still on the
   default YFinance + Bhavcopy Data Provider. Needs `SUPABASE_URL`,
   `SUPABASE_SERVICE_ROLE_KEY` (and `DHAN_*` if using the live provider)
   as repo secrets.
2. **APScheduler daemon**: `python scripts/run_refresh.py --mode=all
   --daemon` (also the `scheduler` service in `docker-compose.yml`).
3. **Manual/cron**: `python scripts/run_refresh.py --mode=<intraday|eod|fundamentals|screener|all>`
   from any external scheduler (e.g. Supabase's own pg_cron calling an Edge
   Function that shells out, or a plain crontab).

All three write to `provider_fetch_log` (success/failure, retry count) and
retry transient provider failures with exponential backoff
(`tenacity`, in `src/services/refresh_service.py` and
`src/data_providers/dhan_provider.py`).

## On-demand refresh (the refresh bar)

The scheduled mechanisms above run independently of the Streamlit app.
Five independent, targeted buttons (`src/utils/refresh_bar.py`) replace
what used to be one bundled "🔄 Market Data Refresh" click -- each
visible only where it's relevant, so a user refreshing fundamentals
doesn't also wait on an F&O bhavcopy download, and vice versa. A click
always clears Streamlit's entire cache (`st.cache_data.clear()`) before
rerunning, so every page's own cached loaders pick up the fresh data
regardless of which page triggered the refresh.

| Button | Pages | Visible when | Renderer |
|---|---|---|---|
| **Fundamental Data Refresh** | Settings ("Data Refresh" section) | always | `render_fundamental_and_bhavcopy_refresh` |
| **Bhavcopy Refresh** (NSE + BSE) | Settings, same section | always | `render_fundamental_and_bhavcopy_refresh` |
| **Stock Data Refresh** | every page except Settings | Data Provider = YFinance/Bhavcopy | `render_stock_refresh_button` |
| **Stock & Option Data Refresh** | every page except Settings | Data Provider = Zerodha | `render_stock_refresh_button` |
| **Stock & Option Data Refresh from Dhan** / **Stock Data Refresh from Dhan** / **Option Data Refresh from Dhan** | every page except Settings | Data Provider = Dhan | `render_stock_refresh_button` → `_render_dhan_stock_option_refresh_buttons` |
| **Portfolio Refresh** | My Trades, My Holdings, My Positions, My CSP, My CC, My Other Trades | Data Provider = Dhan/Zerodha | `render_portfolio_refresh_button` |
| **Refresh Instrument Master - Dhan** | Settings ("Data Provider" section) | Data Provider = Dhan | `_render_dhan_instrument_master_refresh` |

- **Fundamental Data Refresh** -- a fresh Yahoo Finance fundamentals
  fetch only (PE, PEG, dividend yield, 52-week high/low), via
  `supabase/functions/manual-refresh/` with `{"mode": "fundamentals"}`.
  No price/screener writes.
- **Stock Data Refresh** -- price history + dividends via the same Edge
  Function with `{"mode": "price"}`, then a screener/Dashboard
  classification recompute using **carried-forward** fundamentals (no
  fresh fundamentals call in this mode) -- mirrors the cron's own
  `--mode=eod` immediately followed by `--mode=screener`.
- **Bhavcopy Refresh** -- NSE + BSE F&O, fired concurrently (2-worker
  `ThreadPoolExecutor`) via `supabase/functions/fo-refresh/` -- one Edge
  Function, parameterized by a POST body `{"exchange": "NSE" | "BSE"}`.
- **Stock & Option Data Refresh** -- refetches this account's connected
  broker's live quote across the full watched-symbol universe (Nifty50
  constituents + this account's own portfolio symbols) and caches it in
  `user_live_prices` (migration `0030`) for Dashboard/Stock Detail to
  read as an override (`src/utils/refresh_bar.py::_refresh_user_live_prices`).
  Zerodha has no F&O instrument-lookup mechanism in this codebase, so a
  Zerodha-provider account only ever sees this one equity-only button.

  **Dhan gets three buttons instead of one** (`_render_dhan_stock_option_refresh_buttons`),
  side by side:
  - **Stock & Option Data Refresh from Dhan** -- the combined refresh
    above (same `_refresh_user_live_prices`, just relabeled to
    disambiguate from the other two), still the only one that widens the
    equity/ETF universe with every tracked ETF and refetches live LTP for
    every futures/option contract this account's own portfolio holds plus
    the exact CSP/CC option legs the Dashboard's 5% CSP/5% CC columns use
    (`_dhan_fo_universe`, `DhanProvider.get_fo_quotes`, migration `0032`)
    -- not the full option chain for every strike/expiry, just what the
    Screener and the account's own positions actually reference.
  - **Stock Data Refresh from Dhan** -- just the equity/ETF leg
    (`_refresh_dhan_stock_only`, reusing `_refresh_dhan_equity_leg`
    directly), no F&O call at all.
  - **Option Data Refresh from Dhan** -- just the F&O leg
    (`_refresh_dhan_option_only`, reusing `_refresh_dhan_fo_leg` directly),
    no equity/ETF call at all.

  All three are independent clicks against the same account -- none of
  them depend on, or invalidate, either of the others.

  **Performance**: Dhan's instrument master (~211,742 rows across every
  exchange/segment, downloaded from `images.dhan.co`) is cached in
  Supabase, shared across every user and process
  (`dhan_equity_instruments`/`dhan_fo_instruments`, migration `0035`).
  This button **only ever reads that cache** (`dhan_provider.py::_load_
  instrument_master`/`_load_fo_instrument_master`) -- it never downloads
  Dhan's CSV itself, however old the cache is. That used to be automatic
  (refreshed on this button's own click whenever the cache "wasn't fresh
  today"), but made the click unpredictably slow depending on which
  process/worker happened to handle it, since a cache-cold click paid for
  a fresh ~211,742-row download inline. Refreshing that cache is now a
  separate, explicit step: Settings' **"Refresh Instrument Master -
  Dhan"** button (`dhan_provider.py::refresh_dhan_instrument_master`,
  shown only when Data Provider is Dhan -- see [Connecting a
  broker](#connecting-a-broker-settings--data-provider) above), which
  downloads both the equity and F&O slices from **one** shared raw
  download (`_get_raw_instrument_master`) and persists both. If the cache
  has never been populated at all, Stock & Option Data Refresh fails with
  a clear message pointing at that button instead of silently downloading
  or resolving nothing.

  The equity and F&O legs (`_refresh_dhan_equity_leg`/`_refresh_dhan_fo_leg`)
  run **sequentially**, not concurrently -- this was tried as a
  `ThreadPoolExecutor` and reverted after two rounds of real, silent
  breakage: sharing one raw DataFrame between two threads corrupted the
  filtered result for one or both (pandas is not thread-safe for
  concurrent reads of a single shared instance -- confirmed live, a real
  account's resolution dropped from 61/61 equities + 333+/351 F&O to
  15/61 + 0/348), and even after fixing that with a per-leg `.copy()`,
  both legs also make calls through this function's *own* shared
  Supabase `client` (`snapshot_repo.upsert_*`, `_dhan_fo_universe`, and
  the instrument-master loaders' own DB reads/writes) -- confirmed live
  as `httpx.RemoteProtocolError`, since that client's connection isn't
  safe for two threads to use at once either. Every Dhan API call is
  already serialized by a lock regardless of threading (`_throttle`'s
  global rate gate), so sequential execution here gives up only the
  overlap of the two legs' own network *wait* time -- a small trade for
  not corrupting Supabase connections.

  Going sequential above only serializes one *call*'s own two legs --
  it doesn't stop two different users (or two tabs) independently
  clicking "Refresh Instrument Master - Dhan" at once, both racing to
  repopulate `dhan_equity_instruments`/`dhan_fo_instruments`, the shared
  table migration `0035` is for. `dhan_instrument_repo`'s replace
  functions used to `delete()` then `insert()` the fresh rows -- two
  overlapping callers' inserts could land the same `security_id` (the
  primary key) twice, confirmed live as `duplicate key value violates
  unique constraint "dhan_equity_instruments_pkey"` surfacing through the
  Stock & Option Data Refresh button (back when that button could trigger
  this download itself). Fixed by upserting (`on_conflict="security_id"`)
  instead of inserting -- a colliding row from a racing writer now just
  overwrites instead of erroring, since both writers downloaded the same
  Dhan CSV and would write identical data for that ID anyway.

  **A separate, more consequential bug hit resolution itself, not just a
  crash**: `dhan_instrument_repo.get_equity_instruments`/`get_fo_instruments`
  -- read once a `provider_fetch_log` entry already exists for today --
  used a plain, unpaginated `.select().execute()`, hitting the same
  "PostgREST caps a response at 1000 rows" limit already fixed once
  before for the Dashboard's F&O queries (see "Futures & Options" below).
  A 9,854-row `dhan_equity_instruments` table only ever returned its
  first 1,000 rows through this path -- RELIANCE/TCS/HDFCBANK/SBIN and
  most of the Nifty50 sorted past that page and resolved as "not found."
  This only bit a *second* (or later) refresh of the day from a
  *different* process -- the first, cache-cold refresh builds its data
  straight from the Dhan download and never round-trips through this
  query -- which is why it looked like intermittent Dhan-side flakiness
  (58/61 symbols resolving one click, 3/61 minutes later) rather than a
  deterministic truncation bug during live debugging. Fixed by paginating
  both reads (`_paginate`, the same helper/fix `fo_repo` already uses).

  **A third, narrower bug only hit SENSEX/BANKEX** -- the only F&O legs
  allowed to resolve on BSE instead of NSE (stock legs are NSE-only,
  migration `0031`). `DhanProvider.get_fo_quotes` resolved their
  `security_id` correctly but then queried *every* resolved contract
  under one hardcoded `"NSE_FNO"` segment; Dhan's LTP endpoint silently
  returns nothing for a security_id queried under the wrong exchange
  segment, indistinguishable from "Dhan has no matching contract" in the
  refresh summary's missing-contracts caption (confirmed live: 2 of 254
  F&O contracts stuck, both SENSEX strikes). Fixed by having
  `_download_fo_master` keep each row's own exchange (a new column,
  persisted via migration `0037`) and having `get_fo_quotes` split
  resolved security_ids into `NSE_FNO`/`BSE_FNO` lists by it, instead of
  one hardcoded list.
- **Portfolio Refresh** -- re-syncs holdings/positions from the
  connected broker (`src/utils/data_provider_settings.py::sync_broker_portfolio`,
  wrapping the same `_sync_dhan`/`_sync_zerodha` Settings' "Save & Sync"/
  "Update credentials" forms use -- Settings itself no longer has a
  standalone "Sync now" button).

The Fundamental/Stock/Bhavcopy buttons are each implemented as a
**Supabase Edge Function** rather than in Streamlit page code -- a real
fetch-and-write needs the Supabase service-role key (bypasses RLS),
which must never live in Streamlit page code since Streamlit Cloud runs
that code in every logged-in user's own browser session. Each Edge
Function holds the key safely as a Supabase-injected environment
variable (runs server-side inside Supabase's infrastructure); Streamlit
only ever sends the *calling user's own* access token
(`src/services/edge_refresh.py`), never any secret. Stock & Option Data
Refresh and Portfolio Refresh run entirely in Streamlit's own process,
using this account's already-saved `broker_connections` credentials.

It reimplements price/dividend/fundamentals fetching (via Yahoo Finance,
unofficial endpoints, see [Limitations](#limitations)) and the
return/classification math **in TypeScript**
(`supabase/functions/manual-refresh/calculations.ts`,
`yahoo.ts`) -- a deliberate, explicitly-accepted tradeoff to get a truly
instant on-demand refresh with full feature parity to
`run_refresh.py --mode=all`, at the cost of duplicating business logic in
a second language. If you change a rule in `src/calculations/`, mirror it
in `calculations.ts` too; run `deno test
supabase/functions/manual-refresh/calculations.test.ts` to check the port
still matches the documented boundary cases.

A 5-minute cooldown (tracked via `provider_fetch_log`, `provider_name =
'manual_edge'`) applies across all users, to keep repeated clicks from
rate-limiting the whole project's access to Yahoo's endpoints.

As its last step, this function also recomputes the Dashboard's
`dashboard_fo_metrics` cache (a TypeScript port of the same calculation
`fo_service.py` uses, in `supabase/functions/_shared/dashboardMetrics.ts`
-- see [Futures & Options data](#futures--options-fo-data)) so a changed
spot price shows up in the 5% CSP / 5% CC columns immediately,
without waiting for the next scheduled recompute.

**Deploying the Edge Function** (one-time setup, requires the Supabase
CLI -- Edge Functions are genuinely easier to develop/deploy with proper
tooling than via the Dashboard's editor, unlike the SQL migrations
earlier in this README):

```bash
npm install -g supabase   # or: scoop install supabase
# no npm/scoop? download the CLI binary directly from
# https://github.com/supabase/cli/releases (a windows_amd64.zip asset)
supabase login
supabase link --project-ref <your-project-ref>
supabase functions deploy manual-refresh
```

`supabase login` opens an interactive browser OAuth flow -- if you're
running these commands somewhere that can't complete that (a headless
shell, an agent session), generate a personal access token instead from
https://supabase.com/dashboard/account/tokens and export it as
`SUPABASE_ACCESS_TOKEN` before running `link`/`functions deploy`; the CLI
picks it up automatically and skips the browser flow entirely. Treat that
token as a credential -- revoke it from the same page once you're done
deploying.

`SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `SUPABASE_SERVICE_ROLE_KEY` are
automatically available to the function at runtime -- Supabase injects
them into every Edge Function's environment, no manual secret-setting
needed. No changes are required on the Streamlit side beyond having
`SUPABASE_URL` set (already required for everything else) -- the
function's URL is derived from it.

### F&O data refresh (`supabase/functions/fo-refresh/`)

"Bhavcopy Refresh" calls this Edge Function **twice concurrently** --
once with a POST body of `{"exchange": "NSE"}`, once with `{"exchange":
"BSE"}` -- both against the *same* Edge Function, `supabase/functions/fo-refresh/`
(`src/services/edge_refresh.py::trigger_fo_refresh(access_token, exchange)`;
omitting the body defaults to `"NSE"`, so any old caller keeps working).
Each checks whether that exchange has published a newer F&O bhavcopy than
what's already in Supabase for it (the greater of `max(trade_date)` in
`futures_daily_prices` and `option_daily_prices`, scoped by a `source`
prefix -- see below for why both tables are checked, not just futures)
and, only if so, downloads + ingests that one day -- so clicking either
when nothing new is available is cheap (a handful of HTTP requests, no
writes) and returns "Already up to date" instead of silently doing
nothing or re-fetching data you already have.

Deploy it the same way as `manual-refresh` (same CLI, same one-time
`login`/`link` setup):

```bash
supabase functions deploy fo-refresh
```

It reimplements the bhavcopy download + parse **in TypeScript**
(`supabase/functions/fo-refresh/bhavcopy.ts`) -- the same
duplicated-business-logic tradeoff `manual-refresh` already accepts, for
the same reason (a truly instant on-demand path). NSE serves a zip;
Deno's Edge Runtime has no zip library built in and pulling a third-party
one felt like overkill for a single-entry archive, so it reads the ZIP
directly via the Central Directory record and the Web Streams API's
native `DecompressionStream("deflate-raw")` -- no external dependency.
**BSE serves a plain CSV, no zip step at all** -- confirmed live, see
`src/data_providers/bse_fo_provider.py`'s file header. Both exchanges
verified against real, live bhavcopy data (not just synthetic test
fixtures) before this was considered done; run `deno test
supabase/functions/fo-refresh/bhavcopy.test.ts` to check it.

**BSE ingestion is index-options-only.** NSE is the sole source for every
stock future/option (`STF`/`STO`) -- BSE's own stock-level F&O liquidity
is negligible in practice, so those rows are dropped even though BSE's
bhavcopy carries them for many of the same underlyings NSE does. BSE is
only ever used for index options (`IDO`) that genuinely trade there --
SENSEX, BANKEX. This is a per-exchange allow-list, not a universe filter:
`src/data_providers/bse_fo_provider.py`'s `_FUTURES_TYPES`/`_OPTION_TYPES`
and `bhavcopy.ts`'s `futuresTypesFor`/`optionTypesFor` narrow to `set()` /
`{"IDO"}` for BSE specifically, vs NSE's `{"STF"}` / `{"STO", "IDO"}`.

Same 5-minute cooldown as `manual-refresh`, but scoped **per exchange** --
`provider_fetch_log`, `provider_name = 'fo_edge_nse'` or `'fo_edge_bse'`
(renamed from the pre-multi-exchange `'fo_edge'`), `fetch_type = 'fo'`
(added to the allowed `fetch_type` values by migration
`0008_add_fo_fetch_type.sql`) -- so an NSE refresh and a BSE refresh never
block each other. The "already loaded" watermark is scoped the same way,
by a `source` prefix (`nse_fo_bhavcopy%`/`bse_fo_bhavcopy%`, covering both
this Edge Function's own rows and `scripts/fetch_fo_data.py`'s cron/backfill
rows for that exchange) -- **a real bug this fixed**: an exchange-agnostic
watermark (comparing the newest `trade_date` across *any* source) would
make a same-day BSE refresh think it's already up to date the moment an
NSE refresh ran, since both exchanges publish on the same trading days.

**A second real bug, caused by the index-options-only restriction above**:
the watermark originally only checked `futures_daily_prices`. Once BSE
stopped writing futures rows entirely, that query froze on BSE's last
pre-restriction futures date forever -- every later BSE refresh kept
reaching the exchange and "succeeding," but the comparison itself never
advanced, so a manual click could report cooldown against a stale timestamp
and the Dashboard's "Latest BSE Bhavcopy" (`fo_repo.get_latest_fo_trade_date`,
same bug) stuck on that date even after new index-option rows landed in
`option_daily_prices`. Fixed by checking both tables and taking the newer
date, everywhere this watermark is read.

**A third real bug**: a BSE refresh could fail outright with `"Could not
reach BSE: BSE did not return a bhavcopy CSV for <today>..."` even when
BSE's actual data (a few days back) was perfectly reachable. The walk-back
(`findLatestAvailableBhavcopy`) always starts from *today*, and BSE
redirects a request for today's not-yet-published file to its own
homepage (HTTP 200, `text/html`) instead of 404ing like NSE does for the
same case -- `fetchBhavcopyText` correctly throws a diagnostic error for
that one day, but the walk-back used to let the throw propagate
immediately instead of trying the previous day, aborting before it ever
reached the genuinely available earlier bhavcopy. Fixed by having the
walk-back catch a single day's error and keep going, only re-throwing once
every day in the lookback window has failed -- see `bhavcopy.ts`'s and
`src/data_providers/bse_fo_provider.py`'s docstrings for
`findLatestAvailableBhavcopy`/`latest_available_bhavcopy`.

Also recomputes `dashboard_fo_metrics` as its last step whenever it
actually ingests a newer bhavcopy (skipped on the "already up to date"
no-op path, since nothing changed) -- same reasoning and shared
TypeScript module as `manual-refresh` above.

## Futures & Options (F&O) data

The **Options** page (`pages/5_Options.py`) shows, per stock, the futures
term structure and a full calculation breakdown for the Dashboard's two
options-derived screener columns — **5% CSP** and **5% CC** — showing the
actual strikes, premiums, and trade dates used, not just the final
percentage -- both **5% CSP** and **5% CC** are shown as a near/next/far
month table (CC's table also carries "Net Investment" and "Assignment
Profit" columns the Dashboard doesn't). Open it by selecting a row in the
Dashboard's table and clicking "Open in Options", or via the "View F&O /
options" button on Stock Detail.

**5% CC** is a covered-call yield: sell 1 lot of the OTM call whose
strike is the *lowest one still at or above* 5% above spot (not merely
whichever strike is nearest to that line, which could round down below
it). Net Investment = last traded price of the stock − premium collected
(the real capital outlay after the premium reduces it); `cc_pct` =
premium ÷ last traded price × 100 (the yield on the stock's own price);
and Assignment Profit = (strike ÷ Net Investment − 1) × 100 (the total
return of the whole covered-call trade if the shares are called away at
the strike). This replaced an earlier, simpler formula (nearest-strike
match with no "must actually be ≥5% OTM" filter, and Assignment Profit =
premium ÷ (strike − spot) × 100) on request, which itself had replaced an
even earlier, more complex "5% ITM PMCC" (poor-man's-covered-call, three
option legs). The table originally only showed the nearest expiry's
numbers before being extended to all three months to match 5% CSP's
table.

If you hold this stock in one of your own saved portfolios, a third
**"Portfolio CC"** table appears below 5% CC (silently absent otherwise)
-- a per-holding covered-call suggestion, and (since My Holdings' own CC
ROI/CC Assignment ROI columns were removed by request) the only place in
the app showing this figure. For each holding, the strike targeted
depends on whether the position is under water: if your average buy
price is above the current LTP, it targets ~3% above your average buy
price; otherwise (at or above breakeven) it targets ~5% above LTP --
picking whichever listed strike is nearest that target (not floor-
filtered like 5% CC above). **CC ROI** is the ROI when the call expires
OTM -- the premium from writing 1 lot of that call, as a percentage of
your full position's investment. **CC Assignment ROI** is the ROI if the
stock gets called away: the total return if the whole position were
closed out at that strike (plus that lot's premium), relative to your
original cost basis. Both are measured against the stock's total
invested amount, not just the margin, on the assumption that the stock
is pledged and the pledge is used as margin; both show "N/A" for an
expiry with no listed strike near the target. One table per named
portfolio that holds it, each showing its own qty/avg price plus
Strike/Premium/Trade Date/Invested Amount/CC ROI/CC Assignment ROI for
the near, next, and far monthly expiries.

The Dashboard's own **5% CSP** / **5% CC** columns read from a small
precomputed cache table (`dashboard_fo_metrics`, migration `0011`, keyed
by `(symbol, expiry_date)` -- up to 3 rows per symbol, near/next/far)
instead of recalculating across every open option contract on every page
load -- every refresh path (the cron script, `fetch_fo_data.py`, and
Stock Data Refresh/Bhavcopy Refresh below) recomputes all 3 months as its last step, so
it's never more than one refresh out of date. An **"Options month"
dropdown** lets you pick which of the 3 cached months feeds those two
columns -- purely a re-render over already-cached rows, no new fetch --
shown as `"Aug 2026 (25-Aug-26)"`, the full date alongside the month, not
just the month (see below for why). The dropdown's choices are
restricted to expiries that belong to a symbol actually shown in the
screener below (Nifty 50 stocks).

**Stock option data is always NSE-only, never BSE -- a hard, database-
level guarantee (migration `0031_stock_options_nse_only.sql`),
irrespective of which Data Provider an account has selected** (F&O is
never provider-branched at all -- see
[Connecting a broker](#connecting-a-broker-settings--data-provider)
above). BSE's own F&O feed is index-options only by design (SENSEX/
BANKEX, ...) -- `bse_fo_provider.py` already blocks any new stock-type
row from being ingested -- but pre-restriction rows for real stock
symbols (an old BSE monthly expiry a few days off that symbol's real NSE
one) used to linger indefinitely, since `fo_repo.refresh_open_flags`
only checks expiry-date-vs-today, not source, so a stale row just kept
getting silently revived to `is_open = true` forever. **This bug
recurred twice**: an earlier fix only excluded BSE-sourced legs from the
Dashboard's own cache computation (`fo_service.dashboard_metrics_rows`),
which wasn't enough -- every *other* reader of the shared
`latest_option_chain_view` (My CSP's fallback LTP, Analyse Trade, the
Options page, `fo_repo.get_option_chain`) still saw the same stale
garbage unfiltered. Migration `0031` fixes it at the root: a one-time
cleanup deletes any surviving BSE-sourced option data for a non-Index
symbol, and the same guard is now baked directly into
`latest_option_chain_view` itself, so no future consumer -- or ingestion
bug -- can resurface a stock's BSE-derived row again. Showing the full
date (not just the month) in every expiry dropdown, mentioned above, was
the other half of the fix -- two *different* dates landing in the same
calendar month used to render as visually-indistinguishable duplicate
entries, which is exactly how this bug was first noticed and how it
would've stayed hidden even after being fixed. See
`docs/CODEBASE_GUIDE.md`'s Futures & Options section ("Dashboard cache")
for the full pipeline.

**Data source:** the NSE F&O UDiFF **bhavcopy** (one zip per trading day),
the only reliable free source for NSE derivatives — yfinance has none, and
NSE's live option-chain API returns empty JSON to scripts. Load it with:

```bash
python scripts/fetch_fo_data.py            # backfill last 60 trading days
python scripts/fetch_fo_data.py --days 20  # fewer days
python scripts/fetch_fo_data.py --date 2026-07-16   # one specific day
python scripts/fetch_fo_data.py --mock     # synthetic data, no network
```

Requires `SUPABASE_SERVICE_ROLE_KEY` (writes shared market data, bypasses
RLS), and migrations `0007` and `0011` applied first (`0011` adds the
`dashboard_fo_metrics` cache table this script recomputes at the end of
each run). `scripts/seed_mock_data.py` also seeds ~30 days of synthetic
F&O so the Options screen works locally with no network. This is
**end-of-day** data (NSE publishes the file ~6pm IST after close); re-run
the script daily (or via the same schedulers as the cash data) to keep it
current — see [Limitations](#limitations). A large `--days N` backfill
only recalculates which contracts count as expired (`is_open`) once, at
the very end -- if the run dies partway through (a transient network
error, say), re-run it (or just call `fo_repo.refresh_open_flags`
directly) rather than assuming the already-ingested days are somehow
broken; otherwise already-expired contracts from those days can be left
showing as open on the Options screen. See `docs/CODEBASE_GUIDE.md`'s
Futures & Options section for the full incident this caused.

Day-to-day, once the initial backfill is done, the **📊 NSE/BSE F&O Data
Refresh** buttons (see [On-demand refresh](#on-demand-refresh-the-refresh-bar)
above) are the easier way to pick up each new trading day's bhavcopy --
no terminal/service-role key needed, and each is a no-op if nothing new is
published yet.

## Portfolio pages

Your own holdings and F&O positions -- synced live from a connected
broker (see [Connecting a broker](#connecting-a-broker-settings--data-provider)
above), not the Nifty50 screener universe -- span seven pages, six of
which appear in the sidebar: **My Trades** (`pages/7_My_Trades.py`,
sidebar label "All Trades"), **My CSP** (sidebar label "CSP"), **My CC**
(sidebar label "CC"), **My Other Trades** (sidebar label "Other
Trades") -- all four nested under the "My Trades" sidebar section --
plus **My Holdings** (sidebar label "Holdings") and **My Positions**
(sidebar label "Positions"), nested under "My Portfolio". This doc keeps
referring to each page by its file-derived feature name (matching its
filename and its own on-page title), not its current sidebar label,
which is independent and can be renamed without touching the page
itself -- see the "Navigation" note above and the `st.navigation` dict
in `app.py`. **Analyse Trade** is reached only by selecting a row on
All Trades/CC/Other Trades. All seven share one loader/formatting
module, `src/utils/portfolio_page.py`
(cached data loaders, the cache-bust counter, `build_trade_legs`), so a
cache hit on one page is a cache hit on another -- e.g. switching from My
Holdings to My Trades doesn't re-fetch holdings that are still fresh.
Every page keeps the same "one tab per portfolio" structure it always
has, but since a live broker sync now targets exactly one resolved
portfolio per account (see above), in practice there's just the one tab
-- a pre-existing account with multiple portfolio_names from before this
change still shows all of them, just with no way to add another
live-synced one.

**My CC** and **My Other Trades** are filtered views of the exact same
Trade list My Trades computes (`portfolio_service.group_into_trades`) --
`is_covered_call_trade_type`/`is_other_trade_type` split it by `trade_type`
the same way My CSP's `is_csp_trade_type` already did. My CC then renders
a per-leg breakdown like My CSP's, plus the covered stock's own Holding/
Avg Price/LTP/Combined P&L alongside the option leg (see My CC below);
My Other Trades stays at My Trades' own per-trade summary depth (that
richer per-leg breakdown doesn't generalize to an arbitrary multi-leg
strategy, see My Other Trades below) and keeps the same Stock/Index/Other
bucket tables My Trades uses. A Trade lands on My CC only once its Trade
Type is renamed to "Covered Call" on Analyse Trade (or it was
auto-classified as one, see "Auto-classifying a new Trade" below), and on
My Other Trades by default (including the untouched "Trade" label,
and any auto-classified type other than CSP/Covered Call -- see "Trade
Type is auto-classified for a new trade" under My Trades below).

### Connecting a broker (Settings > Data Provider)

There's no upload page anymore -- CSV import was dropped entirely once a
live broker sync became viable as the account's one data source. Instead,
Settings has a **"Data Provider"** section
(`src/utils/data_provider_settings.py`) with one dropdown:
**Dhan**, **Zerodha**, or **YFinance + NSE/BSE Bhavcopy** (the default).
This choice is **account-wide**, not per-portfolio -- a `broker_connections`
row is now keyed `(user_id, broker)` (migration
`0029_broker_connections_account_wide.sql`, collapsed from the original
per-portfolio design), and it governs two things at once:

1. **Stock LTP everywhere it's shown** (Dashboard, Stock Detail, and the
   portfolio pages below) -- Dhan/Zerodha means a live broker quote,
   cached per-account in `user_live_prices` (migration `0030`) by the
   **Stock & Option Data Refresh** button (see [On-demand refresh](#on-demand-refresh-the-refresh-bar)
   below) and read as an override over the shared, possibly-stale
   `daily_screener_snapshots` value. **Fundamentals (PEG, dividend
   yield) and the full F&O options chain (every strike/expiry on the
   Options page) are never provider-branched** -- neither Dhan nor
   Zerodha's API exposes that data, so those always stay yfinance/NSE+BSE-
   bhavcopy-sourced regardless of this setting. **Dhan only** (migration
   `0032`), Stock & Option Data Refresh separately live-prices the
   *specific* F&O contracts that actually matter to this account: every
   futures/option position it holds, and the Dashboard's own cached 5%
   CSP/5% CC legs -- see [On-demand refresh](#on-demand-refresh-the-refresh-bar)
   below.
2. **Where your holdings/positions come from.** Picking Dhan or Zerodha
   reveals a credential form and a "Sync now" button right there in
   Settings; picking the default shows nothing further to connect.
   Sync always targets **one portfolio per account**
   (`portfolio_repo.get_or_default_portfolio_name` -- reuses whatever
   single portfolio_name your holdings/positions already share, or
   `"My Portfolio"` for a brand-new account with nothing saved yet; there's
   no portfolio-name picker anymore).

**Dhan**: paste a Client ID and Access Token (generate one on
`web.dhan.co` -> Profile -> "DhanHQ Trading APIs"; valid for 24 hours),
"Save & Sync" pulls holdings + positions straight from Dhan's API (`GET
/v2/holdings`, `GET /v2/positions`, `POST /v2/marketfeed/ltp` for
position LTPs) via `src/data_providers/dhan_provider.py`
(`portfolio_service.dhan_holdings_from_api`/`dhan_positions_from_api` --
symbol/expiry/strike/type come from Dhan's own structured fields
`tradingSymbol`/`drvExpiryDate`/`drvStrikePrice`/`drvOptionType`, no
name-matching or regex decoding needed). Since the token expires every 24
hours, syncing is always a manual click -- Settings warns once a saved
token is more than ~23 hours old. Fetching live LTP for positions needs
Dhan's separate "Data APIs" subscription (distinct from "Trading APIs");
without it (or for any security Dhan's own feed omits),
`portfolio_service.apply_fallback_option_ltp` fills the gap from this
app's own F&O data (`option_daily_prices` via `latest_option_chain_view`)
for any symbol/expiry/strike this app tracks -- the previous trading
day's close, not a live tick, but still enough to show P&L instead of
N/A. Covers NIFTY/BANKNIFTY (NSE) and SENSEX/BANKEX (BSE) index options
alike; a *stock* option position only ever falls back to NSE's chain,
never BSE's (BSE is index-options-only). **A fallback LTP is flagged, not
silent**: `portfolio_positions.ltp_as_of` (migration `0026`) is set to
that chain row's own trade date whenever the fallback fires, `None` for a
live quote -- My CSP shows `"(as of <date>)"` next to a fallback LTP so
it's never mistaken for a live one. **Security trade-off:** the access
token can also place trades (Dhan has no read-only scope for individual
accounts), stored as entered, protected only by the same row-level
security every other per-user table here relies on -- not separately
encrypted. This app's own code only ever calls the read-only endpoints
above. "Disconnect" removes the saved credentials only; previously synced
holdings/positions are left as-is.

Right below the connect form, a separate **"Refresh Instrument Master -
Dhan"** button (`_render_dhan_instrument_master_refresh`) appears
whenever Data Provider is Dhan -- needs no connection/token of its own,
since Dhan's instrument master is public reference data. This is the
*only* thing that downloads and persists `dhan_equity_instruments`/
`dhan_fo_instruments` (migration `0035`); Stock & Option Data Refresh
only ever reads whatever this button last fetched, however old (see
[On-demand refresh](#on-demand-refresh-the-refresh-bar) above for why
that was deliberately decoupled).

**Zerodha** (`src/data_providers/zerodha_provider.py`) works through a
genuinely different mechanism -- Kite Connect is a paid, app-based
platform, not a self-service token page:

1. **One-time setup, on Zerodha's own site**: register a Kite Connect app
   at developers.kite.trade (**₹2,000+GST/month subscription, billed by
   Zerodha, separate from this project**), which gives you an **API Key**
   and **API Secret**. Set that app's **Redirect URL** to this app's
   Settings page -- `{your app's base URL}/Settings` (the exact
   `/Settings` path is pinned in `app.py`). **If you had Zerodha connected
   before this change, update the Redirect URL** -- it used to point at
   `/My_Broker`, which no longer exists.
2. In Settings, pick "Zerodha", enter that API Key + API Secret once
   ("Save").
3. Click **"Log in to Zerodha"** -- opens Zerodha's own login page in a
   **new browser tab** (standard OAuth-style redirect; this app never
   sees your password or TOTP). After logging in, it redirects back to
   Settings with a one-time `request_token`, which completes the login
   and immediately syncs -- no extra confirmation click, since (unlike
   the old per-portfolio design) there's no portfolio to pick anymore.
4. From then on, "Sync now" pulls holdings + positions straight from
   Kite Connect (`GET /portfolio/holdings`, `GET /portfolio/positions`),
   translated via `portfolio_service.zerodha_holdings_from_api`/
   `zerodha_positions_from_api`. Zerodha's own `tradingsymbol` for an
   F&O position is in the exact same format
   `parse_zerodha_option_instrument` was originally built to decode from
   a CSV positions export's `Instrument` column, so that decoder is
   reused as-is (weekly index options: e.g. `NIFTY2681123000PE`; monthly:
   e.g. `NIFTY26AUG23100PE`/`SBIN25AUG970PE`, expiry computed as that
   month's last Thursday -- doesn't account for an exchange holiday
   shifting that day earlier). Kite's responses also include `last_price`
   directly, so unlike Dhan there's no separate LTP call or fallback step
   needed.

**Kite Connect's session expires at a fixed daily time (~6am IST the next
day), not on a rolling 24-hour window like Dhan's** -- there's no way
around logging in again through step 3 every trading day you want to
sync. Settings detects this (comparing the saved session's start time
against the most recent 6am IST boundary, not a simple hours-old check)
and shows "Log in to Zerodha" again instead of "Sync now" once that
boundary has passed. **Security trade-off, same model as Dhan's:** the
API Secret and access token are stored as entered, protected only by
`broker_connections`' RLS policy. "Disconnect" removes the saved
credentials only; previously synced holdings/positions are left as-is.

Both holdings and positions are saved per-user (`portfolio_holdings`,
migrations `0012`/`0014`; `portfolio_positions`, migration `0016`; manual
Trade groupings/metadata, `portfolio_trade_groups` and
`portfolio_trade_meta`, migrations `0020`/`0021` -- see My Trades below).
A fresh sync fully replaces that broker's previously saved rows of each
type (`replace_broker_holdings`/`replace_broker_positions`); nothing is
merged in from a prior sync.

### My Holdings (`pages/8_My_Holdings.py`)

Equity holdings valued against a live quote from whichever broker(s) this
portfolio has connected, falling back to the app's own
`daily_screener_snapshots` data for any symbol no connected broker prices
(same preference My CSP's LTP Underlying uses) -- same stock/contract
held across multiple brokers within one portfolio is combined into one
row for display (`portfolio_service.merge_holdings`), split into two
tables by `companies.company_type` (migration `0018`):
**ETFs & Mutual Funds** (`ETF`/`Fund`) first, then **Stocks**
(`Equity`/`Index`, and any still-unresolved holding, since there's no
better signal for those). **Both tables share the exact same columns**
-- Stock, Qty, Avg Price, LTP, Investment, Cur Val, P&L, P&L%, 1D Change,
5D Change, 20D Change; the Total Investment/Cur Val/P&L/P&L% stat grid
above both tables still aggregates across everything. 1D/5D/20D Change is
the holding's rupee value change over that period, `%` in parentheses --
e.g. `₹+588.24 (+2.00%)` -- sourced from `daily_screener_snapshots` via
`snapshot_repo.get_latest_returns_and_pe` (`return_1d`/`return_5d`/`return_20d`,
the same fields Dashboard/Stock Detail already read for the Nifty50
universe) -- that table stores only *percentage* returns, not historical
closes, so the rupee change is derived via
`src/calculations/returns.py::value_change_from_pct(cur_val, return_pct)`,
the algebraic inverse of `pct_return`.

**No CC ROI / CC Assignment ROI columns or "Covered call expiry"
dropdown here** -- both were removed by request once the Stocks table
was made to match the ETFs & Mutual Funds table's columns exactly. The
same per-holding covered-call figure still exists, just relocated: see
"Portfolio CC" under the Options page (`pages/5_Options.py`) below, which
computes it live for one stock at a time from your own holdings, with no
dependency on a page-wide expiry selector.

### My Positions (`pages/9_My_Positions.py`)

Open F&O positions synced from your connected broker, one row per leg, no
grouping (see My Trades below for that) -- split into three tables per
portfolio tab: **Stock Options**, **Index Options**, and **Others**.
`portfolio_service.classify_position_bucket` decides which: a position
whose instrument string decoded into an actual option contract sorts by
its underlying's `company_type` (`Index` only -> Index Options,
everything else, including ETF/Fund -> Stock Options); everything that *isn't* a decoded
option -- an undecoded F&O row, a futures position, or a stock/ETF bought
or sold as a *position* rather than a holding -- lands in Others instead.
Stock Options and Index Options share the same columns: Instrument (the
broker's raw contract string), Underlying, Expiry, Strike, Type, Qty
(signed -- negative is short), Avg Price, P&L, P&L%. Others has just
Instrument, Qty, Avg Price, P&L, P&L% -- no expiry/strike/type/underlying,
since none of those apply. Unlike holdings, the LTP behind each P&L here
is whatever the broker sync resolved at sync time, not re-fetched live on
every render (not shown as its own column, only used to compute
P&L/P&L%). For a Dhan-synced position missing LTP (see
[Connecting a broker](#connecting-a-broker-settings--data-provider)
above), `portfolio_service.apply_fallback_option_ltp` fills the gap from
this app's own F&O data -- which now includes index options (NIFTY,
BANKNIFTY via NSE; SENSEX, BANKEX via BSE -- migrations `0018`/`0019`),
not just stock options. BSE is index-options-only, though (see the F&O
data refresh section above) -- a *stock* option position always falls
back to NSE's own chain, never BSE's. Index *futures* remain out of scope
on both exchanges. P&L/P&L% are still recomputed from qty/avg price/LTP
rather than trusted from the broker's own P&L figure, since Zerodha's and
Dhan's own P&L% columns turned out to mean different things (Dhan's is
direction-aware, Zerodha's is a raw price change) -- see
`portfolio_service.compute_positions_view`. A position whose instrument
string doesn't decode is still saved and shown here -- in the Others
table, with no expiry/strike/type.

**LTP only comes from data already loaded in Supabase** -- never a fresh
live fetch triggered by this page. The app's `companies`/
`daily_screener_snapshots` tables normally only cover the 50 Nifty
constituents, so ETFs, gilt/liquid funds, and non-Nifty50 stocks show
"N/A" for LTP/Cur Val/P&L/P&L% right after upload. Once a symbol is
resolved (matched automatically or entered manually), all four refresh
paths -- `scripts/run_refresh.py`, the `manual-refresh` Edge Function
(equity), and `scripts/fetch_fo_data.py`/the `fo-refresh` Edge Function
(futures & options) -- start tracking it: each reads the distinct
symbols across every user's `portfolio_holdings`, registers a minimal
`companies` row for any not already known, and folds them into the same
price/fundamentals/screener (or F&O bhavcopy) fetch every Nifty50 symbol
already gets. So a portfolio stock with listed derivatives (like
Hindustan Zinc or IndusInd Bank) gets both its equity LTP *and* its
futures/options chain populated, purely from being uploaded and resolved
-- no separate step needed. `nifty50_constituents` is never touched by
this, so portfolio-only symbols never become an official constituent --
Settings' Alerts section still reads from `companies_repo.list_current_constituents`
(a plain `nifty50_constituents` query), so its "Applies to" symbol list
stays Nifty50-only. The Dashboard, Stock Detail, and Options all widen
their own symbol universe with the *viewing* user's own portfolio
symbols: the Dashboard's `latest_screener_view` (migration `0013`) does
it via `auth.uid()` at the SQL level, while Stock Detail and Options
each union `companies_repo.list_current_constituents`/`fo_repo.list_fo_symbols`
with `portfolio_repo.list_portfolio_symbols(client, user_id)` in Python
-- so a portfolio-only stock (an ETF, or a non-Nifty50 stock like
Hindustan Zinc or IndusInd Bank) becomes selectable on all three pages
the moment it's tracked, and "Total stocks" grows accordingly (never
hardcoded to 50). Options gracefully shows "No open F&O contracts"
instead of a blank/missing entry for a portfolio symbol with no listed
derivatives (most ETFs). Selecting a holding on My Holdings' own
table (see below) and opening it in Stock Detail or Options follows
directly from this: any resolved holding is, by construction, one of the
viewing user's own portfolio symbols, so it's always selectable on both.
In short: upload → save → click "Manual refresh" (or wait for the next
cron run) → real LTP appears, and the symbol becomes viewable everywhere
except the Settings page's alert "Applies to" list.

**Except the Dashboard's screener list itself doesn't show ETFs/funds**
(migration `0018`'s `companies.company_type`, replacing the boolean
`is_etf` from `0015`) -- a momentum/dividend/PEG stock screener doesn't
make much sense for a fund (those criteria are all meaningless for one),
so real ETFs/funds (NIFTYBEES, GILT5YBEES, LIQUIDCASE, LTGILTCASE) are
excluded from this one list specifically; Stock Detail, Options, and My
Holdings still show them fine. `company_type` is one of `Equity`
(default), `ETF`, `Index`, or `Fund` (reserved, no rows yet); the
screener view only ever shows `Equity` rows, so `Index` (NIFTY,
BANKNIFTY, SENSEX -- seeded by `0018` so Dhan-synced index option
positions and this app's F&O ingestion have a `companies` row to
reference) is excluded the same way ETFs are, with no separate flag
needed. ETF classification happens automatically the first time a new
symbol is tracked, via its real display name (yfinance's `longName`) --
not yfinance's own `quoteType` field, which is unreliable for
Indian-listed ETFs (it returns `"EQUITY"` for every one of these). See
`docs/CODEBASE_GUIDE.md`'s Futures & Options section for the full story.

`0013` also fixed a related bug: the view previously always used
*today's* snapshot row per symbol even when today's price fetch failed,
showing a blank "--" instead of the last known price. It now prefers the
most recent row that actually has a price, falling back across days --
the same resilience `get_latest_prices` (used by My Holdings/My Trades)
already had. The Dashboard (`pages/1_Dashboard.py`) flags exactly which
rows are showing a fallback price: it compares each row's
`snapshot_date` to the newest `snapshot_date` seen anywhere in that
load's batch, and any row falling short gets a " (as of <date>)" suffix
appended to its LTP cell -- shown only when a fallback price is actually
being displayed, never for a symbol with no price at all.

Both My Holdings tables' column headers are clickable/sortable, same as
the Options screen's Futures table -- click a row in either one to select
it (the checkbox on the left) and two buttons appear below that table:
"Open in Stock Detail" and "Open in Options" for that stock.

### My Trades (`pages/7_My_Trades.py`) + Analyse Trade (`pages/10_Analyse_Trade.py`)

My Trades groups holdings *and* F&O positions sharing an underlying into
one **Trade**, unlike My Holdings/My Positions, which each show their own
un-grouped rows. Every leg (a holding or a position row, *unmerged* --
per-broker, same natural identity as `portfolio_trade_groups`' key, not
My Holdings' cross-broker `merge_holdings`) is assigned a `trade_id`
(`portfolio_service.assign_trade_ids`, unchanged from before -- it only
touches symbol/raw_name/broker, so it works identically for holding legs
and position legs) and grouped into three tables:

- **Stock Trades** -- every leg whose underlying resolves to
  `company_type = Equity` (or an unresolved/unknown symbol, same fallback
  My Holdings' old ETF/Stocks split used).
- **Index Trades** -- every leg whose underlying resolves to
  `company_type = Index` only (NIFTY, BANKNIFTY, FINNIFTY, ...).
- **Other Trades** -- everything that doesn't cleanly belong in the other
  two: a leg with no resolved symbol at all (an undecoded F&O contract,
  or an unmatched holding); a `company_type = ETF`/`Fund` leg
  (BANKBEES/GILT5YBEES/GOLDBEES/LIQUIDCASE-style holdings -- neither a
  real equity trade nor a genuine index, on a live request after this
  first defaulted to Stock Trades); or a *manually merged* Trade whose
  legs' underlyings don't all agree on one bucket.

**Manually pin a Trade's table.** If a Trade's auto-computed table isn't
what you want -- e.g. an index ETF you'd rather see alongside Index
Trades despite its `company_type` being `ETF` -- Analyse Trade's "Table
(on My Trades)" dropdown overrides it: "Auto" (the default) follows the
rule above; picking "Stock Trades"/"Index Trades"/"Other Trades" pins it
there regardless of the underlying's own classification. Saved to
`portfolio_trade_meta.bucket_override` (migration `0024`) alongside the
underlying-label/trade-type overrides -- `None` means "no override, use
Auto".

Each table shows **Underlying Instrument**, **Trade Type** (defaults to
"Trade", auto-classified for a new trade -- see below), **Legs**, and
**Total P&L** (summed over the trade's own priced legs). A Holding leg's own LTP feeding that P&L (and the legs table's LTP
column below) prefers a live quote from whichever broker(s) this
portfolio has connected, same as My CSP's LTP Underlying -- falling back
to `daily_screener_snapshots` only for a symbol no connected broker
prices (`portfolio_page.build_trade_legs`, shared by both this page and
Analyse Trade). A Position leg's own LTP is unrelated to this -- it's
already resolved once at sync time (live broker quote, or this app's own
F&O bhavcopy as fallback -- see My Positions above), not recomputed here.
Select a row and click "Analyse Trade" to open that Trade's detail
page (`st.session_state["analyse_trade_id"]`/`["analyse_trade_portfolio"]`
+ `st.switch_page` -- Analyse Trade is registered in `app.py` with
`st.Page(..., visibility="hidden")`, so it's reachable this way but never
appears as its own sidebar link). There you can:

- See every leg in the Trade, one row per leg (Holding legs included),
  in the same columns as My CSP: Trade Date/Underlying/Expiry/Strike/
  Qty/Avg Price/Credit/LTP/P&L/Target P&L/Stop Loss/Breakeven/LTP
  Underlying/Momentum/1D/5D/20D. Unlike My CSP (which only ever shows
  CSP-tagged Position legs), this page has to work for *any* Trade
  shape, so the CSP-specific columns only populate where they're
  actually meaningful: `Credit`/`Target P&L`/`Stop Loss` compute for any
  **short** option leg (a short call included -- these three formulas
  were never really put-specific, see My CC), but `Breakeven` is
  specifically the textbook CSP breakeven (`Strike - Avg Price`) and only
  shows for a genuine short **put** leg -- a Holding, a future, a long
  option, or a short call all show "—" for it. `LTP Underlying`/
  `Momentum`/`1D`/`5D`/`20D` apply to every leg with a resolved symbol
  regardless of leg type, since they're facts about the underlying, not
  the leg's own instrument. Row order still matches `trade_legs` exactly
  (no filtering), so the merge/split row-selection below keeps working
  unchanged.
- **Correct the underlying, set the Trade Date, or rename the Trade
  Type** -- one form. Underlying Instrument and Trade Type are free
  text, not constrained to a known symbol or a fixed list -- e.g.
  correcting a resolved "Tata Motors" to the real post-demerger
  underlying "Tata Motors Passenger Vehicle", or renaming "Trade" to
  something meaningful like "Covered Call" or "Aug Iron Condor". Saved
  to `portfolio_trade_meta` (migration `0021`), keyed by
  `(portfolio_name, trade_id)` -- a different grain from
  `portfolio_trade_groups`' per-*leg* key, since a label/type applies to
  the whole Trade, not one leg.

  **Trade Type is auto-classified for a new trade, on Portfolio
  Refresh.** `portfolio_service.classify_trade_type` reads a trade's legs
  (option type, buy/sell direction, whether a stock holding is present)
  and detects **CSP**, **Covered Call**, **Strangle**, **Jade Lizard**, or
  **Twisted Sister** -- e.g. one short put with no holding -> CSP; a
  holding plus one short call -> Covered Call; a short put + short call
  (same direction, both short or both long) -> Strangle; 3+ legs with
  exactly one bought leg (a bought call -> Jade Lizard, a bought put ->
  Twisted Sister) -> that. Only ever runs for a trade with **no** saved
  Trade Type yet -- an already-classified trade's own label is never
  overwritten automatically. If that trade's current legs later stop
  matching its saved type (a leg closed, a new one appeared), My Trades
  marks it with a "⚠️" next to the Trade Type and Analyse Trade shows a
  warning naming what it currently looks like instead -- a flag to review,
  not an auto-correction. Leaving the underlying field unchanged
  from its computed default doesn't write an override -- only an actual
  correction is stored. **Trade Date** (shown only when the Trade has at
  least one Position leg -- a pure-Holding Trade has nothing to date)
  sits right after Underlying Instrument, and on Save is applied to
  *every* Position leg in the Trade at once (multi-leg Trades like a
  strangle are near-always entered as one package on one day) --
  writes `portfolio_position_meta.trade_date` the same way My CSP's own
  "Set Trade Date" form used to before this moved here, so it applies to
  any Trade you're analysing, not just ones already tagged "CSP"; My
  CSP's Target P&L reads it back for CSP-tagged Trades specifically. See
  My CSP below for what it's used for and the "defaults to today on
  sync" behavior.
- **Merge** other Trades into this one (multiselect of this portfolio's
  other Trade IDs), or **split** selected legs out of this Trade -- either
  back to their own default per-underlying Trade, or into a brand-new
  named Trade ID. Both actions reassign the affected legs'
  `(broker, raw_name) -> trade_id` mapping via
  `portfolio_repo.set_trade_group`/`clear_trade_group_overrides`, the
  same `portfolio_trade_groups` mechanism (migration `0020`) this app
  already used for F&O-only Trade grouping before My Trades existed --
  now applied uniformly to holdings and positions alike.

Both `portfolio_trade_groups` and `portfolio_trade_meta` are keyed by
natural identity (a leg's own `(portfolio_name, broker, raw_name)`, or a
Trade's own `(portfolio_name, trade_id)`) rather than any database row id
-- deliberate, since `replace_broker_holdings`/`replace_broker_positions`
fully delete and reinsert a broker's rows on every upload/sync, so a
grouping/label keyed to an ephemeral row id would be wiped on the very
next refresh. Two related caveats worth knowing: (1) if a broker ever
changes how it formats `raw_name` for the same contract (or the
portfolio/broker pair is later synced from a different source), a
previously grouped leg's override simply stops matching anything and
silently falls back to its default per-underlying Trade -- nothing
breaks, but the grouping needs to be redone for that leg; (2) if a Trade
is renamed via merge (its `trade_id` changes to the target Trade's id),
any `portfolio_trade_meta` row for the *old* `trade_id` is left behind
unused -- no automatic carry-forward, since the target Trade already has
its own (possibly already-customized) label/type and silently overwriting
it would be more surprising than a clean "re-enter it if you still want
it".

### My CSP (`pages/11_My_CSP.py`)

Every open F&O position leg from a Trade whose **Trade Type is "CSP"**
(case-insensitive, whitespace-trimmed -- `portfolio_service.is_csp_trade_type`)
-- there's no separate "mark this as a Cash Secured Put" control; you tag
it the same way you'd rename any Trade Type on Analyse Trade (see My
Trades above), just using the exact word "CSP". A Trade with no
position legs (e.g. accidentally renamed on a pure-holding Trade) simply
contributes nothing here -- Holding legs have no expiry/strike to show
and are silently skipped. Columns, left to right:

- **Trade Date** -- when the position was actually entered, first
  column since everything else on the row can depend on it. No broker
  export or API this app talks to reliably carries this for an
  already-open position, so it's entered manually: go to My Trades,
  select the trade, click "Analyse Trade", and set it there (folded
  into the same form as the underlying/trade-type edits) -- see Analyse
  Trade above. **Defaults to today automatically** the first time a "Sync now" click
  (Settings' Data Provider section) brings in
  a position leg that has no Trade Date yet, so Target P&L never sits
  stuck at N/A just because nobody's visited the form -- change it
  anytime afterward if the real entry date was earlier; an already-set
  date (whether you entered it or a prior sync defaulted it) is never
  overwritten by a later sync.
- **Underlying**, **Expiry**, **Strike**, **Qty** (signed -- negative is
  short), **Avg Price** -- the same fields My Positions shows for an
  option leg.
- **Max Credit** -- `Avg Price * |Qty|`, the total premium collected for
  the leg (what Target P&L and Stop Loss are both expressed as a
  fraction of).
- **LTP** -- shows `"(as of <date>)"` next to the price when it came
  from this app's own end-of-day F&O data rather than a live broker
  quote (`portfolio_positions.ltp_as_of`, migration `0026`) -- most
  commonly a Dhan sync whose Market Quote call failed (e.g. the account
  lacks the separate "Data APIs" subscription), which otherwise
  silently shows a stale close with nothing distinguishing it from a
  live tick.
- **P&L** -- the leg's own P&L with its P&L% in parentheses, e.g.
  `"₹1,234.56 (+12.34%)"` (a combined value+percentage cell, same shape
  as Breakeven below -- there's no separate P&L% column). Prefixed with
  **✅** once P&L has cleared Target P&L, or **❌** once it's fallen
  through Stop Loss -- no marker at all when neither threshold is
  crossed (including whenever Target P&L/Stop Loss themselves aren't
  computable yet, e.g. no Trade Date).
- **Target P&L** -- `max(Max Credit * 0.5, min(Max Credit * 0.95, Max
  Credit * (Duration Held / Duration to Expiry) * 1.2))`, where
  `Duration to Expiry = Expiry - Trade Date`, and `Duration Held = Today
  - Trade Date`, shown with what % of Max Credit it represents in
  parentheses, e.g. `"₹4,275.00 (85.00%)"`. Changes every day as
  `Duration Held` grows -- the `* 1.2` runs the target 20% faster than
  plain linear, reflecting that theta decay tends to accelerate as
  expiry nears (a "higher than average decay" expectation, not a
  straight-line one) -- but it's capped so it never crosses 95% of Max
  Credit no matter how long the position is held, even well past expiry
  (chasing the last 5% isn't worth the assignment/gamma risk of holding
  to the very end). It's also floored at 50% of Max Credit, so early in
  a trade (before the time-decay term catches up) the target never sinks
  below half the premium collected. A rule-of-thumb gauge, not a precise
  pricing model. Blank until Trade Date is set (no duration to compute
  against).
- **Stop Loss** -- ratchets up automatically as the position becomes
  more profitable, and is saved on every visit so it never resets: a
  brand-new leg (nothing saved yet) starts at `-Max Credit` (willing to
  give back the full premium collected before stopping out); once P&L%
  clears 25% the stop moves up to at least breakeven (₹0); once P&L%
  clears 50% it moves up to at least half the max credit locked in.
  Never moves down -- if P&L% later drops back below a threshold it just
  crossed, the stop stays where it already ratcheted to. Doesn't need a
  Trade Date (Max Credit and P&L% are enough).
- **Breakeven** -- the CSP breakeven price (`Strike - Avg Price`, the
  premium collected) followed by how far that sits from the underlying's
  current price in parentheses (`(Breakeven / LTP Underlying - 1)` as a
  %), e.g. `"₹23,455.00 (-2.27%)"` -- negative means breakeven sits
  below the current price (cushion: the underlying would need to fall
  that far before the position loses money past the premium collected),
  positive means it's already fallen through breakeven.
- **LTP Underlying** -- the underlying stock's own current price. If this
  account has a connected broker (Dhan and/or Zerodha, Settings' "Data
  Provider" section) -- a live quote straight from that broker is used;
  otherwise (or for any symbol no connected broker returns a live quote
  for, e.g. an expired token) falls back to `daily_screener_snapshots` --
  the same source My Holdings' Current Value already reads, only as
  fresh as the last Stock Data Refresh (commonly yfinance, ~15-20min
  delayed).
- **Momentum** -- the exact same "Momentum" criterion (B) the
  Dashboard's screener classifies every stock on
  (`src.calculations.classification.criterion_b`: 1D, 5D, AND 20D
  returns all positive), shown with the same ✅/❌/— icon
  (`src.utils.formatting.pass_fail_icon`) the Dashboard uses, so this
  always agrees with what the Dashboard would show for the same
  underlying.
- **1D/5D/20D** -- that stock's own 1-day/5-day/20-day percentage
  return, the same `return_1d`/`return_5d`/`return_20d` fields My
  Holdings' "1D/5D/20D Change" columns are derived from -- shown here as
  a plain percentage, not converted to a rupee amount, since there's no
  "value" of the underlying itself to apply it to.

Trade Date/Target P&L/Stop Loss are saved to a new table,
`portfolio_position_meta` (migration `0025`), keyed by the leg's natural
identity `(portfolio_name, broker, raw_name)`. Like the other Portfolio
pages, one tab per portfolio; a portfolio with no "CSP"-tagged Trades
shows a plain caption rather than an empty table.

### My CC (`pages/12_My_CC.py`)

Every Trade whose Trade Type is exactly "Covered Call"
(`is_covered_call_trade_type`, case-insensitive/trimmed) -- one row per
short-**call** position leg, same Stock/Index/Other bucket split as My
Trades. The option leg's own columns mirror My CSP: **CC Expiry**
(labeled to disambiguate from any expiry the covered stock itself might
imply -- a stock has none, but the label makes clear this column is the
*call's* expiry)/**Strike**/**Qty**/**Avg Price**/**LTP**/**Momentum**/
**1D/5D/20D**, and the same **Trade Date**/**Stop Loss** mechanics
(`csp_max_credit`/`csp_target_pnl`/`csp_stop_loss`) -- relabeled
**Credit** (was "Max Credit" on My CSP), **Option P&L** (was "P&L"), and
**Target Option P&L** (was "Target P&L") to disambiguate from the new
stock-side numbers below -- none of these three are actually put-specific
despite the `csp_` name; they're just premium-collected/time-decay-target/
ratcheting-stop math, equally valid for a short call.

A Covered Call's economics can't be judged from the option alone, so
each row also carries the covered stock's own numbers, read from that
trade's own Holding leg(s) (summed across lots/brokers if the same
underlying is split more than one way within a trade):

- **Holding** -- number of shares of the underlying held in this trade.
- **Avg Stock Price** -- the stock's own investment-weighted average buy
  price (placed right after **Underlying**, before the option's own
  **Avg Price**).
- **Stock LTP** -- the stock's own current price (same broker-live-first,
  screener-snapshot-fallback resolution as My Holdings' Current Value).
- **Stock P&L** -- `(Stock LTP - Avg Stock Price) × Holding`, the stock's
  own P&L on its own, as a % of its own investment (`Holding × Avg Stock
  Price`) -- a ✅ once it clears **Target Stock P&L**.
- **Target Stock P&L** -- a flat 5% of the stock's own investment
  (`Holding × Avg Stock Price`). No percentage shown alongside it (unlike
  every other Target column here) since it's always exactly 5% of that
  same investment by definition -- restating it would be redundant.
- **Combined P&L** -- Stock P&L plus the option leg's own P&L, shown as a
  % of the same stock investment. "—" unless both the stock and the
  option leg are priced.

All stock-side columns show "—"/blank if the trade has no Holding leg to
read (e.g. a naked short call mislabeled "Covered Call").

### My Other Trades (`pages/13_My_Other_Trades.py`)

Unlike My CSP/My CC, this one doesn't add any per-leg options analytics
(no Breakeven/Target P&L/Stop Loss ratchet -- that machinery assumes a
single well-defined option leg, which an arbitrary multi-leg strategy
doesn't have). It's just My Trades' own Stock/Index/Other Trades tables
(`portfolio_service.group_into_trades`, same **Underlying Instrument**/
**Trade Type**/**Legs**/**Total P&L** columns, same "⚠️" mismatch marker,
same row-select → "Analyse Trade" flow), pre-filtered to
`is_other_trade_type(trade_type)` -- i.e. neither "CSP" nor "Covered
Call" -- before the bucket split, so the untouched default "Trade"
label, a Strangle, a Jade Lizard/Twisted Sister, or any other free-text
Trade Type all land here.

Together with My CSP and My CC, every Trade shown on My Trades appears
on exactly one of these three filtered pages (a Trade's `trade_type` can
only match one of `is_csp_trade_type`/`is_covered_call_trade_type`/
`is_other_trade_type` at a time) -- My Trades itself is unfiltered and
keeps showing all of them.

## Docker

```bash
docker compose up app          # Streamlit app only
docker compose up               # + the APScheduler refresh daemon
```

## Limitations

- **The app's custom-rendered HTML (screener table, status icons, stat
  cards, alert badges) depends on a Tailwind CSS CDN link at runtime**
  (`unpkg.com/tailwindcss@2.2.19`, loaded by `inject_tailwind()` in
  `src/utils/ui.py`). Streamlit's own native widgets (buttons, inputs,
  forms, sidebar, tabs) can't be styled by an external CSS framework at
  all -- those are instead reskinned by a separate, self-contained global
  `<style>` override (`inject_global_styles()`, no CDN dependency) that
  ships with the app and doesn't depend on any network request, so
  buttons/inputs/forms/sidebar keep their design-system styling even if
  the Tailwind CDN is unreachable (offline, a restrictive corporate
  firewall, etc.) -- only the Tailwind-classed custom HTML (the screener
  table's mobile card layout, stat cards, badges) would fall back to
  unstyled markup in that case, and everything still renders and is fully
  readable/functional either way. See `docs/CODEBASE_GUIDE.md`'s "design
  system" section for why a `<link>` tag is used for Tailwind instead of
  its more common CDN `<script>`, and how the two styling mechanisms
  divide responsibility.
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
  providers, run `python scripts/cleanup_mock_data.py` (dry run -- prints
  counts of `source = 'mock'` rows in `price_history`,
  `fundamental_snapshots`, and `dividend_events`, deletes nothing) then
  `python scripts/cleanup_mock_data.py --confirm` to actually delete them,
  followed by `run_refresh.py --mode=screener` to recompute
  `daily_screener_snapshots` from the now-clean inputs.
- **`yfinance` is an unofficial Yahoo Finance client, not a licensed
  feed.** It wraps Yahoo's internal JSON API rather than scraping HTML,
  and is a stable, widely-used library, but Yahoo's terms restrict
  automated commercial use and Yahoo has rate-limited/blocked yfinance
  traffic in the past. It's a reasonable default for personal/analytical
  use (which is what was requested here); replace it with Dhan (prices)
  plus a licensed fundamentals vendor before relying on this for a
  commercial product.
- **The manual-refresh Edge Function's fundamentals fetch depends on an
  undocumented Yahoo "crumb" + cookie handshake, more fragile than the
  Python `yfinance` path above.** Its price/dividend endpoint needs no
  auth (verified directly), but its fundamentals endpoint
  (`quoteSummary`) started requiring a session cookie plus a crumb token
  fetched via a separate request -- reimplemented by hand in
  `supabase/functions/manual-refresh/yahoo.ts` since there's no Deno
  equivalent of `yfinance` to manage this automatically. Yahoo can change
  or remove this handshake at any time with no notice; if the Edge
  Function's fundamentals step starts failing for every symbol, this is
  the first thing to check (re-verify the flow with `curl` the same way
  it was confirmed originally -- see `docs/CODEBASE_GUIDE.md`).
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
- **F&O data is end-of-day only, and greeks/implied volatility are not
  stored.** The Options screen is built on the NSE F&O bhavcopy (published
  ~6pm IST after close), the only reliable free NSE-derivatives source:
  yfinance has none, and NSE's live option-chain API returns empty JSON to
  scripts. So "latest price" for a contract is the last trading day's
  close/settlement, not a live/intraday quote, and history builds forward
  from the first `fetch_fo_data.py` run (backfill limited to what NSE's
  archive still serves). Greeks and IV are **not** in the bhavcopy (or any
  free source) and were intentionally left out -- the option tables are
  shaped to gain those columns later (via a Black-Scholes helper) without a
  migration reshape. Index *options* are ingested alongside the 50 equity
  underlyings -- NIFTY/BANKNIFTY via NSE (migration `0018`), SENSEX/BANKEX
  via BSE (migrations `0018`/`0019`); index *futures* (IDF bhavcopy rows)
  stay out of scope on both exchanges. **BSE is index-options-only** --
  its stock-level F&O (`STF`/`STO`) is deliberately never ingested (BSE's
  own liquidity there is negligible), so NSE remains the sole source for
  every *stock* future/option; a stock symbol's F&O rows only ever come
  from NSE, regardless of whether that stock also trades derivatives on
  BSE.
