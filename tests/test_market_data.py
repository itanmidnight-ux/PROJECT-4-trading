"""Tests for BridgeMarketData's incremental candle sync - the remote-bridge
bandwidth optimization for a remote-client (this machine -> remote bridge) setup.

The one invariant that matters: the window BridgeMarketData hands the
engine must be bar-for-bar IDENTICAL to what a naive full fetch would have
returned at that moment. These tests run both against a simulated bridge
feed and diff the results directly, rather than asserting on cache
internals.
"""
from __future__ import annotations

import pandas as pd

from core.market_data import BridgeMarketData
from core.mt5_bridge_client import Tick


class FakeBridgeFeed:
    """Stands in for Mt5BridgeClient: holds a full bar history that
    advances bar-by-bar, serves candles(count) as 'the last N bars right
    now' exactly like the real bridge does, and counts how many BARS each
    call transferred (the thing the optimization is supposed to shrink)."""

    def __init__(self, n_bars: int = 700) -> None:
        self.history = [
            {"time": 1_700_000_000 + i * 60, "open": 4000.0 + i, "high": 4001.0 + i,
             "low": 3999.0 + i, "close": 4000.5 + i, "tick_volume": 10 + i}
            for i in range(n_bars)
        ]
        self.visible = 650  # bars that "exist" so far; advance() reveals more
        self.calls: list[int] = []  # count requested per candles() call

    def advance(self, bars: int = 1) -> None:
        self.visible = min(self.visible + bars, len(self.history))

    def mutate_forming_bar(self) -> None:
        # The forming (newest) bar's close moves tick by tick in reality -
        # the cache must pick up the fresh value, not serve a stale one.
        self.history[self.visible - 1]["close"] += 0.37

    def price(self, symbol: str) -> Tick:
        last = self.history[self.visible - 1]
        return Tick(bid=last["close"] - 0.1, ask=last["close"] + 0.1,
                    spread_price=0.2, time=last["time"])

    def candles(self, symbol: str, timeframe: str = "M1", count: int = 200) -> pd.DataFrame:
        self.calls.append(count)
        return pd.DataFrame(self.history[max(0, self.visible - count):self.visible])


def full_fetch(feed: FakeBridgeFeed, count: int) -> pd.DataFrame:
    return pd.DataFrame(feed.history[max(0, feed.visible - count):feed.visible])


def test_first_call_fetches_the_full_window():
    feed = FakeBridgeFeed()
    md = BridgeMarketData(feed)
    state = md.get_state("XAUUSD", "M1", 600)
    assert feed.calls == [600]
    pd.testing.assert_frame_equal(state.candles, full_fetch(feed, 600))


def test_steady_polling_transfers_only_the_tail_and_stays_identical_to_a_full_fetch():
    """The core guarantee: across many polls (same bar re-polled, forming
    bar mutating, new bars appearing) every returned window matches a
    naive full fetch exactly, while only INCREMENTAL_BARS travel per poll."""
    feed = FakeBridgeFeed()
    md = BridgeMarketData(feed)
    md.get_state("XAUUSD", "M1", 600)  # warm the cache (full fetch)

    for step in range(12):
        if step % 3 == 0:
            feed.advance(1)          # a new bar closed
        else:
            feed.mutate_forming_bar()  # same bar, close moved
        state = md.get_state("XAUUSD", "M1", 600)
        pd.testing.assert_frame_equal(state.candles, full_fetch(feed, 600))

    assert feed.calls[0] == 600
    assert all(c == BridgeMarketData.INCREMENTAL_BARS for c in feed.calls[1:])  # never re-downloaded the window


def test_gap_bigger_than_the_incremental_tail_triggers_a_full_refetch():
    """Engine stopped/wedged for several minutes -> more new bars than the
    tail covers -> reconstructing locally is impossible, so it must fall
    back to a full fetch (correct data beats saved bandwidth)."""
    feed = FakeBridgeFeed()
    md = BridgeMarketData(feed)
    md.get_state("XAUUSD", "M1", 600)

    feed.advance(10)  # 10 new bars - way past the 3-bar tail's reach
    state = md.get_state("XAUUSD", "M1", 600)

    pd.testing.assert_frame_equal(state.candles, full_fetch(feed, 600))
    assert feed.calls == [600, BridgeMarketData.INCREMENTAL_BARS, 600]  # tried the tail, saw the gap, refetched


def test_small_window_request_is_served_incrementally_without_shrinking_the_cache():
    """core/engine.py's restart-reconcile asks for just 5 bars. That must
    neither force a full refetch NOR shrink the 600-bar cache (which would
    force one on the NEXT normal poll)."""
    feed = FakeBridgeFeed()
    md = BridgeMarketData(feed)
    md.get_state("XAUUSD", "M1", 600)

    state_small = md.get_state("XAUUSD", "M1", 5)
    assert len(state_small.candles) == 5
    pd.testing.assert_frame_equal(state_small.candles, full_fetch(feed, 5))

    feed.mutate_forming_bar()
    state_full = md.get_state("XAUUSD", "M1", 600)  # must still be incremental
    pd.testing.assert_frame_equal(state_full.candles, full_fetch(feed, 600))
    assert feed.calls == [600, BridgeMarketData.INCREMENTAL_BARS, BridgeMarketData.INCREMENTAL_BARS]


def test_symbol_or_timeframe_change_invalidates_the_cache():
    feed = FakeBridgeFeed()
    md = BridgeMarketData(feed)
    md.get_state("XAUUSD", "M1", 600)
    md.get_state("XAUUSD", "M5", 600)  # different timeframe: cache must not be reused
    assert feed.calls == [600, 600]
