"""
scripts/fetch_market_data.py - pulls real recent gold price history for
backtesting.

Uses COMEX gold futures (GC=F) via Yahoo Finance's public chart API as
the source, since spot XAUUSD OHLC history isn't freely available
anywhere without a paid feed. GC=F is a liquid, closely correlated proxy
for spot gold - but it is NOT FBS's actual XAUUSD feed. Spread, exact
tick timing, and broker-specific price action will differ. This is for
sanity-checking the strategy's logic against real market structure
(real volatility, real trends, real chop) instead of a random walk -
not for predicting the exact result on the real account. For that,
export real history from the bridge once it's running against FBS (see
README) and pass it to run_backtest.py --csv instead.

Usage:
    .venv/bin/python scripts/fetch_market_data.py
    .venv/bin/python scripts/fetch_market_data.py --interval 5m --range 60d
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import requests

YAHOO_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Safari/537.36"}


def fetch(symbol: str, interval: str, range_: str) -> pd.DataFrame:
    resp = requests.get(
        YAHOO_URL.format(symbol=symbol),
        params={"interval": interval, "range": range_},
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    chart = data.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(f"Yahoo Finance devolvio un error: {chart['error']}")
    results = chart.get("result")
    if not results:
        raise RuntimeError("Respuesta sin datos - simbolo o rango invalido")

    result = results[0]
    ts = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    df = pd.DataFrame({
        "time": ts,
        "open": quote["open"],
        "high": quote["high"],
        "low": quote["low"],
        "close": quote["close"],
    })
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    df["time"] = df["time"].astype(int)
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="GC=F", help="Ticker de Yahoo Finance (default: futuros de oro COMEX)")
    parser.add_argument("--interval", default="1m", choices=["1m", "2m", "5m", "15m", "30m", "1h"])
    parser.add_argument("--range", dest="range_", default="5d",
                         help="Yahoo limita el rango segun el interval (1m -> max ~7d, 5m -> max ~60d, etc.)")
    parser.add_argument("--out", default="data/gold_history.csv")
    args = parser.parse_args()

    print(f"Descargando {args.symbol} interval={args.interval} range={args.range_} desde Yahoo Finance...")
    try:
        df = fetch(args.symbol, args.interval, args.range_)
    except requests.RequestException as exc:
        print(f"ERROR de red: {exc}", file=sys.stderr)
        print("Si esto falla por firewall/proxy, exporta historial real desde el "
              "bridge una vez conectado a FBS y usa esos datos en su lugar.", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if len(df) < 50:
        print(f"AVISO: solo se obtuvieron {len(df)} velas - puede que el rango/interval no tenga tanto historial.")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    start = pd.to_datetime(df["time"].iloc[0], unit="s")
    end = pd.to_datetime(df["time"].iloc[-1], unit="s")
    print(f"Guardadas {len(df)} velas en {out_path}")
    print(f"Rango: {start} -> {end} (UTC)")
    print()
    print("Nota: GC=F son futuros de oro COMEX, un proxy liquido correlacionado con")
    print("XAUUSD spot pero NO el feed real de FBS. Sirve para revisar que la logica")
    print("de la estrategia tenga sentido sobre movimiento de mercado real (tendencias,")
    print("rangos, volatilidad real) - no para predecir el resultado exacto en la cuenta.")


if __name__ == "__main__":
    main()
