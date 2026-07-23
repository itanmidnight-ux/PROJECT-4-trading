"""CLI: python scripts/count_ai_brain_signals.py [--csv path.csv ...]

Ronda 18 pre-flight tool. core/ai_brain.py (the AI signal filter) makes a
REAL OpenRouter API call once per bar where the deterministic strategy
already produced a signal (see core/engine.py: `if signal.side is None:
return` happens BEFORE the brain is ever consulted, and the brain call
itself happens BEFORE core/risk_manager.py sizes the trade - the brain can
only veto a candidate, RiskManager still has the final word on sizing).

A backtest harness that wants to measure "does the brain help?" the same
way engine.py invokes it (only on signal bars, never on every bar) still
needs one real API call per signal bar. Before writing that harness, this
script counts how many such signal bars exist in given CSVs, using
TODAY'S actual config (core/config.py + .env, exactly what core/engine.py
runs live with right now) - so the cost of a real run is a known number
BEFORE a single paid/rate-limited call is made, never after.

This script makes ZERO network calls and never touches core/ai_brain.py,
core/account_supervisor.py, core/risk_manager.py, or the live engine. It
only counts how many times core/backtest.py's replay loop would produce a
non-None strategy signal - the exact bars where engine.py would call the
brain.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from core.backtest import run_backtest  # noqa: E402
from core.config import load_settings  # noqa: E402
from core.risk_manager import SymbolSpec  # noqa: E402
from core.signals import build_strategy_from_settings  # noqa: E402


class _CountingStrategy:
    """Wraps any strategy object 1:1, just counts non-None signal.side
    events - the exact condition core/engine.py uses to decide whether to
    call core/ai_brain.py's OpenRouterBrain.evaluate()."""

    def __init__(self, inner):
        self._inner = inner
        self.signal_count = 0
        self.buy_count = 0
        self.sell_count = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def generate_signal(self, *args, **kwargs):
        sig = self._inner.generate_signal(*args, **kwargs)
        if sig.side:
            self.signal_count += 1
            if sig.side == "BUY":
                self.buy_count += 1
            else:
                self.sell_count += 1
        return sig


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv", nargs="+",
        default=[
            "data/gold_history_m15_train.csv", "data/gold_history_m15_test.csv",
            "data/gold_m1_7d_train.csv", "data/gold_m1_7d_test.csv",
        ],
        help="CSV files to count signal bars in (default: the four Rondas "
             "13+ train/test splits, M15 and M1).",
    )
    args = parser.parse_args()

    settings = load_settings()
    spec = SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=1.0,
                       volume_step=0.01, point=0.01, trade_tick_value=1.0)
    value_per_point_per_lot = spec.trade_tick_value / spec.point
    assumed_spread_price = settings.strat_max_spread_price / 2

    print(f"Config vigente (core/config.py + .env): TIMEFRAME={settings.timeframe}  "
          f"RISK_PER_TRADE_USD={settings.risk_per_trade_usd}  "
          f"STRAT_SL_ATR_MULTIPLE={settings.strat_sl_atr_multiple}  "
          f"STRAT_ENABLE_MA_GRID={settings.strat_enable_ma_grid}")
    print(f"AI_BRAIN_ENABLED={settings.ai_brain_enabled}  "
          f"AI_SUPERVISOR_ENABLED={settings.ai_supervisor_enabled}  "
          f"OPENROUTER_MODEL={settings.openrouter_model!r}")
    print()

    total = 0
    for path in args.csv:
        p = Path(path)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent / p
        candles = pd.read_csv(p)
        strategy = _CountingStrategy(build_strategy_from_settings(settings, value_per_point_per_lot))
        run_backtest(
            candles=candles, spec=spec, starting_balance=50.0, leverage=500,
            risk_per_trade_usd=settings.risk_per_trade_usd, min_tp_usd=settings.min_tp_usd,
            tp_levels=settings.tp_levels, assumed_spread_price=assumed_spread_price,
            strategy=strategy, precompute_indicators=True,
        )
        print(f"{path}: bars={len(candles)} signal_bars={strategy.signal_count} "
              f"(BUY={strategy.buy_count} SELL={strategy.sell_count})  "
              f"<- this many core/ai_brain.py calls a full-replay harness would make")
        total += strategy.signal_count

    print()
    print(f"TOTAL signal bars across all listed CSVs: {total}")
    print("Each one is one real core/ai_brain.py OpenRouter call in a full-replay")
    print("harness. Compare against the ~150-200 call budget before implementing")
    print("or running any harness that actually calls the API.")


if __name__ == "__main__":
    main()
