"""Market data sources. BridgeMarketData is the real one (via Wine/MT5).
SyntheticMarketData lets the engine, strategy, and dashboard be exercised
end-to-end on a machine with no broker connection at all - used by the
test suite and for a first local dry run before anyone touches a real
account."""
from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.mt5_bridge_client import Mt5BridgeClient, Tick


@dataclass
class LiveState:
    tick: Tick
    candles: pd.DataFrame


class MarketDataSource(ABC):
    @abstractmethod
    def get_state(self, symbol: str, timeframe: str, count: int) -> LiveState:
        ...


class BridgeMarketData(MarketDataSource):
    def __init__(self, client: Mt5BridgeClient) -> None:
        self.client = client

    def get_state(self, symbol: str, timeframe: str, count: int) -> LiveState:
        tick = self.client.price(symbol)
        candles = self.client.candles(symbol, timeframe, count)
        return LiveState(tick=tick, candles=candles)


class SyntheticMarketData(MarketDataSource):
    """
    Random-walk generator calibrated loosely to XAUUSD 1m behaviour
    (mean-reverting noise around a slow drift, spread in the 0.15-0.35
    range). This is for local testing ONLY - it proves the engine's
    plumbing works, it says nothing about real profitability.
    """

    def __init__(self, start_price: float = 2400.0, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._np_rng = np.random.default_rng(seed)
        self._price = start_price
        self._candles: list[dict] = []
        self._bootstrap(300)

    def _bootstrap(self, n: int) -> None:
        t = int(time.time()) - n * 60
        for _ in range(n):
            self._candles.append(self._next_candle(t))
            t += 60

    def _next_candle(self, t: int) -> dict:
        o = self._price
        drift = self._np_rng.normal(0, 0.05)
        vol = abs(self._np_rng.normal(0.35, 0.15)) + 0.05
        path = np.cumsum(self._np_rng.normal(0, vol / 4, 6)) + drift
        c = o + path[-1]
        h = max(o, c) + abs(self._np_rng.normal(0, vol / 3))
        l = min(o, c) - abs(self._np_rng.normal(0, vol / 3))
        self._price = c
        return {"time": t, "open": o, "high": h, "low": l, "close": c, "tick_volume": self._rng.randint(20, 200)}

    def get_state(self, symbol: str, timeframe: str, count: int) -> LiveState:
        self._candles.append(self._next_candle(int(time.time())))
        self._candles = self._candles[-max(count, 300):]
        df = pd.DataFrame(self._candles[-count:])
        spread = round(abs(self._np_rng.normal(0.22, 0.06)) + 0.05, 3)
        mid = self._price
        tick = Tick(bid=mid - spread / 2, ask=mid + spread / 2, spread_price=spread, time=int(time.time()))
        return LiveState(tick=tick, candles=df)
