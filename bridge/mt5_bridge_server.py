"""
MT5 bridge server - MUST run under the Windows python.exe inside the Wine
prefix set up by install.sh, NOT under native Linux python. The official
`MetaTrader5` pip package only works when it can load the real Windows
terminal DLLs, which only exist inside the Wine-installed MetaTrader 5
terminal.

This process is the only thing that talks to MetaTrader5 directly. The
Linux-side engine (core/engine.py) talks to this over plain HTTP via
core/mt5_bridge_client.py. That split is what lets the trading logic,
dashboard, and risk manager run in ordinary Linux Python while still
reaching a real MT5 account.

The MetaTrader5 package is NOT thread-safe, so every endpoint that
touches it runs under a single global lock (@synchronized below) even
though Flask serves requests on multiple threads - that keeps /health
responsive while an order or history call is in flight, without ever
letting two mt5.* calls race each other.

Run manually for debugging:
    wine python.exe bridge/mt5_bridge_server.py --port 5001
(run.sh does this for you.)
"""
from __future__ import annotations

import argparse
import functools
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, request

try:
    import MetaTrader5 as mt5
except ImportError:
    print(
        "ERROR: MetaTrader5 package not found. This script must run under "
        "the Wine-side Windows python installed by install.sh, not your "
        "system python.",
        file=sys.stderr,
    )
    raise

app = Flask(__name__)
_lock = threading.RLock()
_connected = False
_last_credentials: dict | None = None  # kept in memory only, to allow auto re-login

logger = logging.getLogger("mt5_bridge")

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
}


def _setup_logging() -> None:
    log_dir = Path(__file__).resolve().parent.parent / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "bridge.log", maxBytes=5_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(console)
    logger.setLevel(logging.INFO)


def synchronized(fn):
    """Serializes access to the MetaTrader5 API across Flask's request threads."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    return wrapper


def _err(message: str, code: int = 400):
    logger.warning("error %s: %s", code, message)
    return jsonify({"ok": False, "error": message}), code


def _try_reconnect() -> bool:
    """Best-effort silent re-login after a dropped connection, using the
    credentials from the last successful /login call. Returns True if the
    connection is (now) usable."""
    global _connected
    if _last_credentials is None:
        return False
    logger.warning("Connection appears down, attempting silent reconnect...")
    if not mt5.initialize():
        logger.error("reconnect: mt5.initialize() failed: %s", mt5.last_error())
        return False
    ok = mt5.login(**_last_credentials)
    _connected = bool(ok)
    if ok:
        logger.info("Reconnected successfully.")
    else:
        logger.error("reconnect: mt5.login() failed: %s", mt5.last_error())
    return _connected


@app.route("/health")
def health():
    return jsonify({"ok": True, "connected": _connected})


@app.route("/login", methods=["POST"])
@synchronized
def login():
    global _connected, _last_credentials
    data = request.get_json(force=True)
    login_id = int(data["login"])
    password = data["password"]
    server = data["server"]

    if not mt5.initialize():
        return _err(f"mt5.initialize() failed: {mt5.last_error()}", 500)
    ok = mt5.login(login_id, password=password, server=server)
    if not ok:
        return _err(f"mt5.login() failed: {mt5.last_error()}", 401)
    _connected = True
    _last_credentials = {"login": login_id, "password": password, "server": server}
    logger.info("Logged in to %s on %s", login_id, server)
    return jsonify({"ok": True})


@app.route("/account")
@synchronized
def account():
    if not _connected and not _try_reconnect():
        return _err("not logged in", 409)
    info = mt5.account_info()
    if info is None and _try_reconnect():
        info = mt5.account_info()
    if info is None:
        return _err(f"account_info() failed: {mt5.last_error()}", 500)
    return jsonify({
        "ok": True,
        "balance": info.balance,
        "equity": info.equity,
        "margin": info.margin,
        "margin_free": info.margin_free,
        "leverage": info.leverage,
        "currency": info.currency,
    })


@app.route("/symbol/<symbol>")
@synchronized
def symbol_info(symbol: str):
    if not mt5.symbol_select(symbol, True):
        return _err(f"symbol_select({symbol}) failed: {mt5.last_error()}", 404)
    info = mt5.symbol_info(symbol)
    if info is None:
        return _err(f"symbol_info({symbol}) not available", 404)

    # Ask MT5 itself what margin a 1.0 lot order would need, rather than
    # deriving it from contract_size/price/account-leverage on the Linux
    # side. Brokers commonly run a DIFFERENT, fixed leverage for metals
    # than whatever the account's forex leverage is set to (FBS does
    # exactly this - metals are pinned to 1:500 regardless of the
    # account's leverage setting, per their own docs) - order_calc_margin
    # is the one call that reflects the broker's actual, current rule
    # instead of an assumption that can be wrong by orders of magnitude.
    margin_initial = None
    tick = mt5.symbol_info_tick(symbol)
    if tick is not None:
        margin = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, symbol, 1.0, tick.ask)
        if margin is not None:
            margin_initial = float(margin)

    return jsonify({
        "ok": True,
        "contract_size": info.trade_contract_size,
        "volume_min": info.volume_min,
        "volume_max": info.volume_max,
        "volume_step": info.volume_step,
        "point": info.point,
        "digits": info.digits,
        "trade_tick_value": info.trade_tick_value,
        "trade_tick_size": info.trade_tick_size,
        "spread": info.spread,
        "margin_initial": margin_initial,
    })


@app.route("/price/<symbol>")
@synchronized
def price(symbol: str):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return _err(f"symbol_info_tick({symbol}) failed: {mt5.last_error()}", 404)
    return jsonify({
        "ok": True,
        "bid": tick.bid,
        "ask": tick.ask,
        "spread_price": round(tick.ask - tick.bid, 8),
        "time": tick.time,
    })


@app.route("/candles/<symbol>")
@synchronized
def candles(symbol: str):
    tf = request.args.get("timeframe", "M1")
    count = int(request.args.get("count", 200))
    mt5_tf = TIMEFRAME_MAP.get(tf)
    if mt5_tf is None:
        return _err(f"unsupported timeframe {tf}")
    rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, count)
    if rates is None:
        return _err(f"copy_rates_from_pos failed: {mt5.last_error()}", 500)
    out = [
        {
            "time": int(r["time"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "tick_volume": int(r["tick_volume"]),
        }
        for r in rates
    ]
    return jsonify({"ok": True, "candles": out})


@app.route("/positions")
@synchronized
def positions():
    symbol = request.args.get("symbol")
    pos = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    if pos is None:
        pos = []
    return jsonify({
        "ok": True,
        "positions": [
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                "volume": p.volume,
                "price_open": p.price_open,
                "price_current": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
            }
            for p in pos
        ],
    })


@app.route("/order/open", methods=["POST"])
@synchronized
def order_open():
    data = request.get_json(force=True)
    symbol = data["symbol"]
    side = data["side"]  # BUY / SELL
    lot = float(data["lot"])
    sl_price = data.get("sl_price")

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return _err(f"no tick for {symbol}", 404)

    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    price = tick.ask if side == "BUY" else tick.bid

    request_dict = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "deviation": 20,
        "magic": 990321,
        "comment": "xauusd-scalper",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    if sl_price:
        request_dict["sl"] = float(sl_price)

    result = mt5.order_send(request_dict)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result else mt5.last_error()
        return _err(f"order_send failed: {code}", 500)

    logger.info("OPEN %s %.4f %s @ %.3f", side, lot, symbol, result.price)
    return jsonify({
        "ok": True,
        "ticket": result.order,
        "price": result.price,
        "volume": result.volume,
    })


@app.route("/order/close", methods=["POST"])
@synchronized
def order_close():
    """Closes `lot` volume of an existing position (partial close support
    for the multi-scale take-profit ladder)."""
    data = request.get_json(force=True)
    ticket = int(data["ticket"])
    lot = float(data["lot"])

    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return _err(f"position {ticket} not found", 404)
    p = pos[0]

    close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(p.symbol)
    if tick is None:
        return _err(f"no tick for {p.symbol}", 404)
    price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

    request_dict = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": p.symbol,
        "volume": min(lot, p.volume),
        "type": close_type,
        "position": ticket,
        "price": price,
        "deviation": 20,
        "magic": 990321,
        "comment": "xauusd-scalper-tp",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request_dict)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result else mt5.last_error()
        return _err(f"order_send (close) failed: {code}", 500)

    logger.info("CLOSE %.4f of ticket %s @ %.3f", result.volume, ticket, result.price)
    return jsonify({
        "ok": True,
        "price": result.price,
        "volume": result.volume,
    })


@app.route("/order/modify", methods=["POST"])
@synchronized
def order_modify():
    """Move SL (used to push a position to breakeven after TP1 hits)."""
    data = request.get_json(force=True)
    ticket = int(data["ticket"])
    sl_price = float(data["sl_price"])

    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return _err(f"position {ticket} not found", 404)
    p = pos[0]

    request_dict = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": p.symbol,
        "position": ticket,
        "sl": sl_price,
        "tp": p.tp,
    }
    result = mt5.order_send(request_dict)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result else mt5.last_error()
        return _err(f"order_send (modify) failed: {code}", 500)
    return jsonify({"ok": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    _setup_logging()
    logger.info("Starting MT5 bridge on %s:%s", args.host, args.port)
    app.run(host=args.host, port=args.port, threaded=True)
