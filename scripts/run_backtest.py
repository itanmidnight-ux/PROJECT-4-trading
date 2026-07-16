"""CLI: python scripts/run_backtest.py [--csv path.csv] [--bars 3000]

Without --csv, generates synthetic candles (same generator the dry-run
engine uses) purely to prove the backtest plumbing works end to end.
For a real read on expectancy, export real history first:
    curl "http://127.0.0.1:5001/candles/XAUUSD?timeframe=M1&count=5000" > history.json
and adapt this script to load that JSON instead.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from core.backtest import run_backtest  # noqa: E402
from core.market_data import SyntheticMarketData  # noqa: E402
from core.risk_manager import SymbolSpec  # noqa: E402


def synthetic_candles(bars: int) -> pd.DataFrame:
    src = SyntheticMarketData(seed=42)
    rows = []
    for _ in range(bars):
        state = src.get_state("XAUUSD", "M1", 1)
        rows.append(state.candles.iloc[-1].to_dict())
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", help="CSV with columns time,open,high,low,close (real MT5 history)")
    parser.add_argument("--bars", type=int, default=3000)
    parser.add_argument("--balance", type=float, default=50.0)
    parser.add_argument("--leverage", type=int, default=1)
    parser.add_argument("--risk-usd", type=float, default=1.0)
    parser.add_argument("--min-tp-usd", type=float, default=0.28)
    parser.add_argument("--tp-levels", type=int, default=3)
    parser.add_argument("--spread", type=float, default=0.25)
    parser.add_argument("--composite", action="store_true",
                         help="Use the composite strategy (mean reversion + the extra M1 signals in "
                              "core/signals.py, per STRAT_ENABLE_* in .env/defaults) instead of just "
                              "mean reversion alone.")
    args = parser.parse_args()

    candles = pd.read_csv(args.csv) if args.csv else synthetic_candles(args.bars)

    spec = SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=1.0,
                       volume_step=0.01, point=0.01, trade_tick_value=1.0)

    strategy = None
    if args.composite:
        from core.config import load_settings  # noqa: PLC0415 - optional import, only needed for --composite
        from core.signals import build_strategy_from_settings  # noqa: PLC0415
        value_per_point_per_lot = spec.trade_tick_value / spec.point
        strategy = build_strategy_from_settings(load_settings(), value_per_point_per_lot)

    result = run_backtest(
        candles=candles, spec=spec, starting_balance=args.balance, leverage=args.leverage,
        risk_per_trade_usd=args.risk_usd, min_tp_usd=args.min_tp_usd, tp_levels=args.tp_levels,
        assumed_spread_price=args.spread, strategy=strategy,
    )

    days = max(len(candles) / 1440, 1e-9)  # 1440 M1 bars/day - approximate, doesn't account for market closures
    print(f"Strategy: {'composite (mean reversion + extra signals)' if args.composite else 'mean reversion only'}")
    print(f"Bars: {len(candles)} (~{days:.1f} days)  Spread assumed: {args.spread}")
    print(f"Trades: {result.trades}  (~{result.trades / days:.2f}/day)  Wins: {result.wins}  Losses: {result.losses}  "
          f"Win rate: {result.win_rate * 100:.1f}%")
    print(f"Total PnL: {result.total_pnl:+.2f} USD   Final balance: {result.final_balance:.2f} USD")
    print(f"Max drawdown: {result.max_drawdown_pct:.1f}%")
    if not args.csv:
        print("\nNOTE: this ran on SYNTHETIC random-walk data, not real market history.")
        print("It only proves the strategy/backtest code executes correctly end to end -")
        print("it says NOTHING about real profitability. Re-run with --csv real history")
        print("before trusting any of these numbers.")


if __name__ == "__main__":
    main()
