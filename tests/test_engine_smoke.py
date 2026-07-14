"""
End-to-end smoke tests for the engine using SimulatedBroker + a scripted,
deterministic market data feed (no synthetic randomness, no real broker).
These exercise the exact code path a live run uses (engine.step calling
strategy -> risk_manager -> broker -> database), just with prices we
control, so trade outcomes are asserted precisely instead of "did not
crash".
"""
from datetime import datetime, timezone

import numpy as np

from core.broker import SimulatedBroker
from core.config import Settings
from core.database import Database
from core.engine import TradingEngine
from core.market_data import LiveState, MarketDataSource
from core.mt5_bridge_client import Tick
from core.risk_manager import SymbolSpec
from tests.test_strategy import build_candles

SPEC = SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=1.0,
                   volume_step=0.01, point=0.01, trade_tick_value=1.0)


class ScriptedMarketData(MarketDataSource):
    """Replays a fixed list of LiveState objects, one per call; repeats the
    last one forever once exhausted."""

    def __init__(self, states: list[LiveState]) -> None:
        self._states = states
        self._i = 0

    def get_state(self, symbol: str, timeframe: str, count: int) -> LiveState:
        state = self._states[min(self._i, len(self._states) - 1)]
        self._i += 1
        return state


def make_settings(**overrides) -> Settings:
    defaults = dict(
        mt5_login="", mt5_password="", mt5_server="FBS-Demo", mt5_is_demo=True,
        bridge_url="http://127.0.0.1:5001", bridge_timeout_ms=8000,
        symbol="XAUUSD", timeframe="M1",
        risk_per_trade_usd=1.0, max_daily_loss_usd=8.0, max_daily_drawdown_pct=20.0,
        max_trades_per_day=1000, min_tp_usd=0.28, tp_levels=3,
        dry_run=True, db_path=":memory:",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def oversold_entry_state():
    rng = np.random.default_rng(1)
    closes = list(2400 + rng.normal(0, 0.03, 29))
    closes.append(closes[-1] - 3.0)
    candles = build_candles(closes)
    last_close = closes[-1]
    tick = Tick(bid=last_close - 0.1, ask=last_close + 0.1, spread_price=0.2, time=1_700_002_000)
    return LiveState(tick=tick, candles=candles), last_close


def test_engine_opens_and_fully_closes_a_winning_trade_on_a_big_favorable_move(tmp_path):
    entry_state, last_close = oversold_entry_state()
    up_tick = Tick(bid=last_close + 50, ask=last_close + 50.2, spread_price=0.2, time=1_700_002_060)
    up_state = LiveState(tick=up_tick, candles=entry_state.candles)

    market_data = ScriptedMarketData([entry_state, up_state, up_state])
    broker = SimulatedBroker(starting_balance=50_000.0, leverage=100, spec=SPEC)
    db = Database(str(tmp_path / "engine_win.db"))
    engine = TradingEngine(make_settings(), market_data, broker, db, poll_seconds=0)

    engine.step()  # opens the BUY on the oversold signal
    assert len(engine._open_positions) == 1

    engine.step()  # big favorable move: should walk through the whole TP ladder
    assert len(engine._open_positions) == 0

    trades = db.recent_trades(limit=5)
    assert len(trades) == 1
    assert trades[0]["side"] == "BUY"
    assert trades[0]["status"] == "closed"
    assert trades[0]["pnl_usd"] > 0
    assert abs(trades[0]["close_fraction"] - 1.0) < 1e-6


def test_engine_stops_out_a_losing_trade_on_a_big_adverse_move(tmp_path):
    entry_state, last_close = oversold_entry_state()
    down_tick = Tick(bid=last_close - 50, ask=last_close - 49.8, spread_price=0.2, time=1_700_002_060)
    down_state = LiveState(tick=down_tick, candles=entry_state.candles)

    market_data = ScriptedMarketData([entry_state, down_state, down_state])
    broker = SimulatedBroker(starting_balance=50_000.0, leverage=100, spec=SPEC)
    db = Database(str(tmp_path / "engine_loss.db"))
    engine = TradingEngine(make_settings(), market_data, broker, db, poll_seconds=0)

    engine.step()
    assert len(engine._open_positions) == 1

    engine.step()  # big adverse move: should hit the stop loss
    assert len(engine._open_positions) == 0

    trades = db.recent_trades(limit=5)
    assert len(trades) == 1
    assert trades[0]["status"] == "closed"
    assert trades[0]["pnl_usd"] < 0


def test_engine_refuses_to_open_when_margin_is_insufficient(tmp_path):
    """Mirrors the real 1:1-leverage / $50-balance scenario: the engine
    must NOT crash or send an impossible order, just skip the trade and
    log why."""
    entry_state, _ = oversold_entry_state()
    market_data = ScriptedMarketData([entry_state, entry_state])
    broker = SimulatedBroker(starting_balance=50.0, leverage=1, spec=SPEC)
    db = Database(str(tmp_path / "engine_margin.db"))
    engine = TradingEngine(make_settings(), market_data, broker, db, poll_seconds=0)

    engine.step()

    assert len(engine._open_positions) == 0
    events = db.recent_events(limit=5)
    assert any("insuficiente" in e["message"].lower() for e in events)


def test_engine_only_holds_one_position_at_a_time(tmp_path):
    entry_state, last_close = oversold_entry_state()
    flat_tick = Tick(bid=last_close, ask=last_close + 0.2, spread_price=0.2, time=1_700_002_060)
    flat_state = LiveState(tick=flat_tick, candles=entry_state.candles)

    market_data = ScriptedMarketData([entry_state] + [flat_state] * 5)
    broker = SimulatedBroker(starting_balance=50_000.0, leverage=100, spec=SPEC)
    db = Database(str(tmp_path / "engine_single.db"))
    engine = TradingEngine(make_settings(), market_data, broker, db, poll_seconds=0)

    for _ in range(5):
        engine.step()

    assert len(engine._open_positions) <= 1
