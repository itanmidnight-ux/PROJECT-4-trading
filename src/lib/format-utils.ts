// ============================================
// RECO-TRADING - Format Utilities
// ============================================
// Formatting helpers for broker market symbols
// ============================================

/** Format broker symbol for display: XAU_USD -> XAU/USD */
export function formatPair(symbol: string): string {
  if (!symbol) return "";
  return symbol.replace("_", "/");
}

/** Unformat display name to broker symbol: XAU/USD -> XAU_USD */
export function unformatPair(display: string): string {
  if (!display) return "";
  return display.replace("/", "_");
}

/** Get display name for symbol */
export function getSymbolDisplayName(symbol: string): string {
  const names: Record<string, string> = {
    XAU_USD: "Gold/USD",
    XAG_USD: "Silver/USD",
    EUR_USD: "EUR/USD",
    GBP_USD: "GBP/USD",
    USD_JPY: "USD/JPY",
    AUD_USD: "AUD/USD",
    USD_CHF: "USD/CHF",
    WTI_USD: "Crude Oil WTI",
    BCO_USD: "Brent Crude",
    US30_USD: "US Wall Street 30",
    SPX500_USD: "US S&P 500",
    NAS100_USD: "US Tech 100",
  };
  return names[symbol] || formatPair(symbol);
}

/** Format price based on symbol */
export function formatSymbolPrice(symbol: string, price: number): string {
  if (!price || price === 0) return "-";

  if (symbol.includes("XAU") || symbol.includes("XAG") || symbol.includes("US30") || symbol.includes("SPX") || symbol.includes("NAS")) {
    return price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  if (symbol.includes("USD") || symbol.includes("JPY")) {
    if (symbol.includes("JPY")) {
      return price.toFixed(3);
    }
    return price.toFixed(5);
  }

  return price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
