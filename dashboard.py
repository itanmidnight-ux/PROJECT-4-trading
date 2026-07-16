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

Two routes are NOT read-only: the Settings tab's save (POST /api/settings)
and the pause/resume toggle (POST /api/bot/pause, /api/bot/resume). No
route can open/close a trade or send anything to the broker - saved
settings only take effect the NEXT time the engine process starts (see
core/config.py::apply_db_overrides), and pause/resume only flips a flag
file the running engine already polls for on its own. Both require
X-Dashboard-Token when DASHBOARD_AUTH_TOKEN is set - see _check_dashboard_auth.
"""
from __future__ import annotations

import argparse
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

settings = load_settings()
db = Database(settings.db_path)

app = Flask(__name__, static_folder=None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    return jsonify({"ok": True, "message": "Guardado. Se aplica la proxima vez que arranque el motor (./run.sh)."})


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
