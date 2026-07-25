"""
dashboard.py - web dashboard for the XAUUSD scalper.

Run with:  .venv/bin/python dashboard.py
The project intentionally exposes only the browser dashboard; the former
native pywebview wrapper has been removed so every launch behaves the same
on desktop, server and headless environments.

Shows account stats, equity curve, win/loss breakdown, P&L by day/month,
trade history, and engine events - all read live from the same SQLite
database the trading engine writes to (data/trades.db by default). Safe to
run at any time, independently of whether the engine is running.

Routes that are NOT read-only: the Settings tab's save (POST
/api/settings), the pause/resume toggle (POST /api/bot/pause,
/api/bot/resume), and engine process control (POST /api/engine/start,
/api/engine/stop). Saved settings only take effect the NEXT time the
engine process starts (see core/config.py::apply_db_overrides);
pause/resume only flips a flag file an ALREADY-RUNNING engine polls for on
its own - it does nothing if the engine process isn't running at all, use
engine/start for that. All of these require X-Dashboard-Token when
DASHBOARD_AUTH_TOKEN is set - see _check_dashboard_auth.

Engine process control (start/stop) is deliberately handled here, not by
run.sh: `./run.sh --start` only brings up the bridge and this dashboard,
so the engine (the thing that actually places orders) only ever runs when
explicitly started from here - see core/engine_supervisor.py for the
crash-resilient (restart-with-backoff) process it spawns and controls.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

from core.config import (  # noqa: E402
    DB_OVERRIDABLE_BOOL_FIELDS,
    DB_OVERRIDABLE_FLOAT_FIELDS,
    DB_OVERRIDABLE_INT_FIELDS,
    apply_db_overrides,
    load_settings,
    ENV_PATH,
)
from core.database import Database  # noqa: E402
from dataclasses import replace  # noqa: E402

# Lazy: the dashboard's read-only/status view must still start on an
# install without pandas (e.g. --no-wine, or a partial/broken venv). The
# MT5 backtest route loads these only when the operator actually requests
# a backtest.
Mt5BridgeClient = None
run_backtest = None

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "dashboard"
RUN_DIR = ROOT / "data" / "run"
ENGINE_PID_FILE = RUN_DIR / "engine.pid"

settings = load_settings()
db = Database(settings.db_path)

app = Flask(__name__, static_folder=None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reap_children(signum: int, frame: object) -> None:
    """SIGCHLD fires the instant any child of this process (the engine
    supervisor spawned by /api/engine/start) exits, for any reason - not
    only when this dashboard itself sent the SIGTERM (POST
    /api/engine/stop). run.sh's --stop kills the supervisor's PID
    directly from the pidfile, bypassing that route entirely, so
    _engine_pid()'s lazy reap-on-next-check isn't enough on its own:
    nothing guarantees a status check happens before something else
    (run.sh's kill_pidfile_external, polling `kill -0` in a loop) finds
    an exited-but-unreaped zombie reporting "still alive" forever.
    Reaping here, proactively, the moment the kernel tells us a child
    exited, closes that gap regardless of who signaled the child or
    when anyone next asks about it."""
    with contextlib.suppress(ChildProcessError):
        while True:
            pid, _status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break


def _engine_deps_available() -> bool:
    """Whether main.py's heavy imports (pandas/numpy) exist in this venv -
    the engine and this dashboard share it, so checking here is
    authoritative. See api_engine_start for why this gate exists."""
    return (importlib.util.find_spec("pandas") is not None
            and importlib.util.find_spec("numpy") is not None)


def _engine_pid() -> int | None:
    """Returns the tracked engine supervisor's PID if it's actually still
    alive - self-heals a stale pidfile (process died without anyone
    calling /api/engine/stop, e.g. it crashed past its own retry budget or
    the machine restarted) by deleting it right here, so every caller
    (status, start, stop) sees a consistent, truthful answer without a
    separate cleanup step anywhere.

    Reaps the process first (os.waitpid with WNOHANG) before checking
    os.kill(pid, 0) - this dashboard process IS the supervisor's real OS
    parent (subprocess.Popen made it so; start_new_session=True only
    detaches its session/controlling terminal, not the parent-child
    relationship wait() relies on), so an exited-but-unreaped supervisor
    sits as a zombie: still present in the process table, still a
    "yes" from kill(pid, 0), forever, until reaped. Found via an actual
    Playwright run of the stop button: without this, /api/status kept
    reporting engine_running=true long after the process had already
    logged its own clean shutdown."""
    if not ENGINE_PID_FILE.exists():
        return None
    try:
        pid = int(ENGINE_PID_FILE.read_text().strip())
    except ValueError:
        ENGINE_PID_FILE.unlink(missing_ok=True)
        return None
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        ENGINE_PID_FILE.unlink(missing_ok=True)
        return None
    return pid


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}  # sqlite3.Row isn't a dict; .keys() is required here


def _write_env_secret(key: str, value: str) -> None:
    """Atomically update one secret in .env without logging or echoing it."""
    path = ENV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f"{key}={value}"
    found = False
    out = []
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(replacement)
            found = True
        else:
            out.append(line)
    if not found:
        out.append(replacement)
    import tempfile
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".env.", delete=False) as tmp:
        tmp.write("\n".join(out) + "\n")
        tmp_path = Path(tmp.name)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)


def _read_secret_field(data: dict, field: str) -> str | None:
    value = data.get(field)
    if value is None or not str(value).strip():
        return None
    value = str(value).strip()
    if len(value) > 512 or "\n" in value or "\r" in value:
        raise ValueError(f"{field} invalida")
    return value


@app.before_request
def _check_dashboard_auth():
    """Defense in depth, same shape as the bridge's own auth (see
    bridge/mt5_bridge_server.py): every read (GET) route already exposed
    real account data with no auth before this, documented and accepted for
    --web --host 0.0.0.0 use. Adding settings-save and pause/resume adds
    WRITE routes that can change stored broker credentials or trading
    state - those (all POST) require the configured token; GETs are
    unchanged so existing --web usage without a token keeps working."""
    if request.method != "POST" or not settings.dashboard_auth_token:
        return None
    supplied = request.headers.get("X-Dashboard-Token")
    if supplied != settings.dashboard_auth_token:
        return jsonify({"ok": False, "error": "unauthorized: missing or invalid X-Dashboard-Token"}), 401
    return None


@app.route("/")
def index():
    return send_from_directory(DASHBOARD_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(DASHBOARD_DIR, filename)


@app.route("/api/status")
def api_status():
    latest_ts = None
    curve = db.equity_curve(limit=1)
    if curve:
        latest_ts = curve[-1]["ts"]
        connected = (time.time() - _parse_ts(latest_ts)) < 30
    else:
        connected = False
    # Deliberately the RUNNING engine's own settings (this process's .env-
    # derived `settings`, loaded once at dashboard startup), not the
    # DB-override layer - a pending settings-tab save hasn't been applied by
    # the actual running engine yet (see core/config.py::apply_db_overrides),
    # so showing it here would claim a connection/account that isn't
    # actually the one in use until the engine restarts. /api/settings is
    # the place pending (not-yet-applied) values belong.
    return jsonify({
        "mode": "LIVE" if not settings.dry_run else "DRY_RUN",
        "connected": connected,
        "login": settings.mt5_login or "sin configurar",
        "server": settings.mt5_server,
        "paused": Path(settings.pause_flag_path).exists(),
        "engine_running": _engine_pid() is not None,
        "auth_required": bool(settings.dashboard_auth_token),
    })


def _parse_ts(iso: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


@app.route("/api/summary")
def api_summary():
    return jsonify(db.summary())


@app.route("/api/equity_curve")
def api_equity_curve():
    rows = db.equity_curve(limit=300)
    return jsonify([_row_to_dict(r) for r in rows])


@app.route("/api/pnl_daily")
def api_pnl_daily():
    rows = db.pnl_by_day(days=14)
    return jsonify([_row_to_dict(r) for r in rows])


@app.route("/api/pnl_monthly")
def api_pnl_monthly():
    rows = db.pnl_by_month(months=12)
    return jsonify([_row_to_dict(r) for r in rows])


@app.route("/api/trades")
def api_trades():
    rows = db.recent_trades(limit=50)
    return jsonify([_row_to_dict(r) for r in rows])


@app.route("/api/events")
def api_events():
    rows = db.recent_events(limit=30)
    return jsonify([_row_to_dict(r) for r in rows])


@app.route("/api/settings")
def api_get_settings():
    """The pending, possibly-not-yet-applied view (DB overrides layered on
    top of .env) - unlike /api/status, which deliberately reports what the
    running engine actually connected with. mt5_password is NEVER echoed
    back in the clear - only whether one is currently stored."""
    effective = apply_db_overrides(settings, db.get_all_settings())
    return jsonify({
        "mt5_login": effective.mt5_login,
        "mt5_server": effective.mt5_server,
        "mt5_is_demo": effective.mt5_is_demo,
        "has_password": bool(effective.mt5_password),
        "dry_run": effective.dry_run,
        "risk_per_trade_usd": effective.risk_per_trade_usd,
        "max_daily_loss_usd": effective.max_daily_loss_usd,
        "max_daily_drawdown_pct": effective.max_daily_drawdown_pct,
        "max_trades_per_day": effective.max_trades_per_day,
        "symbol": effective.symbol,
        "has_openrouter_api_key": bool(effective.openrouter_api_key),
        "has_supervisor_api_key": bool(effective.openrouter_supervisor_api_key),
        "ai_brain_enabled": effective.ai_brain_enabled,
        "ai_supervisor_enabled": effective.ai_supervisor_enabled,
        "openrouter_model": effective.openrouter_model,
        "openrouter_supervisor_model": effective.openrouter_supervisor_model,
    })


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    """Validates and persists to bot_settings (see core/database.py). Does
    NOT touch a running engine - see the module docstring and
    core/config.py::apply_db_overrides for why that's a restart, not a live
    reload. An empty/missing mt5_password means 'leave the stored one
    unchanged', not 'clear it' - the field is never round-tripped to the
    browser to be blanked and resubmitted by accident."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "cuerpo invalido, se esperaba JSON"}), 400

    to_save: dict[str, str] = {}

    global settings
    env_updates: dict[str, str] = {}
    try:
        key1 = _read_secret_field(data, "openrouter_api_key")
        key2 = _read_secret_field(data, "openrouter_supervisor_api_key")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if key1:
        env_updates["OPENROUTER_API_KEY"] = key1
    if key2:
        env_updates["OPENROUTER_SUPERVISOR_API_KEY"] = key2
    if "ai_brain_enabled" in data:
        env_updates["AI_BRAIN_ENABLED"] = "true" if data["ai_brain_enabled"] else "false"
    if "ai_supervisor_enabled" in data:
        env_updates["AI_SUPERVISOR_ENABLED"] = "true" if data["ai_supervisor_enabled"] else "false"

    for key in ("mt5_login", "mt5_server"):
        if key in data and str(data[key]).strip():
            to_save[key] = str(data[key]).strip()
    if data.get("mt5_password") and str(data["mt5_password"]).strip():
        to_save["mt5_password"] = str(data["mt5_password"])

    for key in DB_OVERRIDABLE_BOOL_FIELDS:
        if key in data:
            to_save[key] = "true" if data[key] else "false"

    for key in DB_OVERRIDABLE_FLOAT_FIELDS:
        if key in data:
            try:
                value = float(data[key])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{key} debe ser un numero"}), 400
            if value <= 0:
                return jsonify({"ok": False, "error": f"{key} debe ser mayor que 0"}), 400
            to_save[key] = str(value)

    for key in DB_OVERRIDABLE_INT_FIELDS:
        if key in data:
            try:
                value = int(data[key])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{key} debe ser un numero entero"}), 400
            if value <= 0:
                return jsonify({"ok": False, "error": f"{key} debe ser mayor que 0"}), 400
            to_save[key] = str(value)

    if not to_save and not env_updates:
        return jsonify({"ok": False, "error": "no se recibio ningun campo valido"}), 400

    db.set_settings(to_save)
    for key, value in env_updates.items():
        _write_env_secret(key, value)
    if env_updates:
        # The next engine process reads .env; update this dashboard instance
        # too so /api/settings immediately reflects configured status.
        settings = replace(settings,
            openrouter_api_key=env_updates.get("OPENROUTER_API_KEY", settings.openrouter_api_key),
            openrouter_supervisor_api_key=env_updates.get("OPENROUTER_SUPERVISOR_API_KEY", settings.openrouter_supervisor_api_key),
            ai_brain_enabled=env_updates.get("AI_BRAIN_ENABLED", str(settings.ai_brain_enabled)).lower() == "true",
            ai_supervisor_enabled=env_updates.get("AI_SUPERVISOR_ENABLED", str(settings.ai_supervisor_enabled)).lower() == "true")
    db.log_event(ts=_now_iso(), level="INFO",
                 message="Configuracion actualizada desde el dashboard (secretos guardados sin registrarlos; aplica en el proximo arranque del motor)")
    return jsonify({"ok": True, "message": "Guardado. Se aplica la proxima vez que arranque el motor "
                                            "(boton \"Iniciar motor\" arriba)."})


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    """Read-only MT5 historical test; never calls an order endpoint.

    Default mode uses completed OHLC bars. With tick_mode=true it additionally
    downloads MT5 bid/ask ticks and uses their intrabar path.
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        bars = min(max(int(data.get("bars", 3000)), 100), 200000)
        balance = float(data.get("balance", 50))
        leverage = int(data.get("leverage", 500))
        risk_usd = float(data.get("risk_usd", settings.risk_per_trade_usd))
        # 0.45 = spread real medido contra el bridge MT5 en vivo (GET
        # /price/XAUUSD, Ronda 48, 2026-07-25) - reemplaza el 0.25 usado sin
        # verificar desde el inicio de la Fase 2 (Ronda 13+), que subestimaba
        # el costo real de cada trade a casi la mitad. Es una foto de un
        # momento (el spread real varia con sesion/liquidez), no una
        # constante fisica - el campo del dashboard sigue siendo editable.
        spread = float(data.get("spread", 0.45))
        max_hold = max(int(data.get("max_hold_bars", 0)), 0)
        tick_mode = bool(data.get("tick_mode", False))
        backtest_timeframe = str(data.get("timeframe") or "M1").upper()
        if backtest_timeframe not in {"M1", "M5", "M15", "M30", "H1"}:
            raise ValueError("timeframe no soportado para backtesting")
        date_from = data.get("date_from")
        date_to = data.get("date_to")
        start_ts = end_ts = None
        if date_from or date_to:
            if not (date_from and date_to):
                raise ValueError("date_from y date_to deben enviarse juntos")
            start_ts = int(datetime.fromisoformat(str(date_from).replace("Z", "+00:00")).timestamp())
            end_ts = int(datetime.fromisoformat(str(date_to).replace("Z", "+00:00")).timestamp())
            if end_ts <= start_ts:
                raise ValueError("date_to debe ser posterior a date_from")
        if balance <= 0 or leverage <= 0 or risk_usd <= 0 or spread < 0:
            raise ValueError("balance, leverage y riesgo deben ser positivos; spread no puede ser negativo")
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    effective = apply_db_overrides(settings, db.get_all_settings())
    if not effective.mt5_login or not effective.mt5_password:
        return jsonify({"ok": False, "error": "Configura login y password MT5 en Settings antes de probar."}), 409
    global Mt5BridgeClient, run_backtest
    if Mt5BridgeClient is None:
        from core.mt5_bridge_client import Mt5BridgeClient as _Mt5BridgeClient
        Mt5BridgeClient = _Mt5BridgeClient
    if run_backtest is None:
        from core.backtest import run_backtest as _run_backtest
        run_backtest = _run_backtest
    client = Mt5BridgeClient(effective.bridge_url, effective.bridge_timeout_ms,
                             auth_token=effective.bridge_auth_token, max_retries=1)
    symbol = str(data.get("symbol") or effective.symbol)
    try:
        client.login(effective.mt5_login, effective.mt5_password, effective.mt5_server)
        spec = replace(client.symbol_spec(symbol), margin_initial=None)
        if start_ts is not None:
            # MT5 terminals commonly reject very large copy_rates_range
            # requests even when the date span is valid. Chunking by day
            # keeps each response bounded and preserves the complete period.
            candle_parts = []
            cursor = start_ts
            while cursor < end_ts and sum(len(p) for p in candle_parts) < bars:
                chunk_end = min(cursor + 24 * 3600, end_ts)
                part = client.candles(symbol, backtest_timeframe, 5000, start=cursor, end=chunk_end)
                if not part.empty:
                    candle_parts.append(part)
                cursor = chunk_end
            import pandas as pd
            candles = pd.concat(candle_parts, ignore_index=True) if candle_parts else pd.DataFrame()
            if not candles.empty:
                candles = candles.drop_duplicates(subset=["time"]).sort_values("time").tail(bars).reset_index(drop=True)
        else:
            candles = client.candles(symbol, backtest_timeframe, bars)
        if len(candles) < 100:
            return jsonify({"ok": False, "error": f"MT5 devolvio solo {len(candles)} velas; se necesitan al menos 100."}), 502
        ticks = None
        if tick_mode:
            tick_start = int(candles.iloc[0]["time"])
            tick_end = int(candles.iloc[-1]["time"]) + 60
            # MT5 brokers can return millions of ticks for two months. Query
            # six-hour windows so the bridge never builds one unbounded JSON
            # response; each request remains capped by the MT5 bridge.
            chunks = []
            cursor = tick_start
            while cursor < tick_end:
                chunk_end = min(cursor + 6 * 3600, tick_end)
                part = client.ticks(symbol, count=100000, start=cursor, end=chunk_end)
                if not part.empty:
                    chunks.append(part)
                cursor = chunk_end
            import pandas as pd
            ticks = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
            if ticks.empty or not {"bid", "ask", "time"}.issubset(ticks.columns):
                return jsonify({"ok": False, "error": "MT5 no devolvio ticks bid/ask para ese periodo."}), 502
        from core.signals import build_strategy_from_settings
        tick_size = spec.trade_tick_size or spec.point
        value_per_point = spec.trade_tick_value / tick_size if tick_size else 0.0
        strategy = build_strategy_from_settings(effective, value_per_point)
        result = run_backtest(candles=candles, spec=spec, starting_balance=balance,
            leverage=leverage, risk_per_trade_usd=risk_usd, min_tp_usd=effective.min_tp_usd,
            tp_levels=effective.tp_levels, assumed_spread_price=spread,
            max_trades_per_day=effective.max_trades_per_day, max_hold_bars=max_hold, ticks=ticks,
            strategy=strategy, precompute_indicators=True)
    except Exception as exc:
        db.log_event(ts=_now_iso(), level="WARN", message=f"Backtest MT5 no ejecutado: {type(exc).__name__}")
        return jsonify({"ok": False, "error": f"No se pudo completar el backtest MT5: {exc}"}), 502

    return jsonify({"ok": True, "symbol": symbol, "timeframe": backtest_timeframe, "bars": len(candles),
        "fill_model": ("ticks MT5 bid/ask intrabar" if tick_mode else
                       "OHLC conservador (stop antes que TP si ambos tocan la misma vela)"),
        "tick_to_tick": bool(tick_mode),
        "warning": ("Ticks reales bid/ask de MT5; la señal sigue evaluándose al cierre de cada vela M1."
                    if tick_mode else "No es tick-by-tick: se usaron velas M1 cerradas y OHLC conservador."),
        "trades": int(result.trades), "wins": int(result.wins), "losses": int(result.losses),
        "win_rate": float(result.win_rate), "total_pnl": float(result.total_pnl),
        "max_drawdown_pct": float(result.max_drawdown_pct), "final_balance": float(result.final_balance),
        "starting_balance": balance, "leverage": leverage})


@app.route("/api/bot/pause", methods=["POST"])
def api_bot_pause():
    """Sets the flag the running engine already polls for every step (see
    core/engine.py) - does not touch run.sh's process supervision and does
    not force-close any open position, unlike the emergency kill switch."""
    flag = Path(settings.pause_flag_path)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    db.log_event(ts=_now_iso(), level="WARN", message="Motor pausado manualmente desde el dashboard")
    return jsonify({"ok": True, "paused": True})


@app.route("/api/bot/resume", methods=["POST"])
def api_bot_resume():
    flag = Path(settings.pause_flag_path)
    flag.unlink(missing_ok=True)
    db.log_event(ts=_now_iso(), level="INFO", message="Motor reanudado manualmente desde el dashboard")
    return jsonify({"ok": True, "paused": False})


@app.route("/api/engine/start", methods=["POST"])
def api_engine_start():
    """Spawns core/engine_supervisor.py detached (start_new_session=True:
    survives this dashboard process restarting or exiting) - that module,
    not this route, owns restart-on-crash policy for the actual engine.
    409, not 200, if one is already tracked and alive - starting a second
    one would mean two processes both trying to manage the same broker
    account's positions."""
    if _engine_pid() is not None:
        return jsonify({"ok": False, "error": "El motor ya esta corriendo."}), 409

    # On a partial/broken install (pandas/numpy missing from .venv)
    # main.py can't even import: without this check, spawning the
    # supervisor put it into an eternal crash-restart loop while this
    # dashboard reported "Motor corriendo" (the supervisor process itself
    # stays alive). Refuse up front with a clear reason instead. find_spec,
    # not a subprocess probe: this process and main.py share the same
    # venv, so lookup here is authoritative - and subprocess.run would
    # drag Popen into it, which broke under test mocks of Popen for
    # exactly that reason.
    if not _engine_deps_available():
        return jsonify({"ok": False, "error":
            "Faltan pandas/numpy en esta maquina - el motor no puede correr aca. "
            "Corre ./install.sh (o .venv/bin/pip install -r requirements.txt) para instalarlos."}), 409

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "core.engine_supervisor"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    ENGINE_PID_FILE.write_text(str(proc.pid))
    db.log_event(ts=_now_iso(), level="INFO", message="Motor iniciado desde el dashboard")
    return jsonify({"ok": True, "engine_running": True})


@app.route("/api/engine/stop", methods=["POST"])
def api_engine_stop():
    """Sends SIGTERM to the supervisor (not main.py directly) - the
    supervisor catches it, stops its current child gracefully (up to 15s,
    see core/engine_supervisor.py), and exits without restarting. This
    route does NOT block waiting for that to finish: the pidfile is left
    as-is and _engine_pid() self-heals it once the process actually exits,
    so /api/status settles to engine_running=false within its own next
    poll instead of holding this HTTP request open for up to 15s."""
    pid = _engine_pid()
    if pid is None:
        return jsonify({"ok": False, "error": "El motor no esta corriendo."}), 409
    os.kill(pid, signal.SIGTERM)
    db.log_event(ts=_now_iso(), level="WARN", message="Motor detenido desde el dashboard")
    # Deliberately NOT claiming engine_running: false here - the process is
    # still shutting down (up to ~15s) at the moment this responds. The
    # frontend shows a transitional "deteniendo..." state and lets the next
    # /api/status poll confirm the real transition instead of asserting one
    # that may not have happened yet.
    return jsonify({"ok": True, "stopping": True,
                     "message": "Señal de parada enviada - puede tardar unos segundos en cerrar del todo."})


WEB_DEFAULT_PORT = 9000
DASHBOARD_PORT_FILE = RUN_DIR / "dashboard.port"


def _resolve_candidate_dashboard_script(pid: int) -> Path | None:
    """Best-effort resolution of the absolute path of the `dashboard.py`
    script a candidate process is actually running, by parsing its real
    /proc/<pid>/cmdline argv (NUL-separated) for an argument that looks
    like a `dashboard.py` file and resolving it against that process's own
    /proc/<pid>/cwd (a relative argv path is relative to the process's
    cwd, not ours). Returns None on ANY failure (permission denied, pid
    gone by the time we look, malformed/unreadable /proc entries, no
    matching argv, cwd unreadable) - the caller treats None as "can't
    confirm, don't touch it", never as a reason to guess."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    argv = [a for a in raw.decode(errors="replace").split("\x00") if a]
    candidates = [a for a in argv if a.endswith("dashboard.py")]
    if not candidates:
        return None
    script_arg = candidates[0]
    script_path = Path(script_arg)
    try:
        if script_path.is_absolute():
            return script_path.resolve()
        cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
        return (cwd / script_path).resolve()
    except OSError:
        return None


def _reclaim_stale_dashboard_port(host: str, port: int) -> None:
    """If `port` is already bound by another process running THIS SAME
    dashboard.py script (a stale instance left over from a previous
    run/crash/duplicate manual launch of this exact project checkout),
    terminates it and frees the port - so a fresh launch always ends up on
    WEB_DEFAULT_PORT instead of silently drifting to 9001/9002/... forever.
    Never touches a process whose resolved script path doesn't match this
    project's own `Path(__file__).resolve()` - a mere substring match on
    "dashboard.py" somewhere in argv is NOT enough (an unrelated app with
    the same filename, or - concretely - another checkout/worktree of this
    same repo running its own dashboard.py, would match the substring but
    is a DIFFERENT process we must never kill). Anything that isn't
    confirmably this exact file is left completely alone and
    _find_free_port's normal auto-increment fallback takes over instead,
    exactly as it did before this function existed."""
    own_script = Path(__file__).resolve()
    try:
        out = subprocess.run(["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                              capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return  # lsof unavailable or hung - don't guess, fall back to auto-increment
    pids = [p.strip() for p in out.stdout.split() if p.strip()]
    for pid_str in pids:
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid == os.getpid():
            continue  # can't be ourselves, we haven't bound the port yet, but guard anyway
        candidate_script = _resolve_candidate_dashboard_script(pid)
        if candidate_script is None or candidate_script != own_script:
            continue  # not confirmably THIS project's own dashboard.py - never kill it
        print(f"\033[1;33m[dashboard]\033[0m Puerto {port} ocupado por otra instancia de "
              f"dashboard.py (PID {pid}) - liberándolo...", flush=True)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        for _ in range(30):  # up to ~3s grace period
            time.sleep(0.1)
            if not Path(f"/proc/{pid}").exists():
                break
        else:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def _find_free_port(host: str, start_port: int, max_tries: int = 200) -> int:
    """Probes real bind()s starting at start_port, returning the first that
    succeeds - not a guess based on "is something answering on it" (a
    filtered/closed-but-reserved port would look free to a connect probe and
    still fail to bind). This is what actually stops the crash-restart loop
    run.sh's supervise() used to hit: previously a busy start_port made
    Flask's app.run() raise "Address already in use" and exit 1 immediately,
    over and over, every retry picking the exact same busy port again."""
    import socket

    port = start_port
    for _ in range(max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
                return port
            except OSError:
                port += 1
    raise OSError(f"No se encontro un puerto libre entre {start_port} y {port - 1} en {host}")


def run_server(host: str, port: int) -> None:
    app.run(host=host, port=port, debug=False, use_reloader=False)


def _run_web(host: str, port: int) -> None:
    """Runs the browser dashboard for checking it from a browser on another device (phone,
    another computer on the same network) or on a headless machine with
    no GUI toolkit at all. No route can open/close a trade, but account
    data is real, and the Settings/pause-resume routes can change stored
    broker credentials or trading state - 0.0.0.0 exposes all of that to
    anyone on the network, mitigated by DASHBOARD_AUTH_TOKEN if set."""
    requested_port = port
    print("\033[1;36m[dashboard]\033[0m Preparando dashboard web", flush=True)
    for label in ("cargando configuración", "comprobando rutas", "reservando puerto"):
        print(f"\033[2m  ▸ {label}...\033[0m", flush=True)
    _reclaim_stale_dashboard_port(host, port)
    port = _find_free_port(host, port)
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        DASHBOARD_PORT_FILE.write_text(str(port))
    except OSError:
        pass  # best-effort: run.sh falls back to its own default if this can't be written
    if port != requested_port:
        print(f"AVISO: el puerto {requested_port} estaba ocupado - usando {port} en su lugar.")

    extra = f" (o http://<tu-ip-local>:{port} desde otro dispositivo)" if host == "0.0.0.0" else ""
    print(f"Dashboard web en: http://{host}:{port}{extra}")
    if host == "0.0.0.0":
        print("AVISO: 0.0.0.0 expone el dashboard a toda tu red local: datos reales de la cuenta "
              "(solo lectura) y las rutas de Settings/pausar-reanudar (pueden cambiar la cuenta/"
              "password guardada o pausar el bot). Configura DASHBOARD_AUTH_TOKEN en .env antes de "
              "usar 0.0.0.0 si no confias en todos los dispositivos de tu red, o usa --host "
              "127.0.0.1 (default) para acceso solo desde esta maquina.")
        if not settings.dashboard_auth_token:
            print("AVISO: DASHBOARD_AUTH_TOKEN no esta configurado - las rutas de Settings y "
                  "pausar/reanudar no piden ninguna credencial ahora mismo.")
    print("Ctrl+C para detener.")
    run_server(host, port)


def main() -> None:
    signal.signal(signal.SIGCHLD, _reap_children)
    parser = argparse.ArgumentParser(description="XAUUSD scalper - dashboard")
    parser.add_argument("--web", action="store_true", help="Compatibilidad: el dashboard siempre es web.")
    parser.add_argument("--host", default="127.0.0.1",
                         help="Direccion donde escucha el dashboard (default 127.0.0.1, solo esta "
                              "maquina). 0.0.0.0 expone el dashboard a tu red local.")
    parser.add_argument("--port", type=int, default=WEB_DEFAULT_PORT,
                         help=f"Puerto del dashboard web (default {WEB_DEFAULT_PORT}).")
    args = parser.parse_args()
    _run_web(args.host, args.port)


if __name__ == "__main__":
    main()
