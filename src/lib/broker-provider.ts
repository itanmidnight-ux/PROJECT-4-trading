export type BrokerProvider = 'oanda' | 'weltrade_mt5' | 'ctrader';

export const DEFAULT_BROKER_PROVIDER: BrokerProvider = 'weltrade_mt5';

export const BROKER_PROVIDERS: BrokerProvider[] = ['oanda', 'weltrade_mt5', 'ctrader'];

export function normalizeBrokerProvider(value: string | null | undefined): BrokerProvider {
  if (!value) return DEFAULT_BROKER_PROVIDER;
  const normalized = value.toLowerCase().trim();
  if (normalized === 'weltrade' || normalized === 'weltrade_mt5' || normalized === 'mt5') {
    return 'weltrade_mt5';
  }
  if (normalized === 'ctrader') return 'ctrader';
  return DEFAULT_BROKER_PROVIDER;
}
