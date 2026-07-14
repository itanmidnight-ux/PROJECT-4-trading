"""Loads .env into a typed Settings object. No secrets ever hit git."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

load_dotenv(ENV_PATH)


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    try:
        return float(val) if val else default
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    try:
        return int(val) if val else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    mt5_login: str
    mt5_password: str
    mt5_server: str
    mt5_is_demo: bool

    bridge_url: str
    bridge_timeout_ms: int

    symbol: str
    timeframe: str

    risk_per_trade_usd: float
    max_daily_loss_usd: float
    max_daily_drawdown_pct: float
    max_trades_per_day: int
    min_tp_usd: float
    tp_levels: int

    dry_run: bool

    db_path: str


def load_settings() -> Settings:
    return Settings(
        mt5_login=os.getenv("MT5_LOGIN", ""),
        mt5_password=os.getenv("MT5_PASSWORD", ""),
        mt5_server=os.getenv("MT5_SERVER", "FBS-Demo"),
        mt5_is_demo=_bool("MT5_IS_DEMO", True),
        bridge_url=os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:5001"),
        bridge_timeout_ms=_int("MT5_BRIDGE_TIMEOUT_MS", 8000),
        symbol=os.getenv("SYMBOL", "XAUUSD"),
        timeframe=os.getenv("TIMEFRAME", "M1"),
        risk_per_trade_usd=_float("RISK_PER_TRADE_USD", 1.0),
        max_daily_loss_usd=_float("MAX_DAILY_LOSS_USD", 8.0),
        max_daily_drawdown_pct=_float("MAX_DAILY_DRAWDOWN_PCT", 20.0),
        max_trades_per_day=_int("MAX_TRADES_PER_DAY", 1000),
        min_tp_usd=_float("MIN_TP_USD", 0.28),
        tp_levels=_int("TP_LEVELS", 3),
        dry_run=_bool("DRY_RUN", True),
        db_path=os.getenv("DASHBOARD_DB_PATH", "data/trades.db"),
    )
