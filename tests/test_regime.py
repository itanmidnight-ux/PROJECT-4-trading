import pandas as pd
from pathlib import Path
from core.regime import detect_regime, detect_regime_series


def test_detect_regime_series_matches_detect_regime_on_real_data():
    path = Path(__file__).resolve().parent.parent / "data" / "gold_m1_7d.csv"
    if not path.exists():
        import pytest
        pytest.skip("data/gold_m1_7d.csv not present in this checkout")
    candles = pd.read_csv(path).tail(2000).reset_index(drop=True)

    vectorized = detect_regime_series(candles)

    mismatches = 0
    checked = 0
    for i in range(25, len(candles), 17):  # sample every 17th bar, skip warmup
        window = candles.iloc[max(0, i + 1 - 600): i + 1]
        expected = detect_regime(window)
        got = vectorized.iloc[i]
        checked += 1
        if expected.name != got["name"] or expected.trend != got["trend"]:
            mismatches += 1
            continue
        if abs(expected.adx - got["adx"]) > 0.5 or abs(expected.atr_ratio - got["atr_ratio"]) > 0.01:
            mismatches += 1

    assert checked > 50, "sanity: the sampling loop actually ran enough iterations"
    assert mismatches == 0, f"{mismatches}/{checked} sampled bars diverged between detect_regime and detect_regime_series"


def test_detect_regime_series_matches_detect_regime_at_warmup_boundary():
    """Explicit low-index coverage for the len(df) < 20 "unknown" guard.

    The sampled-range test above only checks i >= 25, which is well past the
    boundary and can never catch an off-by-one in how detect_regime_series
    replicates detect_regime's `len(df) < 20 -> unknown` guard. Row i=19 is
    the first row whose window has length exactly 20 (not < 20), so it must
    get a real classification, not "unknown"; rows i<=18 must exactly match
    Regime("unknown", 0.0, 1.0, "flat").
    """
    path = Path(__file__).resolve().parent.parent / "data" / "gold_m1_7d.csv"
    if not path.exists():
        import pytest
        pytest.skip("data/gold_m1_7d.csv not present in this checkout")
    candles = pd.read_csv(path).tail(2000).reset_index(drop=True)

    vectorized = detect_regime_series(candles)

    for i in (15, 18, 19, 20, 21):
        window = candles.iloc[max(0, i + 1 - 600): i + 1]
        expected = detect_regime(window)
        got = vectorized.iloc[i]

        if i <= 18:
            # Window length is i+1 <= 19 < 20 -> detect_regime's guard fires.
            assert expected.name == "unknown", f"test sanity: expected 'unknown' at i={i}"
            assert got["name"] == "unknown", f"i={i}: name"
            assert got["adx"] == 0.0, f"i={i}: adx"
            assert got["atr_ratio"] == 1.0, f"i={i}: atr_ratio"
            assert got["trend"] == "flat", f"i={i}: trend"
        else:
            # Window length is i+1 >= 20 -> guard does NOT fire, must match
            # detect_regime's real classification exactly on name/trend.
            assert expected.name != "unknown", f"test sanity: expected a real regime at i={i}"
            assert expected.name == got["name"], f"i={i}: name"
            assert expected.trend == got["trend"], f"i={i}: trend"
            assert abs(expected.adx - got["adx"]) <= 0.5, f"i={i}: adx"
            assert abs(expected.atr_ratio - got["atr_ratio"]) <= 0.01, f"i={i}: atr_ratio"
