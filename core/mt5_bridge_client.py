"""HTTP client for the Wine-side MT5 bridge (see bridge/mt5_bridge_server.py).
This is the only way the Linux-side engine talks to the broker.

Resilient to two real-world failure modes: the bridge process being
mid-restart (connection refused - retried with backoff) and the bridge
having lost its MT5 session (reports "not logged in" - the client
transparently re-sends the last known credentials and retries once)."""
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
    def __init__(self, base_url: str, timeout_ms: int = 8000, max_retries: int = 3) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_ms / 1000
        self.max_retries = max_retries
        self._credentials: Optional[dict] = None

    def _raw_get(self, path: str, **params) -> dict:
        resp = requests.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        return resp.json()

    def _raw_post(self, path: str, payload: dict) -> dict:
        resp = requests.post(f"{self.base_url}{path}", json=payload, timeout=self.timeout)
        return resp.json()

    def _call(self, raw_fn, *args, allow_relogin: bool = True, **kwargs) -> dict:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                data = raw_fn(*args, **kwargs)
            except requests.RequestException as exc:
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
                    last_exc = exc
                    time.sleep(min(2 ** attempt, 8))
                    continue
                continue  # retry the original call now that we're logged in again

            raise BridgeError(error)

        raise BridgeError(f"bridge unreachable after {self.max_retries} attempts: {last_exc}")

    def _get(self, path: str, **params) -> dict:
        return self._call(self._raw_get, path, **params)

    def _post(self, path: str, payload: dict) -> dict:
        return self._call(self._raw_post, path, payload)

    def health(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/health", timeout=self.timeout)
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
        )

    def price(self, symbol: str) -> Tick:
        d = self._get(f"/price/{symbol}")
        return Tick(bid=d["bid"], ask=d["ask"], spread_price=d["spread_price"], time=d["time"])

    def candles(self, symbol: str, timeframe: str = "M1", count: int = 200) -> pd.DataFrame:
        d = self._get(f"/candles/{symbol}", timeframe=timeframe, count=count)
        df = pd.DataFrame(d["candles"])
        return df

    def open_order(self, symbol: str, side: str, lot: float, sl_price: Optional[float] = None) -> dict:
        payload = {"symbol": symbol, "side": side, "lot": lot}
        if sl_price:
            payload["sl_price"] = sl_price
        return self._post("/order/open", payload)

    def close_order(self, ticket: str, lot: float) -> dict:
        return self._post("/order/close", {"ticket": ticket, "lot": lot})

    def modify_sl(self, ticket: str, sl_price: float) -> dict:
        return self._post("/order/modify", {"ticket": ticket, "sl_price": sl_price})

    def positions(self, symbol: Optional[str] = None) -> list[dict]:
        d = self._get("/positions", **({"symbol": symbol} if symbol else {}))
        return d["positions"]
