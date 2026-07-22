from unittest.mock import MagicMock

import pandas as pd

from core.account_supervisor import AccountSupervisorBrain


def _bars():
    return pd.DataFrame([{"time": 1000 + i * 60, "open": 4000+i, "high": 4001+i,
                          "low": 3999+i, "close": 4000+i} for i in range(8)])


def test_supervisor_clamps_model_risk_increase_and_caches():
    brain = AccountSupervisorBrain("key", max_calls_per_day=2)
    response = MagicMock()
    response.json.return_value = {"choices": [{"message": {"content":
        '{"allow_entries":true,"risk_multiplier":9,"reason":"safe"}'}}]}
    brain._session.post = MagicMock(return_value=response)
    account = {"balance": 50, "equity": 50, "free_margin": 40}

    first = brain.evaluate(_bars(), account, 6, "BUY", 1420)
    second = brain.evaluate(_bars(), account, 6, "BUY", 1420)

    assert first.allow_entries is True
    assert first.risk_multiplier == 1.0
    assert second == first
    assert brain._session.post.call_count == 1


def test_supervisor_fails_closed_on_invalid_response():
    brain = AccountSupervisorBrain("key")
    response = MagicMock()
    response.json.return_value = {"choices": [{"message": {"content": "not json"}}]}
    brain._session.post = MagicMock(return_value=response)
    decision = brain.evaluate(_bars(), {"balance": 50, "equity": 45, "free_margin": 2}, 6, "SELL", 1420)
    assert decision.allow_entries is False
    assert decision.risk_multiplier == 0.0
