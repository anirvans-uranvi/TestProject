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

export interface NewCompany {
  symbol: string;
  name: string;
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
  }));
}
