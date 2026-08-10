// Direct TypeScript port of resolve_tracked_symbols in
// src/services/portfolio_service.py -- diffs the distinct symbols
// referenced by every user's uploaded portfolio_holdings against the
// companies already known, returning the minimal companies rows to
// register for the rest. Lives in _shared/ (Supabase Edge Functions
// convention) since manual-refresh calls it as part of building its
// price-refresh symbol universe.
//
// IMPORTANT: this mirrors Python logic (src/services/portfolio_service.py
// :: resolve_tracked_symbols). If you change one, mirror it in the
// other -- same accepted tradeoff as dashboardMetrics.ts /
// calculations.ts (see docs/CODEBASE_GUIDE.md).

// Mirrors src.models.enums.CompanyType (migration 0018) -- the string
// values are written straight into the `company_type` column, which has a
// check constraint on exactly these four values.
export type CompanyType = "Equity" | "ETF" | "Index" | "Fund";

export interface NewCompany {
  symbol: string;
  name: string;
  companyType: CompanyType;
}

export function resolveTrackedSymbols(
  portfolioSymbols: string[],
  knownCompanySymbols: Set<string>,
  rawNameBySymbol: Record<string, string>,
): NewCompany[] {
  const newSymbols = Array.from(new Set(portfolioSymbols))
    .filter((symbol) => !knownCompanySymbols.has(symbol))
    .sort();
  return newSymbols.map((symbol) => ({
    symbol,
    name: rawNameBySymbol[symbol] ?? symbol,
    companyType: "Equity",
  }));
}

// Direct TypeScript port of looks_like_etf_name in
// src/services/portfolio_service.py -- see that function's docstring for
// why this is name-based (yfinance's real longName/shortName) rather
// than yfinance's own `quoteType` field, which is unreliable for
// Indian-listed ETFs (verified live: it returns "EQUITY" for every
// ETF/fund this app tracks). The caller (index.ts) fetches each new
// symbol's real display name via yahoo.ts's fetchDisplayName() and sets
// `companyType` on the NewCompany objects above before upserting -- this
// stays a pure string check, mirroring resolveTrackedSymbols() staying a
// pure, network-free diff.
export function looksLikeEtfName(name: string): boolean {
  return name.toLowerCase().includes("etf");
}
