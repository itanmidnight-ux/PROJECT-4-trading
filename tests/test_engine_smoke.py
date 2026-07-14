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


def test_engine_emergency_closes_on_equity_drawdown_before_sl_is_hit(tmp_path):
    """The real-time circuit breaker: a floating loss well inside the
    (now much wider, ATR x 4) stop distance must still force-close the
    position once it breaches the daily equity drawdown limit, instead of
    waiting for the SL to eventually catch up."""
    entry_state, last_close = oversold_entry_state()
    settings = make_settings(max_daily_drawdown_pct=0.1)  # deliberately far tighter than any single trade's SL risk
    broker = SimulatedBroker(starting_balance=50.0, leverage=100, spec=SPEC)
    db = Database(str(tmp_path / "engine_emergency.db"))
    engine = TradingEngine(settings, ScriptedMarketData([entry_state]), broker, db, poll_seconds=0)

    engine.step()  # opens the BUY on the oversold signal
    assert len(engine._open_positions) == 1
    pos = engine._open_positions[0]
    sl_distance = pos.entry_price - pos.sl_price
    assert sl_distance > 0

    # Comfortably inside the SL (30% of the distance to it), but a real
    # floating loss on a $50 account with a 0.1% drawdown limit.
    adverse_move = sl_distance * 0.3
    down_price = last_close - adverse_move
    down_tick = Tick(bid=down_price - 0.1, ask=down_price + 0.1, spread_price=0.2, time=1_700_002_060)
    engine.market_data = ScriptedMarketData([LiveState(tick=down_tick, candles=entry_state.candles)])

    engine.step()

    assert len(engine._open_positions) == 0, "the position must be force-closed, not left riding toward its SL"
    trades = db.recent_trades(limit=5)
    assert len(trades) == 1
    assert trades[0]["status"] == "closed"
    assert trades[0]["tp_level"] == -2, "closed via the emergency path, not a normal SL hit (-1)"
    assert trades[0]["pnl_usd"] < 0

    events = db.recent_events(limit=5)
    assert any("PARADA DE EMERGENCIA" in e["message"] for e in events)
    assert any(e["level"] == "CRITICAL" for e in events)


def test_engine_recovers_a_pre_existing_open_position_on_restart(tmp_path):
    """Simulates the exact gap auto-restart could otherwise create: a
    position opened by a PREVIOUS process instance, with a brand new
    TradingEngine (empty in-memory state) pointed at the same broker."""
    entry_state, last_close = oversold_entry_state()
    market_data = ScriptedMarketData([entry_state, entry_state])
    broker = SimulatedBroker(starting_balance=50_000.0, leverage=100, spec=SPEC)
    broker.open_order("XAUUSD", "BUY", 0.02, sl_price=last_close - 5, fill_price=last_close)

    db = Database(str(tmp_path / "engine_recover.db"))
    engine = TradingEngine(make_settings(), market_data, broker, db, poll_seconds=0)

    engine.step()

    assert len(engine._open_positions) == 1
    recovered = engine._open_positions[0]
    assert recovered.side == "BUY"
    assert recovered.original_lot == 0.02
    assert len(recovered.tp_levels) == 3

    trades = db.recent_trades(limit=5)
    assert len(trades) == 1
    assert trades[0]["status"] == "open"

    events = db.recent_events(limit=5)
    assert any("recuperada" in e["message"].lower() for e in events)


def test_market_stale_detection_pauses_signals_but_keeps_protecting_positions(tmp_path, monkeypatch):
    entry_state, _ = oversold_entry_state()
    market_data = ScriptedMarketData([entry_state])
    broker = SimulatedBroker(starting_balance=50_000.0, leverage=100, spec=SPEC)
    db = Database(str(tmp_path / "engine_stale.db"))
    engine = TradingEngine(make_settings(), market_data, broker, db, poll_seconds=0)
    engine._ensure_initialized()

    fake_time = [1000.0]
    monkeypatch.setattr("core.engine.time.time", lambda: fake_time[0])

    assert engine._is_market_stale(tick_time=123) is False  # first observation
    fake_time[0] += 60
    assert engine._is_market_stale(tick_time=123) is False  # below threshold

    fake_time[0] += 200
    assert engine._is_market_stale(tick_time=123) is True  # now stale
    events = db.recent_events(limit=5)
    assert any("cerrado" in e["message"].lower() for e in events)

    assert engine._is_market_stale(tick_time=456) is False  # ticks resumed
    events = db.recent_events(limit=5)
    assert any("de nuevo" in e["message"].lower() for e in events)


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
