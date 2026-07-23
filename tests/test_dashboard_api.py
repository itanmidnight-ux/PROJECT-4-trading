"""Tests for dashboard.py's mutating Flask routes (settings save, pause/
resume) and the auth gate protecting them. The read-only GET routes are
already exercised end-to-end by `./run.sh verify`; this focuses on the
write paths added for the Settings tab and the pause/resume control, since
those are the ones that can now change stored broker credentials or
trading state - not just read them.

dashboard.py is only ever imported once per test session (module-level
side effects: it opens its own Database at import time), so each test
directly reassigns dashboard.db/dashboard.settings to a fresh, isolated
instance rather than spinning up a real server process.
"""
from dataclasses import replace

import dashboard as dmod
from core.database import Database
from core.risk_manager import SymbolSpec


def make_client(tmp_path, **settings_overrides):
    db = Database(str(tmp_path / "dash_api_test.db"))
    dmod.db = db
    dmod.settings = replace(dmod.settings, **settings_overrides)
    return dmod.app.test_client(), db


def test_get_settings_defaults_and_no_password_leak(tmp_path):
    client, _db = make_client(tmp_path, mt5_login="", mt5_password="", mt5_server="FBS-Demo")
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["mt5_server"] == "FBS-Demo"
    assert data["has_password"] is False
    assert "mt5_password" not in data  # never echoed back, plaintext or otherwise


def test_save_settings_persists_and_get_reflects_it(tmp_path):
    client, _db = make_client(tmp_path)
    resp = client.post("/api/settings", json={
        "mt5_login": "555444", "mt5_server": "ICMarkets-Live", "mt5_password": "hunter2",
        "risk_per_trade_usd": 2.5,
    })
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    data = client.get("/api/settings").get_json()
    assert data["mt5_login"] == "555444"
    assert data["mt5_server"] == "ICMarkets-Live"
    assert data["has_password"] is True
    assert data["risk_per_trade_usd"] == 2.5


def test_save_settings_works_for_any_broker_server_string_not_just_fbs(tmp_path):
    """Explicit check that the server field is free text, not restricted to
    FBS - the bridge's mt5.login() accepts any MT5 server the terminal can
    reach, and the Settings tab must not artificially narrow that."""
    client, _db = make_client(tmp_path)
    resp = client.post("/api/settings", json={"mt5_server": "Pepperstone-Demo01"})
    assert resp.status_code == 200
    assert client.get("/api/settings").get_json()["mt5_server"] == "Pepperstone-Demo01"


def test_save_settings_blank_password_does_not_clear_the_existing_one(tmp_path):
    client, _db = make_client(tmp_path)
    client.post("/api/settings", json={"mt5_password": "hunter2"})
    client.post("/api/settings", json={"mt5_login": "111222"})  # no password field this time

    data = client.get("/api/settings").get_json()
    assert data["mt5_login"] == "111222"
    assert data["has_password"] is True  # untouched by the second, password-less save


def test_save_settings_rejects_invalid_numeric_field(tmp_path):
    client, _db = make_client(tmp_path)
    resp = client.post("/api/settings", json={"risk_per_trade_usd": "not-a-number"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_save_settings_rejects_non_positive_risk(tmp_path):
    client, _db = make_client(tmp_path)
    resp = client.post("/api/settings", json={"risk_per_trade_usd": -1})
    assert resp.status_code == 400


def test_save_settings_with_no_recognized_fields_is_rejected(tmp_path):
    client, _db = make_client(tmp_path)
    resp = client.post("/api/settings", json={"unrelated_field": "x"})
    assert resp.status_code == 400


def test_save_settings_rejects_a_non_json_body(tmp_path):
    client, _db = make_client(tmp_path)
    resp = client.post("/api/settings", data="not json", content_type="text/plain")
    assert resp.status_code == 400


def test_openrouter_keys_are_written_to_env_but_never_returned(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("MT5_LOGIN=old\n", encoding="utf-8")
    monkeypatch.setattr(dmod, "ENV_PATH", env_path)
    client, _db = make_client(tmp_path, dashboard_auth_token="")
    response = client.post("/api/settings", json={
        "openrouter_api_key": "key-one-secret",
        "openrouter_supervisor_api_key": "key-two-secret",
        "ai_brain_enabled": True,
        "ai_supervisor_enabled": True,
    })
    assert response.status_code == 200
    body = response.get_json()
    assert "key-one-secret" not in str(body)
    assert "key-two-secret" not in str(body)
    saved = env_path.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=key-one-secret" in saved
    assert "OPENROUTER_SUPERVISOR_API_KEY=key-two-secret" in saved
    settings = client.get("/api/settings").get_json()
    assert settings["has_openrouter_api_key"] is True
    assert settings["has_supervisor_api_key"] is True
    assert "openrouter_api_key" not in settings
    assert "openrouter_supervisor_api_key" not in settings


def test_pause_and_resume_roundtrip(tmp_path):
    client, _db = make_client(tmp_path, pause_flag_path=str(tmp_path / "PAUSED"))

    assert client.get("/api/status").get_json()["paused"] is False

    r1 = client.post("/api/bot/pause")
    assert r1.get_json() == {"ok": True, "paused": True}
    assert client.get("/api/status").get_json()["paused"] is True

    r2 = client.post("/api/bot/resume")
    assert r2.get_json() == {"ok": True, "paused": False}
    assert client.get("/api/status").get_json()["paused"] is False


def test_pause_and_resume_are_logged_as_events(tmp_path):
    client, db = make_client(tmp_path, pause_flag_path=str(tmp_path / "PAUSED"))
    client.post("/api/bot/pause")
    events = db.recent_events(limit=5)
    assert any("pausado" in e["message"].lower() for e in events)


def test_mutating_routes_require_token_when_configured(tmp_path):
    client, _db = make_client(tmp_path, dashboard_auth_token="secret123",
                               pause_flag_path=str(tmp_path / "PAUSED"))

    assert client.post("/api/bot/pause").status_code == 401
    assert client.post("/api/bot/pause", headers={"X-Dashboard-Token": "wrong"}).status_code == 401
    assert client.post("/api/bot/pause", headers={"X-Dashboard-Token": "secret123"}).status_code == 200


def test_settings_save_also_requires_token_when_configured(tmp_path):
    client, _db = make_client(tmp_path, dashboard_auth_token="secret123")
    resp = client.post("/api/settings", json={"mt5_login": "123"})
    assert resp.status_code == 401


def test_get_routes_do_not_require_token_even_when_configured(tmp_path):
    """GETs stay open when a token is set - only mutating POST routes are
    gated, so existing --web usage without a token keeps working for the
    read-only views it always supported."""
    client, _db = make_client(tmp_path, dashboard_auth_token="secret123")
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/settings").status_code == 200


def test_backtest_requires_credentials_and_is_a_post_route(tmp_path):
    client, _db = make_client(tmp_path, mt5_login="", mt5_password="", dashboard_auth_token="")
    # Flask's static catch-all handles unsupported GETs as a 404; only POST
    # is intentionally registered for this potentially expensive operation.
    assert client.get("/api/backtest").status_code == 404
    response = client.post("/api/backtest", json={"bars": 100})
    assert response.status_code == 409


def test_backtest_route_uses_mt5_history_without_order_calls(tmp_path, monkeypatch):
    import dashboard as dmod
    import pandas as pd

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def login(self, *args): pass
        def symbol_spec(self, symbol):
            return SymbolSpec(100, .01, 1, .01, .01, 1.0, trade_tick_size=.01)
        def candles(self, symbol, timeframe, count):
            return pd.DataFrame([{"time": i, "open": 4000, "high": 4001, "low": 3999, "close": 4000} for i in range(count)])

    client, _db = make_client(tmp_path, mt5_login="123", mt5_password="pw", mt5_server="Demo", dashboard_auth_token="")
    monkeypatch.setattr(dmod, "Mt5BridgeClient", FakeClient)
    response = client.post("/api/backtest", json={"bars": 100, "balance": 50, "leverage": 500})
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["tick_to_tick"] is False
    assert data["fill_model"].startswith("OHLC")


def test_backtest_route_passes_precompute_indicators_true(tmp_path, monkeypatch):
    """/api/backtest must actually use core/backtest.py's fast path -
    precompute_indicators exists but is worthless if the route never
    passes it. Mocks dashboard.run_backtest (the lazily-imported
    module-level name the route calls) and asserts the call it receives
    has precompute_indicators=True in its kwargs."""
    import dashboard as dmod
    import pandas as pd
    from unittest.mock import MagicMock
    from core.backtest import BacktestResult

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def login(self, *args): pass
        def symbol_spec(self, symbol):
            return SymbolSpec(100, .01, 1, .01, .01, 1.0, trade_tick_size=.01)
        def candles(self, symbol, timeframe, count):
            return pd.DataFrame([{"time": i, "open": 4000, "high": 4001, "low": 3999, "close": 4000} for i in range(count)])

    client, _db = make_client(tmp_path, mt5_login="123", mt5_password="pw", mt5_server="Demo", dashboard_auth_token="")
    monkeypatch.setattr(dmod, "Mt5BridgeClient", FakeClient)
    mock_run_backtest = MagicMock(return_value=BacktestResult(trades=0, wins=0, losses=0, total_pnl=0.0,
                                                                max_drawdown_pct=0.0, final_balance=50.0))
    monkeypatch.setattr(dmod, "run_backtest", mock_run_backtest)

    response = client.post("/api/backtest", json={"bars": 100, "balance": 50, "leverage": 500})

    assert response.status_code == 200
    mock_run_backtest.assert_called_once()
    assert mock_run_backtest.call_args.kwargs.get("precompute_indicators") is True


def test_backtest_route_returns_valid_json_when_a_real_trade_closes(tmp_path, monkeypatch):
    """Regression test for a real 500 seen against live MT5 data:
    TypeError: Object of type int64 is not JSON serializable.

    core/backtest.py's run_backtest() accumulates wins/losses via
    `wins += open_pos["realized_pnl"] > 0`, where realized_pnl is a numpy
    float born from pandas Series arithmetic on real candle data - that
    promotes the plain-int accumulator to numpy.int64 (same story for
    balance/total_pnl/max_dd becoming numpy.float64), which Flask's
    default JSON encoder cannot serialize. The route's other real (non-
    mocked-run_backtest) test above, test_backtest_route_uses_mt5_history_
    without_order_calls, uses flat-price candles that never open a
    position, so it never exercises this arithmetic at all. This test
    does NOT mock run_backtest: it feeds a real oversold-then-rally candle
    set (long enough to also clear the route's own 100-bar minimum)
    through the actual ScalpStrategy + RiskManager + run_backtest path so
    a real trade opens and closes, then asserts the Flask test client's
    response is valid JSON with a 200, not a 500."""
    import dashboard as dmod
    import pandas as pd
    from core.strategy import ScalpStrategy
    from core.risk_manager import AccountState, RiskManager
    from tests.test_strategy import _ranging_base, build_candles

    # Same ranging-then-breakout shape as tests.test_strategy.oversold_
    # candles(), just padded with extra leading ranging bars so the total
    # clears /api/backtest's 100-bar minimum without changing the tail
    # that triggers the BUY signal.
    closes = _ranging_base(99)
    closes.append(closes[-1] - 3.0)
    base = build_candles(closes)

    spec = SymbolSpec(100, .01, 1, .01, .01, 1.0, trade_tick_size=.01)
    value_per_point = spec.trade_tick_value / spec.trade_tick_size
    probe = ScalpStrategy(min_tp_usd=0.28, tp_levels=3, value_per_point_per_lot=value_per_point)
    signal = probe.generate_signal(base, spread_price=0.25, lot_hint=spec.volume_min)
    assert signal.side == "BUY"

    # Replicate run_backtest's own sizing step (see
    # test_backtest_counts_partial_tp_then_breakeven_stop_as_a_win in
    # tests/test_backtest.py for the original of this pattern) to know
    # exactly how far price needs to rally to lock in TP1, then fall back
    # to breakeven - a guaranteed net-positive close, i.e. a real win that
    # flows through the numpy-promoting wins/total_pnl arithmetic.
    risk = RiskManager(risk_per_trade_usd=1.0, max_daily_loss_usd=10**9,
                        max_daily_drawdown_pct=100.0, max_trades_per_day=1000)
    account = AccountState(balance=50.0, equity=50.0, free_margin=50.0, leverage=500)
    entry_price = base.iloc[-1]["close"] + 0.25 / 2
    sizing = risk.size_position(account, spec, signal.sl_distance_price, entry_price)
    assert sizing.ok
    tp1_distance = probe.build_tp_ladder(sizing.lot, 0.25)[0].distance_price

    up = entry_price + tp1_distance + 0.3
    prev_close = base.iloc[-1]["close"]
    t = int(base.iloc[-1]["time"]) + 60
    rows = []
    for c in (up, up, entry_price, entry_price):
        rows.append({"time": t, "open": prev_close, "high": max(prev_close, c),
                     "low": min(prev_close, c), "close": c, "tick_volume": 50})
        prev_close = c
        t += 60
    candles = pd.concat([base, pd.DataFrame(rows)], ignore_index=True)

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def login(self, *args): pass
        def symbol_spec(self, symbol): return spec
        def candles(self, symbol, timeframe, count): return candles

    # Pin every strat_* knob and the other strategies' enable flags
    # explicitly, so the ScalpStrategy that dashboard.build_strategy_from_
    # settings() builds matches `probe` above regardless of whatever the
    # real .env this worktree symlinks to happens to contain.
    client, _db = make_client(
        tmp_path, mt5_login="123", mt5_password="pw", mt5_server="Demo", dashboard_auth_token="",
        min_tp_usd=0.28, tp_levels=3, tp_targets_usd=[],
        strat_rsi_oversold=25.0, strat_rsi_overbought=75.0, strat_max_spread_price=0.5,
        strat_min_atr_price=0.15, strat_sl_atr_multiple=4.0, strat_cooldown_bars=2,
        strat_bb_period=20, strat_bb_std=2.0, strat_rsi_period=7, strat_atr_period=14,
        strat_adx_period=14, strat_trend_filter_adx_threshold=35.0,
        strat_enable_momentum_cross=False, strat_enable_rsi_hysteresis=False,
        strat_enable_directional_candle=False, strat_enable_session_open=False,
        strat_enable_asian_breakout=False, strat_enable_m15_trend=False,
        strat_enable_ma_grid=False, strat_enable_quantum_queen=False,
    )
    monkeypatch.setattr(dmod, "Mt5BridgeClient", FakeClient)

    response = client.post("/api/backtest", json={
        "bars": len(candles), "balance": 50, "leverage": 500, "risk_usd": 1.0, "spread": 0.25,
    })

    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json()  # raises if the body isn't valid JSON
    assert data["ok"] is True
    assert data["trades"] == 1
    assert data["wins"] == 1
    assert data["losses"] == 0
    assert data["total_pnl"] > 0


def test_status_reports_the_running_engines_own_settings_not_pending_saves(tmp_path):
    """/api/status must reflect what the (possibly separate, already-
    running) engine process actually connected with - not a settings-tab
    save that hasn't been applied yet (that's what /api/settings is for)."""
    client, _db = make_client(tmp_path, mt5_login="ORIGINAL", mt5_server="FBS-Demo")
    client.post("/api/settings", json={"mt5_login": "PENDING-NOT-YET-APPLIED"})

    status = client.get("/api/status").get_json()
    assert status["login"] == "ORIGINAL"
