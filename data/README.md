# Manual fundamentals data

`manual_fundamentals.csv` and `manual_dividends.csv` are templates (headers
only). They back `ManualFundamentalsProvider` (`FUNDAMENTALS_PROVIDER=manual`),
the stopgap fundamentals source used because Dhan and no other licensed
vendor in scope exposes PE / PEG / dividend data via API.

Populate them by hand (e.g. from NSE corporate filings, exchange
disclosures, or a fundamentals vendor you separately license) with rows
matching:

```
manual_fundamentals.csv: symbol,as_of_date,pe_ratio,peg_ratio,eps,market_cap
manual_dividends.csv:    symbol,ex_date,amount_per_share,dividend_type
```

Leave `pe_ratio`/`peg_ratio` blank (not `0`) when unknown -- the app treats
blank as missing data, not a failed screening criterion. Rows older than
120 days are automatically flagged stale.

For local development without curated data, set `FUNDAMENTALS_PROVIDER=mock`
instead and run `python scripts/seed_mock_data.py` to generate synthetic
but structurally realistic fundamentals and dividend history.
