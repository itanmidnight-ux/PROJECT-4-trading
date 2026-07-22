from unittest.mock import MagicMock

import pandas as pd

from core.ai_brain import OpenRouterBrain


def _candles():
    return pd.DataFrame([
        {"time": 1_700_000_000 + i * 60, "open": 4000 + i, "high": 4001 + i,
         "low": 3999 + i, "close": 4000.5 + i}
        for i in range(12)
    ])


def test_ai_brain_accepts_strict_json_and_caches_per_closed_bar():
    brain = OpenRouterBrain("test-key", max_calls_per_day=2)
    response = MagicMock()
    response.json.return_value = {"choices": [{"message": {"content": '{"allow":true,"reason":"trend aligns"}'}}]}
    brain._session.post = MagicMock(return_value=response)

    first = brain.evaluate(_candles(), "BUY", 0.2, 1.5)
    second = brain.evaluate(_candles(), "BUY", 0.2, 1.5)

    assert first.allow is True
    assert second == first
    assert brain._session.post.call_count == 1
    _, kwargs = brain._session.post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert kwargs["json"]["model"] == "openrouter/free"
    assert kwargs["json"]["max_tokens"] == 32
    assert kwargs["json"]["reasoning"]["effort"] == "none"


def test_ai_brain_fails_closed_for_invalid_response():
    brain = OpenRouterBrain("test-key")
    response = MagicMock()
    response.json.return_value = {"choices": [{"message": {"content": "certainly buy"}}]}
    brain._session.post = MagicMock(return_value=response)

    decision = brain.evaluate(_candles(), "BUY", 0.2, 1.5)

    assert decision.allow is False
    assert "invalida" in decision.reason


def test_ai_brain_vetoes_without_a_key_without_calling_network():
    brain = OpenRouterBrain("")
    brain._session.post = MagicMock()

    decision = brain.evaluate(_candles(), "SELL", 0.2, 1.5)

    assert decision.allow is False
    brain._session.post.assert_not_called()
