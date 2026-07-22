"""Low-cost, fail-closed OpenRouter confirmation gate for trade signals.

This is intentionally not an autonomous trader: an LLM can only reject a
signal that the deterministic, backtested strategy already produced.  It
cannot choose side, lot size, stop, or override RiskManager.  One tiny request
is made only after a candidate signal, then cached by closed-bar timestamp.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date

import pandas as pd
import requests

logger = logging.getLogger("ai_brain")


@dataclass(frozen=True)
class AIDecision:
    allow: bool
    reason: str


class OpenRouterBrain:
    """Strict JSON OpenRouter client with daily call cap and fail-closed I/O."""

    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str = "openrouter/free", timeout_ms: int = 6000,
                 max_calls_per_day: int = 40) -> None:
        self.api_key = api_key
        self.model = model or "openrouter/free"
        self.timeout = (min(3.0, timeout_ms / 1000), timeout_ms / 1000)
        self.max_calls_per_day = max_calls_per_day
        self._session = requests.Session()
        self._day = date.today()
        self._calls_today = 0
        self._cache: dict[tuple[int, str], AIDecision] = {}

    def _roll_day(self) -> None:
        if date.today() != self._day:
            self._day = date.today()
            self._calls_today = 0
            self._cache.clear()

    @staticmethod
    def _compact_context(candles: pd.DataFrame, side: str, spread: float,
                         sl_distance: float) -> tuple[int, str]:
        if candles.empty:
            return 0, "{}"
        # Twelve completed bars provide context while keeping every request
        # small. Values are rounded to instrument price precision agnostically.
        tail = candles.tail(12)
        bars = [[round(float(r.open), 5), round(float(r.high), 5), round(float(r.low), 5), round(float(r.close), 5)]
                for r in tail.itertuples()]
        ts = int(tail.iloc[-1]["time"])
        return ts, json.dumps({"side": side, "spread": round(spread, 5),
                                "sl": round(sl_distance, 5), "bars": bars}, separators=(",", ":"))

    def evaluate(self, candles: pd.DataFrame, side: str, spread: float, sl_distance: float) -> AIDecision:
        self._roll_day()
        bar_time, context = self._compact_context(candles, side, spread, sl_distance)
        key = (bar_time, side)
        if key in self._cache:
            return self._cache[key]
        if not self.api_key:
            return AIDecision(False, "AI veto: falta OPENROUTER_API_KEY")
        if self._calls_today >= self.max_calls_per_day:
            return AIDecision(False, "AI veto: limite diario de consultas alcanzado")

        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 32,
            # This task is a binary safety gate, not a reasoning problem.
            # Disabling thinking preserves the tiny output budget for JSON
            # and minimizes token use on the free dynamic router.
            "reasoning": {"effort": "none", "exclude": True},
            "messages": [
                {"role": "system", "content": "You are a conservative trading signal filter. Reply ONLY JSON: {\"allow\":true|false,\"reason\":\"max 12 words\"}. Allow only if the supplied closed-bar setup supports the requested side; otherwise veto."},
                {"role": "user", "content": context},
            ],
        }
        try:
            response = self._session.post(self.ENDPOINT, json=payload,
                                          headers={"Authorization": f"Bearer {self.api_key}"},
                                          timeout=self.timeout)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            # OpenRouter providers may return OpenAI's plain string or the
            # newer multipart content array. Normalize both without ever
            # trusting an unknown structure as an approval.
            if isinstance(content, list):
                content = "".join(
                    str(part.get("text", "")) if isinstance(part, dict) else str(part)
                    for part in content
                )
            if not isinstance(content, str):
                raise ValueError("OpenRouter returned non-text content")
            match = re.search(r"\{.*\}", content, flags=re.DOTALL)
            parsed = json.loads(match.group(0) if match else content)
            allow = parsed.get("allow") is True
            reason = str(parsed.get("reason", "sin motivo"))[:120]
            decision = AIDecision(allow, f"AI {'confirmo' if allow else 'veto'}: {reason}")
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("AI request failed; vetoing candidate safely: %s", type(exc).__name__)
            decision = AIDecision(False, "AI veto: respuesta no disponible o invalida")
        self._calls_today += 1
        self._cache[key] = decision
        return decision
