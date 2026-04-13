'use client';

import { useEffect, useMemo, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { TrendingUp, TrendingDown, Target } from 'lucide-react';

type PriceRow = {
  symbol: string;
  display: string;
  price: number;
};

const QUICK_LOTS = [0.01, 0.05, 0.1, 0.25, 0.5, 1.0];

function toBrokerSymbol(symbol: string): string {
  return symbol.replace('/', '_').replace('-', '_').toUpperCase();
}

export function QuickTradePanel() {
  const [symbol, setSymbol] = useState('XAU_USD');
  const [lotSize, setLotSize] = useState('0.10');
  const [sl, setSl] = useState('');
  const [tp, setTp] = useState('');
  const [prices, setPrices] = useState<Record<string, PriceRow>>({});
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [status, setStatus] = useState('');

  const symbols = [
    { value: 'XAU_USD', label: 'XAU/USD' },
    { value: 'EUR_USD', label: 'EUR/USD' },
    { value: 'GBP_USD', label: 'GBP/USD' },
    { value: 'USD_JPY', label: 'USD/JPY' },
    { value: 'WTI_USD', label: 'WTI/USD' },
    { value: 'NAS100_USD', label: 'NAS100' },
  ];

  useEffect(() => {
    const fetchPrices = async () => {
      try {
        const res = await fetch('/api/pairs/prices', { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        setPrices(data.prices || {});
      } catch {
        // ignore polling failure
      }
    };

    fetchPrices();
    const timer = setInterval(fetchPrices, 4000);
    return () => clearInterval(timer);
  }, []);

  const selectedPrice = useMemo(() => {
    const row = prices[symbol];
    return row?.price || 0;
  }, [prices, symbol]);

  const submitOrder = async (side: 'buy' | 'sell') => {
    setIsSubmitting(true);
    setStatus('');
    try {
      const res = await fetch('/api/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action: side,
          pair: toBrokerSymbol(symbol),
          quantity: parseFloat(lotSize),
          // live-protection gate handled backend; we intentionally do not auto-confirm here
          stopLoss: sl ? parseFloat(sl) : undefined,
          takeProfit: tp ? parseFloat(tp) : undefined,
        }),
      });

      const data = await res.json();
      if (!res.ok || !data?.success) {
        const msg = data?.error?.message || data?.error || data?.message || 'Order rejected';
        setStatus(`Error: ${msg}`);
        return;
      }

      setStatus(data?.message || 'Order executed');
    } catch {
      setStatus('Error: request failed');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center justify-between text-lg">
          <span>Ejecutar Orden</span>
          <Badge variant="outline" className="text-xs">Manual</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-1">
          <Label>Símbolo</Label>
          <div className="grid grid-cols-3 gap-1">
            {symbols.map((s) => (
              <Button
                key={s.value}
                variant={symbol === s.value ? 'default' : 'outline'}
                size="sm"
                onClick={() => setSymbol(s.value)}
                className="text-xs"
              >
                {s.label}
              </Button>
            ))}
          </div>
        </div>

        <div className="flex justify-between items-center p-2 bg-muted/50 rounded">
          <div>
            <p className="text-xs text-muted-foreground">Precio</p>
            <p className="font-mono font-bold text-cyan-300">
              {selectedPrice > 0 ? selectedPrice.toFixed(5) : '-'}
            </p>
          </div>
          <div className="text-right">
            <p className="text-xs text-muted-foreground">Par</p>
            <p className="font-mono">{symbol.replace('_', '/')}</p>
          </div>
        </div>

        <div className="space-y-1">
          <Label>Volumen (Lotes)</Label>
          <div className="grid grid-cols-6 gap-1 mb-1">
            {QUICK_LOTS.map((lot) => (
              <Button
                key={lot}
                variant={lotSize === lot.toString() ? 'default' : 'outline'}
                size="sm"
                onClick={() => setLotSize(lot.toString())}
                className="text-xs h-7"
              >
                {lot.toFixed(2)}
              </Button>
            ))}
          </div>
          <Input
            type="number"
            value={lotSize}
            onChange={(e) => setLotSize(e.target.value)}
            step="0.01"
            min="0.01"
          />
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1">
            <Label className="flex items-center gap-1 text-red-400">
              <Target className="w-3 h-3" />
              Stop Loss
            </Label>
            <Input
              type="number"
              value={sl}
              onChange={(e) => setSl(e.target.value)}
              placeholder="Opcional"
              step="0.00001"
            />
          </div>
          <div className="space-y-1">
            <Label className="flex items-center gap-1 text-green-400">
              <Target className="w-3 h-3" />
              Take Profit
            </Label>
            <Input
              type="number"
              value={tp}
              onChange={(e) => setTp(e.target.value)}
              placeholder="Opcional"
              step="0.00001"
            />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <Button
            onClick={() => submitOrder('buy')}
            className="bg-green-600 hover:bg-green-700 text-white h-12"
            disabled={isSubmitting}
          >
            <TrendingUp className="w-5 h-5 mr-2" />
            <div className="text-left">
              <div className="text-xs opacity-80">COMPRAR</div>
              <div className="font-bold">{selectedPrice > 0 ? selectedPrice.toFixed(5) : '-'}</div>
            </div>
          </Button>
          <Button
            onClick={() => submitOrder('sell')}
            className="bg-red-600 hover:bg-red-700 text-white h-12"
            disabled={isSubmitting}
          >
            <TrendingDown className="w-5 h-5 mr-2" />
            <div className="text-left">
              <div className="text-xs opacity-80">VENDER</div>
              <div className="font-bold">{selectedPrice > 0 ? selectedPrice.toFixed(5) : '-'}</div>
            </div>
          </Button>
        </div>

        <p className="text-xs text-muted-foreground text-center min-h-4">{status}</p>
      </CardContent>
    </Card>
  );
}
