import numpy as np
import pandas as pd

from core.strategy import ScalpStrategy, compute_indicators


def build_candles(closes: list[float]) -> pd.DataFrame:
    opens = [closes[0]] + closes[:-1]
    rows = []
    for i, (o, c) in enumerate(zip(opens, closes, strict=True)):
        rows.append({
            "time": 1_700_000_000 + i * 60,
            "open": o, "high": max(o, c), "low": min(o, c), "close": c,
            "tick_volume": 50,
        })
    return pd.DataFrame(rows)


def _ranging_base(n: int = 29) -> list[float]:
    """A bounded, oscillating price path (not a flat line, not a trend) -
    deliberately NOT quiet noise before a spike: a single sharp move out of
    genuine quiet chop reads as a strong trend to ADX (see the trend filter
    in core/strategy.py), which made the old fixture here trip that filter
    and fail every 'oversold' test. Oscillation keeps ADX low the way real
    ranging chop would, the same regime this filter is meant to allow."""
    return [2400 + 0.3 * np.sin(i * 0.9) for i in range(n)]


def oversold_candles() -> pd.DataFrame:
    closes = _ranging_base(29)
    closes.append(closes[-1] - 3.0)  # sharp drop out of the range, on the last bar
    return build_candles(closes)


def overbought_candles() -> pd.DataFrame:
    closes = _ranging_base(29)
    closes.append(closes[-1] + 3.0)  # sharp jump out of the range, on the last bar
    return build_candles(closes)


def flat_candles() -> pd.DataFrame:
    return build_candles(_ranging_base(39))  # no breakout bar - stays neutral (RSI ~50)


def strategy() -> ScalpStrategy:
    return ScalpStrategy(min_tp_usd=0.28, tp_levels=3, value_per_point_per_lot=100.0)


def test_compute_indicators_has_expected_columns():
    df = compute_indicators(oversold_candles())
    for col in ("bb_upper", "bb_lower", "bb_mid", "rsi", "atr"):
        assert col in df.columns
    assert 0 <= df["rsi"].iloc[-1] <= 100


def test_oversold_move_triggers_buy_signal():
    s = strategy()
    signal = s.generate_signal(oversold_candles(), spread_price=0.2, lot_hint=0.01)
    assert signal.side == "BUY"
    assert signal.sl_distance_price > 0
    assert len(signal.tp_levels) == 3


def test_overbought_move_triggers_sell_signal():
    s = strategy()
    signal = s.generate_signal(overbought_candles(), spread_price=0.2, lot_hint=0.01)
    assert signal.side == "SELL"


def test_flat_market_gives_no_signal():
    s = strategy()
    signal = s.generate_signal(flat_candles(), spread_price=0.2, lot_hint=0.01)
    assert signal.side is None


def test_wide_spread_blocks_signal_even_on_a_real_setup():
    s = strategy()
    signal = s.generate_signal(oversold_candles(), spread_price=5.0, lot_hint=0.01)
    assert signal.side is None
    assert "spread" in signal.reason


def test_cooldown_blocks_immediate_reentry():
    s = strategy()
    s.on_trade_opened()
    signal = s.generate_signal(oversold_candles(), spread_price=0.2, lot_hint=0.01)
    assert signal.side is None
    assert signal.reason == "cooldown"


def test_cooldown_clears_after_enough_bars():
    s = strategy()
    s.on_trade_opened()
    for _ in range(s.cooldown_bars):
        s.on_bar_closed()
    signal = s.generate_signal(oversold_candles(), spread_price=0.2, lot_hint=0.01)
    assert signal.side == "BUY"


def test_tp_ladder_fractions_sum_to_one_and_distances_increase():
    s = strategy()
    ladder = s.build_tp_ladder(lot=0.02, spread_price=0.2)
    assert len(ladder) == 3
    assert abs(sum(l.close_fraction for l in ladder) - 1.0) < 1e-9
    distances = [l.distance_price for l in ladder]
    assert distances == sorted(distances)
    assert all(d > 0 for d in distances)


def test_indicator_periods_are_configurable_and_change_warmup():
    default = strategy()
    tuned = ScalpStrategy(min_tp_usd=0.28, tp_levels=3, value_per_point_per_lot=100.0,
                           bb_period=50, rsi_period=10, atr_period=20)
    assert tuned._warmup_bars > default._warmup_bars

    # 30 bars is enough for the default (bb_period=20) but not for a
    # strategy tuned to a 50-period Bollinger band.
    signal = tuned.generate_signal(oversold_candles(), spread_price=0.2, lot_hint=0.01)
    assert signal.side is None
    assert signal.reason == "not enough history"


def _trending_candles() -> pd.DataFrame:
    """A steady climb (not chop with a breakout bar) - exactly what ADX is
    supposed to flag as a strong trend, unlike the ranging fixtures above.
    A little noise is essential: a perfectly linear, zero-noise series has
    zero down-ticks, which is its own RSI edge case (avg_loss=0 exactly),
    not a realistic 'strong trend' stand-in."""
    rng = np.random.default_rng(4)
    return build_candles([2400 + i * 0.15 + rng.normal(0, 0.12) for i in range(30)])


def test_trend_filter_blocks_signals_in_a_strong_trend():
    """This is the fix for the bug that blew up the 8-day backtest: fading
    a strong directional move (instead of only genuine range-bound chop)
    is what produced the sequence of large losses."""
    from core.strategy import compute_indicators
    candles = _trending_candles()
    adx = compute_indicators(candles)["adx"].iloc[-1]
    assert adx >= 35.0, f"fixture should read as a strong trend, got ADX={adx:.1f}"

    s = strategy()
    signal = s.generate_signal(candles, spread_price=0.2, lot_hint=0.01)
    assert signal.side is None
    assert "trend" in signal.reason.lower()


def test_trend_filter_threshold_is_configurable():
    candles = _trending_candles()
    strict = strategy()  # default threshold (35.0)
    assert "trend" in strict.generate_signal(candles, spread_price=0.2, lot_hint=0.01).reason.lower()

    permissive = ScalpStrategy(min_tp_usd=0.28, tp_levels=3, value_per_point_per_lot=100.0,
                                trend_filter_adx_threshold=99.0)
    permissive_reason = permissive.generate_signal(candles, spread_price=0.2, lot_hint=0.01).reason
    assert "trend" not in permissive_reason.lower()


def test_ranging_oversold_setup_clears_the_trend_filter():
    """The companion check: genuine range-bound chop with a breakout bar
    must NOT be mistaken for a strong trend (this is what the fixture
    generators in this file are specifically built to produce)."""
    from core.strategy import compute_indicators
    adx = compute_indicators(oversold_candles())["adx"].iloc[-1]
    assert adx < 35.0, f"ranging fixture should NOT read as a strong trend, got ADX={adx:.1f}"


def test_tp1_nets_at_least_min_tp_usd_for_its_slice():
    s = strategy()
    lot = 0.02
    ladder = s.build_tp_ladder(lot=lot, spread_price=0.2)
    tp1 = ladder[0]
    realized_usd = tp1.distance_price * s.value_per_point_per_lot * (lot * tp1.close_fraction)
    assert realized_usd >= s.min_tp_usd
