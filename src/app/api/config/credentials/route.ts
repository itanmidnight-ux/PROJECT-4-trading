import { NextResponse } from 'next/server';
import { apiError } from '@/lib/api-response';
import {
  getActiveBroker,
  getBrokerCredentials,
  getBrokerCredentialStatus,
  loadBrokerCredentials,
  saveBrokerCredentials,
  setActiveBroker,
  setBrokerCredentials,
  validateBrokerCredentials,
  initializeActiveBroker,
} from '@/lib/broker-credentials';
import { normalizeBrokerProvider } from '@/lib/broker-provider';

function parseBroker(request: Request, bodyBroker?: string): string | null {
  const url = new URL(request.url);
  const brokerQ = url.searchParams.get('broker');
  const raw = bodyBroker || brokerQ;
  if (!raw) return null;
  return normalizeBrokerProvider(raw);
}

export async function GET(request: Request) {
  try {
    const active = await getActiveBroker();
    const broker = normalizeBrokerProvider(parseBroker(request) || active);
    await loadBrokerCredentials(broker);
    const status = getBrokerCredentialStatus(broker);

    return NextResponse.json({
      broker,
      activeBroker: active,
      configured: status.configured,
      accountIdPrefix: status.accountIdPrefix,
      isDemo: status.isDemo,
    });
  } catch (error: any) {
    return apiError('INTERNAL_ERROR', error.message || 'Failed to read credential status', 500, {
      configured: false,
    });
  }
}

export async function PUT(request: Request) {
  try {
    const body = await request.json();
    const broker = normalizeBrokerProvider(parseBroker(request, body.broker) || await getActiveBroker());
    const { accountId, apiToken, isDemo, extra, makeActive } = body;

    if (!accountId || !apiToken) {
      return apiError('VALIDATION_ERROR', 'accountId and apiToken are required', 400);
    }

    setBrokerCredentials(broker, String(accountId).trim(), String(apiToken).trim(), isDemo !== false, extra);
    const result = await saveBrokerCredentials(broker);

    if (result.success && makeActive !== false) {
      await setActiveBroker(broker);
      await initializeActiveBroker();
    }

    const status = getBrokerCredentialStatus(broker);
    return NextResponse.json({
      success: result.success,
      message: result.message,
      broker,
      accountIdPrefix: status.accountIdPrefix,
      isDemo: status.isDemo,
    });
  } catch (error: any) {
    return apiError('INTERNAL_ERROR', error.message || 'Failed to save credentials', 500);
  }
}

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const broker = normalizeBrokerProvider(parseBroker(request, body.broker) || await getActiveBroker());
    const current = getBrokerCredentials(broker);

    const accountId = body.accountId || current.accountId;
    const apiToken = body.apiToken || current.apiToken;
    const isDemo = body.isDemo !== undefined ? !!body.isDemo : current.isDemo;
    const extra = { ...(current.extra || {}), ...(body.extra || {}) };

    const validation = await validateBrokerCredentials(broker, { accountId, apiToken, isDemo, extra });

    if (validation.valid && body.accountId && body.apiToken) {
      setBrokerCredentials(broker, accountId, apiToken, isDemo, extra);
      await saveBrokerCredentials(broker);
      if (body.makeActive !== false) {
        await setActiveBroker(broker);
        await initializeActiveBroker();
      }
    }

    return NextResponse.json({
      success: validation.valid,
      message: validation.message,
      balance: validation.balance,
      broker,
    });
  } catch (error: any) {
    return apiError('INTERNAL_ERROR', error.message || 'Failed to validate credentials', 500);
  }
}
