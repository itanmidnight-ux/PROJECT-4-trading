"""HTTP client for the Wine-side MT5 bridge (see bridge/mt5_bridge_server.py).
This is the only way the Linux-side engine talks to the broker.

Resilient to two real-world failure modes: the bridge process being
mid-restart (connection refused - retried with backoff) and the bridge
having lost its MT5 session (reports "not logged in" - the client
transparently re-sends the last known credentials and retries once).

One deliberate asymmetry: READ calls (price, candles, account, positions)
retry network failures with backoff, but MUTATING calls (open/close/
modify) do NOT - see _call's docstring for why (a timed-out order may
have executed anyway; re-sending it blindly can place a real duplicate)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import requests

from core.risk_manager import AccountState, SymbolSpec

logger = logging.getLogger("bridge_client")


class BridgeError(RuntimeError):
    pass


@dataclass
class Tick:
    bid: float
    ask: float
    spread_price: float
    time: int


class Mt5BridgeClient:
    def __init__(self, base_url: str, timeout_ms: int = 8000, max_retries: int = 3,
                 auth_token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        # (connect, read) instead of one number: over a network link to a
        # remote bridge, a DEAD bridge should be detected
        # by the connect phase in a few seconds, while a bridge that is
        # merely SLOW to answer (Wine under load) still gets the full
        # configured window to reply. One combined timeout can't do both -
        # it either makes dead-host detection as slow as the whole budget,
        # or cuts legitimate slow reads short.
        read_timeout = timeout_ms / 1000
        self.timeout = (min(5.0, read_timeout), read_timeout)
        self.max_retries = max_retries
        self._credentials: Optional[dict] = None
        self._headers = {"X-Bridge-Token": auth_token} if auth_token else {}
        # One Session, reused for every call this client makes for its
        # entire lifetime (one instance per engine process - see main.py).
        # requests.get/post without a Session open a brand new TCP
        # connection (and re-do the TLS handshake, for an https bridge_url)
        # on every single call; the engine polls the bridge multiple times
        # (price, candles, account, positions...) every poll_seconds, so a
        # bare module-level requests.get/post was paying that connection
        # setup cost on every one of those, for the whole session's
        # duration, instead of once.
        self._session = requests.Session()

    def _raw_get(self, path: str, **params) -> dict:
        resp = self._session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout,
                                  headers=self._headers)
        return resp.json()

    def _raw_post(self, path: str, payload: dict) -> dict:
        resp = self._session.post(f"{self.base_url}{path}", json=payload, timeout=self.timeout,
                                   headers=self._headers)
        return resp.json()

    def _call(self, raw_fn, *args, allow_relogin: bool = True,
              retry_network: bool = True, **kwargs) -> dict:
        """retry_network=False is for MUTATING calls (open/close/modify):
        a network-level failure there (timeout, connection reset) is
        AMBIGUOUS - the request may have reached MT5 and executed even
        though the response never came back. Blindly re-sending it could
        place a real duplicate order, so those raise immediately and let
        core/engine.py's reconcile-against-the-broker logic (which exists
        exactly for this case) decide what actually happened. A clean
        JSON error from the bridge is NOT ambiguous (the bridge refused
        the request, nothing executed), so bridge-level errors and the
        relogin-and-retry path stay available to mutating calls too."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                data = raw_fn(*args, **kwargs)
            except requests.RequestException as exc:
                if not retry_network:
                    raise BridgeError(
                        f"fallo de red en una llamada que muta estado ({exc}) - NO se reintenta "
                        f"automaticamente porque la orden pudo haber llegado igual al broker; "
                        f"el motor reconciliara contra las posiciones reales."
                    ) from exc
                last_exc = exc
                wait = min(2 ** attempt, 8)
                logger.warning("Bridge unreachable (%s), retrying in %ss...", exc, wait)
                time.sleep(wait)
                continue

            if data.get("ok"):
                return data

            error = data.get("error", "bridge error")
            if allow_relogin and "not logged in" in error.lower() and self._credentials:
                logger.warning("Bridge session dropped, re-logging in and retrying...")
                try:
                    self._raw_post("/login", self._credentials)
                except requests.RequestException as exc:
                    # The relogin itself failing at network level is not
                    # ambiguous for the ORIGINAL call (the bridge cleanly
                    # refused it with "not logged in", so nothing executed) -
                    # but without a session the retry can't succeed either
                    # way, so mutating calls give up here instead of looping.
                    if not retry_network:
                        raise BridgeError(
                            f"la sesion MT5 se cayo y el re-login fallo por red ({exc}) - "
                            f"la orden original NO se ejecuto (el bridge la rechazo limpiamente)."
                        ) from exc
                    last_exc = exc
                    time.sleep(min(2 ** attempt, 8))
                    continue
                continue  # retry the original call now that we're logged in again

            raise BridgeError(error)

        raise BridgeError(f"bridge unreachable after {self.max_retries} attempts: {last_exc}")

    def _get(self, path: str, **params) -> dict:
        return self._call(self._raw_get, path, **params)

    def _post_mutating(self, path: str, payload: dict) -> dict:
        return self._call(self._raw_post, path, payload, retry_network=False)

    def health(self) -> bool:
        try:
            resp = self._session.get(f"{self.base_url}/health", timeout=self.timeout)
            return resp.ok and resp.json().get("ok", False)
        except requests.RequestException:
            return False

    def login(self, login: str, password: str, server: str) -> None:
        self._credentials = {"login": login, "password": password, "server": server}
        self._call(self._raw_post, "/login", self._credentials, allow_relogin=False)

    def account(self) -> AccountState:
        d = self._get("/account")
        return AccountState(
            balance=d["balance"],
            equity=d["equity"],
            free_margin=d["margin_free"],
            leverage=int(d["leverage"]) or 1,
        )

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        d = self._get(f"/symbol/{symbol}")
        return SymbolSpec(
            contract_size=d["contract_size"],
            volume_min=d["volume_min"],
            volume_max=d["volume_max"],
            volume_step=d["volume_step"],
            point=d["point"],
            trade_tick_value=d["trade_tick_value"],
            trade_tick_size=d.get("trade_tick_size") or d["point"],
            margin_initial=d.get("margin_initial"),
        )

    def price(self, symbol: str) -> Tick:
        d = self._get(f"/price/{symbol}")
        return Tick(bid=d["bid"], ask=d["ask"], spread_price=d["spread_price"], time=d["time"])

    def candles(self, symbol: str, timeframe: str = "M1", count: int = 200,
                start: int | None = None, end: int | None = None) -> pd.DataFrame:
        params = {"timeframe": timeframe, "count": count}
        if start is not None:
            params["from"] = int(start)
        if end is not None:
            params["to"] = int(end)
        d = self._get(f"/candles/{symbol}", **params)
        df = pd.DataFrame(d["candles"])
        return df

    def ticks(self, symbol: str, count: int = 10000, start: int | None = None,
              end: int | None = None) -> pd.DataFrame:
        params = {"count": min(max(int(count), 1), 100000)}
        if start is not None:
            params["from"] = int(start)
        if end is not None:
            params["to"] = int(end)
        d = self._get(f"/ticks/{symbol}", **params)
        return pd.DataFrame(d["ticks"])

    def open_order(self, symbol: str, side: str, lot: float, sl_price: Optional[float] = None) -> dict:
        payload = {"symbol": symbol, "side": side, "lot": lot}
        if sl_price:
            payload["sl_price"] = sl_price
        return self._post_mutating("/order/open", payload)

    def close_order(self, ticket: str, lot: float) -> dict:
        return self._post_mutating("/order/close", {"ticket": ticket, "lot": lot})

    def modify_sl(self, ticket: str, sl_price: float) -> dict:
        return self._post_mutating("/order/modify", {"ticket": ticket, "sl_price": sl_price})

    def positions(self, symbol: Optional[str] = None) -> list[dict]:
        d = self._get("/positions", **({"symbol": symbol} if symbol else {}))
        return d["positions"]
