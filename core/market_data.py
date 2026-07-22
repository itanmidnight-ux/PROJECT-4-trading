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
    """Fetches the tick fresh on every poll, but keeps a local cache of the
    candle window and only asks the bridge for the last few bars each time
    (INCREMENTAL_BARS) instead of re-downloading the entire window.

    Why: the engine polls every poll_seconds (default 2s) and the full
    window is candle_history_count bars (default 600) - that's a ~600-row
    JSON payload per poll for data where at most ONE new bar can appear
    per minute and only the forming (last) bar ever changes. Over the
    remote-bridge link a remote-client setup uses (this machine ->
    another one over the network), that's real bandwidth and latency per poll for
    ~597 rows that are guaranteed identical to what we already have.

    Correctness invariant: the merged window must be bar-for-bar identical
    to what a full fetch would have returned. Closed bars are immutable in
    MT5, so replacing the overlap region with the freshly fetched tail
    (which includes the mutable forming bar) preserves that - and any case
    where the invariant CAN'T be guaranteed locally falls back to a full
    fetch instead of guessing:
      - first call ever (nothing cached),
      - caller wants more bars than the cache holds,
      - the fetched tail doesn't overlap the cache (engine was stopped/
        wedged long enough that >INCREMENTAL_BARS-1 new bars appeared,
        weekend gap included - bar TIMES aren't assumed contiguous, only
        overlap is checked, so ordinary market gaps don't false-trigger).
    """

    # 3 = the forming bar + 2 closed bars of slack. At poll_seconds=2 the
    # engine polls ~30x per bar, so consecutive polls can only ever be 0 or
    # 1 bars apart - 2 closed bars of overlap is already generous, and any
    # bigger jump (process stopped for minutes) hits the gap fallback.
    INCREMENTAL_BARS = 3

    def __init__(self, client: Mt5BridgeClient) -> None:
        self.client = client
        self._cache: pd.DataFrame | None = None
        self._cache_key: tuple[str, str] | None = None  # (symbol, timeframe) the cache belongs to

    def get_state(self, symbol: str, timeframe: str, count: int) -> LiveState:
        tick = self.client.price(symbol)
        candles = self._get_candles(symbol, timeframe, count)
        return LiveState(tick=tick, candles=candles)

    def _get_candles(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        key = (symbol, timeframe)
        if self._cache is None or self._cache_key != key or len(self._cache) < count:
            full = self.client.candles(symbol, timeframe, count)
            self._cache, self._cache_key = full, key
            return full.copy()

        tail = self.client.candles(symbol, timeframe, self.INCREMENTAL_BARS)
        if tail.empty:
            return self._cache.tail(count).reset_index(drop=True)

        oldest_new = int(tail["time"].iloc[0])
        cached_times = self._cache["time"]
        if oldest_new > int(cached_times.iloc[-1]):
            # No overlap with what we have - too much happened since the
            # last poll to reconstruct the window locally. Refetch fully.
            full = self.client.candles(symbol, timeframe, count)
            self._cache = full
            return full.copy()

        merged = pd.concat([self._cache[cached_times < oldest_new], tail], ignore_index=True)
        # Never let a small-window caller (engine's restart-reconcile asks
        # for just 5 bars) shrink the cache below what it already held, or
        # the next full-window poll would needlessly refetch everything -
        # and cap it at that same size so it can't grow without bound.
        keep = max(count, len(self._cache))
        self._cache = merged.tail(keep).reset_index(drop=True)
        return self._cache.tail(count).reset_index(drop=True)


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
