"""OANDA v20 REST adapter: broker + market data WITHOUT MetaTrader 5.

Why this exists: the MT5 route requires the real MT5 terminal running
somewhere (under Wine on a Kali/Ubuntu machine - see bridge/). OANDA's
v20 API is plain authenticated HTTPS + JSON, so this adapter runs
identically on Termux, Kali, and Ubuntu with nothing but `requests` -
no terminal, no Wine, no bridge process, no extra machine. Select it
with BROKER_KIND=oanda in .env (plus OANDA_API_TOKEN/OANDA_ACCOUNT_ID
from a free practice account at oanda.com).

Honesty note on "any broker": there is no universal protocol every
broker speaks. What this project offers is a clean BrokerExecutor/
MarketDataSource seam (core/broker.py, core/market_data.py) with two
real implementations - the MT5 bridge (covers every MT5 broker, FBS
included, which has NO public REST API) and this direct OANDA adapter
(no MT5 anywhere). Adding another REST/websocket broker means writing
one more adapter against the same seam, not touching the engine.

Design rules carried over from the MT5 client (core/mt5_bridge_client.py):
  - READ calls (price/candles/account/positions) retry network failures
    with backoff - they're idempotent.
  - MUTATING calls (open/close/modify) do NOT retry on network failures:
    a timed-out order may have executed anyway, and re-sending it could
    place a real duplicate. The exception propagates and core/engine.py's
    reconcile logic decides what actually happened.
  - One requests.Session for the client's lifetime (connection reuse).
  - Split (connect, read) timeouts: dead-host detection stays fast
    without cutting legitimately slow responses short.

Unit semantics: OANDA sizes XAU_USD in UNITS (1 unit = 1 oz), not MT5
lots (1.0 lot = 100 oz). The engine's own math is unit-agnostic - it
only multiplies volume by value_per_point_per_lot = trade_tick_value /
point, which this adapter reports as 1.0 USD per $1 move per unit - so
1 OANDA unit behaves exactly like 0.01 MT5 lots of gold. RiskManager's
minimum-volume risk cap applies unchanged.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests

from core.broker import BrokerExecutor, BrokerPosition, OpenResult
from core.mt5_bridge_client import Tick
from core.risk_manager import AccountState, SymbolSpec

logger = logging.getLogger("oanda")

_BASE_URLS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}


class OandaError(RuntimeError):
    pass


def to_oanda_instrument(symbol: str) -> str:
    """XAUUSD -> XAU_USD, EURUSD -> EUR_USD; anything already containing
    '_' is passed through untouched (lets .env carry a native OANDA name)."""
    if "_" in symbol:
        return symbol
    if len(symbol) == 6:
        return f"{symbol[:3]}_{symbol[3:]}"
    return symbol


def _rfc3339_to_epoch(ts: str) -> int:
    # OANDA timestamps look like 2026-07-18T14:03:00.000000000Z - trim the
    # sub-second part (variable width) and parse the rest.
    return int(datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp())


class OandaClient(BrokerExecutor):
    """Both the broker executor AND the price/candle source (exposes the
    same price()/candles() shape as Mt5BridgeClient, so BridgeMarketData's
    incremental candle cache wraps this client unchanged)."""

    def __init__(self, api_token: str, account_id: str, environment: str = "practice",
                 timeout_ms: int = 8000, max_retries: int = 3) -> None:
        if environment not in _BASE_URLS:
            raise OandaError(f"OANDA_ENV invalido: {environment!r} (usa 'practice' o 'live')")
        self.base_url = _BASE_URLS[environment]
        self.account_id = account_id
        read_timeout = timeout_ms / 1000
        self.timeout = (min(5.0, read_timeout), read_timeout)
        self.max_retries = max_retries
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        })
        # displayPrecision per instrument (XAU_USD: 3) - OANDA rejects SL
        # prices with the wrong number of decimals, so it's fetched (and
        # cached) from the instrument details before formatting any price.
        self._price_precision: dict[str, int] = {}

    # ------------------------------------------------------------ transport
    def _request(self, method: str, path: str, *, json_body: Optional[dict] = None,
                 params: Optional[dict] = None, retry_network: bool = True) -> dict:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._session.request(method, url, json=json_body, params=params,
                                              timeout=self.timeout)
            except requests.RequestException as exc:
                if not retry_network:
                    raise OandaError(
                        f"fallo de red en una llamada que muta estado ({exc}) - NO se reintenta "
                        f"automaticamente porque la orden pudo haber llegado igual a OANDA; "
                        f"el motor reconciliara contra las posiciones reales."
                    ) from exc
                last_exc = exc
                wait = min(2 ** attempt, 8)
                logger.warning("OANDA unreachable (%s), retrying in %ss...", exc, wait)
                time.sleep(wait)
                continue

            if resp.status_code >= 400:
                # A clean HTTP error is NOT ambiguous - OANDA rejected the
                # request, nothing executed - but it isn't retryable either
                # (same request would fail the same way), so raise with the
                # API's own message for the log/dashboard.
                try:
                    detail = resp.json().get("errorMessage", resp.text[:200])
                except ValueError:
                    detail = resp.text[:200]
                raise OandaError(f"OANDA {resp.status_code} en {method} {path}: {detail}")
            return resp.json()

        raise OandaError(f"OANDA unreachable after {self.max_retries} attempts: {last_exc}")

    def _get(self, path: str, **params) -> dict:
        return self._request("GET", path, params=params or None)

    # ----------------------------------------------------------- market data
    def price(self, symbol: str) -> Tick:
        inst = to_oanda_instrument(symbol)
        d = self._get(f"/v3/accounts/{self.account_id}/pricing", instruments=inst)
        prices = d.get("prices") or []
        if not prices:
            raise OandaError(f"OANDA no devolvio precio para {inst}")
        p = prices[0]
        bid = float(p["bids"][0]["price"])
        ask = float(p["asks"][0]["price"])
        return Tick(bid=bid, ask=ask, spread_price=round(ask - bid, 6),
                    time=_rfc3339_to_epoch(p["time"]))

    def candles(self, symbol: str, timeframe: str = "M1", count: int = 200) -> pd.DataFrame:
        inst = to_oanda_instrument(symbol)
        # OANDA granularities use the same M1/M5/M15/H1 names as this
        # project's TIMEFRAME setting - passed through directly.
        d = self._get(f"/v3/instruments/{inst}/candles",
                       granularity=timeframe, count=count, price="M")
        rows = [
            {"time": _rfc3339_to_epoch(c["time"]),
             "open": float(c["mid"]["o"]), "high": float(c["mid"]["h"]),
             "low": float(c["mid"]["l"]), "close": float(c["mid"]["c"]),
             "tick_volume": int(c.get("volume", 0))}
            for c in d.get("candles", [])
        ]
        return pd.DataFrame(rows)

    # -------------------------------------------------------------- account
    def account(self) -> AccountState:
        d = self._get(f"/v3/accounts/{self.account_id}/summary")["account"]
        margin_rate = float(d.get("marginRate") or 1.0)
        leverage = max(1, int(round(1.0 / margin_rate))) if margin_rate > 0 else 1
        return AccountState(
            balance=float(d["balance"]),
            equity=float(d["NAV"]),
            free_margin=float(d["marginAvailable"]),
            leverage=leverage,
        )

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        inst_name = to_oanda_instrument(symbol)
        d = self._get(f"/v3/accounts/{self.account_id}/instruments", instruments=inst_name)
        instruments = d.get("instruments") or []
        if not instruments:
            raise OandaError(f"la cuenta OANDA no tiene el instrumento {inst_name}")
        inst = instruments[0]
        self._price_precision[inst_name] = int(inst.get("displayPrecision", 3))
        point = 10 ** int(inst.get("pipLocation", -2))
        step = 10 ** -int(inst.get("tradeUnitsPrecision", 0))
        return SymbolSpec(
            # 1 unit of XAU_USD = 1 oz: P&L = units x price move, so value
            # per price-unit per volume-unit must come out as 1.0 - hence
            # contract_size=1 and trade_tick_value=point (see module doc).
            contract_size=1.0,
            volume_min=float(inst.get("minimumTradeSize", 1)),
            volume_max=float(inst.get("maximumOrderUnits", 100)),
            volume_step=step,
            point=point,
            trade_tick_value=point,
            margin_initial=None,  # derived from account leverage + price by RiskManager
        )

    def _fmt_price(self, symbol: str, price: float) -> str:
        precision = self._price_precision.get(to_oanda_instrument(symbol), 3)
        return f"{price:.{precision}f}"

    # -------------------------------------------------------------- trading
    def open_order(self, symbol: str, side: str, lot: float, sl_price: float,
                   fill_price: float) -> OpenResult:
        inst = to_oanda_instrument(symbol)
        units = lot if side == "BUY" else -lot
        body = {"order": {
            "type": "MARKET",
            "instrument": inst,
            "units": f"{units:.10g}",
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": self._fmt_price(symbol, sl_price),
                                "timeInForce": "GTC"},
        }}
        d = self._request("POST", f"/v3/accounts/{self.account_id}/orders",
                           json_body=body, retry_network=False)
        fill = d.get("orderFillTransaction")
        if not fill or "tradeOpened" not in fill:
            cancel = d.get("orderCancelTransaction") or {}
            raise OandaError(f"OANDA no ejecuto la orden: {cancel.get('reason', 'sin orderFillTransaction')}")
        return OpenResult(ticket=str(fill["tradeOpened"]["tradeID"]),
                           fill_price=float(fill["price"]))

    def close_partial(self, ticket: str, lot: float, fill_price: float) -> float:
        d = self._request("PUT", f"/v3/accounts/{self.account_id}/trades/{ticket}/close",
                           json_body={"units": f"{lot:.10g}"}, retry_network=False)
        fill = d.get("orderFillTransaction") or {}
        return float(fill.get("pl", 0.0))

    def modify_sl(self, ticket: str, sl_price: float) -> None:
        # Symbol isn't threaded through the BrokerExecutor interface here;
        # use the cached precision of the only instrument this engine
        # trades (there's exactly one entry after symbol_spec()).
        precision = next(iter(self._price_precision.values()), 3)
        body = {"stopLoss": {"price": f"{sl_price:.{precision}f}", "timeInForce": "GTC"}}
        self._request("PUT", f"/v3/accounts/{self.account_id}/trades/{ticket}/orders",
                       json_body=body, retry_network=False)

    def open_positions(self, symbol: str) -> list[BrokerPosition]:
        inst = to_oanda_instrument(symbol)
        d = self._get(f"/v3/accounts/{self.account_id}/openTrades")
        out: list[BrokerPosition] = []
        for t in d.get("trades", []):
            if t.get("instrument") != inst:
                continue
            units = float(t["currentUnits"])
            sl_order = t.get("stopLossOrder") or {}
            out.append(BrokerPosition(
                ticket=str(t["id"]),
                side="BUY" if units > 0 else "SELL",
                volume=abs(units),
                price_open=float(t["price"]),
                sl=float(sl_order["price"]) if sl_order.get("price") else None,
            ))
        return out
