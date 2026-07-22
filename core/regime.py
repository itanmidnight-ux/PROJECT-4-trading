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
