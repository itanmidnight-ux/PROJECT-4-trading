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
_expected_token: str | None = None  # set from --token; None means auth is disabled
_symbol_cache: dict[str, str] = {}  # requested name -> broker's actual name (see _resolve_symbol)

logger = logging.getLogger("mt5_bridge")

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
}

# Where install.sh puts the terminal inside the Wine prefix, as seen from
# the WINDOWS side (this process runs under Wine's python.exe). Passed to
# mt5.initialize() as a fallback when the no-args form can't find/launch
# the terminal on its own - a common Wine-specific failure mode.
TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

# Human translations for the order_send retcodes that actually happen in
# practice - a raw "order_send failed: 10019" in the dashboard's event log
# tells the operator nothing actionable; these do.
RETCODE_HINTS = {
    10004: "requote (el precio se movio) - reintentable",
    10006: "orden rechazada por el broker",
    10013: "request invalido (parametros malformados)",
    10014: "volumen invalido para este simbolo (revisa volume_min/step del broker)",
    10015: "precio invalido",
    10016: "stops invalidos (SL demasiado cerca del precio para este broker)",
    10017: "trading deshabilitado para esta cuenta",
    10018: "mercado cerrado (fin de semana/feriado o fuera de horario del simbolo)",
    10019: "fondos insuficientes (margen) para este volumen",
    10026: "autotrading deshabilitado por el SERVIDOR del broker",
    10027: "autotrading deshabilitado en el TERMINAL - abrilo en MT5 (boton AutoTrading/Algo Trading)",
    10030: "filling mode no soportado por el simbolo",
    10031: "sin conexion con el servidor de trading",
}


def _retcode_msg(code) -> str:
    hint = RETCODE_HINTS.get(code if isinstance(code, int) else None)
    return f"{code} ({hint})" if hint else str(code)


def _initialize_mt5() -> bool:
    """mt5.initialize() with the no-args form first (finds an already-
    running terminal), then with the explicit install path (launches it if
    needed) - under Wine the no-args discovery is the flakier of the two."""
    if mt5.initialize():
        return True
    logger.warning("mt5.initialize() sin ruta fallo (%s), reintentando con %s",
                    mt5.last_error(), TERMINAL_PATH)
    return bool(mt5.initialize(path=TERMINAL_PATH))


def _resolve_symbol(requested: str):
    """Maps the configured symbol name to whatever THIS broker/account
    actually calls it, or None if nothing matches.

    Why: brokers rename symbols per account type - FBS specifically
    appends suffixes on some account tiers (XAUUSD becomes XAUUSDm on
    Micro, XAUUSDc on Cent, etc.), so a bot configured with the standard
    name gets `symbol_select("XAUUSD") failed` on those accounts and dies
    with no trade ever placed, even though gold IS tradable there under
    its suffixed name. Resolution order: exact name; prefix matches
    (XAUUSD*); containing matches (*XAUUSD*). Among candidates, symbols
    whose trading is disabled are skipped and the SHORTEST name wins (the
    closest real variant, not some exotic cross that merely contains the
    string). Cached per requested name for the life of the process."""
    cached = _symbol_cache.get(requested)
    if cached is not None:
        return cached
    if mt5.symbol_select(requested, True):
        _symbol_cache[requested] = requested
        return requested
    for pattern in (f"{requested}*", f"*{requested}*"):
        candidates = mt5.symbols_get(pattern) or ()
        tradable = [s for s in candidates
                    if getattr(s, "trade_mode", None) != getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0)]
        if not tradable:
            continue
        best = min(tradable, key=lambda s: len(s.name))
        if mt5.symbol_select(best.name, True):
            logger.info("Simbolo %s no existe con ese nombre exacto en este broker/cuenta - "
                         "usando %s (variante del mismo instrumento; tipico de cuentas "
                         "FBS Micro/Cent con sufijo).", requested, best.name)
            _symbol_cache[requested] = best.name
            return best.name
    return None


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


def _pick_filling_mode(symbol: str) -> int:
    """An order's type_filling must match what the SYMBOL actually accepts
    (SYMBOL_FILLING_FOK=1 / SYMBOL_FILLING_IOC=2 bitmask on symbol_info) -
    a mismatch fails EVERY order with retcode 10030 (unsupported filling
    mode), which would silently take the whole bot offline with no way to
    trade at all even though everything else (bridge, risk sizing, signal
    generation) is working correctly. This used to hardcode
    ORDER_FILLING_IOC, which works for many broker/symbol combinations but
    isn't guaranteed - ask the broker what it actually supports instead of
    assuming. IOC preferred when available (matches the deviation/slippage
    tolerance already used for market orders); FOK and RETURN as fallbacks
    in MT5's own documented preference order.

    Not exercised by the test suite - this module only runs under the
    Windows python.exe inside Wine, which is unavailable in this sandbox.
    The bitmask logic itself matches MetaTrader5's documented SYMBOL_FILLING_*
    constants, not something guessed for FBS specifically.
    """
    info = mt5.symbol_info(symbol)
    filling_mask = info.filling_mode if info is not None else 0
    if filling_mask & 2:  # SYMBOL_FILLING_IOC
        return mt5.ORDER_FILLING_IOC
    if filling_mask & 1:  # SYMBOL_FILLING_FOK
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


@app.before_request
def _check_auth():
    """
    Defense in depth: this API can open/close real trades and log in with
    real credentials, and binds to 127.0.0.1 by default - but a shared
    token means a misconfiguration that widens that binding (--host
    0.0.0.0, an unexpected Docker/WSL network path, a future change that
    forgets why the default matters) doesn't also hand out unauthenticated
    trade execution. /health is exempt (pure liveness, no account access)
    so orchestration scripts can poll it without needing the token.
    """
    if _expected_token is None or request.path == "/health":
        return None
    supplied = request.headers.get("X-Bridge-Token")
    if supplied != _expected_token:
        return _err("unauthorized: missing or invalid X-Bridge-Token", 401)
    return None


def _try_reconnect() -> bool:
    """Best-effort silent re-login after a dropped connection, using the
    credentials from the last successful /login call. Returns True if the
    connection is (now) usable."""
    global _connected
    if _last_credentials is None:
        return False
    logger.warning("Connection appears down, attempting silent reconnect...")
    if not _initialize_mt5():
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

    if not _initialize_mt5():
        return _err(f"mt5.initialize() failed: {mt5.last_error()} - el terminal MT5 no arranco. "
                     f"Revisa que exista {TERMINAL_PATH} (corre ./install.sh si no).", 500)
    ok = mt5.login(login_id, password=password, server=server)
    if not ok:
        return _err(f"mt5.login() failed: {mt5.last_error()}. Causas tipicas: password incorrecto "
                     "(el de la cuenta DEMO de FBS expira a los pocos dias - regeneralo en el area "
                     "de cliente), o el nombre de servidor no es exactamente el de tu cuenta "
                     "(ej. FBS-Demo vs FBS-Real; copialo tal cual del mail/area de FBS).", 401)
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
    resolved = _resolve_symbol(symbol)
    if resolved is None:
        return _err(f"el simbolo {symbol} no existe en este broker/cuenta (se probaron tambien "
                     f"variantes {symbol}* y *{symbol}*): {mt5.last_error()}", 404)
    symbol = resolved
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
        "symbol": symbol,  # the broker's ACTUAL name (may differ from the requested one - FBS suffixes)
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
    resolved = _resolve_symbol(symbol)
    if resolved is None:
        return _err(f"el simbolo {symbol} no existe en este broker/cuenta: {mt5.last_error()}", 404)
    tick = mt5.symbol_info_tick(resolved)
    if tick is None:
        return _err(f"symbol_info_tick({resolved}) failed: {mt5.last_error()}", 404)
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
    resolved = _resolve_symbol(symbol)
    if resolved is None:
        return _err(f"el simbolo {symbol} no existe en este broker/cuenta: {mt5.last_error()}", 404)
    symbol = resolved
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
    if symbol:
        # Filter by the broker's REAL name - with an FBS suffix account,
        # positions live under e.g. XAUUSDm, so filtering by the requested
        # XAUUSD would silently return [] and break restart-recovery.
        symbol = _resolve_symbol(symbol) or symbol
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

    resolved = _resolve_symbol(symbol)
    if resolved is None:
        return _err(f"el simbolo {symbol} no existe en este broker/cuenta: {mt5.last_error()}", 404)
    symbol = resolved
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
        "type_filling": _pick_filling_mode(symbol),
    }
    if sl_price:
        request_dict["sl"] = float(sl_price)

    result = mt5.order_send(request_dict)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result else mt5.last_error()
        return _err(f"order_send failed: {_retcode_msg(code)}", 500)

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
        "type_filling": _pick_filling_mode(p.symbol),
    }
    result = mt5.order_send(request_dict)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result else mt5.last_error()
        return _err(f"order_send (close) failed: {_retcode_msg(code)}", 500)

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
        return _err(f"order_send (modify) failed: {_retcode_msg(code)}", 500)
    return jsonify({"ok": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--token", default="", help="Shared secret required on every request "
                         "(except /health) via the X-Bridge-Token header. Passed by run.sh from "
                         "BRIDGE_AUTH_TOKEN in .env; leave unset only for local manual debugging.")
    args = parser.parse_args()
    _setup_logging()
    if args.token:
        _expected_token = args.token
    else:
        logger.warning(
            "No --token supplied: this bridge is running WITHOUT authentication. Fine for "
            "manual debugging on 127.0.0.1, but install.sh normally generates BRIDGE_AUTH_TOKEN "
            "and run.sh passes it automatically - if you're seeing this from run.sh, check .env."
        )
    logger.info("Starting MT5 bridge on %s:%s (auth %s)", args.host, args.port,
                "enabled" if _expected_token else "DISABLED")
    app.run(host=args.host, port=args.port, threaded=True)
