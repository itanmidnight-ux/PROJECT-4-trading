"""Faithful port of the 12-vote confirmation system from the user-provided
"QuantumQueenPro_M1.mq5" EA (base timeframe M1, filter M15, macro H1 - the
same mapping the user's own file uses, not the M1/M5 shift the earlier,
partial adaptation in core/signals.py used for 5 of these ideas).

This is DELIBERATELY a separate module from core/signals.py's
MomentumCrossStrategy/RsiHysteresisStrategy/etc. Those 5 classes are
already backtested and documented (README "Ronda 4"/"Ronda 9") as
independent, OR-combined signals - changing their internals would silently
invalidate those numbers. This module instead faithfully reproduces the
source EA's OWN aggregation method: sum 12 directional votes (S01-S12),
require |sum| >= a threshold (2 in the source), and require at least one
vote from the "confirmed" bucket (S01, S05, S07, S09, S12 - the ones that
use a higher-timeframe filter, not pure M1 noise) - exactly
GetSignal()'s logic in the source file, translated 1:1.

What is NOT ported, on the same grounds as core/signals.py's module
docstring, and for the same real reason (README "Ronda 2"): the source
EA's risk model is a grid that averages into a loser with NO per-order
stop-loss and a single take-profit on the whole basket's weighted-average
price. Every vote here instead goes through core.risk_manager.RiskManager
exactly like every other signal in this project - a real, sized stop-loss,
never an averaged-down grid leg.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from core.signals import CooldownGate, SubSignal, _ema_cross, resample_m1_to_tf


def _closed_htf_frame(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Same 'closed bars only' principle as core.signals._last_closed_m15_bar,
    generalized to any higher timeframe built by resampling M1 - drops the
    last resampled bucket if the most recent M1 bar isn't that bucket's
    final minute, so slope/alignment reads never repaint mid-bar."""
    htf = resample_m1_to_tf(df, minutes)
    if htf.empty or len(df) < 1:
        return htf
    last_m1_time = int(df.iloc[-1]["time"])
    seconds_per_block = minutes * 60
    minute_in_block = (last_m1_time % seconds_per_block) // 60
    if minute_in_block != minutes - 1:
        htf = htf.iloc[:-1]
    return htf


@dataclass
class _HtfContext:
    """Precomputed once per bar and handed to every vote function - mirrors
    the source EA's RefreshIndicators(), which reads every timeframe's
    indicators exactly once per M1 bar instead of each strategy redoing it."""
    m1_ok: bool
    m15_ok: bool
    h1_ok: bool
    ema9_m15: Optional[pd.Series] = None
    ema21_m15: Optional[pd.Series] = None
    ema50_m15: Optional[pd.Series] = None
    ema50_h1: Optional[pd.Series] = None


def _build_htf_context(df: pd.DataFrame) -> _HtfContext:
    m15 = _closed_htf_frame(df, 15)
    h1 = _closed_htf_frame(df, 60)

    m15_ok = len(m15) >= 3
    h1_ok = len(h1) >= 3

    ctx = _HtfContext(m1_ok=True, m15_ok=m15_ok, h1_ok=h1_ok)
    if m15_ok:
        ctx.ema9_m15 = m15["close"].ewm(span=9, adjust=False).mean()
        ctx.ema21_m15 = m15["close"].ewm(span=21, adjust=False).mean()
        ctx.ema50_m15 = m15["close"].ewm(span=50, adjust=False).mean()
    if h1_ok:
        ctx.ema50_h1 = h1["close"].ewm(span=50, adjust=False).mean()
    return ctx


# ----------------------------------------------------------------------
# S01-S12: each returns -1 (SELL vote), 0 (no vote) or 1 (BUY vote).
# `ind` is core.strategy.compute_indicators(df) - already has ema9/21/50,
# rsi, atr on the M1 series. `df` is raw M1 OHLC, oldest->newest.
# ----------------------------------------------------------------------

def _s01_m1_cross_m15_slope(ind: pd.DataFrame, ctx: _HtfContext) -> int:
    """S01: M1 EMA9/21 cross + M15 EMA50 slope filter."""
    if not ctx.m15_ok:
        return 0
    cross = _ema_cross(ind)
    if cross is None or ctx.ema50_m15 is None or len(ctx.ema50_m15) < 2:
        return 0
    rising = ctx.ema50_m15.iloc[-1] > ctx.ema50_m15.iloc[-2]
    if cross == "BUY" and rising:
        return 1
    if cross == "SELL" and not rising:
        return -1
    return 0


def _s02_rsi_hysteresis(ind: pd.DataFrame, upper: float = 52.0, lower: float = 48.0) -> int:
    """S02: M1 RSI crossing 50 with 52/48 hysteresis."""
    if len(ind) < 3:
        return 0
    prev, cur = ind.iloc[-3]["rsi"], ind.iloc[-2]["rsi"]
    if pd.isna(prev) or pd.isna(cur):
        return 0
    if prev < lower and cur >= upper:
        return 1
    if prev > upper and cur <= lower:
        return -1
    return 0


def _s03_strong_candle_m15_aligned(df: pd.DataFrame, ind: pd.DataFrame, ctx: _HtfContext) -> int:
    """S03: strong directional M1 candle, WITH the source's own documented
    fix applied (its comment: a strong candle with no trend filter traded
    against the higher trend just as often as with it - now requires M15
    EMA9/21 alignment matching the candle's direction)."""
    if len(df) < 2 or not ctx.m15_ok or ctx.ema9_m15 is None or ctx.ema21_m15 is None:
        return 0
    last = ind.iloc[-1]
    atr = last["atr"]
    if pd.isna(atr) or atr <= 0:
        return 0
    candle = df.iloc[-2]
    rng = candle["high"] - candle["low"]
    if rng <= 0 or rng < atr * 0.8:
        return 0
    body = candle["close"] - candle["open"]
    if abs(body) < rng * 0.5:
        return 0
    m15_bull = ctx.ema9_m15.iloc[-1] > ctx.ema21_m15.iloc[-1]
    m15_bear = ctx.ema9_m15.iloc[-1] < ctx.ema21_m15.iloc[-1]
    if body > 0 and (candle["close"] - candle["low"]) > rng * 0.6 and m15_bull:
        return 1
    if body < 0 and (candle["high"] - candle["close"]) > rng * 0.6 and m15_bear:
        return -1
    return 0


def _s04_range_scalp(ind: pd.DataFrame) -> int:
    """S04: low-ATR range scalp + RSI momentum shift."""
    if len(ind) < 3:
        return 0
    last = ind.iloc[-1]
    atr, atr_baseline = last["atr"], last["atr_baseline"]
    if pd.isna(atr) or pd.isna(atr_baseline) or atr_baseline <= 0:
        return 0
    if atr >= atr_baseline * 0.8:
        return 0
    prev, cur = ind.iloc[-3]["rsi"], ind.iloc[-2]["rsi"]
    if pd.isna(prev) or pd.isna(cur):
        return 0
    if cur > 55.0 and prev <= 50.0:
        return 1
    if cur < 45.0 and prev >= 50.0:
        return -1
    return 0


def _s05_pullback_ema21(df: pd.DataFrame, ind: pd.DataFrame, ctx: _HtfContext) -> int:
    """S05 (confirmed-bucket): M1 pullback to its own EMA21, in the
    direction of the M15 trend (EMA9>EMA21>EMA50 bullish, reverse bearish)."""
    if len(df) < 2 or not ctx.m15_ok or ctx.ema9_m15 is None or ctx.ema21_m15 is None or ctx.ema50_m15 is None:
        return 0
    last = ind.iloc[-2]
    if pd.isna(last["ema21"]):
        return 0
    lo1, hi1, cl1 = df.iloc[-2]["low"], df.iloc[-2]["high"], df.iloc[-2]["close"]
    ema = last["ema21"]
    m15_bull = ctx.ema9_m15.iloc[-1] > ctx.ema21_m15.iloc[-1] > ctx.ema50_m15.iloc[-1]
    m15_bear = ctx.ema9_m15.iloc[-1] < ctx.ema21_m15.iloc[-1] < ctx.ema50_m15.iloc[-1]
    if m15_bull and lo1 <= ema and cl1 > ema:
        return 1
    if m15_bear and hi1 >= ema and cl1 < ema:
        return -1
    return 0


def _s06_london_open(df: pd.DataFrame, ind: pd.DataFrame, start_hour: int = 7, end_hour: int = 9) -> int:
    """S06: M1 EMA9/21 cross during the London open window (07:30-09:30
    UTC in the source; approximated here to the closed hour boundary the
    project already uses elsewhere - see start/end hour params)."""
    if len(df) < 2:
        return 0
    ts = pd.to_datetime(int(df.iloc[-2]["time"]), unit="s")
    in_window = (ts.hour == start_hour and ts.minute >= 30) or (start_hour < ts.hour < end_hour) or (ts.hour == end_hour and ts.minute < 30)
    if not in_window:
        return 0
    cross = _ema_cross(ind)
    if cross == "BUY":
        return 1
    if cross == "SELL":
        return -1
    return 0


def _s07_newyork_trend(df: pd.DataFrame, ind: pd.DataFrame, ctx: _HtfContext,
                        start_hour: int = 12, end_hour: int = 16) -> int:
    """S07 (confirmed-bucket): New York session window, M15 trend AND M1
    alignment both required (no cross event needed - continuation read)."""
    if len(df) < 2 or not ctx.m15_ok or ctx.ema9_m15 is None or ctx.ema21_m15 is None:
        return 0
    ts = pd.to_datetime(int(df.iloc[-2]["time"]), unit="s")
    if not (start_hour <= ts.hour < end_hour):
        return 0
    last = ind.iloc[-2]
    if pd.isna(last["ema9"]) or pd.isna(last["ema21"]) or pd.isna(last["ema50"]):
        return 0
    m15_bull = ctx.ema9_m15.iloc[-1] > ctx.ema21_m15.iloc[-1]
    m15_bear = ctx.ema9_m15.iloc[-1] < ctx.ema21_m15.iloc[-1]
    m1_bull = last["ema9"] > last["ema21"] > last["ema50"]
    m1_bear = last["ema9"] < last["ema21"] < last["ema50"]
    if m15_bull and m1_bull:
        return 1
    if m15_bear and m1_bear:
        return -1
    return 0


def _s08_asian_breakout(df: pd.DataFrame, ind: pd.DataFrame,
                         range_start: int = 0, range_end: int = 7,
                         breakout_start: int = 7, breakout_end: int = 10,
                         buffer_pct: float = 0.05) -> int:
    """S08: confirmed close beyond the Asian session's (00:00-07:00 UTC)
    high/low during the 07:00-10:00 breakout window. Recomputes the range
    from whatever M1 history is in `df` each call (stateless, unlike
    core.signals.AsianRangeBreakoutStrategy's incremental version) - fine
    for a backtest window, matches its result on the same data."""
    if len(df) < 2:
        return 0
    ts = pd.to_datetime(int(df.iloc[-2]["time"]), unit="s")
    if not (breakout_start <= ts.hour < breakout_end):
        return 0
    times = pd.to_datetime(df["time"], unit="s")
    today = ts.normalize()
    same_day = df[times.dt.normalize() == today]
    in_range = same_day[times.loc[same_day.index].dt.hour.between(range_start, range_end - 1)]
    if in_range.empty:
        return 0
    rng_high, rng_low = float(in_range["high"].max()), float(in_range["low"].min())
    if rng_high <= rng_low:
        return 0
    buf = (rng_high - rng_low) * buffer_pct
    close = df.iloc[-2]["close"]
    if close > rng_high + buf:
        return 1
    if close < rng_low - buf:
        return -1
    return 0


def _s09_triple_alignment(ctx: _HtfContext, ind: pd.DataFrame) -> int:
    """S09 (confirmed-bucket): H1 EMA50 slope + M15 EMA50 slope + M1
    EMA9/21 cross, all three aligned - the source's strictest filter."""
    if not ctx.h1_ok or not ctx.m15_ok or ctx.ema50_h1 is None or ctx.ema50_m15 is None:
        return 0
    if len(ctx.ema50_h1) < 2 or len(ctx.ema50_m15) < 2:
        return 0
    h1_up = ctx.ema50_h1.iloc[-1] > ctx.ema50_h1.iloc[-2]
    h1_down = ctx.ema50_h1.iloc[-1] < ctx.ema50_h1.iloc[-2]
    m15_up = ctx.ema50_m15.iloc[-1] > ctx.ema50_m15.iloc[-2]
    m15_down = ctx.ema50_m15.iloc[-1] < ctx.ema50_m15.iloc[-2]
    cross = _ema_cross(ind)
    if h1_up and m15_up and cross == "BUY":
        return 1
    if h1_down and m15_down and cross == "SELL":
        return -1
    return 0


def _s10_volatility_expansion(ind: pd.DataFrame, ctx: _HtfContext) -> int:
    """S10: M1 ATR expansion (>1.2x baseline) + M1 EMA cross + M15
    EMA9-vs-EMA50 position filter."""
    if not ctx.m15_ok or ctx.ema9_m15 is None or ctx.ema50_m15 is None:
        return 0
    last = ind.iloc[-1]
    atr, atr_baseline = last["atr"], last["atr_baseline"]
    if pd.isna(atr) or pd.isna(atr_baseline) or atr_baseline <= 0:
        return 0
    if atr <= atr_baseline * 1.2:
        return 0
    cross = _ema_cross(ind)
    if cross is None:
        return 0
    m15_above = ctx.ema9_m15.iloc[-1] > ctx.ema50_m15.iloc[-1]
    m15_below = ctx.ema9_m15.iloc[-1] < ctx.ema50_m15.iloc[-1]
    if cross == "BUY" and m15_above:
        return 1
    if cross == "SELL" and m15_below:
        return -1
    return 0


def _s11_swing_structure(df: pd.DataFrame, ind: pd.DataFrame, ctx: _HtfContext) -> int:
    """S11: 3-swing HH/LL structure on M1 bars 1/4/7 (the source's OWN
    documented fix - the original 1/3/5 spacing was pure M1 noise) + RSI
    bias + M15 alignment requirement (also the source's own fix)."""
    if len(df) < 8 or not ctx.m15_ok or ctx.ema9_m15 is None or ctx.ema21_m15 is None:
        return 0
    h1, h4, h7 = df.iloc[-2]["high"], df.iloc[-5]["high"], df.iloc[-8]["high"]
    l1, l4, l7 = df.iloc[-2]["low"], df.iloc[-5]["low"], df.iloc[-8]["low"]
    rsi = ind.iloc[-2]["rsi"]
    if pd.isna(rsi):
        return 0
    m15_bull = ctx.ema9_m15.iloc[-1] > ctx.ema21_m15.iloc[-1]
    m15_bear = ctx.ema9_m15.iloc[-1] < ctx.ema21_m15.iloc[-1]
    if h1 > h4 > h7 and rsi > 52.0 and m15_bull:
        return 1
    if l1 < l4 < l7 and rsi < 48.0 and m15_bear:
        return -1
    return 0


def _s12_m15_cross_m1_confirm(ind: pd.DataFrame, ctx: _HtfContext) -> int:
    """S12 (confirmed-bucket): EMA9/21 cross on M15 itself (not M1),
    confirmed by M1 EMA9/21 alignment - distinct from S01, which crosses
    on M1 and only reads M15's slope."""
    if not ctx.m15_ok or ctx.ema9_m15 is None or ctx.ema21_m15 is None or len(ctx.ema9_m15) < 3:
        return 0
    prev9, prev21 = ctx.ema9_m15.iloc[-3], ctx.ema21_m15.iloc[-3]
    cur9, cur21 = ctx.ema9_m15.iloc[-2], ctx.ema21_m15.iloc[-2]
    last = ind.iloc[-1]
    if pd.isna(last["ema9"]) or pd.isna(last["ema21"]):
        return 0
    buy_x = prev9 < prev21 and cur9 >= cur21
    sel_x = prev9 > prev21 and cur9 <= cur21
    if buy_x and last["ema9"] > last["ema21"]:
        return 1
    if sel_x and last["ema9"] < last["ema21"]:
        return -1
    return 0


# Index into the 12-vote tuple, matching the source EA's own
# `confirmedIdx[5]={0,4,6,8,11}` (S01,S05,S07,S09,S12 - the ones with a
# higher-timeframe filter, as opposed to pure M1-only reads).
_CONFIRMED_BUCKET = (0, 4, 6, 8, 11)

_VOTE_NAMES = (
    "S01_EMA_Cross_M15Slope", "S02_RSI_50Cross", "S03_Strong_Candle_M15Aligned",
    "S04_Range_Scalp", "S05_Pullback_EMA21", "S06_London_Open",
    "S07_NewYork_Trend", "S08_Asian_Breakout", "S09_Triple_EMA_H1M15M1",
    "S10_Volatility_Expansion", "S11_Swing_Structure", "S12_M15_EMA_Cross",
)


def compute_votes(df: pd.DataFrame, ind: pd.DataFrame, mask: int = 4095) -> tuple[int, ...]:
    """Returns the 12 raw votes (-1/0/1), each masked off (forced to 0) if
    its bit isn't set in `mask` - same bit layout as the source EA's
    InpStrategyMask (bit 0 = S01 ... bit 11 = S12)."""
    ctx = _build_htf_context(df)
    raw = (
        _s01_m1_cross_m15_slope(ind, ctx),
        _s02_rsi_hysteresis(ind),
        _s03_strong_candle_m15_aligned(df, ind, ctx),
        _s04_range_scalp(ind),
        _s05_pullback_ema21(df, ind, ctx),
        _s06_london_open(df, ind),
        _s07_newyork_trend(df, ind, ctx),
        _s08_asian_breakout(df, ind),
        _s09_triple_alignment(ctx, ind),
        _s10_volatility_expansion(ind, ctx),
        _s11_swing_structure(df, ind, ctx),
        _s12_m15_cross_m1_confirm(ind, ctx),
    )
    return tuple(v if (mask & (1 << i)) else 0 for i, v in enumerate(raw))


class QuantumQueenVoteStrategy(CooldownGate):
    """Faithful port of the source EA's GetSignal(): sums the 12 votes
    above, fires only if |sum| >= threshold AND at least one vote in the
    confirmed bucket (S01/S05/S07/S09/S12) agrees with the summed
    direction - exactly the source's own confluence rule, meant to reduce
    the "noise votes fire alone" case the raw OR-composite in
    core/signals.py doesn't guard against.

    Risk model deliberately NOT ported (see module docstring): stop-loss
    is a real ATR multiple through RiskManager, never SL=0 grid averaging.
    """

    def __init__(self, cooldown_bars: int, max_spread_price: float, min_atr_price: float,
                 sl_atr_multiple: float = 2.0, mask: int = 4095, threshold: int = 2) -> None:
        super().__init__(cooldown_bars)
        self.max_spread_price = max_spread_price
        self.min_atr_price = min_atr_price
        self.sl_atr_multiple = sl_atr_multiple
        self.mask = mask
        self.threshold = threshold

    def check(self, df: pd.DataFrame, ind: pd.DataFrame, spread_price: float) -> SubSignal:
        if not self._cooled_down:
            return SubSignal(side=None, reason="cooldown")
        if spread_price > self.max_spread_price:
            return SubSignal(side=None, reason="spread too wide")
        last = ind.iloc[-1]
        if pd.isna(last["atr"]) or last["atr"] < self.min_atr_price:
            return SubSignal(side=None, reason="volatility too low")
        if len(df) < 8:
            return SubSignal(side=None, reason="not enough history")

        votes = compute_votes(df, ind, self.mask)
        score = sum(votes)

        side: Optional[str] = None
        if score >= self.threshold:
            side = "BUY"
        elif score <= -self.threshold:
            side = "SELL"
        if side is None:
            return SubSignal(side=None, reason=f"qq_vote: score {score:+d} below threshold")

        wanted = 1 if side == "BUY" else -1
        confirmed = sum(1 for i in _CONFIRMED_BUCKET if votes[i] == wanted)
        if confirmed == 0:
            return SubSignal(side=None, reason=f"qq_vote: score {score:+d} but no confirmed-bucket vote (discarded)")

        combo = "+".join(name for name, v in zip(_VOTE_NAMES, votes) if v == wanted)
        return SubSignal(side=side, sl_distance_price=last["atr"] * self.sl_atr_multiple,
                          reason=f"qq_vote: score {score:+d} ({combo})")
