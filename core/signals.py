"""
Extra independent M1 entry signals, layered on top of the validated
Bollinger+RSI mean-reversion strategy in core/strategy.py (that one stays
unchanged - these are additional, separate ways an entry can qualify).

Where these ideas come from: adapted from a reconstruction of the public
"Quantum Queen" MT5 EA the user provided, whose actual entry logic is 12
directional filters (EMA crosses, RSI thresholds, session windows, range
breakouts, swing structure) summed into one confirmation score. Two things
from that EA were deliberately NOT ported, on purpose, after the user
picked "exclude it" when asked directly:

  1. Its risk model: no per-order stop-loss at all (SL=0 on every market
     order), a grid that ADDS positions as price moves further against
     you, and a single take-profit on the weighted-average entry of the
     whole basket. That is a martingale/grid pattern - the same failure
     mode already found (and fixed) in this project's own backtest
     history (see README, "Ronda 2": position sizing without a real
     dollar-risk cap wiped a demo account in hours). Every strategy below
     instead always returns a stop-loss distance and goes through the
     EXACT same core.risk_manager.RiskManager.size_position() call as the
     original mean-reversion strategy - same RISK_PER_TRADE_USD cap, same
     refusal to size a trade past it, no averaging down.
  2. Its trade frequency: the source EA caps at 5 NEW entries/day on M15
     bars by design - nowhere close to "hundreds of trades/day". What IS
     legitimately portable is the IDEA of having several independent,
     narrower setups instead of one - each one only adds trade
     opportunities on bars where it specifically applies, so the honest
     effect on daily trade count is additive across strategies, not a
     multiplier chosen up front. scripts/run_backtest.py --composite
     reports the real number on real data - see README for the actual
     result, not a target asserted in this docstring.

Every strategy here shares the interface CompositeStrategy expects:
    check(df, ind, spread_price) -> SubSignal
where `df` is raw OHLC (oldest->newest) and `ind` is
core.strategy.compute_indicators(df) run once and reused by all of them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from core.strategy import ScalpStrategy, Side, Signal, compute_indicators, compute_vol_ratio
from core.regime import Regime, detect_regime


@dataclass
class SubSignal:
    side: Optional[Side]
    sl_distance_price: float = 0.0
    reason: str = ""


def resample_m1_to_tf(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Builds higher-timeframe OHLC bars out of the M1 window we already
    have (clock-aligned, e.g. real 5-minute boundaries) instead of an extra
    bridge round-trip per poll for every timeframe a filter wants to look
    at - the M1 window already carries the same information."""
    # MT5 (and this project's own candle rows) timestamp a bar by its OPEN
    # time, so an M1 bar belongs to the M5 bar whose open time is <= it and
    # < open+5min - i.e. left-closed, left-labeled buckets, not right/right
    # (which would misassign bars relative to how the data is actually
    # timestamped).
    idx = pd.to_datetime(df["time"], unit="s")
    frame = df.set_index(idx)
    agg = frame.resample(f"{minutes}min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    )
    return agg.dropna().reset_index(drop=True)


class CooldownGate:
    """Shared cooldown bookkeeping - every sub-strategy gets on_bar_closed/
    on_trade_opened called together by CompositeStrategy, so as long as
    they're all constructed with the same cooldown_bars they stay in sync
    without any of them needing to know about the others."""

    def __init__(self, cooldown_bars: int) -> None:
        self.cooldown_bars = cooldown_bars
        self._bars_since_last_trade = cooldown_bars

    def on_bar_closed(self) -> None:
        self._bars_since_last_trade += 1

    def on_trade_opened(self) -> None:
        self._bars_since_last_trade = 0

    @property
    def _cooled_down(self) -> bool:
        return self._bars_since_last_trade >= self.cooldown_bars


def _ema_cross(ind: pd.DataFrame) -> Optional[Side]:
    """M1 EMA9/EMA21 crossing over the last two CLOSED bars."""
    if len(ind) < 3:
        return None
    prev, last = ind.iloc[-3], ind.iloc[-2]  # last two fully closed bars
    if pd.isna(prev["ema9"]) or pd.isna(prev["ema21"]) or pd.isna(last["ema9"]) or pd.isna(last["ema21"]):
        return None
    if prev["ema9"] < prev["ema21"] and last["ema9"] >= last["ema21"]:
        return "BUY"
    if prev["ema9"] > prev["ema21"] and last["ema9"] <= last["ema21"]:
        return "SELL"
    return None


class MomentumCrossStrategy(CooldownGate):
    """Adapted from the source EA's S01/S12 (EMA9/21 cross confirmed by a
    higher-timeframe EMA50 slope). Shifted one timeframe band down since
    this engine trades M1, not M15: the cross is on M1 (was M15), the
    higher-timeframe slope filter is M5 built by resampling the same M1
    window (was H1). This is a TREND-continuation signal, the opposite
    regime of the mean-reversion strategy, so it deliberately does NOT
    apply the ADX "avoid strong trends" filter - a trend is exactly what
    this one wants to catch."""

    def __init__(self, cooldown_bars: int, max_spread_price: float, min_atr_price: float,
                 sl_atr_multiple: float = 2.0) -> None:
        super().__init__(cooldown_bars)
        self.max_spread_price = max_spread_price
        self.min_atr_price = min_atr_price
        self.sl_atr_multiple = sl_atr_multiple

    def check(self, df: pd.DataFrame, ind: pd.DataFrame, spread_price: float) -> SubSignal:
        if not self._cooled_down:
            return SubSignal(side=None, reason="cooldown")
        if spread_price > self.max_spread_price:
            return SubSignal(side=None, reason="spread too wide")
        last = ind.iloc[-1]
        if pd.isna(last["atr"]) or last["atr"] < self.min_atr_price:
            return SubSignal(side=None, reason="volatility too low")

        cross = _ema_cross(ind)
        if cross is None:
            return SubSignal(side=None, reason="no cross")

        m5 = resample_m1_to_tf(df, 5)
        if len(m5) < 12:
            return SubSignal(side=None, reason="not enough M5 history yet")
        m5_ema50 = m5["close"].ewm(span=50, adjust=False).mean()
        if len(m5_ema50) < 2:
            return SubSignal(side=None, reason="not enough M5 history yet")
        m5_rising = m5_ema50.iloc[-1] > m5_ema50.iloc[-2]

        if cross == "BUY" and m5_rising:
            return SubSignal(side="BUY", sl_distance_price=last["atr"] * self.sl_atr_multiple,
                              reason="momentum_cross: M1 EMA9/21 buy cross + M5 EMA50 rising")
        if cross == "SELL" and not m5_rising:
            return SubSignal(side="SELL", sl_distance_price=last["atr"] * self.sl_atr_multiple,
                              reason="momentum_cross: M1 EMA9/21 sell cross + M5 EMA50 falling")
        return SubSignal(side=None, reason="cross against the M5 slope")


class RsiHysteresisStrategy(CooldownGate):
    """Adapted from S02: RSI crossing through the 50 midline with a 52/48
    hysteresis band (avoids re-firing on noise sitting right at 50).
    Catches a momentum shift the mean-reversion strategy structurally
    can't - that one only ever looks at the RSI EXTREMES."""

    def __init__(self, cooldown_bars: int, max_spread_price: float, min_atr_price: float,
                 sl_atr_multiple: float = 2.0, upper: float = 52.0, lower: float = 48.0) -> None:
        super().__init__(cooldown_bars)
        self.max_spread_price = max_spread_price
        self.min_atr_price = min_atr_price
        self.sl_atr_multiple = sl_atr_multiple
        self.upper = upper
        self.lower = lower

    def check(self, df: pd.DataFrame, ind: pd.DataFrame, spread_price: float) -> SubSignal:
        if not self._cooled_down:
            return SubSignal(side=None, reason="cooldown")
        if spread_price > self.max_spread_price:
            return SubSignal(side=None, reason="spread too wide")
        if len(ind) < 3:
            return SubSignal(side=None, reason="not enough history")
        last = ind.iloc[-1]
        if pd.isna(last["atr"]) or last["atr"] < self.min_atr_price:
            return SubSignal(side=None, reason="volatility too low")

        prev, cur = ind.iloc[-3]["rsi"], ind.iloc[-2]["rsi"]
        if pd.isna(prev) or pd.isna(cur):
            return SubSignal(side=None, reason="rsi warming up")

        if prev < self.lower and cur >= self.upper:
            return SubSignal(side="BUY", sl_distance_price=last["atr"] * self.sl_atr_multiple,
                              reason="rsi_hysteresis: RSI crossed up through 50")
        if prev > self.upper and cur <= self.lower:
            return SubSignal(side="SELL", sl_distance_price=last["atr"] * self.sl_atr_multiple,
                              reason="rsi_hysteresis: RSI crossed down through 50")
        return SubSignal(side=None, reason="no rsi cross")


class DirectionalCandleStrategy(CooldownGate):
    """Adapted from S03: a single strong directional candle (big body
    relative to its range, range wide relative to ATR, closing near its
    extreme) read as a momentum thrust worth following. Stop is structural
    (beyond the candle's own low/high), not a bare ATR multiple - that's
    the standard stop placement for a thrust-bar continuation entry."""

    def __init__(self, cooldown_bars: int, max_spread_price: float, min_atr_price: float,
                 sl_buffer_atr_mult: float = 0.3) -> None:
        super().__init__(cooldown_bars)
        self.max_spread_price = max_spread_price
        self.min_atr_price = min_atr_price
        self.sl_buffer_atr_mult = sl_buffer_atr_mult

    def check(self, df: pd.DataFrame, ind: pd.DataFrame, spread_price: float) -> SubSignal:
        if not self._cooled_down:
            return SubSignal(side=None, reason="cooldown")
        if spread_price > self.max_spread_price:
            return SubSignal(side=None, reason="spread too wide")
        if len(df) < 2:
            return SubSignal(side=None, reason="not enough history")
        last = ind.iloc[-1]
        atr = last["atr"]
        if pd.isna(atr) or atr < self.min_atr_price:
            return SubSignal(side=None, reason="volatility too low")

        candle = df.iloc[-2]  # last fully closed bar
        rng = candle["high"] - candle["low"]
        if rng <= 0 or rng < atr * 0.8:
            return SubSignal(side=None, reason="candle range too small")
        body = abs(candle["close"] - candle["open"])
        if body < rng * 0.5:
            return SubSignal(side=None, reason="candle body too small")

        buffer = atr * self.sl_buffer_atr_mult
        if candle["close"] > candle["open"] and (candle["close"] - candle["low"]) > rng * 0.6:
            return SubSignal(side="BUY", sl_distance_price=(candle["close"] - candle["low"]) + buffer,
                              reason="directional_candle: strong bullish thrust bar")
        if candle["close"] < candle["open"] and (candle["high"] - candle["close"]) > rng * 0.6:
            return SubSignal(side="SELL", sl_distance_price=(candle["high"] - candle["close"]) + buffer,
                              reason="directional_candle: strong bearish thrust bar")
        return SubSignal(side=None, reason="candle not directional enough")


class SessionOpenStrategy(CooldownGate):
    """Adapted from S06/S07: trade M1 EMA9/21 crosses only during the
    London and New York session opens (UTC hours), when gold genuinely
    tends to see its most liquid, most directional moves of the day -
    outside those windows this strategy simply produces no signal, it
    doesn't relax any other filter to compensate."""

    def __init__(self, cooldown_bars: int, max_spread_price: float, min_atr_price: float,
                 sl_atr_multiple: float, london_start_hour: int, london_end_hour: int,
                 ny_start_hour: int, ny_end_hour: int) -> None:
        super().__init__(cooldown_bars)
        self.max_spread_price = max_spread_price
        self.min_atr_price = min_atr_price
        self.sl_atr_multiple = sl_atr_multiple
        self.london_start_hour = london_start_hour
        self.london_end_hour = london_end_hour
        self.ny_start_hour = ny_start_hour
        self.ny_end_hour = ny_end_hour

    def _in_session(self, ts: pd.Timestamp) -> bool:
        h = ts.hour
        return (self.london_start_hour <= h < self.london_end_hour) or (self.ny_start_hour <= h < self.ny_end_hour)

    def check(self, df: pd.DataFrame, ind: pd.DataFrame, spread_price: float) -> SubSignal:
        if not self._cooled_down:
            return SubSignal(side=None, reason="cooldown")
        if spread_price > self.max_spread_price:
            return SubSignal(side=None, reason="spread too wide")
        if len(df) < 2:
            return SubSignal(side=None, reason="not enough history")

        last_closed_time = pd.to_datetime(int(df.iloc[-2]["time"]), unit="s")
        if not self._in_session(last_closed_time):
            return SubSignal(side=None, reason="outside London/NY session window")

        last = ind.iloc[-1]
        if pd.isna(last["atr"]) or last["atr"] < self.min_atr_price:
            return SubSignal(side=None, reason="volatility too low")

        cross = _ema_cross(ind)
        if cross is None:
            return SubSignal(side=None, reason="no cross")
        return SubSignal(side=cross, sl_distance_price=last["atr"] * self.sl_atr_multiple,
                          reason=f"session_open: EMA9/21 {cross.lower()} cross during London/NY open")


class AsianRangeBreakoutStrategy(CooldownGate):
    """Adapted from S08: tracks the Asian session's (00:00-07:00 UTC by
    default) high/low, then looks for a confirmed close beyond that range
    (plus a small buffer) during the breakout window right after (07:00-
    10:00 UTC by default).

    Stateful ACROSS calls (unlike the other strategies here, which are
    pure functions of the current window): the range only needs the last
    closed bar added each time, not a full rescan, so it survives on
    whatever candle window the engine happens to fetch. The real
    limitation - shared with any restart-recovery in this engine, see
    core/engine.py's own _reconcile_open_positions docstring - is that if
    the process restarts mid-Asian-session, the range only backfills from
    whatever history the current window covers (settings.candle_history_count),
    not from before that. On a fresh start mid-session this can understate
    the true range; it does not use it wrongly, it just may have less of it.
    """

    def __init__(self, cooldown_bars: int, max_spread_price: float, min_atr_price: float,
                 sl_atr_multiple: float, range_start_hour: int, range_end_hour: int,
                 breakout_start_hour: int, breakout_end_hour: int, buffer_pct: float) -> None:
        super().__init__(cooldown_bars)
        self.max_spread_price = max_spread_price
        self.min_atr_price = min_atr_price
        self.sl_atr_multiple = sl_atr_multiple
        self.range_start_hour = range_start_hour
        self.range_end_hour = range_end_hour
        self.breakout_start_hour = breakout_start_hour
        self.breakout_end_hour = breakout_end_hour
        self.buffer_pct = buffer_pct

        self._day: Optional[pd.Timestamp] = None
        self._range_high = float("-inf")
        self._range_low = float("inf")
        self._last_seen_bar_time: Optional[int] = None

    def _update_range(self, df: pd.DataFrame) -> None:
        if len(df) < 2:
            return
        closed = df.iloc[-2]
        bar_time = int(closed["time"])
        if bar_time == self._last_seen_bar_time:
            return  # already folded this closed bar into the range

        ts = pd.to_datetime(bar_time, unit="s")
        today = ts.normalize()
        if self._day != today:
            self._day = today
            self._range_high = float("-inf")
            self._range_low = float("inf")
            # Best-effort backfill from whatever history is already in this
            # window (see class docstring: a restart mid-session only
            # backfills as far back as the fetched window reaches).
            same_day = df[pd.to_datetime(df["time"], unit="s").dt.normalize() == today]
            in_range_hours = same_day[
                pd.to_datetime(same_day["time"], unit="s").dt.hour.between(
                    self.range_start_hour, self.range_end_hour - 1
                )
            ]
            if not in_range_hours.empty:
                self._range_high = float(in_range_hours["high"].max())
                self._range_low = float(in_range_hours["low"].min())

        if self.range_start_hour <= ts.hour < self.range_end_hour:
            self._range_high = max(self._range_high, float(closed["high"]))
            self._range_low = min(self._range_low, float(closed["low"]))

        self._last_seen_bar_time = bar_time

    def check(self, df: pd.DataFrame, ind: pd.DataFrame, spread_price: float) -> SubSignal:
        self._update_range(df)  # always, so the range keeps building even outside the breakout window

        if not self._cooled_down:
            return SubSignal(side=None, reason="cooldown")
        if spread_price > self.max_spread_price:
            return SubSignal(side=None, reason="spread too wide")
        if len(df) < 2:
            return SubSignal(side=None, reason="not enough history")

        last_closed_time = pd.to_datetime(int(df.iloc[-2]["time"]), unit="s")
        if not (self.breakout_start_hour <= last_closed_time.hour < self.breakout_end_hour):
            return SubSignal(side=None, reason="outside Asian breakout window")

        if self._range_high <= self._range_low or self._range_high == float("-inf"):
            return SubSignal(side=None, reason="no Asian range recorded yet")

        last = ind.iloc[-1]
        if pd.isna(last["atr"]) or last["atr"] < self.min_atr_price:
            return SubSignal(side=None, reason="volatility too low")

        rng = self._range_high - self._range_low
        buf = rng * self.buffer_pct
        close = df.iloc[-2]["close"]

        if close > self._range_high + buf:
            return SubSignal(side="BUY", sl_distance_price=max(last["atr"] * self.sl_atr_multiple, buf * 2),
                              reason="asian_breakout: confirmed close above the Asian range")
        if close < self._range_low - buf:
            return SubSignal(side="SELL", sl_distance_price=max(last["atr"] * self.sl_atr_multiple, buf * 2),
                              reason="asian_breakout: confirmed close below the Asian range")
        return SubSignal(side=None, reason="inside the Asian range")


def _last_closed_m15_bar(df: pd.DataFrame) -> Optional[pd.Series]:
    """Returns the most recent M15 OHLC bar that is FULLY closed - i.e. the
    M1 window in `df` covers all 15 of its minutes - or None if there
    isn't one yet. This is deliberately NOT "just take resample_m1_to_tf's
    last row": that function aggregates whatever M1 bars happen to fall in
    the current 15-minute bucket, closed or not, so its last row is very
    often a PARTIAL M15 candle whose close keeps moving every time a new
    M1 bar arrives - exactly the "repaint" Ronda 8 eliminated for M1
    entries (a signal that appears and disappears mid-bar). Same principle
    here, one timeframe band up: a bucket only counts once the M1 bar that
    is its 15th minute (minute_in_block == 14) has itself closed.
    """
    if len(df) < 1:
        return None
    m15 = resample_m1_to_tf(df, 15)
    if m15.empty:
        return None

    last_m1_time = int(df.iloc[-1]["time"])
    minute_in_block = (last_m1_time % (15 * 60)) // 60
    if minute_in_block == 14:
        return m15.iloc[-1]  # the M1 bar we just got IS that bucket's last minute - it's closed
    if len(m15) >= 2:
        return m15.iloc[-2]  # current bucket still forming - fall back to the previous, closed one
    return None


class M15TrendStrategy(CooldownGate):
    """Not adapted from the reference EA (see module docstring) - a
    user-requested M15 context filter for M1 entries, added to chase raw
    trade FREQUENCY over per-trade size (explicit tradeoff the user asked
    for), while staying inside this project's one non-negotiable rule
    (see core/strategy.py): every signal still gets a real, sized
    stop-loss through the same RiskManager path as everything else here.

    Logic: look at the last CLOSED M15 candle only (see
    _last_closed_m15_bar - never the M15 bucket still being built from the
    current M1 window, same "closed bars only" principle Ronda 8 already
    applied to M1 entries). A bearish M15 close (close < open) is read as
    a sell context, a bullish M15 close (close > open) as a buy context -
    the simplest possible directional read on purpose, so it fires on
    almost every M15 close that isn't flat, maximizing how often this
    signal can contribute a trade. A small body-vs-range floor excludes
    genuine doji/flat M15 candles, which carry no directional information
    to hand to M1.

    Because this reads M15 context but still enters on the current M1
    bar's terms, it is a coarser, noisier read than the other M1-native
    signals in this file - see README "Ronda 9" for whether it actually
    helps net PnL on real data or just adds trades, the same honest test
    every other signal here already went through.
    """

    def __init__(self, cooldown_bars: int, max_spread_price: float, min_atr_price: float,
                 sl_atr_multiple: float = 2.0, min_body_range_ratio: float = 0.1) -> None:
        super().__init__(cooldown_bars)
        self.max_spread_price = max_spread_price
        self.min_atr_price = min_atr_price
        self.sl_atr_multiple = sl_atr_multiple
        self.min_body_range_ratio = min_body_range_ratio

    def check(self, df: pd.DataFrame, ind: pd.DataFrame, spread_price: float) -> SubSignal:
        if not self._cooled_down:
            return SubSignal(side=None, reason="cooldown")
        if spread_price > self.max_spread_price:
            return SubSignal(side=None, reason="spread too wide")
        last = ind.iloc[-1]
        if pd.isna(last["atr"]) or last["atr"] < self.min_atr_price:
            return SubSignal(side=None, reason="volatility too low")

        bar = _last_closed_m15_bar(df)
        if bar is None:
            return SubSignal(side=None, reason="not enough M15 history yet")

        rng = bar["high"] - bar["low"]
        body = bar["close"] - bar["open"]
        if rng <= 0 or abs(body) < rng * self.min_body_range_ratio:
            return SubSignal(side=None, reason="M15 candle too flat/doji")

        if body > 0:
            return SubSignal(side="BUY", sl_distance_price=last["atr"] * self.sl_atr_multiple,
                              reason="m15_trend: last closed M15 candle bullish (close > open)")
        return SubSignal(side="SELL", sl_distance_price=last["atr"] * self.sl_atr_multiple,
                          reason="m15_trend: last closed M15 candle bearish (close < open)")


class MACrossGridStrategy(CooldownGate):
    """Adapted from a user-submitted MQL5 EA ("ScalpMaster Pro v1.0"),
    requested to be ported "as is" to M15. Its entry logic (ProcessBar's
    STATE_IDLE/STATE_MA_CROSS/STATE_WAIT_EMA/STATE_IN_TRADE machine) is:
    SMA(9) crossing SMA(26), confirmed one CLOSED bar later (never the
    still-forming bar - same anti-repaint principle as `_ema_cross` above
    and Ronda 8's M1 fix), then a fixed 2-bar wait before entering, with a
    permissive RSI filter on the bar right before entry (blocks BUY only
    if RSI >= 70, blocks SELL only if RSI <= 30 - rejects the extreme
    case, doesn't gate anything else).

    One thing from that EA was deliberately NOT ported, and this is the
    important part: **its orders open with NO stop-loss at all** -
    `Trade.Buy(lot, _Symbol, 0, 0, 0, ...)` passes 0 for both sl and tp: a
    market order with zero risk protection. The only defense is a
    trailing stop that activates AFTER price first reaches the first
    level of a 6-level take-profit grid - if price never gets there and
    keeps moving the wrong way instead, the position rides unprotected
    with no limit. That is incompatible with core/risk_manager.py, which
    needs a real SL *distance* up front to size a position against
    RISK_PER_TRADE_USD in the first place, and it is exactly the failure
    mode this project already found the hard way (README, "Ronda 2": a
    position sized without a real dollar-risk cap wiped a demo account in
    hours). A "backtest with high profits" run on a no-SL EA is not
    trustworthy evidence on its own - it only means the sampled window
    didn't happen to contain the one adverse move that would have
    uncovered the missing floor; that is sample bias, not an edge. So,
    same as every other strategy in this file: this class always returns
    a real ATR-based SL distance and goes through the exact same
    RiskManager path as mean-reversion - no exceptions.

    Also simplified, and noted rather than silently dropped: the source
    EA tracks an EMA(9)/EMA(26) cross during the 2-bar wait purely to
    adjust WHEN a time-based exit starts counting - it does not gate or
    trigger the entry itself. This project already measured a generic
    pre-TP1 time-based exit (README "Ronda 8", `max_hold_bars`) and found
    it does not help on real data in either direction tested, so that
    EMA-driven timing nuance is not reimplemented as a separate mechanism
    here - `--max-hold-bars` in scripts/run_backtest.py is already
    available (off by default, per that finding) for anyone who wants to
    experiment with time-based exits generally.

    Timeframe: unlike M15TrendStrategy above (which deliberately reads
    M15 CONTEXT into an M1-native entry via resample_m1_to_tf), this
    class does no internal resampling - it trades at whatever native bar
    granularity the `df`/`ind` it's given already are, matching the
    source EA's own M15 ProcessBar cadence directly. That means it is
    meant to be backtested against native M15 OHLC (see
    scripts/fetch_market_data.py --interval 15m) and, if ever run live,
    needs the engine's own TIMEFRAME=M15 (core/config.py) - core/engine.py
    already fetches candles at whatever timeframe is configured, so no
    special-casing is needed there, but note that running this alongside
    the M1-tuned extras above at TIMEFRAME=M15 would change THEIR bar
    meaning too (an informed tradeoff for whoever enables that
    combination, not something this class works around).

    State is deduplicated by the last CLOSED bar's timestamp (same
    pattern as AsianRangeBreakoutStrategy's `_last_seen_bar_time` above)
    so repeated polls within the same still-forming bar don't advance the
    wait counter more than once per real bar close. If the 2-bar wait
    completes but the entry is blocked (cooldown, spread, ATR, RSI, or
    ADX), the setup is dropped rather than retried on a later bar - a
    missed opportunity is simply missed, not queued.

    min_adx (README "Ronda 11"): an SMA cross is a trend-FOLLOWING signal,
    so it only means something when a real trend exists - on real M1
    XAUUSD data this class fired 111 times in 5.4 days at 78.4% win rate
    and still lost -$29.24 (avg win +$0.14 vs avg loss -$3.54: most fires
    were during choppy/ranging bars where the "cross" was noise, not
    trend, so a handful of full-ATR-stop losses wiped out a pile of tiny
    grid-TP1 wins). ADX(14) (already computed by compute_indicators for
    the mean-reversion side, reused here) measures trend STRENGTH - gating
    entries on ADX >= min_adx cut both the loss and the drawdown by
    roughly 5-6x on the same data (TEST half: -$29.26 -> -$5.27 at
    min_adx=20, drawdown 62.1% -> 13.5%), a real, honestly-measured
    improvement. It is NOT a full fix: at min_adx=20 (the only threshold
    with an out-of-sample trade count large enough to trust, 10 TEST
    trades) the result is still net negative on TEST; higher thresholds
    "look" profitable both halves but on 2-5 trades each, too few to
    distinguish from luck. 0.0 (off) is the class default so existing
    callers/tests are unaffected; config.py's default once the parent
    STRAT_ENABLE_MA_GRID flag is turned on is 20.0, chosen for that
    honestly-measured risk reduction, not because it was shown to make
    this signal profitable at M1.
    """

    def __init__(self, cooldown_bars: int, max_spread_price: float, min_atr_price: float,
                 sl_atr_multiple: float = 3.0, ma_fast_period: int = 9, ma_slow_period: int = 26,
                 entry_wait_bars: int = 2, rsi_overbought: float = 70.0, rsi_oversold: float = 30.0,
                 min_adx: float = 0.0) -> None:
        super().__init__(cooldown_bars)
        self.max_spread_price = max_spread_price
        self.min_atr_price = min_atr_price
        self.sl_atr_multiple = sl_atr_multiple
        self.ma_fast_period = ma_fast_period
        self.ma_slow_period = ma_slow_period
        self.entry_wait_bars = entry_wait_bars
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        # 0.0 = off (backward-compatible default - see Ronda 11 in README
        # for why this exists and why it is NOT on by default at the class
        # level even though config.py's recommended value is 20.0 once
        # explicitly enabled).
        self.min_adx = min_adx

        self._last_seen_bar_time: Optional[int] = None
        self._pending_side: Optional[Side] = None
        self._bars_since_cross = 0

    def _advance_state(self, df: pd.DataFrame) -> Optional[Side]:
        """Called once per newly closed bar only (see check()'s dedup).
        Returns the side to enter THIS bar if the wait just completed,
        else None. A fresh MA cross always (re)arms the wait, even over an
        already-pending one - matches the source EA's state machine,
        where a new MA_CROSS event always resets STATE_MA_CROSS."""
        sma_fast = df["close"].rolling(self.ma_fast_period).mean()
        sma_slow = df["close"].rolling(self.ma_slow_period).mean()
        if len(sma_fast) < 3 or pd.isna(sma_fast.iloc[-3]) or pd.isna(sma_slow.iloc[-3]):
            return None

        prev_fast, prev_slow = sma_fast.iloc[-3], sma_slow.iloc[-3]
        last_fast, last_slow = sma_fast.iloc[-2], sma_slow.iloc[-2]

        cross: Optional[Side] = None
        if prev_fast < prev_slow and last_fast >= last_slow:
            cross = "BUY"
        elif prev_fast > prev_slow and last_fast <= last_slow:
            cross = "SELL"

        if cross is not None:
            self._pending_side = cross
            self._bars_since_cross = 0
            return None

        if self._pending_side is None:
            return None

        self._bars_since_cross += 1
        if self._bars_since_cross == self.entry_wait_bars:
            side = self._pending_side
            self._pending_side = None
            return side
        return None

    def check(self, df: pd.DataFrame, ind: pd.DataFrame, spread_price: float) -> SubSignal:
        if len(df) < self.ma_slow_period + 5:
            return SubSignal(side=None, reason="not enough history")

        last_closed_time = int(df.iloc[-2]["time"])
        fire_side: Optional[Side] = None
        if last_closed_time != self._last_seen_bar_time:
            self._last_seen_bar_time = last_closed_time
            fire_side = self._advance_state(df)

        if not self._cooled_down:
            return SubSignal(side=None, reason="cooldown")
        if spread_price > self.max_spread_price:
            return SubSignal(side=None, reason="spread too wide")

        last = ind.iloc[-1]
        if pd.isna(last["atr"]) or last["atr"] < self.min_atr_price:
            return SubSignal(side=None, reason="volatility too low")

        if fire_side is None:
            return SubSignal(side=None, reason="no ma_grid setup ready")

        if self.min_adx > 0:
            adx = last["adx"]
            if pd.isna(adx) or adx < self.min_adx:
                return SubSignal(side=None, reason=f"ma_grid: ADX {adx:.1f} < {self.min_adx} (sin tendencia real, filtrado)")

        rsi_prev = ind.iloc[-2]["rsi"]  # RSI of the bar right before entry - source EA's RSI(1)
        if pd.isna(rsi_prev):
            return SubSignal(side=None, reason="rsi warming up")
        if fire_side == "BUY" and rsi_prev >= self.rsi_overbought:
            return SubSignal(side=None, reason=f"ma_grid: RSI overbought ({rsi_prev:.1f}), skipping BUY")
        if fire_side == "SELL" and rsi_prev <= self.rsi_oversold:
            return SubSignal(side=None, reason=f"ma_grid: RSI oversold ({rsi_prev:.1f}), skipping SELL")

        sl_distance = last["atr"] * self.sl_atr_multiple
        return SubSignal(side=fire_side, sl_distance_price=sl_distance,
                          reason=f"ma_grid: SMA{self.ma_fast_period}/{self.ma_slow_period} cross "
                                 f"+ {self.entry_wait_bars}-bar wait, RSI {rsi_prev:.1f}")


class CompositeStrategy:
    """Orchestrates the validated mean-reversion strategy plus zero or more
    of the extra signals above, sharing one interface with plain
    ScalpStrategy (generate_signal/on_bar_closed/on_trade_opened/
    min_tp_distance_for_lot/build_tp_ladder) so core/engine.py and
    core/backtest.py don't need to know which one they're holding.

    Per bar: try mean-reversion first (unchanged priority - it's the one
    with actual backtest history behind it), then each extra signal in the
    order given. The FIRST one that fires wins - core/engine.py only ever
    holds one position at a time, so there is never a case of two signals
    firing "together" to reconcile. Every signal that fires gets the same
    TP ladder (delegated to the mean-reversion instance - the ladder is
    about dollar risk/reward structure, not specific to any one setup) and
    the same SL-vs-TP1 sanity floor mean-reversion already enforced.

    With extra_strategies=[] this is byte-for-byte the same behavior as
    calling the wrapped ScalpStrategy directly, including its exact
    rejection `reason` - so enabling zero extra signals via config doesn't
    change anything about the already-backtested baseline.
    """

    def __init__(
        self,
        mean_reversion: ScalpStrategy,
        extra_strategies: list[tuple[str, object]],
        indicator_rsi_period: int = 14,
        indicator_atr_period: int = 14,
        regime_filter_enabled: bool = True,
        regime_adx_trend_threshold: float = 25.0,
        regime_atr_ratio_high: float = 1.35,
        regime_atr_ratio_low: float = 0.65,
        prefer_quantum_queen: bool = False,
    ) -> None:
        self._mean_reversion = mean_reversion
        self._extra = extra_strategies
        self._indicator_rsi_period = indicator_rsi_period
        self._indicator_atr_period = indicator_atr_period
        self._regime_filter_enabled = regime_filter_enabled
        self._regime_kwargs = dict(adx_threshold=regime_adx_trend_threshold,
                                   atr_high=regime_atr_ratio_high, atr_low=regime_atr_ratio_low)
        self._prefer_quantum_queen = prefer_quantum_queen
        # EMA50 (used by MomentumCrossStrategy on the M5-resampled window)
        # is the slowest thing any extra signal looks at - 55 M1 bars is a
        # deliberately generous floor before even trying them, on top of
        # whatever compute_indicators() itself needs.
        self._warmup_bars = max(mean_reversion._warmup_bars, 55)

    def on_bar_closed(self) -> None:
        self._mean_reversion.on_bar_closed()
        for _, sub in self._extra:
            sub.on_bar_closed()

    def on_trade_opened(self) -> None:
        self._mean_reversion.on_trade_opened()
        for _, sub in self._extra:
            sub.on_trade_opened()

    def min_tp_distance_for_lot(self, lot: float) -> float:
        return self._mean_reversion.min_tp_distance_for_lot(lot)

    def build_tp_ladder(self, lot: float, spread_price: float, vol_ratio: float = 1.0):
        return self._mean_reversion.build_tp_ladder(lot, spread_price, vol_ratio=vol_ratio)

    def generate_signal(self, df: pd.DataFrame, spread_price: float, lot_hint: float,
                         precomputed_mr_indicators: pd.DataFrame | None = None,
                         precomputed_composite_indicators: pd.DataFrame | None = None,
                         precomputed_regime: "Regime | None" = None) -> Signal:
        mr_signal = self._mean_reversion.generate_signal(df, spread_price, lot_hint,
                                                           precomputed_indicators=precomputed_mr_indicators)
        if not self._prefer_quantum_queen and mr_signal.side is not None:
            return mr_signal
        if not self._extra:
            return mr_signal

        if len(df) < self._warmup_bars:
            return mr_signal

        if precomputed_composite_indicators is not None:
            ind = precomputed_composite_indicators
        else:
            ind = compute_indicators(df, rsi_period=self._indicator_rsi_period, atr_period=self._indicator_atr_period)
        if precomputed_regime is not None:
            regime = precomputed_regime
        else:
            regime = detect_regime(df, **self._regime_kwargs) if self._regime_filter_enabled else None
        last = ind.iloc[-1]
        vol_ratio = compute_vol_ratio(last["atr"], last["atr_baseline"])

        ordered = self._extra
        if self._prefer_quantum_queen:
            ordered = sorted(self._extra, key=lambda item: 0 if item[0] == "quantum_queen" else 1)
        for name, sub in ordered:
            if regime and regime.name == "volatile" and name in {"ma_grid", "quantum_queen"}:
                continue
            sub_signal = sub.check(df, ind, spread_price)
            if sub_signal.side is None:
                continue
            sl_distance = max(sub_signal.sl_distance_price, self._mean_reversion.min_tp_distance_for_lot(lot_hint) * 1.5)
            tp_levels = self._mean_reversion.build_tp_ladder(lot_hint, spread_price, vol_ratio=vol_ratio)
            return Signal(side=sub_signal.side, sl_distance_price=sl_distance, tp_levels=tp_levels,
                           reason=f"{name}: {sub_signal.reason}", vol_ratio=vol_ratio)

        return mr_signal


def build_strategy_from_settings(settings, value_per_point_per_lot: float) -> CompositeStrategy:
    """Single place that turns Settings + a symbol's value-per-point into
    the strategy the engine/backtest actually run - keeps core/engine.py
    and core/backtest.py from duplicating this wiring."""
    mean_reversion = ScalpStrategy(
        min_tp_usd=settings.min_tp_usd,
        tp_levels=settings.tp_levels,
        value_per_point_per_lot=value_per_point_per_lot,
        rsi_oversold=settings.strat_rsi_oversold,
        rsi_overbought=settings.strat_rsi_overbought,
        max_spread_price=settings.strat_max_spread_price,
        min_atr_price=settings.strat_min_atr_price,
        sl_atr_multiple=settings.strat_sl_atr_multiple,
        cooldown_bars=settings.strat_cooldown_bars,
        bb_period=settings.strat_bb_period,
        bb_std=settings.strat_bb_std,
        rsi_period=settings.strat_rsi_period,
        atr_period=settings.strat_atr_period,
        adx_period=settings.strat_adx_period,
        trend_filter_adx_threshold=settings.strat_trend_filter_adx_threshold,
        tp_targets_usd=settings.tp_targets_usd,
    )

    extra: list[tuple[str, object]] = []
    cooldown = settings.strat_cooldown_bars
    spread = settings.strat_max_spread_price
    min_atr = settings.strat_min_atr_price

    if settings.strat_enable_momentum_cross:
        extra.append(("momentum_cross", MomentumCrossStrategy(
            cooldown_bars=cooldown, max_spread_price=spread, min_atr_price=min_atr,
            sl_atr_multiple=settings.strat_momentum_sl_atr_multiple,
        )))
    if settings.strat_enable_rsi_hysteresis:
        extra.append(("rsi_hysteresis", RsiHysteresisStrategy(
            cooldown_bars=cooldown, max_spread_price=spread, min_atr_price=min_atr,
            sl_atr_multiple=settings.strat_rsi_hysteresis_sl_atr_multiple,
            upper=settings.strat_rsi_hysteresis_upper, lower=settings.strat_rsi_hysteresis_lower,
        )))
    if settings.strat_enable_directional_candle:
        extra.append(("directional_candle", DirectionalCandleStrategy(
            cooldown_bars=cooldown, max_spread_price=spread, min_atr_price=min_atr,
            sl_buffer_atr_mult=settings.strat_directional_candle_sl_buffer_atr_mult,
        )))
    if settings.strat_enable_session_open:
        extra.append(("session_open", SessionOpenStrategy(
            cooldown_bars=cooldown, max_spread_price=spread, min_atr_price=min_atr,
            sl_atr_multiple=settings.strat_session_sl_atr_multiple,
            london_start_hour=settings.strat_session_london_start_hour,
            london_end_hour=settings.strat_session_london_end_hour,
            ny_start_hour=settings.strat_session_ny_start_hour,
            ny_end_hour=settings.strat_session_ny_end_hour,
        )))
    if settings.strat_enable_asian_breakout:
        extra.append(("asian_breakout", AsianRangeBreakoutStrategy(
            cooldown_bars=cooldown, max_spread_price=spread, min_atr_price=min_atr,
            sl_atr_multiple=settings.strat_asian_sl_atr_multiple,
            range_start_hour=settings.strat_asian_range_start_hour,
            range_end_hour=settings.strat_asian_range_end_hour,
            breakout_start_hour=settings.strat_asian_breakout_start_hour,
            breakout_end_hour=settings.strat_asian_breakout_end_hour,
            buffer_pct=settings.strat_asian_breakout_buffer_pct,
        )))
    if settings.strat_enable_m15_trend:
        extra.append(("m15_trend", M15TrendStrategy(
            cooldown_bars=cooldown, max_spread_price=spread, min_atr_price=min_atr,
            sl_atr_multiple=settings.strat_m15_trend_sl_atr_multiple,
            min_body_range_ratio=settings.strat_m15_trend_min_body_range_ratio,
        )))
    if settings.strat_enable_ma_grid:
        extra.append(("ma_grid", MACrossGridStrategy(
            cooldown_bars=cooldown, max_spread_price=spread, min_atr_price=min_atr,
            sl_atr_multiple=settings.strat_ma_grid_sl_atr_multiple,
            ma_fast_period=settings.strat_ma_grid_fast_period,
            ma_slow_period=settings.strat_ma_grid_slow_period,
            entry_wait_bars=settings.strat_ma_grid_entry_wait_bars,
            rsi_overbought=settings.strat_ma_grid_rsi_overbought,
            rsi_oversold=settings.strat_ma_grid_rsi_oversold,
            min_adx=settings.strat_ma_grid_min_adx,
        )))
    if settings.strat_enable_quantum_queen:
        from core.quantum_queen import QuantumQueenVoteStrategy
        extra.append(("quantum_queen", QuantumQueenVoteStrategy(
            cooldown_bars=cooldown, max_spread_price=spread, min_atr_price=min_atr,
            mask=settings.strat_quantum_mask, threshold=settings.strat_quantum_threshold,
        )))

    return CompositeStrategy(
        mean_reversion=mean_reversion,
        extra_strategies=extra,
        indicator_rsi_period=settings.strat_composite_rsi_period,
        indicator_atr_period=settings.strat_composite_atr_period,
        regime_filter_enabled=settings.regime_filter_enabled,
        regime_adx_trend_threshold=settings.regime_adx_trend_threshold,
        regime_atr_ratio_high=settings.regime_atr_ratio_high,
        regime_atr_ratio_low=settings.regime_atr_ratio_low,
        prefer_quantum_queen=settings.strat_quantum_primary,
    )
