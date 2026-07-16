"""
dashboard.py - dashboard for the XAUUSD scalper, as either a native
desktop window or a plain web server.

Run with:  .venv/bin/python dashboard.py
Options are prompted interactively if run from a terminal with no flags;
non-interactively (e.g. from a script) it defaults to the native window,
matching this file's original behavior. Flags skip the prompt:
    .venv/bin/python dashboard.py --native   # force native window
    .venv/bin/python dashboard.py --web      # web server on :9000

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
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

from core.config import (  # noqa: E402
    DB_OVERRIDABLE_BOOL_FIELDS,
    DB_OVERRIDABLE_FLOAT_FIELDS,
    DB_OVERRIDABLE_INT_FIELDS,
    apply_db_overrides,
    load_settings,
)
from core.database import Database  # noqa: E402

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

    if not to_save:
        return jsonify({"ok": False, "error": "no se recibio ningun campo valido"}), 400

    db.set_settings(to_save)
    db.log_event(ts=_now_iso(), level="INFO",
                 message="Configuracion actualizada desde el dashboard (aplica en el proximo arranque del motor)")
    return jsonify({"ok": True, "message": "Guardado. Se aplica la proxima vez que arranque el motor "
                                            "(boton \"Iniciar motor\" arriba)."})


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


def run_server(host: str, port: int) -> None:
    app.run(host=host, port=port, debug=False, use_reloader=False)


def _run_native() -> None:
    """Original behavior: the Flask server only ever listens on
    127.0.0.1 internally, and a native pywebview window points at that
    local address - nothing here is reachable from outside this machine.
    pywebview is imported here, not at module load, so --web mode still
    works on a machine with no GUI toolkit installed at all (e.g. a
    headless server) - it would previously have failed to even start in
    that case, regardless of which mode was wanted."""
    import webview

    internal_port = 8765
    thread = threading.Thread(target=run_server, args=("127.0.0.1", internal_port), daemon=True)
    thread.start()
    time.sleep(0.6)

    webview.create_window(
        "XAUUSD Scalper — Dashboard",
        f"http://127.0.0.1:{internal_port}",
        width=1360,
        height=880,
        min_size=(960, 600),
        background_color="#0b0c0f",
    )
    webview.start()


def _run_web(host: str, port: int) -> None:
    """Runs the same dashboard as a plain web server instead of a native
    window - for checking it from a browser on another device (phone,
    another computer on the same network) or on a headless machine with
    no GUI toolkit at all. No route can open/close a trade, but account
    data is real, and the Settings/pause-resume routes can change stored
    broker credentials or trading state - 0.0.0.0 exposes all of that to
    anyone on the network, mitigated by DASHBOARD_AUTH_TOKEN if set."""
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


def _prompt_mode() -> str:
    print("Como queres abrir el dashboard?")
    print("  1) Ventana nativa de escritorio (default)")
    print(f"  2) Dashboard web (http://127.0.0.1:{WEB_DEFAULT_PORT}, se abre en el navegador)")
    try:
        choice = input("Elegi 1 o 2 [1]: ").strip()
    except EOFError:
        return "native"
    return "web" if choice == "2" else "native"


def main() -> None:
    signal.signal(signal.SIGCHLD, _reap_children)
    parser = argparse.ArgumentParser(description="XAUUSD scalper - dashboard")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--web", action="store_true",
                             help="Corre como dashboard web en vez de ventana nativa.")
    mode_group.add_argument("--native", action="store_true",
                             help="Fuerza ventana nativa (salta el prompt interactivo).")
    parser.add_argument("--host", default="127.0.0.1",
                         help="Direccion donde escucha --web (default 127.0.0.1, solo esta "
                              "maquina). 0.0.0.0 expone el dashboard a tu red local.")
    parser.add_argument("--port", type=int, default=WEB_DEFAULT_PORT,
                         help=f"Puerto para --web (default {WEB_DEFAULT_PORT}).")
    args = parser.parse_args()

    if args.web:
        mode = "web"
    elif args.native:
        mode = "native"
    elif sys.stdin.isatty():
        mode = _prompt_mode()
    else:
        mode = "native"  # non-interactive, no flags: keep this file's original default behavior

    if mode == "web":
        _run_web(args.host, args.port)
    else:
        _run_native()


if __name__ == "__main__":
    main()
