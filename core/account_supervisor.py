"""Conservative account-level OpenRouter supervisor.

The model can only pause entries or reduce the next trade's risk. It cannot
increase risk, choose a lot, change stops, close positions, or bypass any
RiskManager circuit breaker. Network/parse errors fail closed.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date

import pandas as pd
import requests

logger = logging.getLogger("account_supervisor")


@dataclass(frozen=True)
class SupervisorDecision:
    allow_entries: bool
    risk_multiplier: float
    reason: str


class AccountSupervisorBrain:
    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str = "openrouter/free", timeout_ms: int = 6000,
                 max_calls_per_day: int = 24) -> None:
        self.api_key = api_key
        self.model = model or "openrouter/free"
        self.timeout = (min(3.0, timeout_ms / 1000), timeout_ms / 1000)
        self.max_calls_per_day = max_calls_per_day
        self._session = requests.Session()
        self._day = date.today()
        self._calls_today = 0
        self._cache: dict[int, SupervisorDecision] = {}

    def _roll_day(self) -> None:
        if date.today() != self._day:
            self._day = date.today()
            self._calls_today = 0
            self._cache.clear()

    @staticmethod
    def _context(candles: pd.DataFrame, account: dict, base_risk: float,
                 candidate_side: str, bar_time: int) -> str:
        tail = candles.tail(8)
        bars = [[round(float(r.open), 4), round(float(r.high), 4), round(float(r.low), 4), round(float(r.close), 4)]
                for r in tail.itertuples()]
        return json.dumps({
            "bar_time": bar_time, "candidate_side": candidate_side,
            "balance": round(float(account.get("balance", 0)), 2),
            "equity": round(float(account.get("equity", 0)), 2),
            "free_margin": round(float(account.get("free_margin", 0)), 2),
            "base_risk_usd": round(base_risk, 4), "bars": bars,
        }, separators=(",", ":"))

    def evaluate(self, candles: pd.DataFrame, account: dict, base_risk: float,
                 candidate_side: str, bar_time: int) -> SupervisorDecision:
        self._roll_day()
        if bar_time in self._cache:
            return self._cache[bar_time]
        if not self.api_key:
            return SupervisorDecision(False, 0.0, "Supervisor veto: falta OPENROUTER_SUPERVISOR_API_KEY")
        if self._calls_today >= self.max_calls_per_day:
            return SupervisorDecision(False, 0.0, "Supervisor veto: limite diario de consultas alcanzado")
        payload = {
            "model": self.model, "temperature": 0, "max_tokens": 40,
            "reasoning": {"effort": "none", "exclude": True},
            "messages": [
                {"role": "system", "content": "You are a conservative account risk supervisor. Reply ONLY JSON {\"allow_entries\":true|false,\"risk_multiplier\":number,\"reason\":\"max 12 words\"}. Never increase risk; veto or reduce risk when equity/margin/market looks unsafe."},
                {"role": "user", "content": self._context(candles, account, base_risk, candidate_side, bar_time)},
            ],
        }
        try:
            response = self._session.post(self.ENDPOINT, json=payload,
                                          headers={"Authorization": f"Bearer {self.api_key}"},
                                          timeout=self.timeout)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in content)
            if not isinstance(content, str):
                raise ValueError("non-text response")
            match = re.search(r"\{.*\}", content, flags=re.DOTALL)
            parsed = json.loads(match.group(0) if match else content)
            allow = parsed.get("allow_entries") is True
            multiplier = float(parsed.get("risk_multiplier", 0.0))
            # The model is never allowed to increase configured risk.
            multiplier = max(0.0, min(1.0, multiplier))
            if allow and multiplier <= 0:
                allow = False
            reason = str(parsed.get("reason", "sin motivo"))[:120]
            decision = SupervisorDecision(allow, multiplier, f"Supervisor {'permite' if allow else 'veto'}: {reason}")
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Supervisor request failed; vetoing safely: %s", type(exc).__name__)
            decision = SupervisorDecision(False, 0.0, "Supervisor veto: respuesta no disponible o invalida")
        self._calls_today += 1
        self._cache[bar_time] = decision
        return decision
