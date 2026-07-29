"""Ronda 49: busca una estrategia SIMPLE (una entrada, un SL, un TP fijo en
multiplo de R, sin escalera de TP, sin grid, sin recovery) que generalice
train->test bajo el spread real medido en Ronda 48 (0.45), pedido explicito
del dueno del proyecto: "la mejor estrategia es muy simple y facil de
repetir".

No hay CSV historico ni bridge MT5 disponible en este entorno (contenedor
cloud, sin Wine/MetaTrader5) - los datos de las Rondas 1-48 vivian solo en
la maquina local del dueno y estan en .gitignore. Para poder medir algo
REAL (no sintetico) se usa scripts/fetch_market_data.py, que baja futuros
de oro COMEX (GC=F, Yahoo Finance) - un proxy liquido correlacionado con
XAUUSD spot pero NO el feed de FBS. Sirve para juzgar si un patron tiene
estructura real en el mercado (no ruido), no para prometer el numero exacto
en la cuenta real - ver README/fetch_market_data.py.

Cada candidato reusa el `check()`/`generate_signal()` YA implementado y
gateado (BB/RSI extremo, spread max, ATR minimo, cooldown) en
core/signals.py o core/strategy.py - no se reinventa la deteccion de
patrones, solo se reemplaza su salida (una escalera de TP en dolares) por
UN solo TP a un multiplo R fijo de su propio SL estructural. Esto se corre
a traves de core/backtest.py::run_backtest sin tocarlo, para heredar el
mismo modelo de fill/spread/margen/riesgo que ya usaron las Rondas 1-48.

Metodologia identica a Rondas 13-48: split cronologico 60/40 train/test,
decision solo si mejora en AMBAS mitades, documentado incluso si el
resultado es negativo.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from core.backtest import run_backtest  # noqa: E402
from core.risk_manager import SymbolSpec  # noqa: E402
from core.strategy import ScalpStrategy, Signal, TpLevel, compute_indicators  # noqa: E402
from core.signals import (  # noqa: E402
    EngulfingReversalStrategy,
    MicroRangeBreakoutStrategy,
    MomentumCrossStrategy,
    PinBarReversalStrategy,
    TightPinBarReversalStrategy,
)


class SingleShotStrategy:
    """Adaptador: envuelve un sub-signal de core/signals.py (interfaz
    `check(df, ind, spread) -> SubSignal`) o el mean-reversion base de
    core/strategy.py (interfaz `generate_signal(...) -> Signal`) en una
    estrategia standalone con UN solo nivel de TP = tp_r_multiple * SL
    estructural (mas el spread, igual que build_tp_ladder original) y
    close_fraction=1.0 - cero escalera, cero breakeven parcial, cero grid.
    Esa es la parte "simple y facil de repetir": una regla de entrada, una
    regla de salida, sin parametros ocultos de gestion de posicion.
    """

    def __init__(self, sub, tp_r_multiple: float, sl_atr_multiple_for_grid: float = 2.0):
        self.sub = sub
        self.tp_r_multiple = tp_r_multiple
        # Solo lo lee _build_grid_leg si grid_enabled=True (no lo esta acá)
        self.sl_atr_multiple = sl_atr_multiple_for_grid
        self._last_sl_distance = 0.0
        self._uses_check = hasattr(sub, "check")
        # compute_indicators() se corre UNA vez para toda la corrida (ver
        # run_backtest, precompute_indicators=True) con estos periodos -
        # los mismos defaults de ScalpStrategy que todo core/signals.py ya
        # asume como la unica fuente compartida de BB/RSI/ATR/ADX.
        self.bb_period, self.bb_std = 20, 2.0
        self.rsi_period, self.atr_period, self.adx_period = 7, 14, 14

    def on_bar_closed(self) -> None:
        self.sub.on_bar_closed()

    def on_trade_opened(self) -> None:
        self.sub.on_trade_opened()

    def generate_signal(self, df, spread_price, lot_hint, precomputed_indicators=None, **_kw):
        if self._uses_check:
            ind = precomputed_indicators if precomputed_indicators is not None else compute_indicators(df)
            sub_signal = self.sub.check(df, ind, spread_price)
            side, sl_distance, reason = sub_signal.side, sub_signal.sl_distance_price, sub_signal.reason
        else:
            sig = self.sub.generate_signal(df, spread_price, lot_hint, precomputed_indicators=precomputed_indicators)
            side, sl_distance, reason = sig.side, sig.sl_distance_price, sig.reason
        if side is None:
            return Signal(side=None, reason=reason)
        self._last_sl_distance = sl_distance
        return Signal(side=side, sl_distance_price=sl_distance, reason=reason)

    def build_tp_ladder(self, lot, spread_price, vol_ratio: float = 1.0):
        tp_distance = self._last_sl_distance * self.tp_r_multiple + spread_price
        return [TpLevel(distance_price=tp_distance, close_fraction=1.0)]

    def min_tp_distance_for_lot(self, lot):
        return self._last_sl_distance * self.tp_r_multiple


SPEC = SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=1.0,
                   volume_step=0.01, point=0.01, trade_tick_value=1.0)

STARTING_BALANCE = 100.0
LEVERAGE = 500
RISK_PER_TRADE_USD = 5.0
SPREAD = 0.45  # real medido Ronda 48, no el 0.25 usado sin verificar en Rondas 13-47


def configure(balance: float, risk_usd: float) -> None:
    global STARTING_BALANCE, RISK_PER_TRADE_USD
    STARTING_BALANCE = balance
    RISK_PER_TRADE_USD = risk_usd


def make_candidates(tp_r: float) -> dict[str, object]:
    base = ScalpStrategy(min_tp_usd=0.60, tp_levels=8, value_per_point_per_lot=100.0,
                          sl_atr_multiple=3.5)
    return {
        "mean_reversion_bbrsi": SingleShotStrategy(base, tp_r, sl_atr_multiple_for_grid=3.5),
        "momentum_cross_ema9_21": SingleShotStrategy(
            MomentumCrossStrategy(cooldown_bars=2, max_spread_price=0.5, min_atr_price=0.15, sl_atr_multiple=2.0),
            tp_r, sl_atr_multiple_for_grid=2.0),
        "engulfing_at_extreme": SingleShotStrategy(
            EngulfingReversalStrategy(cooldown_bars=2, max_spread_price=0.5, min_atr_price=0.15),
            tp_r),
        "pin_bar_at_extreme": SingleShotStrategy(
            PinBarReversalStrategy(cooldown_bars=2, max_spread_price=0.5, min_atr_price=0.15),
            tp_r),
        "tight_pin_bar": SingleShotStrategy(
            TightPinBarReversalStrategy(cooldown_bars=2, max_spread_price=0.5, min_atr_price=0.15),
            tp_r),
        "micro_range_breakout": SingleShotStrategy(
            MicroRangeBreakoutStrategy(cooldown_bars=2, max_spread_price=0.5, min_atr_price=0.15),
            tp_r),
    }


def split(df: pd.DataFrame, frac: float = 0.6):
    n = int(len(df) * frac)
    return df.iloc[:n].reset_index(drop=True), df.iloc[n:].reset_index(drop=True)


@dataclass
class Row:
    name: str
    tp_r: float
    half: str
    trades: int
    win_rate: float
    pnl: float
    avg_pnl_trade: float
    max_dd: float


def run_one(name, strategy, candles) -> tuple:
    result = run_backtest(
        candles=candles, spec=SPEC, starting_balance=STARTING_BALANCE, leverage=LEVERAGE,
        risk_per_trade_usd=RISK_PER_TRADE_USD, min_tp_usd=0.60, tp_levels=1,
        assumed_spread_price=SPREAD, strategy=strategy, precompute_indicators=True,
    )
    avg = result.total_pnl / result.trades if result.trades else 0.0
    return result, avg


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--label", default="")
    parser.add_argument("--tp-r", type=float, nargs="+", default=[1.0, 1.5, 2.0])
    parser.add_argument("--only", nargs="*", default=None, help="subset of candidate names")
    parser.add_argument("--balance", type=float, default=100.0)
    parser.add_argument("--risk-usd", type=float, default=5.0)
    args = parser.parse_args()
    configure(args.balance, args.risk_usd)

    candles = pd.read_csv(args.csv)
    train, test = split(candles)
    print(f"=== {args.label or args.csv} === total={len(candles)} train={len(train)} test={len(test)} "
          f"spread={SPREAD} balance={STARTING_BALANCE} risk_usd={RISK_PER_TRADE_USD}")

    rows: list[Row] = []
    for tp_r in args.tp_r:
        candidates = make_candidates(tp_r)
        names = args.only if args.only else list(candidates.keys())
        for name in names:
            for half_name, half_df in (("TRAIN", train), ("TEST", test)):
                strategy = make_candidates(tp_r)[name]  # fresh instance per half (no state leak)
                result, avg = run_one(name, strategy, half_df)
                rows.append(Row(name, tp_r, half_name, result.trades, result.win_rate * 100,
                                 result.total_pnl, avg, result.max_drawdown_pct))

    print(f"{'strategy':<24}{'tp_r':>6}{'half':>7}{'trades':>8}{'win%':>8}{'pnl':>10}{'avg/trd':>10}{'maxDD%':>8}")
    for r in rows:
        print(f"{r.name:<24}{r.tp_r:>6.1f}{r.half:>7}{r.trades:>8}{r.win_rate:>8.1f}{r.pnl:>10.2f}"
              f"{r.avg_pnl_trade:>10.3f}{r.max_dd:>8.1f}")


if __name__ == "__main__":
    main()
