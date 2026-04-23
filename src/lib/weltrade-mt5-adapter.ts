import type { Candle } from '@/lib/analysis-engine';
import type {
  IBroker,
  SymbolSpec,
  OrderResult,
  PositionData,
  AccountData,
  OrderBookData,
} from '@/lib/broker-interface';

const DEFAULT_BRIDGE_URL = process.env.WELTRADE_MT5_BRIDGE_URL || 'http://127.0.0.1:5001';
const DEFAULT_TIMEOUT_MS = parseInt(process.env.WELTRADE_MT5_TIMEOUT_MS || '10000', 10);

const SYMBOL_SPECS: Record<string, SymbolSpec> = {
  XAU_USD: {
    symbol: 'XAU_USD', displayName: 'XAU/USD', category: 'metal',
    pipSize: 0.01, pipValue: 1, minLotSize: 0.01, maxLotSize: 100,
    lotStep: 0.01, contractSize: 100, tradingHours: '24/5',
    spreadTypical: 0.30, makerFee: 0, takerFee: 0,
  },
  EUR_USD: {
    symbol: 'EUR_USD', displayName: 'EUR/USD', category: 'forex',
    pipSize: 0.0001, pipValue: 10, minLotSize: 0.01, maxLotSize: 100,
    lotStep: 0.01, contractSize: 100000, tradingHours: '24/5',
    spreadTypical: 1.0, makerFee: 0, takerFee: 0,
  },
  GBP_USD: {
    symbol: 'GBP_USD', displayName: 'GBP/USD', category: 'forex',
    pipSize: 0.0001, pipValue: 10, minLotSize: 0.01, maxLotSize: 100,
    lotStep: 0.01, contractSize: 100000, tradingHours: '24/5',
    spreadTypical: 1.2, makerFee: 0, takerFee: 0,
  },
  USD_JPY: {
    symbol: 'USD_JPY', displayName: 'USD/JPY', category: 'forex',
    pipSize: 0.01, pipValue: 10, minLotSize: 0.01, maxLotSize: 100,
    lotStep: 0.01, contractSize: 100000, tradingHours: '24/5',
    spreadTypical: 1.0, makerFee: 0, takerFee: 0,
  },
  WTI_USD: {
    symbol: 'WTI_USD', displayName: 'WTI/USD', category: 'energy',
    pipSize: 0.01, pipValue: 1, minLotSize: 0.01, maxLotSize: 50,
    lotStep: 0.01, contractSize: 1000, tradingHours: '24/5',
    spreadTypical: 0.03, makerFee: 0, takerFee: 0,
  },
};

type BridgeCredentials = {
  password?: string;
  server?: string;
  demoServer?: string;
  liveServer?: string;
  terminalPath?: string;
};

export class WeltradeMt5Adapter implements IBroker {
  private login: string = '';
  private secret: string = '';
  private isDemo: boolean = true;
  private extra: BridgeCredentials = {};
  private connected = false;
  private baseUrl = DEFAULT_BRIDGE_URL;

  getBrokerName(): string {
    return 'Weltrade-MT5';
  }

  setCredentials(accountId: string, apiToken: string, isDemo: boolean): void {
    this.login = accountId;
    this.secret = apiToken;
    this.isDemo = isDemo;
    this.connected = false;

    // Support JSON token payload for bridge-specific fields.
    try {
      const parsed = JSON.parse(apiToken);
      if (parsed && typeof parsed === 'object') {
        this.secret = parsed.password || parsed.token || '';
        this.extra = {
          password: parsed.password,
          server: parsed.server,
          demoServer: parsed.demoServer,
          liveServer: parsed.liveServer,
          terminalPath: parsed.terminalPath,
        };
      }
    } catch {
      this.extra = { password: apiToken };
    }
  }

  async validateCredentials(): Promise<{ valid: boolean; message: string }> {
    if (!this.login || !this.secret) {
      return { valid: false, message: 'Weltrade MT5 login and password are required' };
    }

    try {
      const health = await this.bridgeGet('/health').catch(() => null);
      if (!health || health.status !== 'ok') {
        return { valid: false, message: 'MT5 bridge is offline. Start bridge service first.' };
      }

      const result = await this.bridgePost('/auth/validate', {
        login: this.login,
        password: this.secret,
        server: this.resolveServer(),
        terminalPath: this.extra.terminalPath,
        isDemo: this.isDemo,
      });

      if (!result?.valid) {
        return { valid: false, message: result?.message || 'Invalid MT5 credentials' };
      }

      this.connected = true;
      return { valid: true, message: result.message || 'Connected to Weltrade MT5 bridge' };
    } catch (err) {
      return { valid: false, message: err instanceof Error ? err.message : 'Weltrade validation failed' };
    }
  }

  async getSupportedSymbols(): Promise<string[]> {
    const local = Object.keys(SYMBOL_SPECS);
    try {
      const symbols = await this.bridgeGet('/symbols');
      if (Array.isArray(symbols)) {
        return symbols.map((s) => this.normalizeSymbol(s));
      }
    } catch {
      // Fallback to local map.
    }
    return local;
  }

  async getSymbolSpec(symbol: string): Promise<SymbolSpec> {
    const normalized = this.normalizeSymbol(symbol);
    return SYMBOL_SPECS[normalized] || {
      symbol: normalized, displayName: normalized.replace('_', '/'), category: 'forex',
      pipSize: 0.0001, pipValue: 10, minLotSize: 0.01, maxLotSize: 100,
      lotStep: 0.01, contractSize: 100000, tradingHours: '24/5',
      spreadTypical: 1.0, makerFee: 0, takerFee: 0,
    };
  }

  async getPrice(symbol: string): Promise<number> {
    const normalized = this.normalizeSymbol(symbol);
    const mt5 = this.toMt5Symbol(normalized);
    const data = await this.bridgeGet('/price', { symbol: mt5 });
    const bid = parseFloat(data?.bid ?? data?.price ?? '0');
    const ask = parseFloat(data?.ask ?? data?.price ?? '0');
    if (bid > 0 && ask > 0) return (bid + ask) / 2;
    return bid || ask || 0;
  }

  async getBatchPrices(symbols: string[]): Promise<Record<string, number>> {
    const out: Record<string, number> = {};
    await Promise.all(symbols.map(async (s) => {
      out[s] = await this.getPrice(s);
    }));
    return out;
  }

  async getKlines(symbol: string, timeframe: string, limit: number): Promise<Candle[]> {
    const normalized = this.normalizeSymbol(symbol);
    const mt5 = this.toMt5Symbol(normalized);
    const tf = this.mapTimeframe(timeframe);
    const data = await this.bridgeGet('/klines', { symbol: mt5, timeframe: tf, limit: String(limit) });
    if (!Array.isArray(data)) return [];
    return data.map((c: any) => ({
      time: Number(c.time || c.timestamp || Date.now()),
      open: Number(c.open || 0),
      high: Number(c.high || 0),
      low: Number(c.low || 0),
      close: Number(c.close || 0),
      volume: Number(c.volume || c.tick_volume || 0),
    }));
  }

  async getOrderBook(symbol: string, depth: number = 10): Promise<OrderBookData> {
    const price = await this.getPrice(symbol);
    const spread = Math.max(price * 0.0001, 0.01);
    const bids = Array.from({ length: depth }, (_, i) => ({
      price: +(price - spread * (i + 1) * 0.5).toFixed(5),
      quantity: 10 - i > 0 ? 10 - i : 1,
    }));
    const asks = Array.from({ length: depth }, (_, i) => ({
      price: +(price + spread * (i + 1) * 0.5).toFixed(5),
      quantity: 10 - i > 0 ? 10 - i : 1,
    }));
    return { symbol: this.normalizeSymbol(symbol), bids, asks, spread };
  }

  async placeMarketOrder(symbol: string, side: 'BUY' | 'SELL', quantity: number): Promise<OrderResult> {
    return this.placeOrder(symbol, side, quantity, 'MARKET');
  }

  async placeLimitOrder(symbol: string, side: 'BUY' | 'SELL', quantity: number, price: number): Promise<OrderResult> {
    return this.placeOrder(symbol, side, quantity, 'LIMIT', price);
  }

  async placeStopOrder(symbol: string, side: 'BUY' | 'SELL', quantity: number, stopPrice: number): Promise<OrderResult> {
    return this.placeOrder(symbol, side, quantity, 'STOP', stopPrice);
  }

  async closePosition(symbol: string, quantity: number): Promise<OrderResult> {
    const normalized = this.normalizeSymbol(symbol);
    const data = await this.bridgePost('/position/close', {
      symbol: this.toMt5Symbol(normalized),
      quantity,
      login: this.login,
      isDemo: this.isDemo,
    });
    return {
      success: !!data?.success,
      orderId: String(data?.orderId || data?.ticket || ''),
      symbol: normalized,
      side: 'SELL',
      type: 'MARKET',
      quantity,
      status: data?.status || 'CLOSED',
      error: data?.success ? undefined : (data?.error || 'Close position failed'),
    };
  }

  async cancelOrder(symbol: string, orderId: string): Promise<boolean> {
    const normalized = this.normalizeSymbol(symbol);
    const data = await this.bridgePost('/order/cancel', {
      symbol: this.toMt5Symbol(normalized),
      orderId,
      login: this.login,
      isDemo: this.isDemo,
    });
    return !!data?.success;
  }

  async getAccountData(): Promise<AccountData> {
    const acc = await this.bridgeGet('/account', { login: this.login, isDemo: String(this.isDemo) });
    const balance = Number(acc?.balance ?? 0);
    const equity = Number(acc?.equity ?? balance);
    const margin = Number(acc?.margin ?? 0);
    const freeMargin = Number(acc?.freeMargin ?? acc?.margin_free ?? 0);
    const marginLevel = Number(acc?.marginLevel ?? (margin > 0 ? (equity / margin) * 100 : 0));
    return {
      balance,
      equity,
      margin,
      freeMargin,
      marginLevel,
      currency: acc?.currency || 'USD',
    };
  }

  async getOpenPositions(): Promise<PositionData[]> {
    const rows = await this.bridgeGet('/positions', { login: this.login, isDemo: String(this.isDemo) });
    if (!Array.isArray(rows)) return [];
    return rows.map((p: any) => ({
      id: String(p.id || p.ticket || ''),
      symbol: this.normalizeSymbol(p.symbol || ''),
      side: String(p.side || p.type || '').toUpperCase().includes('SELL') ? 'SHORT' : 'LONG',
      entryPrice: Number(p.entryPrice ?? p.price_open ?? 0),
      currentPrice: Number(p.currentPrice ?? p.price_current ?? p.price_open ?? 0),
      quantity: Number(p.quantity ?? p.volume ?? 0),
      unrealizedPnl: Number(p.unrealizedPnl ?? p.profit ?? 0),
      stopLoss: p.stopLoss ?? p.sl ?? undefined,
      takeProfit: p.takeProfit ?? p.tp ?? undefined,
      openedAt: new Date(p.openedAt || p.time || Date.now()),
    }));
  }

  async getBalance(): Promise<number> {
    const data = await this.getAccountData();
    return data.balance;
  }

  isMarketOpen(symbol: string): boolean {
    const spec = SYMBOL_SPECS[this.normalizeSymbol(symbol)];
    if (!spec) return true;
    if (spec.tradingHours === '24/7') return true;
    const day = new Date().getUTCDay();
    return day !== 0 && day !== 6;
  }

  isConnected(): boolean {
    return this.connected;
  }

  private async placeOrder(
    symbol: string,
    side: 'BUY' | 'SELL',
    quantity: number,
    type: 'MARKET' | 'LIMIT' | 'STOP',
    price?: number
  ): Promise<OrderResult> {
    const normalized = this.normalizeSymbol(symbol);
    const endpoint =
      type === 'MARKET' ? '/order/market'
      : type === 'LIMIT' ? '/order/limit'
      : '/order/stop';

    try {
      const data = await this.bridgePost(endpoint, {
        symbol: this.toMt5Symbol(normalized),
        side,
        quantity,
        price,
        login: this.login,
        isDemo: this.isDemo,
      });

      return {
        success: !!data?.success,
        orderId: String(data?.orderId || data?.ticket || ''),
        symbol: normalized,
        side,
        type,
        quantity,
        price,
        fillPrice: Number(data?.fillPrice ?? data?.price ?? 0),
        status: data?.status || (data?.success ? 'FILLED' : 'REJECTED'),
        error: data?.success ? undefined : (data?.error || 'Order failed'),
      };
    } catch (err) {
      return {
        success: false,
        symbol: normalized,
        side,
        type,
        quantity,
        price,
        error: err instanceof Error ? err.message : 'Order failed',
      };
    }
  }

  private normalizeSymbol(symbol: string): string {
    const clean = symbol.toUpperCase().replace(/[\/\-\s]/g, '');
    const mapping: Record<string, string> = {
      XAUUSD: 'XAU_USD',
      XAGUSD: 'XAG_USD',
      EURUSD: 'EUR_USD',
      GBPUSD: 'GBP_USD',
      USDJPY: 'USD_JPY',
      WTIUSD: 'WTI_USD',
      USOIL: 'WTI_USD',
    };
    return mapping[clean] || symbol.replace('/', '_').replace('-', '_').toUpperCase();
  }

  private toMt5Symbol(symbol: string): string {
    return symbol.replace('_', '');
  }

  private resolveServer(): string | undefined {
    if (this.extra.server && String(this.extra.server).trim()) return String(this.extra.server).trim();
    const fromMode = this.isDemo
      ? (this.extra.demoServer || process.env.WELTRADE_MT5_DEMO_SERVER || 'MetaQuotes-Demo')
      : (this.extra.liveServer || process.env.WELTRADE_MT5_SERVER || 'Weltrade-Live');
    return String(fromMode || '').trim() || undefined;
  }

  private mapTimeframe(tf: string): string {
    const map: Record<string, string> = {
      '1m': 'M1',
      '3m': 'M3',
      '5m': 'M5',
      '15m': 'M15',
      '30m': 'M30',
      '1h': 'H1',
      '4h': 'H4',
      '1d': 'D1',
      '1w': 'W1',
    };
    return map[tf] || 'M5';
  }

  private async bridgeGet(path: string, query: Record<string, string> = {}): Promise<any> {
    const params = new URLSearchParams(query);
    const url = `${this.baseUrl}${path}${params.size > 0 ? `?${params.toString()}` : ''}`;
    const res = await this.fetchWithTimeout(url, { method: 'GET' });
    if (!res.ok) {
      throw new Error(`Bridge GET ${path} failed: HTTP ${res.status}`);
    }
    return res.json();
  }

  private async bridgePost(path: string, body: Record<string, any>): Promise<any> {
    const url = `${this.baseUrl}${path}`;
    const res = await this.fetchWithTimeout(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(`Bridge POST ${path} failed: HTTP ${res.status} ${text}`);
    }
    return res.json();
  }

  private async fetchWithTimeout(url: string, init: RequestInit): Promise<Response> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
    try {
      return await fetch(url, { ...init, signal: controller.signal });
    } finally {
      clearTimeout(timeout);
    }
  }
}

let globalWeltradeMt5: WeltradeMt5Adapter | null = null;

export function getWeltradeMt5Broker(): WeltradeMt5Adapter {
  if (!globalWeltradeMt5) {
    globalWeltradeMt5 = new WeltradeMt5Adapter();
  }
  return globalWeltradeMt5;
}
