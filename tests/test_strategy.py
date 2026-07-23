import numpy as np
import pandas as pd

from core.strategy import ScalpStrategy, Signal, compute_indicators


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


def test_tp_targets_usd_unset_preserves_original_ladder_behavior():
    """Backward compatibility: leaving TP_TARGETS_USD unset must produce
    byte-for-byte the same ladder as before this feature existed - the
    README's backtest history is tied to this exact default behavior."""
    with_targets_unset = ScalpStrategy(min_tp_usd=0.28, tp_levels=3, value_per_point_per_lot=100.0)
    without_targets_param = strategy()
    lot, spread = 0.02, 0.2
    assert with_targets_unset.build_tp_ladder(lot, spread) == without_targets_param.build_tp_ladder(lot, spread)


def test_tp_targets_usd_each_level_nets_its_own_configured_target():
    """The actual feature: each level books ITS OWN dollar amount, not
    just a price-distance multiple of the first level."""
    targets = [0.28, 0.60, 1.20]
    s = ScalpStrategy(min_tp_usd=0.28, tp_levels=3, value_per_point_per_lot=100.0,
                       tp_targets_usd=targets)
    lot = 0.02
    ladder = s.build_tp_ladder(lot=lot, spread_price=0.2)
    assert len(ladder) == 3
    for level, target in zip(ladder, targets, strict=True):
        # distance includes a spread buffer, so realized profit is
        # slightly ABOVE the raw target, never below it.
        realized_usd = level.distance_price * s.value_per_point_per_lot * (lot * level.close_fraction)
        assert realized_usd >= target
        assert realized_usd < target + 5.0  # sanity: buffer shouldn't balloon it


def test_tp_targets_usd_shorter_than_tp_levels_repeats_the_last_value():
    s = ScalpStrategy(min_tp_usd=0.28, tp_levels=3, value_per_point_per_lot=100.0,
                       tp_targets_usd=[0.28])  # only one target for 3 levels
    ladder = s.build_tp_ladder(lot=0.02, spread_price=0.2)
    assert len(ladder) == 3
    distances = [l.distance_price for l in ladder]
    assert distances == sorted(distances)
    assert all(d > 0 for d in distances)


def test_tp_targets_usd_misconfigured_as_decreasing_still_produces_a_valid_ladder():
    """A ladder must scale outward - if someone configures decreasing
    targets by mistake, the levels must still increase in price distance
    (price can't reach a 'further' TP before a 'closer' one), not silently
    build a broken ladder."""
    s = ScalpStrategy(min_tp_usd=0.28, tp_levels=3, value_per_point_per_lot=100.0,
                       tp_targets_usd=[1.20, 0.60, 0.28])  # deliberately decreasing
    ladder = s.build_tp_ladder(lot=0.02, spread_price=0.2)
    distances = [l.distance_price for l in ladder]
    assert distances == sorted(distances)
    assert len(set(distances)) == 3  # strictly increasing, not just non-decreasing


# ------------------------------------------------ adaptive ladder (vol_ratio)
def test_vol_ratio_default_matches_the_original_static_ladder_exactly():
    s = strategy()
    default_ladder = s.build_tp_ladder(lot=0.02, spread_price=0.2)
    explicit_neutral = s.build_tp_ladder(lot=0.02, spread_price=0.2, vol_ratio=1.0)
    assert default_ladder == explicit_neutral


def test_low_vol_ratio_produces_a_denser_ladder_for_levels_after_the_first():
    s = strategy()
    quiet = s.build_tp_ladder(lot=0.02, spread_price=0.2, vol_ratio=0.5)
    normal = s.build_tp_ladder(lot=0.02, spread_price=0.2, vol_ratio=1.0)
    # TP1 (index 0) is never touched by vol_ratio - only spacing beyond it.
    assert quiet[0].distance_price == normal[0].distance_price
    assert quiet[1].distance_price < normal[1].distance_price
    assert quiet[2].distance_price < normal[2].distance_price


def test_high_vol_ratio_produces_a_wider_ladder_for_levels_after_the_first():
    s = strategy()
    volatile = s.build_tp_ladder(lot=0.02, spread_price=0.2, vol_ratio=2.0)
    normal = s.build_tp_ladder(lot=0.02, spread_price=0.2, vol_ratio=1.0)
    assert volatile[0].distance_price == normal[0].distance_price
    assert volatile[1].distance_price > normal[1].distance_price
    assert volatile[2].distance_price > normal[2].distance_price


def test_adaptive_ladder_still_nets_at_least_min_tp_usd_on_tp1_regardless_of_vol_ratio():
    s = strategy()
    lot = 0.02
    for vr in (0.5, 1.0, 2.0):
        ladder = s.build_tp_ladder(lot=lot, spread_price=0.2, vol_ratio=vr)
        tp1 = ladder[0]
        realized_usd = tp1.distance_price * s.value_per_point_per_lot * (lot * tp1.close_fraction)
        assert realized_usd >= s.min_tp_usd


def test_tp_targets_usd_path_ignores_vol_ratio_entirely():
    """Explicit operator-chosen dollar targets are untouched by the
    adaptive spacing feature - same guarantee as before it existed."""
    targets = [0.28, 0.60, 1.20]
    s = ScalpStrategy(min_tp_usd=0.28, tp_levels=3, value_per_point_per_lot=100.0, tp_targets_usd=targets)
    quiet = s.build_tp_ladder(lot=0.02, spread_price=0.2, vol_ratio=0.5)
    volatile = s.build_tp_ladder(lot=0.02, spread_price=0.2, vol_ratio=2.0)
    assert quiet == volatile


def test_compute_vol_ratio_clamps_to_configured_bounds():
    from core.strategy import VOL_RATIO_MAX, VOL_RATIO_MIN, compute_vol_ratio
    assert compute_vol_ratio(atr=10.0, atr_baseline=0.1) == VOL_RATIO_MAX  # would be 100x uncapped
    assert compute_vol_ratio(atr=0.01, atr_baseline=10.0) == VOL_RATIO_MIN  # would be ~0.001x uncapped
    assert compute_vol_ratio(atr=1.0, atr_baseline=1.0) == 1.0


def test_compute_vol_ratio_is_neutral_on_missing_or_invalid_data():
    from core.strategy import compute_vol_ratio
    assert compute_vol_ratio(atr=float("nan"), atr_baseline=1.0) == 1.0
    assert compute_vol_ratio(atr=1.0, atr_baseline=float("nan")) == 1.0
    assert compute_vol_ratio(atr=1.0, atr_baseline=0.0) == 1.0


def test_generate_signal_sets_a_sane_vol_ratio_on_a_real_signal():
    s = strategy()
    signal = s.generate_signal(oversold_candles(), spread_price=0.2, lot_hint=0.01)
    assert signal.side == "BUY"
    assert 0.5 <= signal.vol_ratio <= 2.0


def test_signal_without_an_explicit_vol_ratio_defaults_to_neutral():
    assert Signal(side=None).vol_ratio == 1.0


# ------------------------------------------------ precomputed indicators
def _trending_down_candles(n: int = 60) -> pd.DataFrame:
    close = 2000.0 - np.arange(n) * 0.5
    return pd.DataFrame({
        "time": range(n), "open": close + 0.2, "high": close + 0.3,
        "low": close - 0.3, "close": close,
    })


def test_generate_signal_uses_precomputed_indicators_when_given():
    """When precomputed_indicators is passed, generate_signal must use it
    verbatim (not recompute) - proven by feeding a precomputed frame with
    deliberately doctored indicator values that would flip the decision
    (force rsi/bb into an oversold BUY setup) even though the real
    (uncomputed-from-df) values wouldn't produce that signal."""
    strat = ScalpStrategy(min_tp_usd=0.5, tp_levels=3, value_per_point_per_lot=1.0)
    df = _trending_down_candles()

    real_ind = compute_indicators(df, bb_period=strat.bb_period, bb_std=strat.bb_std,
                                   rsi_period=strat.rsi_period, atr_period=strat.atr_period,
                                   adx_period=strat.adx_period)
    doctored = real_ind.copy()
    last_idx = doctored.index[-1]
    doctored.loc[last_idx, "bb_lower"] = df["close"].iloc[-1] + 10  # close is now "below" bb_lower
    doctored.loc[last_idx, "rsi"] = 5.0  # deeply oversold
    doctored.loc[last_idx, "adx"] = 0.0  # no trend filter block
    doctored.loc[last_idx, "atr"] = 5.0  # well above min_atr_price

    result = strat.generate_signal(df, spread_price=0.2, lot_hint=0.01, precomputed_indicators=doctored)
    assert result.side == "BUY", f"expected doctored precomputed indicators to force a BUY, got {result}"


def test_generate_signal_without_precomputed_indicators_unchanged():
    """precomputed_indicators=None (the default) must behave exactly like
    calling generate_signal with the old 3-arg signature."""
    strat = ScalpStrategy(min_tp_usd=0.5, tp_levels=3, value_per_point_per_lot=1.0)
    df = _trending_down_candles()
    old_style = strat.generate_signal(df, 0.2, 0.01)
    new_style = strat.generate_signal(df, 0.2, 0.01, precomputed_indicators=None)
    assert old_style == new_style
