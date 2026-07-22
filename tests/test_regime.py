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
