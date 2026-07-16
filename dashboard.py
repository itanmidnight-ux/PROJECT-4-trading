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
and trade history - all read live from the same SQLite database the
trading engine writes to (data/trades.db by default). Safe to run at any
time, independently of whether the engine is running. Every route here is
read-only - there is no way to open/close a trade from the dashboard.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask import Flask, jsonify, send_from_directory  # noqa: E402

from core.config import load_settings  # noqa: E402
from core.database import Database  # noqa: E402

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "dashboard"

settings = load_settings()
db = Database(settings.db_path)

app = Flask(__name__, static_folder=None)


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}  # sqlite3.Row isn't a dict; .keys() is required here


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
    return jsonify({
        "mode": "LIVE" if not settings.dry_run else "DRY_RUN",
        "connected": connected,
        "login": settings.mt5_login or "sin configurar",
        "server": settings.mt5_server,
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
    no GUI toolkit at all. Every route is read-only (no order execution
    is possible from here), but account balance/trade history are still
    real account data - 0.0.0.0 exposes them to anyone on the network."""
    extra = f" (o http://<tu-ip-local>:{port} desde otro dispositivo)" if host == "0.0.0.0" else ""
    print(f"Dashboard web en: http://{host}:{port}{extra}")
    if host == "0.0.0.0":
        print("AVISO: 0.0.0.0 expone el dashboard a toda tu red local (solo lectura - balance, "
              "trades, eventos - ninguna ruta puede abrir/cerrar operaciones, pero siguen siendo "
              "datos reales de la cuenta). Usa --host 127.0.0.1 (default) para acceso solo desde "
              "esta maquina.")
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
