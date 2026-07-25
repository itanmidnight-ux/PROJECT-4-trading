"""Ronda 48 paso 5b: sweep combinado MIN_TP_USD x STRAT_SL_ATR_MULTIPLE bajo
spread real (0.45), SOLO TRAIN. El sweep univariado (ronda48_sweep_spread_real.py)
encontro que MIN_TP_USD=1.20 (+$5.18) y MIN_TP_USD=3.00 (+$13.31) mejoran el
baseline (-$21.78) con SL fijo en 3.5, pero SL solo (con MIN_TP fijo en 0.60)
nunca se puso positivo. Este grid busca el mejor par combinado antes de mirar
TEST, con la misma disciplina de siempre.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from core.backtest import run_backtest  # noqa: E402
from core.config import Settings  # noqa: E402
from core.risk_manager import SymbolSpec  # noqa: E402
from core.signals import build_strategy_from_settings  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SPREAD = 0.45


def _test_spec() -> SymbolSpec:
    return SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=100, volume_step=0.01,
                       point=0.01, trade_tick_value=1.0, trade_tick_size=0.01, margin_initial=None)


def _settings(min_tp_usd: float, sl_atr_multiple: float) -> Settings:
    return Settings(
        mt5_login="1", mt5_password="x", mt5_server="s", mt5_is_demo=True,
        bridge_url="http://127.0.0.1:5001", bridge_timeout_ms=8000,
        symbol="XAUUSD", timeframe="M1", risk_per_trade_usd=5.25,
        max_daily_loss_usd=25.0, max_daily_drawdown_pct=20.0, max_trades_per_day=1000,
        min_tp_usd=min_tp_usd, tp_levels=8, dry_run=True, db_path=":memory:",
        strat_sl_atr_multiple=sl_atr_multiple, strat_enable_ma_grid=True,
    )


def load_csv(name: str) -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / name).reset_index(drop=True)


def run(candles: pd.DataFrame, min_tp_usd: float, sl_atr_multiple: float):
    spec = _test_spec()
    settings = _settings(min_tp_usd, sl_atr_multiple)
    value_per_point = spec.trade_tick_value / (spec.trade_tick_size or spec.point)
    strategy = build_strategy_from_settings(settings, value_per_point)
    return run_backtest(
        candles=candles, spec=spec, starting_balance=100.45, leverage=500,
        risk_per_trade_usd=settings.risk_per_trade_usd, min_tp_usd=settings.min_tp_usd,
        tp_levels=settings.tp_levels, assumed_spread_price=SPREAD, strategy=strategy,
        precompute_indicators=True, max_daily_loss_usd=settings.max_daily_loss_usd,
        max_daily_drawdown_pct=settings.max_daily_drawdown_pct,
    )


def main():
    train = load_csv("gold_m1_7d_train.csv")
    min_tps = [1.00, 1.20, 1.50, 2.00, 2.50, 3.00, 3.50, 4.00, 5.00]
    sls = [2.5, 3.0, 3.5, 4.0]

    results = []
    print("=== Grid MIN_TP_USD x STRAT_SL_ATR_MULTIPLE, spread real 0.45, TRAIN ===", flush=True)
    for sl in sls:
        for mt in min_tps:
            r = run(train, mt, sl)
            results.append((mt, sl, r.total_pnl, r.trades, r.win_rate, r.max_drawdown_pct))
            print(f"MIN_TP_USD={mt:.2f} SL={sl:.1f} -> trades={r.trades} "
                  f"win_rate={r.win_rate*100:.1f}% total_pnl=${r.total_pnl:.2f} "
                  f"max_dd={r.max_drawdown_pct:.2f}%", flush=True)

    results.sort(key=lambda x: x[2], reverse=True)
    print("\n=== TOP 8 combinaciones por total_pnl en TRAIN ===", flush=True)
    for mt, sl, pnl, trades, wr, dd in results[:8]:
        print(f"MIN_TP_USD={mt:.2f} SL={sl:.1f} -> total_pnl=${pnl:.2f} trades={trades} "
              f"win_rate={wr*100:.1f}% max_dd={dd:.2f}%", flush=True)


if __name__ == "__main__":
    main()
