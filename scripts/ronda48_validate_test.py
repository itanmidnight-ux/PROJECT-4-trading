"""Ronda 48 paso 5c: valida en TEST (held-out, jamas mirado durante el sweep)
los mejores candidatos encontrados en TRAIN bajo spread real 0.45. Misma
disciplina de siempre: si TEST no confirma la mejora de TRAIN, no se toca
ningun default.
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
    test = load_csv("gold_m1_7d_test.csv")
    full = load_csv("gold_m1_7d.csv")

    candidates = [
        ("BASELINE (0.60/3.5, default actual)", 0.60, 3.5),
        ("MIN_TP=3.00 SL=4.0 (mejor TRAIN)", 3.00, 4.0),
        ("MIN_TP=1.20 SL=4.0", 1.20, 4.0),
        ("MIN_TP=1.00 SL=4.0", 1.00, 4.0),
        ("MIN_TP=1.20 SL=2.5", 1.20, 2.5),
    ]

    for label, mt, sl in candidates:
        r_train = run(train, mt, sl)
        r_test = run(test, mt, sl)
        r_full = run(full, mt, sl)
        print(f"--- {label} ---", flush=True)
        print(f"  TRAIN: trades={r_train.trades} win_rate={r_train.win_rate*100:.1f}% "
              f"total_pnl=${r_train.total_pnl:.2f} max_dd={r_train.max_drawdown_pct:.2f}%", flush=True)
        print(f"  TEST : trades={r_test.trades} win_rate={r_test.win_rate*100:.1f}% "
              f"total_pnl=${r_test.total_pnl:.2f} max_dd={r_test.max_drawdown_pct:.2f}%", flush=True)
        print(f"  FULL : trades={r_full.trades} win_rate={r_full.win_rate*100:.1f}% "
              f"total_pnl=${r_full.total_pnl:.2f} max_dd={r_full.max_drawdown_pct:.2f}%", flush=True)


if __name__ == "__main__":
    main()
