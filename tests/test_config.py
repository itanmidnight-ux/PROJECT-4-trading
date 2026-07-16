import os

from core.config import apply_db_overrides, load_settings


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


def test_tp_targets_usd_defaults_empty_and_parses_comma_list(monkeypatch):
    monkeypatch.delenv("TP_TARGETS_USD", raising=False)
    assert load_settings().tp_targets_usd == []

    monkeypatch.setenv("TP_TARGETS_USD", "0.28,0.60,1.20")
    assert load_settings().tp_targets_usd == [0.28, 0.60, 1.20]

    monkeypatch.setenv("TP_TARGETS_USD", " 0.5 , 1.0 ")  # tolerates stray whitespace
    assert load_settings().tp_targets_usd == [0.5, 1.0]


def test_tp_targets_usd_malformed_falls_back_to_empty(monkeypatch):
    monkeypatch.setenv("TP_TARGETS_USD", "not-a-number,0.5")
    assert load_settings().tp_targets_usd == []


def test_apply_db_overrides_empty_dict_is_a_noop():
    settings = load_settings()
    assert apply_db_overrides(settings, {}) is settings


def test_apply_db_overrides_overrides_connection_fields():
    settings = load_settings()
    overridden = apply_db_overrides(settings, {
        "mt5_login": "999888", "mt5_password": "hunter2", "mt5_server": "ICMarkets-Live",
    })
    assert overridden.mt5_login == "999888"
    assert overridden.mt5_password == "hunter2"
    assert overridden.mt5_server == "ICMarkets-Live"
    # everything else untouched
    assert overridden.symbol == settings.symbol
    assert overridden.risk_per_trade_usd == settings.risk_per_trade_usd


def test_apply_db_overrides_casts_numeric_and_bool_fields():
    settings = load_settings()
    overridden = apply_db_overrides(settings, {
        "risk_per_trade_usd": "2.5", "max_trades_per_day": "500",
        "dry_run": "false", "mt5_is_demo": "true",
    })
    assert overridden.risk_per_trade_usd == 2.5
    assert overridden.max_trades_per_day == 500
    assert overridden.dry_run is False
    assert overridden.mt5_is_demo is True


def test_apply_db_overrides_ignores_malformed_numeric_value_instead_of_crashing():
    settings = load_settings()
    overridden = apply_db_overrides(settings, {"risk_per_trade_usd": "not-a-number"})
    assert overridden.risk_per_trade_usd == settings.risk_per_trade_usd


def test_apply_db_overrides_never_touches_db_path():
    """db_path can't be DB-overridden - chicken-and-egg, the DB has to
    already be open using .env's path before it can be asked for overrides."""
    settings = load_settings()
    overridden = apply_db_overrides(settings, {"db_path": "/tmp/somewhere-else.db"})
    assert overridden.db_path == settings.db_path


def test_dashboard_auth_token_defaults_blank_and_reads_from_env(monkeypatch):
    monkeypatch.delenv("DASHBOARD_AUTH_TOKEN", raising=False)
    assert load_settings().dashboard_auth_token == ""
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "secret123")
    assert load_settings().dashboard_auth_token == "secret123"


def test_pause_flag_path_defaults_and_reads_from_env(monkeypatch):
    monkeypatch.delenv("PAUSE_FLAG_PATH", raising=False)
    assert load_settings().pause_flag_path == "data/PAUSED"
    monkeypatch.setenv("PAUSE_FLAG_PATH", "/tmp/custom_pause")
    assert load_settings().pause_flag_path == "/tmp/custom_pause"
