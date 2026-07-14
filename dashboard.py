"""
dashboard.py - native desktop dashboard for the XAUUSD scalper.

Run with:  .venv/bin/python dashboard.py

Opens a native window (via pywebview) showing account stats, equity
curve, win/loss breakdown, P&L by day/month, and trade history - all
read live from the same SQLite database the trading engine writes to
(data/trades.db by default). Safe to run at any time, independently of
whether the engine is running.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import webview  # noqa: E402
from flask import Flask, jsonify, send_from_directory  # noqa: E402

from core.config import load_settings  # noqa: E402
from core.database import Database  # noqa: E402

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "dashboard"

settings = load_settings()
db = Database(settings.db_path)

app = Flask(__name__, static_folder=None)


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


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


def run_server():
    app.run(host="127.0.0.1", port=8765, debug=False, use_reloader=False)


def main() -> None:
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    time.sleep(0.6)

    webview.create_window(
        "XAUUSD Scalper — Dashboard",
        "http://127.0.0.1:8765",
        width=1360,
        height=880,
        min_size=(960, 600),
        background_color="#0b0c0f",
    )
    webview.start()


if __name__ == "__main__":
    main()
