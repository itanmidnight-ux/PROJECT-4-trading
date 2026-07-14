"""
1-minute XAUUSD mean-reversion scalper with a multi-scale take-profit
ladder.

Honesty note (read this before tuning MIN_TP_USD down further): this
strategy trades short-term mean reversion at the extremes of a Bollinger
Band + RSI squeeze. It has no edge on pure noise - the edge, such as it
is, comes from fading statistically stretched short-term moves, filtered
so a trade is only taken when the expected move comfortably clears the
live spread plus a small buffer. There is no configuration of this file
that makes losses impossible; the spread/RSI/ATR filters exist to keep
the number of low-quality trades down, not to guarantee wins.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

import numpy as np
import pandas as pd

Side = Literal["BUY", "SELL"]


@dataclass
class TpLevel:
    distance_price: float   # distance from entry, in price units
    close_fraction: float   # fraction of the position to close at this level


@dataclass
class Signal:
    side: Optional[Side]
    sl_distance_price: float = 0.0
    tp_levels: list[TpLevel] = field(default_factory=list)
    reason: str = ""


def compute_indicators(df: pd.DataFrame, bb_period: int = 20, bb_std: float = 2.0,
                        rsi_period: int = 7, atr_period: int = 14) -> pd.DataFrame:
    """df must have columns: open, high, low, close (oldest -> newest)."""
    out = df.copy()

    mid = out["close"].rolling(bb_period).mean()
    std = out["close"].rolling(bb_period).std(ddof=0)
    out["bb_mid"] = mid
    out["bb_upper"] = mid + bb_std * std
    out["bb_lower"] = mid - bb_std * std

    delta = out["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / rsi_period, min_periods=rsi_period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / rsi_period, min_periods=rsi_period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["rsi"] = 100 - (100 / (1 + rs))
    out["rsi"] = out["rsi"].fillna(50)

    prev_close = out["close"].shift(1)
    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - prev_close).abs(),
        (out["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1 / atr_period, min_periods=atr_period, adjust=False).mean()

    return out


class ScalpStrategy:
    def __init__(
        self,
        min_tp_usd: float,
        tp_levels: int,
        value_per_point_per_lot: float,
        rsi_oversold: float = 25.0,
        rsi_overbought: float = 75.0,
        max_spread_price: float = 0.5,
        min_atr_price: float = 0.15,
        sl_atr_multiple: float = 1.2,
        cooldown_bars: int = 2,
        bb_period: int = 20,
        bb_std: float = 2.0,
        rsi_period: int = 7,
        atr_period: int = 14,
    ) -> None:
        self.min_tp_usd = min_tp_usd
        self.tp_levels = max(1, tp_levels)
        self.value_per_point_per_lot = value_per_point_per_lot
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.max_spread_price = max_spread_price
        self.min_atr_price = min_atr_price
        self.sl_atr_multiple = sl_atr_multiple
        self.cooldown_bars = cooldown_bars
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.atr_period = atr_period
        self._bars_since_last_trade = cooldown_bars
        # compute_indicators needs at least this many bars to warm up its
        # slowest rolling window (Bollinger); generate_signal enforces it.
        self._warmup_bars = max(bb_period, rsi_period, atr_period) + 5

    def on_bar_closed(self) -> None:
        self._bars_since_last_trade += 1

    def on_trade_opened(self) -> None:
        self._bars_since_last_trade = 0

    def min_tp_distance_for_lot(self, lot: float) -> float:
        """Price distance needed for the FIRST tp level to net >= min_tp_usd for this lot."""
        value_per_point = self.value_per_point_per_lot * lot
        if value_per_point <= 0:
            return float("inf")
        return self.min_tp_usd / value_per_point

    def build_tp_ladder(self, lot: float, spread_price: float) -> list[TpLevel]:
        """
        Multi-scale TP: first level locks in >= min_tp_usd as soon as
        possible (plus spread buffer), later levels scale out further out
        for the rest of the move. Fractions sum to 1.0.
        """
        base_distance = self.min_tp_distance_for_lot(lot) + spread_price
        n = self.tp_levels
        levels: list[TpLevel] = []
        # geometric spacing: 1x, 1.8x, 3x, ... of the base distance
        multipliers = [1.0 + 0.8 * i for i in range(n)]
        # front-load the fraction closed on the first (safest) level
        raw_fractions = [1.0 / (i + 1) for i in range(n)]
        total = sum(raw_fractions)
        fractions = [f / total for f in raw_fractions]
        for mult, frac in zip(multipliers, fractions):
            levels.append(TpLevel(distance_price=base_distance * mult, close_fraction=frac))
        return levels

    def generate_signal(self, df: pd.DataFrame, spread_price: float, lot_hint: float) -> Signal:
        if self._bars_since_last_trade < self.cooldown_bars:
            return Signal(side=None, reason="cooldown")

        if len(df) < self._warmup_bars:
            return Signal(side=None, reason="not enough history")

        ind = compute_indicators(df, bb_period=self.bb_period, bb_std=self.bb_std,
                                  rsi_period=self.rsi_period, atr_period=self.atr_period)
        last = ind.iloc[-1]

        if any(pd.isna(last[c]) for c in ("bb_upper", "bb_lower", "rsi", "atr")):
            return Signal(side=None, reason="indicators warming up")

        if spread_price > self.max_spread_price:
            return Signal(side=None, reason=f"spread too wide ({spread_price:.3f})")

        if last["atr"] < self.min_atr_price:
            return Signal(side=None, reason="volatility too low for target to clear spread")

        close = last["close"]
        side: Optional[Side] = None
        if close <= last["bb_lower"] and last["rsi"] <= self.rsi_oversold:
            side = "BUY"
        elif close >= last["bb_upper"] and last["rsi"] >= self.rsi_overbought:
            side = "SELL"

        if side is None:
            return Signal(side=None, reason="no setup")

        sl_distance = max(last["atr"] * self.sl_atr_multiple, self.min_tp_distance_for_lot(lot_hint) * 1.5)
        tp_levels = self.build_tp_ladder(lot_hint, spread_price)

        return Signal(side=side, sl_distance_price=sl_distance, tp_levels=tp_levels, reason="signal")
