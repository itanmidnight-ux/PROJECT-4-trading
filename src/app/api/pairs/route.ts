import { NextResponse } from 'next/server';
import { getActiveSymbol, getSupportedSymbols, getTickerPrice } from '@/lib/broker-manager';
import { automation } from '@/lib/automation';
import { apiError } from '@/lib/api-response';
import { formatPair } from '@/lib/format-utils';

export async function GET() {
  try {
    const symbols = await getSupportedSymbols();
    const active = getActiveSymbol();
    const pairs = await Promise.all(
      symbols.map(async (symbol) => {
        const price = await getTickerPrice(symbol).catch(() => 0);
        return {
          symbol,
          display: formatPair(symbol),
          price,
          change24h: 0,
          high24h: 0,
          low24h: 0,
          volume24h: 0,
          active: symbol === active,
          lastUpdate: Date.now(),
        };
      })
    );

    return NextResponse.json({
      pairs,
      activePair: active,
      defaultPairs: pairs,
      popularPairs: pairs.slice(0, 8),
    });
  } catch (error: any) {
    return apiError('INTERNAL_ERROR', error.message || 'Failed to load pairs', 500, {
      pairs: ['XAU_USD'],
      activePair: 'XAU_USD',
    });
  }
}

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const { action, symbol } = body;
    if (!symbol) {
      return apiError('VALIDATION_ERROR', 'Symbol is required', 400);
    }
    const clean = String(symbol).replace('/', '_').toUpperCase();

    if (action === 'setActive') {
      await automation.getExecutionEngine().setPair(clean);
      return NextResponse.json({
        success: true,
        message: `Active pair set to ${clean}`,
        activePair: clean,
      });
    }

    return apiError('VALIDATION_ERROR', 'Unknown action. Use: setActive', 400);
  } catch (error: any) {
    return apiError('INTERNAL_ERROR', error.message || 'Failed to update pair', 500);
  }
}
