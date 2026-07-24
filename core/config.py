"""Loads .env into a typed Settings object. No secrets ever hit git."""
from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field, replace
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


def _float_list(name: str, default: list[float]) -> list[float]:
    val = os.getenv(name)
    if not val or not val.strip():
        return default
    try:
        parsed = [float(x.strip()) for x in val.split(",") if x.strip()]
    except ValueError:
        return default
    return parsed if parsed else default


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

    # Optional AI confirmation layer. It may only CONFIRM or VETO a signal
    # already produced by the deterministic strategy; risk sizing, stops and
    # circuit breakers remain outside its control.
    ai_brain_enabled: bool = False
    openrouter_api_key: str = ""
    openrouter_model: str = "openrouter/free"
    ai_brain_timeout_ms: int = 6000
    ai_brain_max_calls_per_day: int = 40
    ai_supervisor_enabled: bool = False
    openrouter_supervisor_api_key: str = ""
    openrouter_supervisor_model: str = "openrouter/free"
    ai_supervisor_timeout_ms: int = 6000
    ai_supervisor_max_calls_per_day: int = 24

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

    # Which broker adapter the engine connects through (see core/broker.py's
    # BrokerExecutor seam):
    #   "mt5_bridge" (default) - the Wine/MT5 bridge; covers any MT5 broker
    #       (FBS included - FBS has no public REST API, so this is the only
    #       honest way to reach an FBS account).
    #   "oanda" - direct OANDA v20 REST (core/oanda.py): NO MetaTrader, no
    #       Wine, no bridge process anywhere - pure HTTPS, works the same
    #       on any Linux install. Needs the OANDA_* values below.
    broker_kind: str = "mt5_bridge"
    oanda_api_token: str = ""
    oanda_account_id: str = ""
    oanda_environment: str = "practice"  # "practice" (demo) or "live"

    # Manual kill switch: if this file exists, the engine force-closes any
    # open position at market on its next poll and halts (see core/engine.py,
    # main.py). Checked every step independent of core/engine_supervisor.py's
    # own process supervision, so it works even without access to the
    # dashboard or a terminal on the machine running the bot -
    # e.g. `touch data/EMERGENCY_STOP` from anything that can write to the
    # filesystem (a synced folder, a cron job, a phone SSH app).
    kill_switch_path: str = "data/EMERGENCY_STOP"

    # Manual pause: distinct from kill_switch_path above. While this file
    # exists the engine stops opening NEW trades but keeps managing/
    # protecting any already-open position and keeps running (no process
    # exit, unlike the kill switch) - a reversible day-to-day control, not
    # an emergency measure. Toggled by the dashboard's pause/resume button.
    pause_flag_path: str = "data/PAUSED"

    # Shared secret required (via X-Dashboard-Token) on dashboard.py's
    # mutating routes (settings save, pause/resume) when set - mirrors
    # bridge_auth_token's role for the MT5 bridge. Blank means those routes
    # are unauthenticated, fine on 127.0.0.1-only native mode but a real gap
    # if the dashboard is ever run with --web --host 0.0.0.0.
    dashboard_auth_token: str = ""

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
    strat_sl_atr_multiple: float = 3.5
    strat_cooldown_bars: int = 2
    strat_bb_period: int = 20
    strat_bb_std: float = 2.0
    strat_rsi_period: int = 7
    strat_atr_period: int = 14
    strat_adx_period: int = 14
    strat_trend_filter_adx_threshold: float = 35.0

    # Optional explicit per-level TP profit targets in USD, e.g.
    # "0.28,0.60,1.20" - each level books its own configured dollar amount
    # for its slice instead of only the first level being dollar-anchored
    # (min_tp_usd) and later levels being geometric multiples of its price
    # distance. Empty (default) preserves the original, already-backtested
    # behavior - see core/strategy.py's build_tp_ladder.
    tp_targets_usd: list[float] = field(default_factory=list)

    # How many M1 candles the engine fetches from the bridge each poll.
    # 200 (the old hardcoded value) is enough for the mean-reversion
    # strategy alone, but not for strat_enable_momentum_cross's M5 EMA50
    # warmup or strat_enable_asian_breakout's range backfill on a fresh
    # restart - both want several hours of M1 history. One bridge round
    # trip regardless of this value (bigger JSON payload, not more calls).
    candle_history_count: int = 600

    # Extra independent M1 entry signals layered on top of the
    # mean-reversion strategy above - see core/signals.py's module
    # docstring for what each one does and, importantly, what was
    # deliberately NOT ported from the reference EA they're adapted from
    # (no stop-loss-free grid/martingale averaging - every signal here
    # goes through the exact same RiskManager sizing/SL as mean-reversion,
    # no exceptions). Individually toggleable, and DEFAULT OFF - a real
    # backtest on 7 real days of gold (see README, "Ronda 4") found every
    # single one of these, added on its own, increases trade count a lot
    # (as intended) but makes real dollar PnL meaningfully WORSE than
    # mean-reversion alone, not better (small TP1 target vs a
    # proportionally wider stop is a poor risk:reward for a continuation
    # entry - mean-reversion gets away with it because it enters AT an
    # extreme, these don't). Shipped here, tested, and off by default so
    # turning one on is an informed opt-in after your own backtest, not an
    # accidental regression versus the already-validated baseline.
    strat_enable_momentum_cross: bool = False
    strat_enable_rsi_hysteresis: bool = False
    strat_enable_directional_candle: bool = False
    strat_enable_session_open: bool = False
    strat_enable_asian_breakout: bool = False
    # M15 trend context (last CLOSED M15 candle's close vs open) as an M1
    # entry filter - user-requested, aimed at maximum trade FREQUENCY over
    # per-trade size. See README "Ronda 9" for the real backtest result on
    # this dataset before flipping this to true.
    strat_enable_m15_trend: bool = False

    # Indicators shared by the extra signals above (RSI/ATR periods) -
    # deliberately separate from strat_rsi_period/strat_atr_period, which
    # only tune the mean-reversion strategy and must stay exactly as
    # backtested (see README) regardless of what these are set to.
    strat_composite_rsi_period: int = 14
    strat_composite_atr_period: int = 14

    strat_momentum_sl_atr_multiple: float = 2.0

    strat_rsi_hysteresis_sl_atr_multiple: float = 2.0
    strat_rsi_hysteresis_upper: float = 52.0
    strat_rsi_hysteresis_lower: float = 48.0

    strat_directional_candle_sl_buffer_atr_mult: float = 0.3

    strat_session_sl_atr_multiple: float = 2.0
    strat_session_london_start_hour: int = 7
    strat_session_london_end_hour: int = 10
    strat_session_ny_start_hour: int = 12
    strat_session_ny_end_hour: int = 16

    strat_asian_sl_atr_multiple: float = 2.0
    strat_asian_range_start_hour: int = 0
    strat_asian_range_end_hour: int = 7
    strat_asian_breakout_start_hour: int = 7
    strat_asian_breakout_end_hour: int = 10
    strat_asian_breakout_buffer_pct: float = 0.05

    strat_m15_trend_sl_atr_multiple: float = 2.0
    strat_m15_trend_min_body_range_ratio: float = 0.1

    # Seventh extra signal - ported from a user-submitted MQL5 EA
    # ("ScalpMaster Pro v1.0"), SMA(9)/SMA(26) cross + fixed 2-bar wait +
    # permissive RSI(70/30) filter, WITH a real ATR-based stop-loss added
    # (the original opens with SL=0 - see core/signals.py::MACrossGridStrategy
    # docstring for why that was rejected). Meant for native M15 bars, not
    # M1 - see README "Ronda 10" for the real backtest result on this
    # dataset before flipping this to true.
    # Ronda 21: Ronda 20's MIN_TP_USD 0.28 -> 0.60 rescued this signal -
    # TRAIN +$11.72/89.3% win (was -$29.24), TEST +$3.19/80.0% win, see
    # .env.example "Ronda 21" for the full sweep. Enabled by default.
    strat_enable_ma_grid: bool = True
    strat_ma_grid_sl_atr_multiple: float = 3.0
    strat_ma_grid_fast_period: int = 9
    strat_ma_grid_slow_period: int = 26
    strat_ma_grid_entry_wait_bars: int = 2
    strat_ma_grid_rsi_overbought: float = 70.0
    strat_ma_grid_rsi_oversold: float = 30.0
    # 20.0 = the honestly-measured risk-reducing value from README "Ronda
    # 11" (5-6x smaller loss/drawdown on real M1 data), NOT a value proven
    # to make this signal profitable - see MACrossGridStrategy's docstring.
    strat_ma_grid_min_adx: float = 20.0

    # Eighth/ninth extra signals (Ronda 19) - NOT adapted from the
    # reference EA, genuinely new: classic Japanese-candlestick reversal
    # patterns (bullish/bearish engulfing, hammer/shooting-star pin bar)
    # gated to only fire at a Bollinger Band/RSI extreme, the same context
    # the validated mean-reversion strategy already requires - see
    # core/signals.py's EngulfingReversalStrategy/PinBarReversalStrategy
    # docstrings for the full thesis. See .env.example "Ronda 19" for the
    # real train/test backtest result before flipping either to true.
    strat_enable_engulfing: bool = False
    strat_engulfing_sl_buffer_atr_mult: float = 0.3
    strat_engulfing_rsi_oversold: float = 30.0
    strat_engulfing_rsi_overbought: float = 70.0
    strat_engulfing_bb_tolerance_atr_mult: float = 0.3
    strat_enable_pin_bar: bool = False
    strat_pin_bar_sl_buffer_atr_mult: float = 0.3
    strat_pin_bar_rsi_oversold: float = 30.0
    strat_pin_bar_rsi_overbought: float = 70.0
    strat_pin_bar_bb_tolerance_atr_mult: float = 0.3
    strat_pin_bar_min_wick_body_ratio: float = 2.0
    strat_pin_bar_max_opposite_wick_ratio: float = 0.5
    strat_pin_bar_min_close_position_ratio: float = 0.6

    # Optional confluence/regime and bounded basket management. All are
    # disabled by default; enabling them never bypasses RiskManager.
    strat_enable_quantum_queen: bool = False
    strat_quantum_primary: bool = False
    strat_quantum_threshold: int = 2
    strat_quantum_mask: int = 4095
    regime_filter_enabled: bool = True
    regime_adx_trend_threshold: float = 25.0
    regime_atr_ratio_high: float = 1.35
    regime_atr_ratio_low: float = 0.65
    grid_enabled: bool = False
    grid_max_positions: int = 3
    grid_step_atr: float = 1.0
    grid_lot_multiplier: float = 1.0
    grid_max_lot: float = 0.09
    recovery_enabled: bool = False
    recovery_max_levels: int = 2
    recovery_min_regime: str = "range"
    dynamic_lot_cap: float = 0.09


def load_settings() -> Settings:
    return Settings(
        mt5_login=os.getenv("MT5_LOGIN", ""),
        mt5_password=os.getenv("MT5_PASSWORD", ""),
        mt5_server=os.getenv("MT5_SERVER", "FBS-Demo"),
        mt5_is_demo=_bool("MT5_IS_DEMO", True),
        bridge_url=os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:5001"),
        bridge_timeout_ms=_int("MT5_BRIDGE_TIMEOUT_MS", 8000),
        broker_kind=os.getenv("BROKER_KIND", "mt5_bridge").strip().lower(),
        oanda_api_token=os.getenv("OANDA_API_TOKEN", ""),
        oanda_account_id=os.getenv("OANDA_ACCOUNT_ID", ""),
        oanda_environment=os.getenv("OANDA_ENV", "practice").strip().lower(),
        symbol=os.getenv("SYMBOL", "XAUUSD"),
        timeframe=os.getenv("TIMEFRAME", "M1"),
        risk_per_trade_usd=_float("RISK_PER_TRADE_USD", 6.0),  # see .env.example / README "Ronda 12" for why 6.0, not 3.0
        max_daily_loss_usd=_float("MAX_DAILY_LOSS_USD", 8.0),
        max_daily_drawdown_pct=_float("MAX_DAILY_DRAWDOWN_PCT", 20.0),
        max_trades_per_day=_int("MAX_TRADES_PER_DAY", 1000),
        min_tp_usd=_float("MIN_TP_USD", 0.60),  # see .env.example "Ronda 20" for the sweep behind 0.28 -> 0.60
        tp_levels=_int("TP_LEVELS", 5),  # see .env.example / README "Ronda 5" for why 5, not 3
        # Keep the library fallback safe when called without a .env; the
        # installer-created .env is explicitly DRY_RUN=false for this live
        # deployment, while tests/synthetic runs pass their own Settings.
        dry_run=_bool("DRY_RUN", True),
        db_path=os.getenv("DASHBOARD_DB_PATH", "data/trades.db"),
        ai_brain_enabled=_bool("AI_BRAIN_ENABLED", False),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "openrouter/free").strip(),
        ai_brain_timeout_ms=max(_int("AI_BRAIN_TIMEOUT_MS", 6000), 500),
        ai_brain_max_calls_per_day=max(_int("AI_BRAIN_MAX_CALLS_PER_DAY", 40), 0),
        ai_supervisor_enabled=_bool("AI_SUPERVISOR_ENABLED", False),
        openrouter_supervisor_api_key=os.getenv("OPENROUTER_SUPERVISOR_API_KEY", ""),
        openrouter_supervisor_model=os.getenv("OPENROUTER_SUPERVISOR_MODEL", "openrouter/free").strip(),
        ai_supervisor_timeout_ms=max(_int("AI_SUPERVISOR_TIMEOUT_MS", 6000), 500),
        ai_supervisor_max_calls_per_day=max(_int("AI_SUPERVISOR_MAX_CALLS_PER_DAY", 24), 0),
        snapshot_retention_days=_int("SNAPSHOT_RETENTION_DAYS", 30),
        strat_rsi_oversold=_float("STRAT_RSI_OVERSOLD", 25.0),
        strat_rsi_overbought=_float("STRAT_RSI_OVERBOUGHT", 75.0),
        strat_max_spread_price=_float("STRAT_MAX_SPREAD_PRICE", 0.5),
        strat_min_atr_price=_float("STRAT_MIN_ATR_PRICE", 0.15),
        strat_sl_atr_multiple=_float("STRAT_SL_ATR_MULTIPLE", 3.5),
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
        tp_targets_usd=_float_list("TP_TARGETS_USD", []),
        pause_flag_path=os.getenv("PAUSE_FLAG_PATH", "data/PAUSED"),
        dashboard_auth_token=os.getenv("DASHBOARD_AUTH_TOKEN", ""),
        candle_history_count=_int("CANDLE_HISTORY_COUNT", 600),
        strat_enable_momentum_cross=_bool("STRAT_ENABLE_MOMENTUM_CROSS", False),
        strat_enable_rsi_hysteresis=_bool("STRAT_ENABLE_RSI_HYSTERESIS", False),
        strat_enable_directional_candle=_bool("STRAT_ENABLE_DIRECTIONAL_CANDLE", False),
        strat_enable_session_open=_bool("STRAT_ENABLE_SESSION_OPEN", False),
        strat_enable_asian_breakout=_bool("STRAT_ENABLE_ASIAN_BREAKOUT", False),
        strat_enable_m15_trend=_bool("STRAT_ENABLE_M15_TREND", False),
        strat_composite_rsi_period=_int("STRAT_COMPOSITE_RSI_PERIOD", 14),
        strat_composite_atr_period=_int("STRAT_COMPOSITE_ATR_PERIOD", 14),
        strat_momentum_sl_atr_multiple=_float("STRAT_MOMENTUM_SL_ATR_MULTIPLE", 2.0),
        strat_rsi_hysteresis_sl_atr_multiple=_float("STRAT_RSI_HYSTERESIS_SL_ATR_MULTIPLE", 2.0),
        strat_rsi_hysteresis_upper=_float("STRAT_RSI_HYSTERESIS_UPPER", 52.0),
        strat_rsi_hysteresis_lower=_float("STRAT_RSI_HYSTERESIS_LOWER", 48.0),
        strat_directional_candle_sl_buffer_atr_mult=_float("STRAT_DIRECTIONAL_CANDLE_SL_BUFFER_ATR_MULT", 0.3),
        strat_session_sl_atr_multiple=_float("STRAT_SESSION_SL_ATR_MULTIPLE", 2.0),
        strat_session_london_start_hour=_int("STRAT_SESSION_LONDON_START_HOUR", 7),
        strat_session_london_end_hour=_int("STRAT_SESSION_LONDON_END_HOUR", 10),
        strat_session_ny_start_hour=_int("STRAT_SESSION_NY_START_HOUR", 12),
        strat_session_ny_end_hour=_int("STRAT_SESSION_NY_END_HOUR", 16),
        strat_asian_sl_atr_multiple=_float("STRAT_ASIAN_SL_ATR_MULTIPLE", 2.0),
        strat_asian_range_start_hour=_int("STRAT_ASIAN_RANGE_START_HOUR", 0),
        strat_asian_range_end_hour=_int("STRAT_ASIAN_RANGE_END_HOUR", 7),
        strat_asian_breakout_start_hour=_int("STRAT_ASIAN_BREAKOUT_START_HOUR", 7),
        strat_asian_breakout_end_hour=_int("STRAT_ASIAN_BREAKOUT_END_HOUR", 10),
        strat_asian_breakout_buffer_pct=_float("STRAT_ASIAN_BREAKOUT_BUFFER_PCT", 0.05),
        strat_m15_trend_sl_atr_multiple=_float("STRAT_M15_TREND_SL_ATR_MULTIPLE", 2.0),
        strat_m15_trend_min_body_range_ratio=_float("STRAT_M15_TREND_MIN_BODY_RANGE_RATIO", 0.1),
        strat_enable_ma_grid=_bool("STRAT_ENABLE_MA_GRID", True),
        strat_ma_grid_sl_atr_multiple=_float("STRAT_MA_GRID_SL_ATR_MULTIPLE", 3.0),
        strat_ma_grid_fast_period=_int("STRAT_MA_GRID_FAST_PERIOD", 9),
        strat_ma_grid_slow_period=_int("STRAT_MA_GRID_SLOW_PERIOD", 26),
        strat_ma_grid_entry_wait_bars=_int("STRAT_MA_GRID_ENTRY_WAIT_BARS", 2),
        strat_ma_grid_rsi_overbought=_float("STRAT_MA_GRID_RSI_OVERBOUGHT", 70.0),
        strat_ma_grid_rsi_oversold=_float("STRAT_MA_GRID_RSI_OVERSOLD", 30.0),
        strat_ma_grid_min_adx=_float("STRAT_MA_GRID_MIN_ADX", 20.0),
        strat_enable_engulfing=_bool("STRAT_ENABLE_ENGULFING", False),
        strat_engulfing_sl_buffer_atr_mult=_float("STRAT_ENGULFING_SL_BUFFER_ATR_MULT", 0.3),
        strat_engulfing_rsi_oversold=_float("STRAT_ENGULFING_RSI_OVERSOLD", 30.0),
        strat_engulfing_rsi_overbought=_float("STRAT_ENGULFING_RSI_OVERBOUGHT", 70.0),
        strat_engulfing_bb_tolerance_atr_mult=_float("STRAT_ENGULFING_BB_TOLERANCE_ATR_MULT", 0.3),
        strat_enable_pin_bar=_bool("STRAT_ENABLE_PIN_BAR", False),
        strat_pin_bar_sl_buffer_atr_mult=_float("STRAT_PIN_BAR_SL_BUFFER_ATR_MULT", 0.3),
        strat_pin_bar_rsi_oversold=_float("STRAT_PIN_BAR_RSI_OVERSOLD", 30.0),
        strat_pin_bar_rsi_overbought=_float("STRAT_PIN_BAR_RSI_OVERBOUGHT", 70.0),
        strat_pin_bar_bb_tolerance_atr_mult=_float("STRAT_PIN_BAR_BB_TOLERANCE_ATR_MULT", 0.3),
        strat_pin_bar_min_wick_body_ratio=_float("STRAT_PIN_BAR_MIN_WICK_BODY_RATIO", 2.0),
        strat_pin_bar_max_opposite_wick_ratio=_float("STRAT_PIN_BAR_MAX_OPPOSITE_WICK_RATIO", 0.5),
        strat_pin_bar_min_close_position_ratio=_float("STRAT_PIN_BAR_MIN_CLOSE_POSITION_RATIO", 0.6),
        strat_enable_quantum_queen=_bool("STRAT_ENABLE_QUANTUM_QUEEN", False),
        strat_quantum_primary=_bool("STRAT_QUANTUM_PRIMARY", False),
        strat_quantum_threshold=max(_int("STRAT_QUANTUM_THRESHOLD", 2), 1),
        strat_quantum_mask=max(_int("STRAT_QUANTUM_MASK", 4095), 0),
        regime_filter_enabled=_bool("REGIME_FILTER_ENABLED", True),
        regime_adx_trend_threshold=_float("REGIME_ADX_TREND_THRESHOLD", 25.0),
        regime_atr_ratio_high=_float("REGIME_ATR_RATIO_HIGH", 1.35),
        regime_atr_ratio_low=_float("REGIME_ATR_RATIO_LOW", 0.65),
        grid_enabled=_bool("GRID_ENABLED", False),
        grid_max_positions=max(_int("GRID_MAX_POSITIONS", 3), 1),
        grid_step_atr=max(_float("GRID_STEP_ATR", 1.0), 0.1),
        grid_lot_multiplier=max(_float("GRID_LOT_MULTIPLIER", 1.0), 0.1),
        grid_max_lot=min(max(_float("GRID_MAX_LOT", 0.09), 0.0), 0.09),
        recovery_enabled=_bool("RECOVERY_ENABLED", False),
        recovery_max_levels=max(_int("RECOVERY_MAX_LEVELS", 2), 0),
        recovery_min_regime=os.getenv("RECOVERY_MIN_REGIME", "range").strip().lower(),
        dynamic_lot_cap=min(max(_float("DYNAMIC_LOT_CAP", 0.09), 0.0), 0.09),
    )


# Fields the dashboard's Settings tab is allowed to override, and how to cast
# the TEXT value stored in bot_settings back to the right Python type. A
# deliberately small subset (broker connection + risk limits) - not the full
# Settings surface, and never db_path itself (chicken-and-egg: the DB has to
# already be open, using the path .env gave us, before it can be asked for
# overrides).
DB_OVERRIDABLE_BOOL_FIELDS = {"mt5_is_demo", "dry_run"}
DB_OVERRIDABLE_FLOAT_FIELDS = {"risk_per_trade_usd", "max_daily_loss_usd", "max_daily_drawdown_pct"}
DB_OVERRIDABLE_INT_FIELDS = {"max_trades_per_day"}
DB_OVERRIDABLE_STR_FIELDS = {"mt5_login", "mt5_password", "mt5_server"}


def apply_db_overrides(settings: Settings, db_settings: dict[str, str]) -> Settings:
    """Layers settings saved from the dashboard (core/database.py's
    bot_settings table) on top of .env-derived Settings. Call once at
    startup, after the Database is open - see main.py. A malformed stored
    value (shouldn't happen via the dashboard's own validation, but the DB
    doesn't enforce types) is skipped rather than crashing the engine; the
    .env-derived default is used for that one field instead."""
    if not db_settings:
        return settings
    overrides: dict[str, object] = {}
    for key in DB_OVERRIDABLE_STR_FIELDS:
        if key in db_settings:
            overrides[key] = db_settings[key]
    for key in DB_OVERRIDABLE_BOOL_FIELDS:
        if key in db_settings:
            overrides[key] = db_settings[key].strip().lower() in ("1", "true", "yes", "on")
    for key in DB_OVERRIDABLE_FLOAT_FIELDS:
        if key in db_settings:
            with contextlib.suppress(ValueError):
                overrides[key] = float(db_settings[key])
    for key in DB_OVERRIDABLE_INT_FIELDS:
        if key in db_settings:
            with contextlib.suppress(ValueError):
                overrides[key] = int(db_settings[key])
    return replace(settings, **overrides) if overrides else settings
