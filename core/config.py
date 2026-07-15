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

    # How often the engine polls the bridge for a fresh price/candles and
    # re-checks open positions. Lower = reacts faster to price moves (matters
    # most for not giving back gains between TP levels), but every poll is a
    # round trip through the Wine bridge process - going far below ~0.5s on
    # typical hardware just means most polls return the same tick without
    # the broker having produced a new one yet, not real extra speed.
    poll_seconds: float = 2.0

    # Shared secret sent as X-Bridge-Token on every bridge request. Blank
    # means the bridge is running without auth (see bridge/mt5_bridge_server.py) -
    # install.sh generates one automatically; only expect this to be blank
    # if .env was hand-written instead of created by install.sh.
    bridge_auth_token: str = ""

    # Manual kill switch: if this file exists, the engine force-closes any
    # open position at market on its next poll and halts (see core/engine.py,
    # main.py). Checked every step independent of run.sh/stop.sh, so it
    # works even without terminal access to the machine running the bot -
    # e.g. `touch data/EMERGENCY_STOP` from anything that can write to the
    # filesystem (a synced folder, a cron job, a phone SSH app).
    kill_switch_path: str = "data/EMERGENCY_STOP"

    # account_snapshots gets a row every engine poll with no natural cap
    # (unlike trades/events, which only grow with real activity) - the
    # engine prunes rows older than this on an hourly check so the DB
    # doesn't grow unbounded on a long-running deployment.
    snapshot_retention_days: int = 30

    # Strategy tuning - defaults match ScalpStrategy's own defaults, kept
    # here too so they show up in .env.example instead of only in code.
    strat_rsi_oversold: float = 25.0
    strat_rsi_overbought: float = 75.0
    strat_max_spread_price: float = 0.5
    strat_min_atr_price: float = 0.15
    strat_sl_atr_multiple: float = 4.0
    strat_cooldown_bars: int = 2
    strat_bb_period: int = 20
    strat_bb_std: float = 2.0
    strat_rsi_period: int = 7
    strat_atr_period: int = 14
    strat_adx_period: int = 14
    strat_trend_filter_adx_threshold: float = 35.0


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
        snapshot_retention_days=_int("SNAPSHOT_RETENTION_DAYS", 30),
        strat_rsi_oversold=_float("STRAT_RSI_OVERSOLD", 25.0),
        strat_rsi_overbought=_float("STRAT_RSI_OVERBOUGHT", 75.0),
        strat_max_spread_price=_float("STRAT_MAX_SPREAD_PRICE", 0.5),
        strat_min_atr_price=_float("STRAT_MIN_ATR_PRICE", 0.15),
        strat_sl_atr_multiple=_float("STRAT_SL_ATR_MULTIPLE", 4.0),
        strat_cooldown_bars=_int("STRAT_COOLDOWN_BARS", 2),
        strat_bb_period=_int("STRAT_BB_PERIOD", 20),
        strat_bb_std=_float("STRAT_BB_STD", 2.0),
        strat_rsi_period=_int("STRAT_RSI_PERIOD", 7),
        strat_atr_period=_int("STRAT_ATR_PERIOD", 14),
        strat_adx_period=_int("STRAT_ADX_PERIOD", 14),
        strat_trend_filter_adx_threshold=_float("STRAT_TREND_FILTER_ADX_THRESHOLD", 35.0),
        poll_seconds=max(_float("POLL_SECONDS", 2.0), 0.25),
        bridge_auth_token=os.getenv("BRIDGE_AUTH_TOKEN", ""),
        kill_switch_path=os.getenv("KILL_SWITCH_PATH", "data/EMERGENCY_STOP"),
    )
