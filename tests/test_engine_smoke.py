"""
End-to-end smoke tests for the engine using SimulatedBroker + a scripted,
deterministic market data feed (no synthetic randomness, no real broker).
These exercise the exact code path a live run uses (engine.step calling
strategy -> risk_manager -> broker -> database), just with prices we
control, so trade outcomes are asserted precisely instead of "did not
crash".
"""
from datetime import datetime, timedelta, timezone

import pytest

from core.broker import SimulatedBroker
from core.config import Settings
from core.database import Database
from core.engine import EngineHalted, TradingEngine
from core.market_data import LiveState, MarketDataSource
from core.mt5_bridge_client import Tick
from core.risk_manager import SymbolSpec
from tests.test_strategy import _ranging_base, build_candles

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


class FlakyBroker(SimulatedBroker):
    """Wraps SimulatedBroker to inject a controlled close_partial failure -
    simulates a bridge/network hiccup right as an order lands, where the
    caller never finds out whether the close actually reached the broker.

    broker_actually_closed=False: the order never went through (genuine
    transient failure) - the underlying position is untouched, so
    open_positions() still reports it.

    broker_actually_closed=True: the order DID execute at the broker, only
    the HTTP response back to the engine was lost - the underlying close
    happens for real before raising, so open_positions() no longer reports
    the ticket, exactly like a real "acknowledgement lost" scenario."""

    def __init__(self, *args, fail_times: int = 1, broker_actually_closed: bool = False,
                 fail_open_times: int = 0, broker_actually_opened: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self._fail_remaining = fail_times
        self._broker_actually_closed = broker_actually_closed
        self._fail_open_remaining = fail_open_times
        self._broker_actually_opened = broker_actually_opened

    def close_partial(self, ticket: str, lot: float, fill_price: float) -> float:
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            if self._broker_actually_closed:
                super().close_partial(ticket, lot, fill_price)
            raise RuntimeError("simulated bridge communication failure")
        return super().close_partial(ticket, lot, fill_price)

    def open_order(self, symbol: str, side: str, lot: float, sl_price: float, fill_price: float):
        if self._fail_open_remaining > 0:
            self._fail_open_remaining -= 1
            if self._broker_actually_opened:
                super().open_order(symbol, side, lot, sl_price, fill_price)
            raise RuntimeError("simulated bridge communication failure on open")
        return super().open_order(symbol, side, lot, sl_price, fill_price)


def make_settings(**overrides) -> Settings:
    defaults = {
        "mt5_login": "", "mt5_password": "", "mt5_server": "FBS-Demo", "mt5_is_demo": True,
        "bridge_url": "http://127.0.0.1:5001", "bridge_timeout_ms": 8000,
        "symbol": "XAUUSD", "timeframe": "M1",
        "risk_per_trade_usd": 1.0, "max_daily_loss_usd": 8.0, "max_daily_drawdown_pct": 20.0,
        "max_trades_per_day": 1000, "min_tp_usd": 0.28, "tp_levels": 3,
        "dry_run": True, "db_path": ":memory:",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def oversold_entry_state():
    closes = _ranging_base(29)
    closes.append(closes[-1] - 3.0)  # the signal bar (last CLOSED bar)
    # One more bar on top: the FORMING bar, which MT5/OANDA always include
    # as the last row of a candle fetch. The engine excludes it when
    # evaluating entries (closed-bars-only, matching the backtest), so the
    # signal must live on the second-to-last row - exactly like live data.
    closes.append(closes[-1])
    candles = build_candles(closes)
    last_close = closes[-1]
    tick = Tick(bid=last_close - 0.1, ask=last_close + 0.1, spread_price=0.2, time=1_700_002_000)
    return LiveState(tick=tick, candles=candles), last_close


def test_engine_ignores_a_signal_that_only_exists_on_the_forming_bar(tmp_path):
    """Regression for the closed-bars-only entry rule: a sharp drop on the
    FORMING (still-open) bar - which used to trigger an immediate,
    never-backtested mid-bar entry - must be ignored until that bar
    actually closes. Only the backtest-validated view (completed bars)
    may produce entries."""
    closes = _ranging_base(29)
    closes.append(closes[-1])         # last CLOSED bar: nothing special
    closes.append(closes[-1] - 3.0)   # forming bar: the "signal" lives ONLY here
    candles = build_candles(closes)
    tick = Tick(bid=closes[-1] - 0.1, ask=closes[-1] + 0.1, spread_price=0.2, time=1_700_002_000)
    market_data = ScriptedMarketData([LiveState(tick=tick, candles=candles)])
    broker = SimulatedBroker(starting_balance=50_000.0, leverage=100, spec=SPEC)
    engine = TradingEngine(make_settings(), market_data, broker, Database(str(tmp_path / "forming.db")), poll_seconds=0)

    engine.step()
    assert len(engine._open_positions) == 0  # no entry off the half-formed bar


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


def test_close_failure_when_position_is_still_at_the_broker_retries_next_step(tmp_path):
    """A genuine transient failure (bridge timeout, order never executed):
    the position must stay tracked, untouched, and get retried - not get
    silently dropped or double-closed."""
    entry_state, last_close = oversold_entry_state()
    down_tick = Tick(bid=last_close - 50, ask=last_close - 49.8, spread_price=0.2, time=1_700_002_060)
    down_state = LiveState(tick=down_tick, candles=entry_state.candles)

    market_data = ScriptedMarketData([entry_state, down_state, down_state])
    broker = FlakyBroker(starting_balance=50_000.0, leverage=100, spec=SPEC,
                          fail_times=1, broker_actually_closed=False)
    db = Database(str(tmp_path / "engine_flaky_retry.db"))
    engine = TradingEngine(make_settings(), market_data, broker, db, poll_seconds=0)

    engine.step()  # opens the BUY
    assert len(engine._open_positions) == 1

    engine.step()  # SL hit, but close_partial fails (order never reached the broker)
    assert len(engine._open_positions) == 1, "must stay tracked for a retry, not vanish"
    assert db.recent_trades(limit=5)[0]["status"] == "open"

    engine.step()  # retried automatically on the next poll, now succeeds
    assert len(engine._open_positions) == 0
    trades = db.recent_trades(limit=5)
    assert trades[0]["status"] == "closed"
    assert trades[0]["pnl_usd"] < 0


def test_close_failure_after_broker_already_closed_it_does_not_wedge_the_engine(tmp_path):
    """The dangerous case this exists for: the close order DID execute at
    the broker, only the HTTP response back was lost. Without reconciling
    this, the engine would retry closing a ticket the broker no longer
    has, get 'not found' forever, and - since it only ever holds one
    position at a time - never be able to open a new trade again."""
    entry_state, last_close = oversold_entry_state()
    down_tick = Tick(bid=last_close - 50, ask=last_close - 49.8, spread_price=0.2, time=1_700_002_060)
    down_state = LiveState(tick=down_tick, candles=entry_state.candles)

    market_data = ScriptedMarketData([entry_state, down_state, down_state, down_state])
    broker = FlakyBroker(starting_balance=50_000.0, leverage=100, spec=SPEC,
                          fail_times=1, broker_actually_closed=True)
    db = Database(str(tmp_path / "engine_flaky_reconcile.db"))
    engine = TradingEngine(make_settings(), market_data, broker, db, poll_seconds=0)

    engine.step()  # opens the BUY
    assert len(engine._open_positions) == 1

    engine.step()  # SL hit; the close executes at the broker but the response is lost
    assert len(engine._open_positions) == 0, "must be reconciled as closed, not stuck forever"
    trades = db.recent_trades(limit=5)
    assert trades[0]["status"] == "closed"

    events = db.recent_events(limit=5)
    assert any("no se pudo confirmar" in e["message"].lower() for e in events)
    assert any(e["level"] == "CRITICAL" for e in events)

    # The real point of the fix: the engine must be free to trade again,
    # not permanently blocked by a phantom open position. Reaching this
    # line without an exception (and with room for a new position) proves it.
    engine.step()
    assert len(engine._open_positions) <= 1


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


def test_engine_does_not_spam_emergency_alert_once_flat_and_still_breached(tmp_path):
    """Real bug, reproduced live in production: once the drawdown breach
    force-closes every position, the account is flat and there is nothing
    left to close - but check_equity_drawdown correctly keeps reporting
    breached=True for the rest of the trading day (that's the intended
    "daily" circuit-breaker semantics, see RiskManager.check_equity_drawdown
    - it must NOT silently clear early just because we already reacted to
    it once). The bug was calling _emergency_close_all_positions() on every
    single subsequent engine.step() while breached, which unconditionally
    logs a fresh CRITICAL "cerrando todas las posiciones abiertas" event
    even with zero positions open - in production this produced a new
    CRITICAL log/DB event every ~2.3s forever, forever blocking recovery
    visibility with noise. Fixed: exactly one CRITICAL alert per breach
    episode, not one per poll cycle, while still correctly blocking new
    trades (via the early `return`) every cycle for as long as breached
    stays true."""
    entry_state, last_close = oversold_entry_state()
    settings = make_settings(max_daily_drawdown_pct=0.1)
    broker = SimulatedBroker(starting_balance=50.0, leverage=100, spec=SPEC)
    db = Database(str(tmp_path / "engine_emergency_nospam.db"))
    engine = TradingEngine(settings, ScriptedMarketData([entry_state]), broker, db, poll_seconds=0)

    engine.step()  # opens the BUY on the oversold signal
    pos = engine._open_positions[0]
    sl_distance = pos.entry_price - pos.sl_price
    adverse_move = sl_distance * 0.3
    down_price = last_close - adverse_move
    down_tick = Tick(bid=down_price - 0.1, ask=down_price + 0.1, spread_price=0.2, time=1_700_002_060)
    down_state = LiveState(tick=down_tick, candles=entry_state.candles)
    engine.market_data = ScriptedMarketData([down_state])

    engine.step()  # drawdown breached, emergency close succeeds, account now flat
    assert len(engine._open_positions) == 0

    # Still breached (same day, same low equity, nothing to recover it) -
    # simulate 5 more poll cycles exactly like the live engine's loop does.
    for _ in range(5):
        engine.market_data = ScriptedMarketData([down_state])
        engine.step()

    critical_events = [e for e in db.recent_events(limit=50) if e["level"] == "CRITICAL"]
    assert len(critical_events) == 1, (
        f"expected exactly 1 CRITICAL emergency alert for the whole breach episode, "
        f"got {len(critical_events)} - the engine is spamming on every poll cycle"
    )


def test_emergency_close_failure_keeps_position_tracked_for_retry(tmp_path):
    """The drawdown circuit breaker's close must not silently drop the
    position if the broker call fails - same class of bug as a normal
    SL/TP close failure (test_close_failure_* above), just reached via the
    emergency-close path instead."""
    entry_state, last_close = oversold_entry_state()
    settings = make_settings(max_daily_drawdown_pct=0.1)
    broker = FlakyBroker(starting_balance=50.0, leverage=100, spec=SPEC,
                          fail_times=1, broker_actually_closed=False)
    db = Database(str(tmp_path / "engine_emergency_flaky.db"))
    engine = TradingEngine(settings, ScriptedMarketData([entry_state]), broker, db, poll_seconds=0)

    engine.step()
    assert len(engine._open_positions) == 1
    pos = engine._open_positions[0]
    sl_distance = pos.entry_price - pos.sl_price
    adverse_move = sl_distance * 0.3
    down_price = last_close - adverse_move
    down_tick = Tick(bid=down_price - 0.1, ask=down_price + 0.1, spread_price=0.2, time=1_700_002_060)
    down_state = LiveState(tick=down_tick, candles=entry_state.candles)
    engine.market_data = ScriptedMarketData([down_state])

    engine.step()  # drawdown breached, emergency close attempted, fails (transient)
    assert len(engine._open_positions) == 1, "must stay tracked for a retry, not silently dropped"
    assert db.recent_trades(limit=5)[0]["status"] == "open"

    engine.step()  # retried: drawdown is still breached (nothing actually closed last time), now succeeds
    assert len(engine._open_positions) == 0
    trades = db.recent_trades(limit=5)
    assert trades[0]["status"] == "closed"


def test_open_order_failure_when_broker_never_received_it_retries_cleanly(tmp_path):
    """A genuine transient failure on open: nothing must be tracked, and
    the same signal is free to try again on the next poll."""
    entry_state, _ = oversold_entry_state()
    market_data = ScriptedMarketData([entry_state] * 4)
    broker = FlakyBroker(starting_balance=50_000.0, leverage=100, spec=SPEC,
                          fail_open_times=1, broker_actually_opened=False)
    db = Database(str(tmp_path / "engine_open_flaky_retry.db"))
    engine = TradingEngine(make_settings(), market_data, broker, db, poll_seconds=0)

    engine.step()  # open_order fails (order never reached the broker)
    assert len(engine._open_positions) == 0
    assert db.recent_trades(limit=5) == []

    engine.step()  # retried on the next poll, now succeeds
    assert len(engine._open_positions) == 1
    assert db.recent_trades(limit=5)[0]["status"] == "open"


def test_open_order_failure_when_broker_actually_opened_it_does_not_duplicate(tmp_path):
    """The dangerous case this exists for: open_order's HTTP response was
    lost, but the order DID execute at the broker. If the engine assumed
    nothing happened and just tried the same signal again, it would place
    a second, genuinely duplicate real position - a direct doubling of
    real risk, not just a bookkeeping glitch."""
    entry_state, _ = oversold_entry_state()
    market_data = ScriptedMarketData([entry_state] * 4)
    broker = FlakyBroker(starting_balance=50_000.0, leverage=100, spec=SPEC,
                          fail_open_times=1, broker_actually_opened=True)
    db = Database(str(tmp_path / "engine_open_flaky_reconcile.db"))
    engine = TradingEngine(make_settings(), market_data, broker, db, poll_seconds=0)

    engine.step()  # open_order raises, but the order DID fill at the broker
    assert len(engine._open_positions) == 1, "must adopt the real position instead of assuming nothing happened"
    assert len(broker._positions) == 1, "and must not have placed a second order at the broker"

    engine.step()  # would-be retry: must NOT place a duplicate now that one is tracked
    assert len(engine._open_positions) == 1
    assert len(broker._positions) == 1


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


def test_prune_old_snapshots_is_throttled_to_once_per_check_interval(tmp_path, monkeypatch):
    """account_snapshots is written every poll, so pruning must NOT run a
    DELETE on every step - only after PRUNE_CHECK_INTERVAL_SECONDS of wall
    clock time has actually passed."""
    entry_state, _ = oversold_entry_state()
    market_data = ScriptedMarketData([entry_state])
    broker = SimulatedBroker(starting_balance=50_000.0, leverage=100, spec=SPEC)
    db = Database(str(tmp_path / "engine_prune.db"))
    engine = TradingEngine(make_settings(snapshot_retention_days=30), market_data, broker, db, poll_seconds=0)
    engine._ensure_initialized()

    old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    db.record_snapshot(ts=old_ts, balance=50.0, equity=50.0, free_margin=50.0)

    # _last_prune_wallclock starts at 0.0, so the fake clock must already be
    # past the check interval for the first call to actually run the prune.
    fake_time = [TradingEngine.PRUNE_CHECK_INTERVAL_SECONDS + 1.0]
    monkeypatch.setattr("core.engine.time.time", lambda: fake_time[0])

    engine._maybe_prune_old_snapshots()  # first call: past the interval since wallclock 0, runs
    assert len(db.equity_curve(limit=10)) == 0, "the old snapshot should have been pruned"

    db.record_snapshot(ts=old_ts, balance=50.0, equity=50.0, free_margin=50.0)
    fake_time[0] += 10  # well under the hourly check interval
    engine._maybe_prune_old_snapshots()
    assert len(db.equity_curve(limit=10)) == 1, "throttled: must not have pruned again yet"

    fake_time[0] += TradingEngine.PRUNE_CHECK_INTERVAL_SECONDS
    engine._maybe_prune_old_snapshots()
    assert len(db.equity_curve(limit=10)) == 0, "interval elapsed: should prune again"


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


def test_kill_switch_file_force_closes_open_position_and_halts(tmp_path):
    """The manual kill switch (data/EMERGENCY_STOP by default) is the only
    way to stop the bot without terminal/process access - this must
    actually close any open position at market, not just block new ones."""
    kill_switch = tmp_path / "EMERGENCY_STOP"
    entry_state, last_close = oversold_entry_state()
    flat_tick = Tick(bid=last_close, ask=last_close + 0.2, spread_price=0.2, time=1_700_002_060)
    flat_state = LiveState(tick=flat_tick, candles=entry_state.candles)

    market_data = ScriptedMarketData([entry_state, flat_state])
    broker = SimulatedBroker(starting_balance=50_000.0, leverage=100, spec=SPEC)
    db = Database(str(tmp_path / "engine_killswitch.db"))
    settings = make_settings(kill_switch_path=str(kill_switch))
    engine = TradingEngine(settings, market_data, broker, db, poll_seconds=0)

    engine.step()  # opens the BUY on the oversold signal
    assert len(engine._open_positions) == 1

    kill_switch.touch()
    with pytest.raises(EngineHalted):
        engine.step()

    assert len(engine._open_positions) == 0, "kill switch must force-close the open position"
    trades = db.recent_trades(limit=5)
    assert trades[0]["status"] == "closed"
    assert trades[0]["tp_level"] == -2, "closed via the emergency path, not a normal SL/TP hit"

    events = db.recent_events(limit=5)
    assert any("emergencia" in e["message"].lower() for e in events)


def test_kill_switch_file_blocks_new_trades_when_flat(tmp_path):
    kill_switch = tmp_path / "EMERGENCY_STOP"
    kill_switch.touch()
    entry_state, _ = oversold_entry_state()
    market_data = ScriptedMarketData([entry_state])
    broker = SimulatedBroker(starting_balance=50_000.0, leverage=100, spec=SPEC)
    db = Database(str(tmp_path / "engine_killswitch_flat.db"))
    settings = make_settings(kill_switch_path=str(kill_switch))
    engine = TradingEngine(settings, market_data, broker, db, poll_seconds=0)

    with pytest.raises(EngineHalted):
        engine.step()

    assert len(engine._open_positions) == 0
    assert db.recent_trades(limit=5) == []


def test_pause_flag_blocks_opening_a_new_trade_when_flat(tmp_path):
    """Unlike the kill switch, pausing must NOT raise/halt the engine - it's
    a reversible, everyday control, not an emergency measure."""
    pause_flag = tmp_path / "PAUSED"
    pause_flag.touch()
    entry_state, _ = oversold_entry_state()
    market_data = ScriptedMarketData([entry_state])
    broker = SimulatedBroker(starting_balance=50_000.0, leverage=100, spec=SPEC)
    db = Database(str(tmp_path / "engine_pause_flat.db"))
    settings = make_settings(pause_flag_path=str(pause_flag))
    engine = TradingEngine(settings, market_data, broker, db, poll_seconds=0)

    engine.step()  # would open on this oversold signal if not paused

    assert len(engine._open_positions) == 0
    assert db.recent_trades(limit=5) == []


def test_pause_flag_keeps_managing_an_already_open_position(tmp_path):
    """The core distinction from the kill switch: pause does NOT force-close
    existing positions - it only blocks new ones. An open position's own
    SL/TP still gets processed normally while paused."""
    pause_flag = tmp_path / "PAUSED"
    entry_state, last_close = oversold_entry_state()
    down_tick = Tick(bid=last_close - 50, ask=last_close - 49.8, spread_price=0.2, time=1_700_002_060)
    down_state = LiveState(tick=down_tick, candles=entry_state.candles)
    market_data = ScriptedMarketData([entry_state, down_state])
    broker = SimulatedBroker(starting_balance=50_000.0, leverage=100, spec=SPEC)
    db = Database(str(tmp_path / "engine_pause_managed.db"))
    settings = make_settings(pause_flag_path=str(pause_flag))
    engine = TradingEngine(settings, market_data, broker, db, poll_seconds=0)

    engine.step()  # opens the BUY (not paused yet)
    assert len(engine._open_positions) == 1

    pause_flag.touch()  # pause AFTER the position is already open
    engine.step()  # big adverse move: the SL must still fire despite the pause

    assert len(engine._open_positions) == 0
    trades = db.recent_trades(limit=5)
    assert trades[0]["status"] == "closed"
    assert trades[0]["pnl_usd"] < 0


def test_removing_the_pause_flag_resumes_trading_on_the_next_step(tmp_path):
    pause_flag = tmp_path / "PAUSED"
    pause_flag.touch()
    entry_state, _ = oversold_entry_state()
    market_data = ScriptedMarketData([entry_state, entry_state])
    broker = SimulatedBroker(starting_balance=50_000.0, leverage=100, spec=SPEC)
    db = Database(str(tmp_path / "engine_pause_resume.db"))
    settings = make_settings(pause_flag_path=str(pause_flag))
    engine = TradingEngine(settings, market_data, broker, db, poll_seconds=0)

    engine.step()
    assert len(engine._open_positions) == 0, "must stay paused"

    pause_flag.unlink()
    engine.step()
    assert len(engine._open_positions) == 1, "must resume immediately once unpaused, no restart needed"
