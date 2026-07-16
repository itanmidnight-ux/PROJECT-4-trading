"""Tests for the extra M1 signals in core/signals.py (adapted from the
user-provided reference EA, minus its no-stop-loss grid/martingale risk
model - see that module's docstring) and CompositeStrategy, which
orchestrates them on top of the existing, unchanged mean-reversion
strategy.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.signals import (
    AsianRangeBreakoutStrategy,
    CompositeStrategy,
    CooldownGate,
    DirectionalCandleStrategy,
    MomentumCrossStrategy,
    RsiHysteresisStrategy,
    SessionOpenStrategy,
    SubSignal,
    resample_m1_to_tf,
)
from core.strategy import ScalpStrategy, compute_indicators

BASE_TIME = 1_700_000_000  # 2023-11-14 22:13:20 UTC - arbitrary anchor


def make_df(closes: list[float], start_time: int = BASE_TIME, step: int = 60) -> pd.DataFrame:
    opens = [closes[0]] + closes[:-1]
    rows = []
    t = start_time
    for o, c in zip(opens, closes, strict=True):
        rows.append({"time": t, "open": o, "high": max(o, c) + 0.02, "low": min(o, c) - 0.02, "close": c})
        t += step
    return pd.DataFrame(rows)


def flat_ind(n: int, atr: float = 0.3, rsi: float = 50.0) -> pd.DataFrame:
    """A minimal hand-built indicator frame for strategies that only read
    specific columns - avoids depending on real Bollinger/EMA warm-up for
    tests that aren't exercising that machinery."""
    return pd.DataFrame({"atr": [atr] * n, "rsi": [rsi] * n,
                          "ema9": [0.0] * n, "ema21": [0.0] * n, "ema50": [0.0] * n})


# --------------------------------------------------------------- resample
def test_resample_m1_to_tf_aggregates_ohlc_correctly():
    # 10 minutes of M1 bars, clean round timestamps -> 2 M5 bars
    closes = [100 + i for i in range(10)]
    df = make_df(closes, start_time=1_700_000_000 - (1_700_000_000 % 300))
    m5 = resample_m1_to_tf(df, 5)
    assert len(m5) == 2
    assert m5.iloc[0]["open"] == df.iloc[0]["open"]
    assert m5.iloc[0]["close"] == df.iloc[4]["close"]
    assert m5.iloc[0]["high"] == df.iloc[0:5]["high"].max()
    assert m5.iloc[0]["low"] == df.iloc[0:5]["low"].min()


# --------------------------------------------------------- RsiHysteresis
def rsi_strategy(**kwargs) -> RsiHysteresisStrategy:
    return RsiHysteresisStrategy(cooldown_bars=2, max_spread_price=0.5, min_atr_price=0.1,
                                  sl_atr_multiple=2.0, **kwargs)


def test_rsi_hysteresis_fires_buy_on_upward_cross():
    s = rsi_strategy()
    ind = flat_ind(5, atr=0.3)
    ind.loc[2, "rsi"] = 40.0  # prev < lower(48)
    ind.loc[3, "rsi"] = 60.0  # cur >= upper(52)
    df = make_df([100.0] * 5)
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side == "BUY"
    assert sig.sl_distance_price == 0.3 * 2.0


def test_rsi_hysteresis_fires_sell_on_downward_cross():
    s = rsi_strategy()
    ind = flat_ind(5, atr=0.3)
    ind.loc[2, "rsi"] = 60.0
    ind.loc[3, "rsi"] = 40.0
    df = make_df([100.0] * 5)
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side == "SELL"


def test_rsi_hysteresis_no_signal_when_rsi_stays_near_50():
    s = rsi_strategy()
    ind = flat_ind(5, atr=0.3, rsi=50.0)
    df = make_df([100.0] * 5)
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side is None


def test_rsi_hysteresis_respects_cooldown():
    s = rsi_strategy()
    s.on_trade_opened()
    ind = flat_ind(5, atr=0.3)
    ind.loc[2, "rsi"] = 40.0
    ind.loc[3, "rsi"] = 60.0
    df = make_df([100.0] * 5)
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side is None
    assert sig.reason == "cooldown"


def test_rsi_hysteresis_blocked_by_wide_spread():
    s = rsi_strategy()
    ind = flat_ind(5, atr=0.3)
    ind.loc[2, "rsi"] = 40.0
    ind.loc[3, "rsi"] = 60.0
    df = make_df([100.0] * 5)
    sig = s.check(df, ind, spread_price=5.0)
    assert sig.side is None
    assert "spread" in sig.reason


def test_rsi_hysteresis_blocked_by_low_volatility():
    s = rsi_strategy()
    ind = flat_ind(5, atr=0.01)  # below min_atr_price=0.1
    ind.loc[2, "rsi"] = 40.0
    ind.loc[3, "rsi"] = 60.0
    df = make_df([100.0] * 5)
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side is None
    assert "volatility" in sig.reason


# ----------------------------------------------------- DirectionalCandle
def candle_strategy(**kwargs) -> DirectionalCandleStrategy:
    return DirectionalCandleStrategy(cooldown_bars=2, max_spread_price=0.5, min_atr_price=0.1,
                                      sl_buffer_atr_mult=0.3, **kwargs)


def test_directional_candle_fires_buy_on_strong_bullish_thrust():
    s = candle_strategy()
    df = pd.DataFrame([
        {"time": BASE_TIME, "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0},
        {"time": BASE_TIME + 60, "open": 100.0, "high": 101.5, "low": 99.95, "close": 101.4},  # strong bull bar
        {"time": BASE_TIME + 120, "open": 101.4, "high": 101.5, "low": 101.3, "close": 101.4},  # forming bar
    ])
    ind = flat_ind(3, atr=0.3)
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side == "BUY"
    assert sig.sl_distance_price > 0


def test_directional_candle_fires_sell_on_strong_bearish_thrust():
    s = candle_strategy()
    df = pd.DataFrame([
        {"time": BASE_TIME, "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0},
        {"time": BASE_TIME + 60, "open": 101.4, "high": 101.45, "low": 99.95, "close": 100.05},  # strong bear bar
        {"time": BASE_TIME + 120, "open": 100.05, "high": 100.1, "low": 100.0, "close": 100.05},
    ])
    ind = flat_ind(3, atr=0.3)
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side == "SELL"


def test_directional_candle_ignores_small_indecisive_candle():
    s = candle_strategy()
    df = pd.DataFrame([
        {"time": BASE_TIME, "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0},
        {"time": BASE_TIME + 60, "open": 100.0, "high": 100.05, "low": 99.98, "close": 100.02},  # tiny candle
        {"time": BASE_TIME + 120, "open": 100.02, "high": 100.05, "low": 100.0, "close": 100.02},
    ])
    ind = flat_ind(3, atr=0.3)
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side is None


# ----------------------------------------------------------- SessionOpen
def session_strategy(**kwargs) -> SessionOpenStrategy:
    defaults = {"cooldown_bars": 2, "max_spread_price": 0.5, "min_atr_price": 0.1, "sl_atr_multiple": 2.0,
                "london_start_hour": 7, "london_end_hour": 10, "ny_start_hour": 12, "ny_end_hour": 16}
    defaults.update(kwargs)
    return SessionOpenStrategy(**defaults)


def _cross_ind(direction: str, n: int = 4, atr: float = 0.3) -> pd.DataFrame:
    ind = flat_ind(n, atr=atr)
    if direction == "BUY":
        ind.loc[n - 3, "ema9"], ind.loc[n - 3, "ema21"] = 10.0, 10.2
        ind.loc[n - 2, "ema9"], ind.loc[n - 2, "ema21"] = 10.3, 10.2
    else:
        ind.loc[n - 3, "ema9"], ind.loc[n - 3, "ema21"] = 10.2, 10.0
        ind.loc[n - 2, "ema9"], ind.loc[n - 2, "ema21"] = 10.1, 10.2
    return ind


def _epoch_at_utc_hour(hour: int) -> int:
    # BASE_TIME's own UTC hour, then shift to the target hour the same day.
    base_day = BASE_TIME - (BASE_TIME % 86400)
    return base_day + hour * 3600 + 30 * 60  # HH:30:00 UTC


def test_session_open_fires_during_london_window():
    s = session_strategy()
    t = _epoch_at_utc_hour(8)  # inside 07:00-10:00 London window
    df = make_df([100.0, 100.1, 100.2, 100.3], start_time=t - 180)
    assert pd.to_datetime(int(df.iloc[-2]["time"]), unit="s").hour == 8
    ind = _cross_ind("BUY")
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side == "BUY"


def test_session_open_fires_during_ny_window():
    s = session_strategy()
    t = _epoch_at_utc_hour(13)  # inside 12:00-16:00 NY window
    df = make_df([100.0, 100.1, 100.2, 100.3], start_time=t - 180)
    ind = _cross_ind("SELL")
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side == "SELL"


def test_session_open_silent_outside_session_windows():
    s = session_strategy()
    t = _epoch_at_utc_hour(2)  # outside both windows
    df = make_df([100.0, 100.1, 100.2, 100.3], start_time=t - 180)
    ind = _cross_ind("BUY")
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side is None
    assert "session" in sig.reason


def test_session_open_no_signal_without_a_cross_even_inside_the_window():
    s = session_strategy()
    t = _epoch_at_utc_hour(8)
    df = make_df([100.0, 100.1, 100.2, 100.3], start_time=t - 180)
    ind = flat_ind(4, atr=0.3)  # no ema cross set up
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side is None


# ------------------------------------------------------------ MomentumCross
def momentum_strategy(**kwargs) -> MomentumCrossStrategy:
    return MomentumCrossStrategy(cooldown_bars=2, max_spread_price=0.5, min_atr_price=0.05,
                                  sl_atr_multiple=2.0, **kwargs)


def _rising_trend_df(n: int = 400) -> list[float]:
    rng = np.random.default_rng(7)
    return [100 + i * 0.01 + rng.normal(0, 0.03) for i in range(n)]


def test_momentum_cross_fires_buy_when_cross_agrees_with_rising_m5_trend():
    closes = _rising_trend_df(400)
    # Force a clean local dip-then-cross right at the tail, on top of the
    # broader uptrend (which should keep the M5 EMA50 rising).
    closes += [closes[-1] - 0.6, closes[-1] - 0.9, closes[-1] - 0.3, closes[-1] + 0.8, closes[-1] + 1.4]
    df = make_df(closes)
    ind = compute_indicators(df)

    # Self-verify the preconditions this test relies on before asserting
    # the strategy's behavior on top of them (same style as test_strategy.py).
    m5 = resample_m1_to_tf(df, 5)
    m5_ema50 = m5["close"].ewm(span=50, adjust=False).mean()
    assert m5_ema50.iloc[-1] > m5_ema50.iloc[-2], "fixture should produce a rising M5 EMA50"
    prev, last = ind.iloc[-3], ind.iloc[-2]
    assert prev["ema9"] < prev["ema21"] and last["ema9"] >= last["ema21"], "fixture should produce a bullish M1 EMA cross"

    s = momentum_strategy()
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side == "BUY"
    assert sig.sl_distance_price > 0


def test_momentum_cross_silent_with_no_history():
    s = momentum_strategy()
    df = make_df([100.0] * 10)
    ind = compute_indicators(df)
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side is None


# ------------------------------------------------------- AsianRangeBreakout
def asian_strategy(**kwargs) -> AsianRangeBreakoutStrategy:
    defaults = {"cooldown_bars": 2, "max_spread_price": 0.5, "min_atr_price": 0.05, "sl_atr_multiple": 2.0,
                "range_start_hour": 0, "range_end_hour": 7, "breakout_start_hour": 7, "breakout_end_hour": 10,
                "buffer_pct": 0.05}
    defaults.update(kwargs)
    return AsianRangeBreakoutStrategy(**defaults)


def _asian_session_df(range_low: float = 99.0, range_high: float = 101.0, breakout_close: float | None = None):
    """Builds one bar per hour across the Asian range (00:00-07:00 UTC)
    establishing [range_low, range_high], then a final closed bar at 08:00
    UTC (inside the 07:00-10:00 breakout window) closing at breakout_close."""
    base_day = BASE_TIME - (BASE_TIME % 86400)
    rows = []
    for h in range(0, 7):
        t = base_day + h * 3600
        lo = range_low if h == 0 else range_low + 0.3
        hi = range_high if h == 3 else range_high - 0.3
        rows.append({"time": t, "open": (lo + hi) / 2, "high": hi, "low": lo, "close": (lo + hi) / 2})
    close = breakout_close if breakout_close is not None else (range_low + range_high) / 2
    rows.append({"time": base_day + 8 * 3600, "open": close, "high": close + 0.05, "low": close - 0.05, "close": close})
    rows.append({"time": base_day + 8 * 3600 + 60, "open": close, "high": close + 0.02, "low": close - 0.02, "close": close})  # forming bar
    return pd.DataFrame(rows)


def test_asian_breakout_fires_buy_on_confirmed_close_above_the_range():
    s = asian_strategy()
    df = _asian_session_df(range_low=99.0, range_high=101.0, breakout_close=101.5)
    ind = flat_ind(len(df), atr=0.3)
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side == "BUY"


def test_asian_breakout_fires_sell_on_confirmed_close_below_the_range():
    s = asian_strategy()
    df = _asian_session_df(range_low=99.0, range_high=101.0, breakout_close=98.5)
    ind = flat_ind(len(df), atr=0.3)
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side == "SELL"


def test_asian_breakout_silent_when_price_stays_inside_the_range():
    s = asian_strategy()
    df = _asian_session_df(range_low=99.0, range_high=101.0, breakout_close=100.0)
    ind = flat_ind(len(df), atr=0.3)
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side is None


def test_asian_breakout_silent_outside_the_breakout_window():
    s = asian_strategy()
    base_day = BASE_TIME - (BASE_TIME % 86400)
    rows = [{"time": base_day + h * 3600, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0} for h in range(7)]
    # Last closed bar at 11:00 UTC - after the 07:00-10:00 breakout window closes.
    rows.append({"time": base_day + 11 * 3600, "open": 102.0, "high": 102.1, "low": 101.9, "close": 102.0})
    rows.append({"time": base_day + 11 * 3600 + 60, "open": 102.0, "high": 102.1, "low": 101.9, "close": 102.0})
    df = pd.DataFrame(rows)
    ind = flat_ind(len(df), atr=0.3)
    sig = s.check(df, ind, spread_price=0.2)
    assert sig.side is None
    assert "window" in sig.reason


def test_asian_breakout_range_persists_across_incremental_calls():
    """Mirrors real usage: check() is called every poll, not once - the
    range must accumulate across calls, not just work when handed the
    whole day's history in one shot."""
    s = asian_strategy()
    base_day = BASE_TIME - (BASE_TIME % 86400)
    # Feed the Asian session bar-by-bar, like live polling would.
    for h in range(0, 7):
        lo, hi = 99.0 + (0.1 if h != 0 else 0.0), 101.0 - (0.1 if h != 3 else 0.0)
        rows = [{"time": base_day + hh * 3600, "open": 100.0, "high": 101.0 - 0.1, "low": 99.0 + 0.1, "close": 100.0}
                for hh in range(h)]
        rows.append({"time": base_day + h * 3600, "open": (lo + hi) / 2, "high": hi, "low": lo, "close": (lo + hi) / 2})
        rows.append({"time": base_day + h * 3600 + 60, "open": (lo + hi) / 2, "high": hi, "low": lo, "close": (lo + hi) / 2})
        df = pd.DataFrame(rows)
        ind = flat_ind(len(df), atr=0.3)
        s.check(df, ind, spread_price=0.2)

    assert s._range_high == 101.0
    assert s._range_low == 99.0


# ------------------------------------------------------------- Composite
class _AlwaysFires(CooldownGate):
    """Fires every time it's actually asked (still respects cooldown, like
    every real sub-strategy) - used to test CompositeStrategy's fallthrough/
    delegation/lockstep behavior without depending on real indicator math."""

    def __init__(self, side: str = "BUY", sl: float = 0.5, cooldown_bars: int = 2) -> None:
        super().__init__(cooldown_bars)
        self.side = side
        self.sl = sl
        self.calls = 0

    def check(self, df, ind, spread_price) -> SubSignal:
        self.calls += 1
        if not self._cooled_down:
            return SubSignal(side=None, reason="cooldown")
        return SubSignal(side=self.side, sl_distance_price=self.sl, reason="always fires")


class _NeverFires:
    def __init__(self) -> None:
        self.calls = 0

    def on_bar_closed(self) -> None:
        pass

    def on_trade_opened(self) -> None:
        pass

    def check(self, df, ind, spread_price) -> SubSignal:
        self.calls += 1
        return SubSignal(side=None, reason="never fires")


def _flat_candles(n: int = 60) -> pd.DataFrame:
    # >= CompositeStrategy's 55-bar warmup floor (see core/signals.py) so
    # extras actually get evaluated instead of short-circuiting on
    # "not enough history".
    from tests.test_strategy import _ranging_base, build_candles
    return build_candles(_ranging_base(n))


def mean_reversion() -> ScalpStrategy:
    return ScalpStrategy(min_tp_usd=0.28, tp_levels=3, value_per_point_per_lot=100.0)


def test_composite_with_no_extras_matches_bare_scalp_strategy_exactly():
    mr = mean_reversion()
    composite = CompositeStrategy(mean_reversion=mr, extra_strategies=[])
    df = _flat_candles()
    bare_signal = mean_reversion().generate_signal(df, 0.2, 0.01)
    composite_signal = composite.generate_signal(df, 0.2, 0.01)
    assert composite_signal.side == bare_signal.side
    assert composite_signal.reason == bare_signal.reason


def test_composite_prefers_mean_reversion_when_it_fires():
    from tests.test_strategy import oversold_candles
    mr = mean_reversion()
    never = _NeverFires()
    composite = CompositeStrategy(mean_reversion=mr, extra_strategies=[("never", never)])
    signal = composite.generate_signal(oversold_candles(), spread_price=0.2, lot_hint=0.01)
    assert signal.side == "BUY"  # from mean reversion, per test_strategy.py's own fixture
    assert never.calls == 0  # never even evaluated - mean reversion already fired


def test_composite_falls_through_to_first_firing_extra_in_order():
    mr = mean_reversion()
    never = _NeverFires()
    always = _AlwaysFires(side="SELL", sl=0.6)
    composite = CompositeStrategy(mean_reversion=mr, extra_strategies=[("never", never), ("always", always)])
    df = _flat_candles()  # mean reversion stays silent on this fixture
    signal = composite.generate_signal(df, spread_price=0.2, lot_hint=0.01)
    assert signal.side == "SELL"
    assert "always" in signal.reason
    assert never.calls == 1
    assert always.calls == 1


def test_composite_returns_mean_reversions_reason_when_nothing_fires():
    mr = mean_reversion()
    never = _NeverFires()
    composite = CompositeStrategy(mean_reversion=mr, extra_strategies=[("never", never)])
    df = _flat_candles()
    signal = composite.generate_signal(df, spread_price=0.2, lot_hint=0.01)
    assert signal.side is None
    assert signal.reason  # mean reversion's own specific rejection reason, not a generic string


def test_composite_delegates_tp_ladder_and_min_tp_distance_to_mean_reversion():
    mr = mean_reversion()
    composite = CompositeStrategy(mean_reversion=mr, extra_strategies=[])
    assert composite.build_tp_ladder(0.02, 0.2) == mr.build_tp_ladder(0.02, 0.2)
    assert composite.min_tp_distance_for_lot(0.02) == mr.min_tp_distance_for_lot(0.02)


def test_composite_applies_sl_floor_so_stop_is_never_tighter_than_tp1():
    mr = mean_reversion()
    tiny_sl = _AlwaysFires(side="BUY", sl=0.00001)  # unrealistically tight
    composite = CompositeStrategy(mean_reversion=mr, extra_strategies=[("tiny", tiny_sl)])
    df = _flat_candles()
    signal = composite.generate_signal(df, spread_price=0.2, lot_hint=0.01)
    assert signal.side == "BUY"
    assert signal.sl_distance_price >= mr.min_tp_distance_for_lot(0.01) * 1.5


def test_composite_shares_cooldown_lockstep_across_all_sub_strategies():
    mr = mean_reversion()
    always = _AlwaysFires(side="BUY")
    composite = CompositeStrategy(mean_reversion=mr, extra_strategies=[("always", always)])
    composite.on_trade_opened()  # resets every sub-strategy's cooldown together
    df = _flat_candles()
    signal = composite.generate_signal(df, spread_price=0.2, lot_hint=0.01)
    assert signal.side is None  # blocked by cooldown on both mean_reversion AND the extra
    for _ in range(mr.cooldown_bars):
        composite.on_bar_closed()
    signal = composite.generate_signal(df, spread_price=0.2, lot_hint=0.01)
    assert signal.side == "BUY"  # cooldown cleared, extra fires now


def test_composite_computes_and_applies_vol_ratio_when_an_extra_fires():
    """When mean-reversion stays silent and an extra signal fires,
    CompositeStrategy must compute its own vol_ratio from the shared `ind`
    frame (mean-reversion's own generate_signal never ran to compute one)
    and use it to build that signal's TP ladder - not silently fall back
    to the static vol_ratio=1.0 ladder."""
    mr = mean_reversion()
    always = _AlwaysFires(side="BUY")
    composite = CompositeStrategy(mean_reversion=mr, extra_strategies=[("always", always)])
    df = _flat_candles()

    signal = composite.generate_signal(df, spread_price=0.2, lot_hint=0.01)
    assert signal.side == "BUY"
    assert 0.5 <= signal.vol_ratio <= 2.0

    expected_ladder = mr.build_tp_ladder(0.01, 0.2, vol_ratio=signal.vol_ratio)
    assert signal.tp_levels == expected_ladder
