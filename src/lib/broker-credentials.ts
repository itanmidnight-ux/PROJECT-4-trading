import { encryptCredential, decryptCredential } from '@/lib/security';
import { getSetting, setSetting } from '@/lib/settings-manager';
import { normalizeBrokerProvider, type BrokerProvider, DEFAULT_BROKER_PROVIDER } from '@/lib/broker-provider';
import { getOandaBroker } from '@/lib/oanda-adapter';
import { getCTraderBroker } from '@/lib/ctrader-adapter';
import { getWeltradeMt5Broker } from '@/lib/weltrade-mt5-adapter';
import { initializeBrokerByProvider } from '@/lib/broker-manager';

export interface BrokerCredentials {
  broker: BrokerProvider;
  accountId: string;
  apiToken: string;
  isDemo: boolean;
  extra?: Record<string, any>;
}

const ACTIVE_BROKER_KEY = 'broker_active';

function keyFor(broker: BrokerProvider, suffix: 'account_id' | 'api_token' | 'is_demo' | 'extra'): string {
  return `broker_${broker}_${suffix}`;
}

const memoryStore: Record<BrokerProvider, BrokerCredentials> = {
  oanda: {
    broker: 'oanda',
    accountId: process.env.OANDA_ACCOUNT_ID || '',
    apiToken: process.env.OANDA_API_TOKEN || '',
    isDemo: process.env.OANDA_IS_DEMO !== 'false',
  },
  weltrade_mt5: {
    broker: 'weltrade_mt5',
    accountId: process.env.WELTRADE_MT5_LOGIN || '',
    apiToken: process.env.WELTRADE_MT5_PASSWORD || '',
    isDemo: process.env.WELTRADE_MT5_IS_DEMO !== 'false',
    extra: {
      server: process.env.WELTRADE_MT5_SERVER || '',
      terminalPath: process.env.WELTRADE_MT5_TERMINAL_PATH || '',
    },
  },
  ctrader: {
    broker: 'ctrader',
    accountId: process.env.CTRADER_APP_ID || '',
    apiToken: process.env.CTRADER_APP_SECRET || '',
    isDemo: process.env.CTRADER_IS_DEMO !== 'false',
    extra: {
      ctraderId: process.env.CTRADER_ID || '',
    },
  },
};

function getBrokerInstance(broker: BrokerProvider) {
  if (broker === 'weltrade_mt5') return getWeltradeMt5Broker();
  if (broker === 'ctrader') return getCTraderBroker();
  return getOandaBroker();
}

function encodeTokenForBroker(creds: BrokerCredentials): string {
  if (creds.broker === 'weltrade_mt5') {
    return JSON.stringify({
      password: creds.apiToken,
      server: creds.extra?.server || '',
      terminalPath: creds.extra?.terminalPath || '',
    });
  }
  if (creds.broker === 'ctrader') {
    return String(creds.apiToken || '');
  }
  return String(creds.apiToken || '');
}

function accountIdForBroker(creds: BrokerCredentials): string {
  if (creds.broker === 'ctrader') {
    const ctid = creds.extra?.ctraderId ? String(creds.extra.ctraderId) : '';
    return ctid ? `${creds.accountId}:${ctid}` : creds.accountId;
  }
  return creds.accountId;
}

function applyBrokerCredentials(creds: BrokerCredentials): void {
  const broker = getBrokerInstance(creds.broker);
  broker.setCredentials(accountIdForBroker(creds), encodeTokenForBroker(creds), creds.isDemo);
}

export function getBrokerCredentials(broker: BrokerProvider): BrokerCredentials {
  return { ...memoryStore[broker], extra: { ...(memoryStore[broker].extra || {}) } };
}

export function setBrokerCredentials(
  broker: BrokerProvider,
  accountId: string,
  apiToken: string,
  isDemo: boolean,
  extra?: Record<string, any>
): void {
  memoryStore[broker] = {
    broker,
    accountId,
    apiToken,
    isDemo,
    extra: extra || memoryStore[broker].extra || {},
  };
  applyBrokerCredentials(memoryStore[broker]);
}

export async function saveBrokerCredentials(broker: BrokerProvider): Promise<{ success: boolean; message: string }> {
  try {
    const creds = memoryStore[broker];
    if (!creds.accountId || !creds.apiToken) {
      return { success: false, message: `${broker} accountId and token/password required` };
    }

    await setSetting(keyFor(broker, 'account_id'), encryptCredential(creds.accountId), 'api');
    await setSetting(keyFor(broker, 'api_token'), encryptCredential(creds.apiToken), 'api');
    await setSetting(keyFor(broker, 'is_demo'), creds.isDemo ? 'true' : 'false', 'api');
    await setSetting(keyFor(broker, 'extra'), encryptCredential(JSON.stringify(creds.extra || {})), 'api');
    applyBrokerCredentials(creds);
    return { success: true, message: `${broker} credentials saved` };
  } catch (err) {
    return { success: false, message: err instanceof Error ? err.message : 'Save failed' };
  }
}

export async function loadBrokerCredentials(broker: BrokerProvider): Promise<boolean> {
  try {
    const encAccountId = await getSetting(keyFor(broker, 'account_id'));
    const encToken = await getSetting(keyFor(broker, 'api_token'));
    const isDemoStr = await getSetting(keyFor(broker, 'is_demo'));
    const encExtra = await getSetting(keyFor(broker, 'extra'));

    if (encAccountId && encToken) {
      const accountId = decryptCredential(encAccountId);
      const apiToken = decryptCredential(encToken);
      const extraRaw = encExtra ? decryptCredential(encExtra) : '{}';
      let extra: Record<string, any> = {};
      try {
        extra = JSON.parse(extraRaw || '{}');
      } catch {
        extra = {};
      }

      memoryStore[broker] = {
        broker,
        accountId,
        apiToken,
        isDemo: isDemoStr !== 'false',
        extra,
      };
      applyBrokerCredentials(memoryStore[broker]);
      return !!(accountId && apiToken);
    }

    // fallback to env defaults already loaded in memoryStore
    applyBrokerCredentials(memoryStore[broker]);
    return !!(memoryStore[broker].accountId && memoryStore[broker].apiToken);
  } catch (err) {
    console.error(`[BROKER-CRED] Load failed (${broker}):`, err);
    return false;
  }
}

export async function loadAllBrokerCredentials(): Promise<void> {
  await Promise.all([
    loadBrokerCredentials('oanda'),
    loadBrokerCredentials('weltrade_mt5'),
    loadBrokerCredentials('ctrader'),
  ]);
}

export async function validateBrokerCredentials(
  broker: BrokerProvider,
  overrides?: Partial<Omit<BrokerCredentials, 'broker'>>
): Promise<{ valid: boolean; message: string; balance?: number }> {
  const base = getBrokerCredentials(broker);
  const merged: BrokerCredentials = {
    broker,
    accountId: overrides?.accountId ?? base.accountId,
    apiToken: overrides?.apiToken ?? base.apiToken,
    isDemo: overrides?.isDemo ?? base.isDemo,
    extra: { ...(base.extra || {}), ...(overrides?.extra || {}) },
  };

  if (!merged.accountId || !merged.apiToken) {
    return { valid: false, message: `${broker} credentials are required` };
  }

  const instance = getBrokerInstance(broker);
  instance.setCredentials(accountIdForBroker(merged), encodeTokenForBroker(merged), merged.isDemo);
  const result = await instance.validateCredentials();
  if (!result.valid) return { valid: false, message: result.message };
  const balance = await instance.getBalance().catch(() => 0);
  return { valid: true, message: result.message, balance };
}

export function getBrokerCredentialStatus(broker: BrokerProvider): {
  broker: BrokerProvider;
  configured: boolean;
  accountIdPrefix: string | null;
  isDemo: boolean;
} {
  const creds = memoryStore[broker];
  return {
    broker,
    configured: !!(creds.accountId && creds.apiToken),
    accountIdPrefix: creds.accountId ? creds.accountId.slice(0, 6) : null,
    isDemo: creds.isDemo,
  };
}

export async function setActiveBroker(broker: BrokerProvider): Promise<void> {
  await setSetting(ACTIVE_BROKER_KEY, broker, 'general');
}

export async function getActiveBroker(): Promise<BrokerProvider> {
  const raw = await getSetting(ACTIVE_BROKER_KEY);
  return normalizeBrokerProvider(raw || DEFAULT_BROKER_PROVIDER);
}

export async function initializeActiveBroker(): Promise<{ success: boolean; message: string; broker: BrokerProvider }> {
  await loadAllBrokerCredentials();
  const active = await getActiveBroker();
  const priority = [active, 'weltrade_mt5', 'oanda', 'ctrader']
    .filter((b, i, arr) => arr.indexOf(b) === i) as BrokerProvider[];

  for (const broker of priority) {
    const creds = getBrokerCredentials(broker);
    if (!creds.accountId || !creds.apiToken) continue;

    const initialized = await initializeBrokerByProvider(
      broker,
      accountIdForBroker(creds),
      encodeTokenForBroker(creds),
      creds.isDemo,
      process.env.TRADING_SYMBOL || 'XAU_USD'
    );

    if (initialized.success) {
      if (broker !== active) {
        await setActiveBroker(broker);
        return {
          ...initialized,
          broker,
          message: `${initialized.message} (fallback from ${active})`,
        };
      }
      return { ...initialized, broker };
    }
  }

  return {
    success: false,
    message: `No usable credentials configured. Checked: ${priority.join(', ')}`,
    broker: active,
  };
}
