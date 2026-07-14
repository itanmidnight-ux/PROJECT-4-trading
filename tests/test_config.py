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
