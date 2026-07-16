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


def test_status_reports_the_running_engines_own_settings_not_pending_saves(tmp_path):
    """/api/status must reflect what the (possibly separate, already-
    running) engine process actually connected with - not a settings-tab
    save that hasn't been applied yet (that's what /api/settings is for)."""
    client, _db = make_client(tmp_path, mt5_login="ORIGINAL", mt5_server="FBS-Demo")
    client.post("/api/settings", json={"mt5_login": "PENDING-NOT-YET-APPLIED"})

    status = client.get("/api/status").get_json()
    assert status["login"] == "ORIGINAL"
