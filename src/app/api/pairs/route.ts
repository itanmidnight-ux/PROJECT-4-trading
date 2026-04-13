import { NextResponse } from 'next/server';
import { getActiveSymbol, getSupportedSymbols } from '@/lib/broker-manager';
import { automation } from '@/lib/automation';
import { apiError } from '@/lib/api-response';

export async function GET() {
  try {
    const symbols = await getSupportedSymbols();
    const active = getActiveSymbol();
    return NextResponse.json({
      pairs: symbols,
      activePair: active,
      defaultPairs: symbols,
      popularPairs: symbols.slice(0, 8),
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
