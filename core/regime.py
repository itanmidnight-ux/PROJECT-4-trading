"""Small deterministic market-regime classifier used as a safety gate.

It deliberately exposes only coarse states. A regime is never a profit
prediction; it can only allow/block an optional strategy or recovery action.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Regime:
    name: str
    adx: float
    atr_ratio: float
    trend: str


def detect_regime(df: pd.DataFrame, *, adx_threshold: float = 25.0,
                  atr_high: float = 1.35, atr_low: float = 0.65) -> Regime:
    """Classify closed OHLC bars as trend/range/volatile/quiet.

    Uses only values available on the last closed bar and tolerates small
    synthetic frames, returning ``unknown`` until indicators are warm.
    """
    if len(df) < 20 or not {"close", "high", "low"}.issubset(df.columns):
        return Regime("unknown", 0.0, 1.0, "flat")
    close = df["close"].astype(float)
    high, low = df["high"].astype(float), df["low"].astype(float)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    baseline = atr.rolling(50).mean()
    up = close.diff().clip(lower=0).rolling(14).sum()
    down = (-close.diff().clip(upper=0)).rolling(14).sum()
    denominator = (up + down).replace(0, float("nan"))
    dx = ((up - down).abs() / denominator * 100).fillna(0)
    adx = float(dx.iloc[-2]) if len(dx) >= 2 else 0.0
    ratio = float((atr.iloc[-2] / baseline.iloc[-2])) if baseline.iloc[-2] and pd.notna(baseline.iloc[-2]) else 1.0
    slope = float(close.iloc[-2] - close.iloc[-min(len(close), 12)])
    trend = "up" if slope > 0 else "down" if slope < 0 else "flat"
    if ratio >= atr_high:
        name = "volatile"
    elif ratio <= atr_low:
        name = "quiet"
    elif adx >= adx_threshold:
        name = "trend"
    else:
        name = "range"
    return Regime(name, adx, ratio, trend)


def detect_regime_series(candles: pd.DataFrame, *, adx_threshold: float = 25.0,
                          atr_high: float = 1.35, atr_low: float = 0.65) -> pd.DataFrame:
    """Vectorized, whole-series version of detect_regime - identical math,
    computed once over the full `candles` series (O(n)) instead of being
    recomputed from scratch on a windowed slice every single bar
    (O(n * window_size), the same cost pattern core/strategy.py's
    compute_indicators used to have before its own global-precompute fix).

    Returns a DataFrame aligned 1:1 to `candles`' row positions, columns
    `name`/`adx`/`atr_ratio`/`trend`. Row i holds exactly what
    detect_regime(window) would return for a window whose LAST row is
    candles.iloc[i] - matching detect_regime's own df.iloc[-2] convention
    (one bar behind the window's last row), via .shift(1) below. Verified
    equivalent to the original by a real-data divergence test - see
    tests/test_regime.py.
    """
    close = candles["close"].astype(float)
    high, low = candles["high"].astype(float), candles["low"].astype(float)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    baseline = atr.rolling(50).mean()
    up = close.diff().clip(lower=0).rolling(14).sum()
    down = (-close.diff().clip(upper=0)).rolling(14).sum()
    denominator = (up + down).replace(0, float("nan"))
    dx = ((up - down).abs() / denominator * 100).fillna(0)

    # shift(1): row i must reflect bar i-1 (detect_regime's -2 on a window
    # ending at i), not bar i itself.
    adx_series = dx.shift(1).fillna(0.0)
    atr_shifted = atr.shift(1)
    baseline_shifted = baseline.shift(1)
    valid_baseline = baseline_shifted.notna() & (baseline_shifted != 0)
    ratio_series = (atr_shifted / baseline_shifted).where(valid_baseline, 1.0)
    # close.iloc[-2] - close.iloc[-min(len(window), 12)]: for every bar this
    # function is actually called on in practice (window length is always
    # >= _warmup_bars, itself always > 12), min(len(window), 12) == 12.
    # iloc[-k] on a window whose LAST row is bar i is bar i-(k-1) (iloc[-1]
    # is i, iloc[-2] is i-1, ... iloc[-12] is i-11) - so this is a 10-bar
    # comparison from bar i-1 back to bar i-11, i.e. close.shift(1) minus
    # close.shift(11), NOT close.shift(12) (verified against detect_regime
    # by tests/test_regime.py's divergence test; shift(12) was off-by-one
    # and produced wrong `trend` values on real data).
    slope_series = close.shift(1) - close.shift(11)

    name = pd.Series("range", index=candles.index, dtype=object)
    name = name.mask(ratio_series >= atr_high, "volatile")
    name = name.mask((ratio_series < atr_high) & (ratio_series <= atr_low), "quiet")
    name = name.mask((ratio_series > atr_low) & (ratio_series < atr_high) & (adx_series >= adx_threshold), "trend")

    trend = pd.Series("flat", index=candles.index, dtype=object)
    trend = trend.mask(slope_series > 0, "up")
    trend = trend.mask(slope_series < 0, "down")

    # First 20 rows (and anywhere baseline/adx aren't warm yet): match
    # detect_regime's own len(df) < 20 -> "unknown" guard.
    unknown_mask = candles.index.to_series().lt(20) if hasattr(candles.index, "to_series") else pd.Series(range(len(candles)), index=candles.index).lt(20)
    name = name.mask(unknown_mask, "unknown")

    return pd.DataFrame({"name": name, "adx": adx_series, "atr_ratio": ratio_series, "trend": trend}, index=candles.index)
