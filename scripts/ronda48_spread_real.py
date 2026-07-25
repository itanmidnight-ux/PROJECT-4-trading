"""Ronda 48: re-mide el estado real de produccion (mean_reversion + ma_grid,
combo real .env) con el spread REAL medido contra el bridge (0.45) en vez
del 0.25 hardcodeado que uso toda la Fase 2 (incluida Ronda 43).

Reproduce EXACTAMENTE el harness de tests/test_backtest.py::_combo_real_run
/ _ronda43_settings / _test_spec (mismo starting_balance=100.45, leverage=500,
precompute_indicators=True, misma construccion de estrategia via
build_strategy_from_settings) para poder comparar manzanas con manzanas.
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


def _test_spec() -> SymbolSpec:
    return SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=100, volume_step=0.01,
                       point=0.01, trade_tick_value=1.0, trade_tick_size=0.01, margin_initial=None)


def _ronda43_settings(**overrides) -> Settings:
    kwargs = dict(
        mt5_login="1", mt5_password="x", mt5_server="s", mt5_is_demo=True,
        bridge_url="http://127.0.0.1:5001", bridge_timeout_ms=8000,
        symbol="XAUUSD", timeframe="M1", risk_per_trade_usd=5.25,
        max_daily_loss_usd=25.0, max_daily_drawdown_pct=20.0, max_trades_per_day=1000,
        min_tp_usd=0.60, tp_levels=8, dry_run=True, db_path=":memory:",
        strat_sl_atr_multiple=3.5, strat_enable_ma_grid=True,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def load_csv(name: str) -> pd.DataFrame:
    path = DATA_DIR / name
    return pd.read_csv(path).reset_index(drop=True)


def n_days(candles: pd.DataFrame) -> int:
    dates = pd.to_datetime(candles["time"], unit="s").dt.date
    return dates.nunique()


def combo_real_run(candles: pd.DataFrame, spread: float):
    spec = _test_spec()
    settings = _ronda43_settings()
    value_per_point = spec.trade_tick_value / (spec.trade_tick_size or spec.point)
    strategy = build_strategy_from_settings(settings, value_per_point)
    return run_backtest(
        candles=candles, spec=spec, starting_balance=100.45, leverage=500,
        risk_per_trade_usd=settings.risk_per_trade_usd, min_tp_usd=settings.min_tp_usd,
        tp_levels=settings.tp_levels, assumed_spread_price=spread, strategy=strategy,
        precompute_indicators=True, max_daily_loss_usd=settings.max_daily_loss_usd,
        max_daily_drawdown_pct=settings.max_daily_drawdown_pct,
    )


def report(label: str, candles: pd.DataFrame, spread: float, days: float | None = None):
    r = combo_real_run(candles, spread)
    pnl_per_day = f"{r.total_pnl / days:.2f}" if days else "n/a"
    pct_per_day = f"{(r.total_pnl / 100.45) / days * 100:.2f}%" if days else "n/a"
    print(f"[{label}] spread={spread} bars={len(candles)} -> "
          f"trades={r.trades} wins={r.wins} losses={r.losses} "
          f"win_rate={r.win_rate*100:.1f}% total_pnl=${r.total_pnl:.2f} "
          f"final_balance=${r.final_balance:.2f} max_dd={r.max_drawdown_pct:.2f}% "
          f"pnl/day=${pnl_per_day} ({pct_per_day}/dia)",
          flush=True)
    return r


def main():
    full = load_csv("gold_m1_7d.csv")
    train = load_csv("gold_m1_7d_train.csv")
    test = load_csv("gold_m1_7d_test.csv")

    d_full, d_train, d_test = n_days(full), n_days(train), n_days(test)
    print(f"dias unicos -> full={d_full} train={d_train} test={d_test}", flush=True)

    print("=== Paso 2: reproducir Ronda 43 (spread=0.25, 7 dias completos) ===", flush=True)
    report("FULL 7d - spread 0.25 (repro Ronda 43)", full, 0.25, days=d_full)

    print("\n=== Paso 3: mismo combo con spread REAL 0.45 ===", flush=True)
    report("FULL 7d - spread 0.45 (real)", full, 0.45, days=d_full)

    print("\n=== TRAIN/TEST con spread 0.25 (referencia) ===", flush=True)
    report("TRAIN - spread 0.25", train, 0.25, days=d_train)
    report("TEST  - spread 0.25", test, 0.25, days=d_test)

    print("\n=== TRAIN/TEST con spread REAL 0.45 ===", flush=True)
    report("TRAIN - spread 0.45", train, 0.45, days=d_train)
    report("TEST  - spread 0.45", test, 0.45, days=d_test)


if __name__ == "__main__":
    main()
