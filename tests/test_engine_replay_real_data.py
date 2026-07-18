"""Replays REAL gold market history (data/gold_history_1m.csv, committed
COMEX 1m data) through the actual TradingEngine.step() loop - not the
backtester - with a SimulatedBroker. This is the closest thing to a live
DRY_RUN session that can run inside the test suite: real price movement,
real volatility spikes, real market gaps, through the exact engine code
path production uses (signal -> risk sizing -> order -> TP ladder ->
trailing -> SL -> DB), asserting it survives without a single exception
and leaves its books consistent.

Deliberately NOT an expectancy/profit assertion - see the README's
backtest rounds for those. This guards the ENGINE's behavior on real
data: no crash, every DB trade row consistent, no orphaned position
state, and the daily-loss / equity-drawdown brakes allowed to do their
job if the replayed stretch happens to trip them (the replay compresses
days of market into one wall-clock "day", so brakes firing is valid
behavior, not a failure).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from core.broker import SimulatedBroker
from core.config import Settings
from core.database import Database
from core.engine import TradingEngine
from core.market_data import LiveState, MarketDataSource
from core.mt5_bridge_client import Tick
from core.risk_manager import SymbolSpec

CSV = Path(__file__).resolve().parent.parent / "data" / "gold_history_1m.csv"
SPEC = SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=1.0,
                   volume_step=0.01, point=0.01, trade_tick_value=1.0)
SPREAD = 0.25


class RealDataReplay(MarketDataSource):
    """Serves the real candle history one bar per get_state call: a sliding
    window (the last bar playing the 'forming' role, like MT5/OANDA feeds)
    plus a tick at that bar's close."""

    def __init__(self, df: pd.DataFrame, window: int = 300) -> None:
        self.df = df.reset_index(drop=True)
        self.window = window
        self.i = 60  # start with enough real history for indicator warmup

    def get_state(self, symbol: str, timeframe: str, count: int) -> LiveState:
        if self.i < len(self.df) - 1:
            self.i += 1
        w = self.df.iloc[max(0, self.i + 1 - self.window): self.i + 1].reset_index(drop=True)
        mid = float(w.iloc[-1]["close"])
        tick = Tick(bid=mid - SPREAD / 2, ask=mid + SPREAD / 2,
                    spread_price=SPREAD, time=int(w.iloc[-1]["time"]))
        return LiveState(tick=tick, candles=w)

    @property
    def done(self) -> bool:
        return self.i >= len(self.df) - 1


def make_settings() -> Settings:
    return Settings(
        mt5_login="", mt5_password="", mt5_server="FBS-Demo", mt5_is_demo=True,
        bridge_url="http://127.0.0.1:5001", bridge_timeout_ms=8000,
        symbol="XAUUSD", timeframe="M1",
        risk_per_trade_usd=3.0, max_daily_loss_usd=8.0, max_daily_drawdown_pct=20.0,
        max_trades_per_day=1000, min_tp_usd=0.28, tp_levels=5,
        dry_run=True, db_path=":memory:",
    )


@pytest.mark.skipif(not CSV.exists(), reason="repo's real gold CSV not present")
def test_engine_survives_a_real_market_replay_with_consistent_books(tmp_path):
    df = pd.read_csv(CSV).head(800)  # ~13 hours of real 1m gold - keeps the test fast
    md = RealDataReplay(df)
    db = Database(str(tmp_path / "replay.db"))
    broker = SimulatedBroker(starting_balance=50.0, leverage=500, spec=SPEC)
    engine = TradingEngine(make_settings(), md, broker, db, poll_seconds=0)

    steps = 0
    while not md.done:
        engine.step()  # any exception fails the test - that IS the assertion
        steps += 1
    assert steps > 700

    # Books must be consistent afterwards: whatever the engine still tracks
    # must exist at the broker, and vice versa - no phantom/orphan positions.
    broker_tickets = {p.ticket for p in broker.open_positions("XAUUSD")}
    engine_tickets = {p.ticket for p in engine._open_positions}
    assert engine_tickets == broker_tickets

    # Every closed DB row must be fully populated (no half-written trades).
    for t in db.recent_trades(limit=1000):
        assert t["side"] in ("BUY", "SELL")
        assert t["lot"] > 0
        if t["status"] == "closed":
            assert t["closed_at"] is not None
