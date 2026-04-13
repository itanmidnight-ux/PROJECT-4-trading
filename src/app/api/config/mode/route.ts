import { NextResponse } from 'next/server';
import { apiError } from '@/lib/api-response';
import {
  getActiveBroker,
  getBrokerCredentials,
  initializeActiveBroker,
  saveBrokerCredentials,
  setBrokerCredentials,
} from '@/lib/broker-credentials';

export async function GET() {
  try {
    const broker = await getActiveBroker();
    const creds = getBrokerCredentials(broker);
    const isDemo = creds.isDemo;

    return NextResponse.json({
      broker,
      mode: isDemo ? 'demo' : 'live',
      testnet: isDemo,
      message: isDemo
        ? `Running in ${broker} demo mode`
        : `Running in ${broker} live mode (REAL MONEY)`,
      credentialsStatus: {
        configured: !!(creds.accountId && creds.apiToken),
      },
    });
  } catch (error: any) {
    return apiError('INTERNAL_ERROR', error.message || 'Failed to load mode', 500);
  }
}

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const broker = await getActiveBroker();
    const creds = getBrokerCredentials(broker);
    const requestedDemo = typeof body.testnet === 'boolean'
      ? body.testnet
      : (typeof body.isDemo === 'boolean' ? body.isDemo : undefined);

    if (requestedDemo === undefined) {
      return apiError('VALIDATION_ERROR', 'testnet or isDemo boolean is required', 400);
    }

    if (requestedDemo === creds.isDemo) {
      return NextResponse.json({
        success: true,
        changed: false,
        broker,
        mode: requestedDemo ? 'demo' : 'live',
        message: `Already in ${requestedDemo ? 'demo' : 'live'} mode`,
      });
    }

    if (!creds.accountId || !creds.apiToken) {
      return apiError(
        'VALIDATION_ERROR',
        `No credentials configured for ${broker}`,
        400,
        { changed: false, broker, requiresCredentials: true }
      );
    }

    setBrokerCredentials(broker, creds.accountId, creds.apiToken, requestedDemo, creds.extra);
    await saveBrokerCredentials(broker);
    const init = await initializeActiveBroker();

    return NextResponse.json({
      success: init.success,
      changed: true,
      broker,
      mode: requestedDemo ? 'demo' : 'live',
      previousMode: requestedDemo ? 'live' : 'demo',
      message: init.message,
    });
  } catch (error: any) {
    return apiError('INTERNAL_ERROR', error.message || 'Failed to switch mode', 500);
  }
}
