import { NextResponse } from "next/server";
import { getSupportedSymbols, getTickerPrice, getActiveSymbol } from "@/lib/broker-manager";
import { formatPair } from "@/lib/format-utils";
import { apiError } from '@/lib/api-response';

export async function GET() {
  try {
    const symbols = await getSupportedSymbols();
    const active = getActiveSymbol() || symbols[0] || "XAU_USD";

    const prices: Record<string, any> = {};
    await Promise.all(
      symbols.map(async (sym) => {
        const p = await getTickerPrice(sym).catch(() => 0);
        prices[sym] = {
          symbol: sym,
          display: formatPair(sym),
          price: p,
          change24h: 0,
          high24h: 0,
          low24h: 0,
          volume24h: 0,
          active: sym === active,
          lastUpdate: Date.now(),
        };
      })
    );

    return NextResponse.json({ prices, source: "broker", timestamp: Date.now() });
  } catch (error: any) {
    return apiError('INTERNAL_ERROR', error.message || 'Failed to load prices', 500, {
      prices: {},
      source: 'error',
    });
  }
}
