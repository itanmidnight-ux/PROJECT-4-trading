import os

from core.config import load_settings


def test_defaults_when_env_vars_absent(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("MT5_", "STRAT_", "RISK_", "MAX_", "MIN_TP", "TP_LEVELS", "DRY_RUN", "SYMBOL", "TIMEFRAME")):
            monkeypatch.delenv(key, raising=False)
    settings = load_settings()
    assert settings.symbol == "XAUUSD"
    assert settings.dry_run is True
    assert settings.strat_rsi_oversold == 25.0
    assert settings.strat_bb_period == 20
    assert settings.tp_levels == 3


def test_strategy_env_vars_override_defaults(monkeypatch):
    monkeypatch.setenv("STRAT_RSI_OVERSOLD", "18.5")
    monkeypatch.setenv("STRAT_BB_PERIOD", "30")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("MAX_TRADES_PER_DAY", "250")
    settings = load_settings()
    assert settings.strat_rsi_oversold == 18.5
    assert settings.strat_bb_period == 30
    assert settings.dry_run is False
    assert settings.max_trades_per_day == 250


def test_malformed_numeric_env_var_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("STRAT_RSI_OVERSOLD", "not-a-number")
    settings = load_settings()
    assert settings.strat_rsi_oversold == 25.0


def test_poll_seconds_default_and_override(monkeypatch):
    monkeypatch.delenv("POLL_SECONDS", raising=False)
    assert load_settings().poll_seconds == 2.0

    monkeypatch.setenv("POLL_SECONDS", "0.75")
    assert load_settings().poll_seconds == 0.75


def test_poll_seconds_is_floored_to_avoid_hammering_the_bridge(monkeypatch):
    monkeypatch.setenv("POLL_SECONDS", "0.01")
    assert load_settings().poll_seconds == 0.25


def test_bridge_auth_token_defaults_blank_and_reads_from_env(monkeypatch):
    monkeypatch.delenv("BRIDGE_AUTH_TOKEN", raising=False)
    assert load_settings().bridge_auth_token == ""

    monkeypatch.setenv("BRIDGE_AUTH_TOKEN", "abc123")
    assert load_settings().bridge_auth_token == "abc123"


def test_kill_switch_path_defaults_and_reads_from_env(monkeypatch):
    monkeypatch.delenv("KILL_SWITCH_PATH", raising=False)
    assert load_settings().kill_switch_path == "data/EMERGENCY_STOP"

    monkeypatch.setenv("KILL_SWITCH_PATH", "/tmp/custom_stop")
    assert load_settings().kill_switch_path == "/tmp/custom_stop"


def test_snapshot_retention_days_defaults_and_reads_from_env(monkeypatch):
    monkeypatch.delenv("SNAPSHOT_RETENTION_DAYS", raising=False)
    assert load_settings().snapshot_retention_days == 30

    monkeypatch.setenv("SNAPSHOT_RETENTION_DAYS", "7")
    assert load_settings().snapshot_retention_days == 7
