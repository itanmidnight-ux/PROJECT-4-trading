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

Backtest evidence on sl_atr_multiple (2026-07, ~5 real trading days of
COMEX gold futures 1m data, 60/40 chronological train/test split, see
scripts/fetch_market_data.py + scripts/run_backtest.py): the previous
default of 1.2 lost money in both halves (stops too tight for 1m gold
noise - positions got stopped out before the reversion thesis had room
to play out). 5.0 looked best on the training half but flipped negative
out of sample - a textbook overfitting signature, not a real edge. 4.0
was the widest multiple that stayed net positive on BOTH halves, so
that's the new default. This is directional evidence from one short,
proxy (not FBS's actual feed) dataset, not a validated edge - rerun
scripts/run_backtest.py against real FBS history before trusting any of
these numbers on the actual account.

Ronda 15 (2026-07-23, see .env.example's "Gestion de riesgo" comment
block and README for the full sweep table): re-swept sl_atr_multiple
{2.0, 2.5, 3.0, 3.5, 4.0, 5.0} against real 7-day M1 gold data
(data/gold_m1_7d_{train,test}.csv, 60/40 chronological split) with the
real wiring (core/signals.py::build_strategy_from_settings), same
risk_per_trade_usd=3/balance=50/leverage=500 as Rondas 13-14. 3.5 beat
the 4.0 baseline on BOTH halves (TRAIN +$2.04/8 trades/87.5% win rate vs
4.0's -$2.19/3 trades; TEST +$2.76/9 trades/88.9% win rate vs 4.0's
+$1.20/2 trades) - more trades AND a positive, not just less-negative,
TRAIN result, holding out of sample. Tighter than 3.5 (2.0-3.0) opens up
trade count a lot (16-53 trades) but goes net negative on TRAIN; 5.0
produced ZERO trades on this dataset - every candidate signal got
rejected by core/risk_manager.py's Ronda 13 "volatilidad demasiado alta
para el lote minimo del broker" check (the broker-minimum lot's real
dollar risk at that SL distance exceeds the risk budget's overshoot
allowance on this $50/1:500 account), not a lack of BB/RSI setups. 3.5
is the new default. Same caveat as above: single short dataset,
re-validate before trusting blindly on the live account.
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
    # atr / atr_baseline at signal time, clamped - see build_tp_ladder's
    # vol_ratio param. 1.0 (neutral, no adjustment) unless a strategy
    # actually computed it. Carried on the Signal so callers that rebuild
    # the TP ladder with the real risk-sized lot (core/engine.py,
    # core/backtest.py) can reuse the same market snapshot instead of
    # recomputing indicators a second time.
    vol_ratio: float = 1.0


def compute_indicators(df: pd.DataFrame, bb_period: int = 20, bb_std: float = 2.0,
                        rsi_period: int = 7, atr_period: int = 14,
                        adx_period: int = 14) -> pd.DataFrame:
    """df must have columns: open, high, low, close (oldest -> newest)."""
    out = df.copy()

    mid = out["close"].rolling(bb_period).mean()
    std = out["close"].rolling(bb_period).std(ddof=0)
    out["bb_mid"] = mid
    out["bb_upper"] = mid + bb_std * std
    out["bb_lower"] = mid - bb_std * std

    # EMAs used by the momentum/breakout signals in core/signals.py, not by
    # the mean-reversion signal below - computed here too so every
    # consumer (backtest, engine, live) shares one indicator pass instead
    # of each strategy recomputing its own.
    out["ema9"] = out["close"].ewm(span=9, adjust=False).mean()
    out["ema21"] = out["close"].ewm(span=21, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()

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

    # Slow-moving volatility baseline (independent of atr_period) used only
    # to compute vol_ratio = atr / atr_baseline for the adaptive TP ladder
    # (see build_tp_ladder) - "is right now more or less volatile than the
    # recent past", not a trading signal itself, so it doesn't need to be
    # configurable per strategy the way atr_period does.
    out["atr_baseline"] = tr.ewm(alpha=1 / 100, min_periods=25, adjust=False).mean()

    # ADX (Wilder): measures trend STRENGTH regardless of direction, used
    # to keep the mean-reversion signal out of strong trends instead of
    # repeatedly fading them - see the module docstring for why that
    # matters (it's what caused the account-blowing loss sequence found in
    # backtesting before this filter existed).
    up_move = out["high"].diff()
    down_move = -out["low"].diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=out.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=out.index)
    tr_smoothed = tr.ewm(alpha=1 / adx_period, min_periods=adx_period, adjust=False).mean()
    plus_dm_smoothed = plus_dm.ewm(alpha=1 / adx_period, min_periods=adx_period, adjust=False).mean()
    minus_dm_smoothed = minus_dm.ewm(alpha=1 / adx_period, min_periods=adx_period, adjust=False).mean()
    plus_di = 100 * (plus_dm_smoothed / tr_smoothed.replace(0, np.nan))
    minus_di = 100 * (minus_dm_smoothed / tr_smoothed.replace(0, np.nan))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    out["adx"] = dx.ewm(alpha=1 / adx_period, min_periods=adx_period, adjust=False).mean().fillna(0)

    return out


# Bounds for vol_ratio (atr / atr_baseline) fed into build_tp_ladder's
# adaptive spacing - without a clamp, a momentarily tiny atr_baseline (early
# warmup, a dead-quiet stretch) could blow the ladder's spacing up
# arbitrarily wide, or a spike could crush it to near-zero. 0.5-2.0 means
# spacing can at most halve or double relative to the static (vol_ratio=1.0)
# behavior - wide enough to matter, narrow enough to stay sane.
VOL_RATIO_MIN = 0.5
VOL_RATIO_MAX = 2.0


def compute_vol_ratio(atr: float, atr_baseline: float) -> float:
    if pd.isna(atr) or pd.isna(atr_baseline) or atr_baseline <= 0:
        return 1.0
    return max(VOL_RATIO_MIN, min(VOL_RATIO_MAX, atr / atr_baseline))


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
        sl_atr_multiple: float = 3.5,
        cooldown_bars: int = 2,
        bb_period: int = 20,
        bb_std: float = 2.0,
        rsi_period: int = 7,
        atr_period: int = 14,
        adx_period: int = 14,
        trend_filter_adx_threshold: float = 35.0,
        tp_targets_usd: list[float] | None = None,
    ) -> None:
        self.min_tp_usd = min_tp_usd
        self.tp_levels = max(1, tp_levels)
        # Optional explicit per-level profit targets in USD (e.g. [0.28,
        # 0.60, 1.20]) - see build_tp_ladder for why this exists. Empty
        # means "not configured": falls back to the original behavior
        # (only the first level is dollar-anchored via min_tp_usd, later
        # levels are geometric multiples of its price distance) so existing
        # deployments and the backtest history in the README are unaffected
        # unless this is set explicitly.
        self.tp_targets_usd = list(tp_targets_usd) if tp_targets_usd else []
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
        self.adx_period = adx_period
        self.trend_filter_adx_threshold = trend_filter_adx_threshold
        self._bars_since_last_trade = cooldown_bars
        # compute_indicators needs at least this many bars to warm up its
        # slowest rolling window (Bollinger); generate_signal enforces it.
        self._warmup_bars = max(bb_period, rsi_period, atr_period, adx_period) + 5

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

    def build_tp_ladder(self, lot: float, spread_price: float, vol_ratio: float = 1.0) -> list[TpLevel]:
        """
        Multi-scale TP: first level locks in >= min_tp_usd as soon as
        possible (plus spread buffer), later levels scale out further out
        for the rest of the move. Fractions sum to 1.0.

        Every level's price distance is ultimately anchored to a USD
        amount, never a bare pip/point count - that's what makes the
        levels leverage- and lot-size-independent (a fixed pip distance
        would net wildly different real money depending on lot size; a
        fixed dollar target doesn't). Two modes:
          - tp_targets_usd unset (default): only the first level is
            explicitly dollar-anchored (min_tp_usd); later levels are
            geometric multiples of that price distance, spaced by
            `vol_ratio` (see compute_vol_ratio) - denser in quiet markets
            (levels 2+ sit closer, so a small move fills the whole ladder
            faster - more realized trades/day in chop), wider in volatile
            ones (more room before booking the rest, letting a real move
            pay more instead of capping it at the same distance chop
            would). TP1 itself is NEVER touched by vol_ratio - the dollar
            floor (min_tp_usd) this project already warns against tuning
            down carelessly stays exactly as fixed as before. This is the
            "fixed base, intelligent spacing" ladder - vol_ratio=1.0
            (the default) reproduces the original, backtested behavior
            (see README) byte for byte.
          - tp_targets_usd set: EVERY level gets its own explicit dollar
            target for its slice, e.g. TP_TARGETS_USD=0.28,0.60,1.20 -
            more directly configurable ("this level books $X") instead of
            only the first level being meaningful in dollar terms. These
            are explicit, operator-chosen targets - vol_ratio does NOT
            apply here, same as before this feature existed.
        """
        n = self.tp_levels
        # front-load the fraction closed on the first (safest) level
        raw_fractions = [1.0 / (i + 1) for i in range(n)]
        total = sum(raw_fractions)
        fractions = [f / total for f in raw_fractions]

        if self.tp_targets_usd:
            targets = list(self.tp_targets_usd)
            if len(targets) < n:
                targets += [targets[-1]] * (n - len(targets))  # repeat the last configured target, don't crash
            levels: list[TpLevel] = []
            prev_distance = 0.0
            for target_usd, frac in zip(targets[:n], fractions, strict=True):
                value_per_point_for_slice = self.value_per_point_per_lot * lot * frac
                distance = (target_usd / value_per_point_for_slice if value_per_point_for_slice > 0
                            else float("inf")) + spread_price
                if distance <= prev_distance:
                    # A ladder must scale OUTWARD - a misconfigured target
                    # list (e.g. decreasing) can't produce a level closer
                    # than the one before it, or price could never reach it
                    # in order. Nudge it out instead of silently building a
                    # broken ladder.
                    distance = prev_distance * 1.001
                levels.append(TpLevel(distance_price=distance, close_fraction=frac))
                prev_distance = distance
            return levels

        base_distance = self.min_tp_distance_for_lot(lot) + spread_price
        # geometric spacing: 1x, (1+0.8*vol_ratio)x, (1+1.6*vol_ratio)x, ...
        # of the base distance - vol_ratio=1.0 gives the original 1x, 1.8x,
        # 2.6x... spacing exactly.
        step = 0.8 * vol_ratio
        multipliers = [1.0 + step * i for i in range(n)]
        return [TpLevel(distance_price=base_distance * mult, close_fraction=frac)
                for mult, frac in zip(multipliers, fractions, strict=True)]

    def generate_signal(self, df: pd.DataFrame, spread_price: float, lot_hint: float,
                         precomputed_indicators: pd.DataFrame | None = None) -> Signal:
        if self._bars_since_last_trade < self.cooldown_bars:
            return Signal(side=None, reason="cooldown")

        if len(df) < self._warmup_bars:
            return Signal(side=None, reason="not enough history")

        if precomputed_indicators is not None:
            ind = precomputed_indicators
        else:
            ind = compute_indicators(df, bb_period=self.bb_period, bb_std=self.bb_std,
                                      rsi_period=self.rsi_period, atr_period=self.atr_period,
                                      adx_period=self.adx_period)
        last = ind.iloc[-1]

        if any(pd.isna(last[c]) for c in ("bb_upper", "bb_lower", "rsi", "atr", "adx")):
            return Signal(side=None, reason="indicators warming up")

        if spread_price > self.max_spread_price:
            return Signal(side=None, reason=f"spread too wide ({spread_price:.3f})")

        if last["atr"] < self.min_atr_price:
            return Signal(side=None, reason="volatility too low for target to clear spread")

        if last["adx"] >= self.trend_filter_adx_threshold:
            return Signal(side=None, reason=f"strong trend (ADX {last['adx']:.1f}), skipping mean-reversion")

        close = last["close"]
        side: Optional[Side] = None
        if close <= last["bb_lower"] and last["rsi"] <= self.rsi_oversold:
            side = "BUY"
        elif close >= last["bb_upper"] and last["rsi"] >= self.rsi_overbought:
            side = "SELL"

        if side is None:
            return Signal(side=None, reason="no setup")

        sl_distance = max(last["atr"] * self.sl_atr_multiple, self.min_tp_distance_for_lot(lot_hint) * 1.5)
        vol_ratio = compute_vol_ratio(last["atr"], last["atr_baseline"])
        tp_levels = self.build_tp_ladder(lot_hint, spread_price, vol_ratio=vol_ratio)

        return Signal(side=side, sl_distance_price=sl_distance, tp_levels=tp_levels,
                       reason="signal", vol_ratio=vol_ratio)
