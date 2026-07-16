"""Tests for the Linux-side bridge client, using a fake requests.Session
response instead of a real Wine/MT5 bridge process."""
from unittest.mock import MagicMock, patch

import pytest

from core.mt5_bridge_client import BridgeError, Mt5BridgeClient


def _fake_response(payload: dict, ok: bool = True):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.ok = ok
    return resp


def test_symbol_spec_picks_up_broker_reported_margin_initial():
    """This is the fix for a real bug: FBS runs metals at a FIXED 1:500
    leverage independent of the account's forex leverage setting, so
    deriving margin from contract_size/price/account.leverage can be
    wrong by orders of magnitude. order_calc_margin (surfaced by the
    bridge as margin_initial) is the broker's own authoritative figure
    and must take priority - this just checks it makes it through the
    client intact."""
    payload = {
        "ok": True, "contract_size": 100.0, "volume_min": 0.01, "volume_max": 50.0,
        "volume_step": 0.01, "point": 0.01, "digits": 2, "trade_tick_value": 1.0,
        "trade_tick_size": 0.01, "spread": 20, "margin_initial": 8.13,
    }
    client = Mt5BridgeClient("http://127.0.0.1:5001")
    with patch("requests.Session.get", return_value=_fake_response(payload)):
        spec = client.symbol_spec("XAUUSD")
    assert spec.margin_initial == 8.13


def test_symbol_spec_handles_missing_margin_initial_gracefully():
    """If the bridge/MT5 couldn't compute it (e.g. no tick yet), the
    field is absent/null and callers must fall back, not crash."""
    payload = {
        "ok": True, "contract_size": 100.0, "volume_min": 0.01, "volume_max": 50.0,
        "volume_step": 0.01, "point": 0.01, "digits": 2, "trade_tick_value": 1.0,
        "trade_tick_size": 0.01, "spread": 20, "margin_initial": None,
    }
    client = Mt5BridgeClient("http://127.0.0.1:5001")
    with patch("requests.Session.get", return_value=_fake_response(payload)):
        spec = client.symbol_spec("XAUUSD")
    assert spec.margin_initial is None


def test_bridge_error_raised_on_not_ok_response():
    payload = {"ok": False, "error": "symbol_select(XAUUSD) failed"}
    client = Mt5BridgeClient("http://127.0.0.1:5001", max_retries=1)
    with patch("requests.Session.get", return_value=_fake_response(payload)), \
            pytest.raises(BridgeError, match="symbol_select"):
        client.symbol_spec("XAUUSD")


def test_auth_token_sent_as_header_when_configured():
    """The bridge can open/close real trades - a configured token must
    actually be sent, or the auth check added to the bridge server would
    be silently pointless."""
    payload = {"ok": True, "contract_size": 100.0, "volume_min": 0.01, "volume_max": 50.0,
               "volume_step": 0.01, "point": 0.01, "digits": 2, "trade_tick_value": 1.0,
               "trade_tick_size": 0.01, "spread": 20, "margin_initial": None}
    client = Mt5BridgeClient("http://127.0.0.1:5001", auth_token="secret-token-123")
    with patch("requests.Session.get", return_value=_fake_response(payload)) as mock_get:
        client.symbol_spec("XAUUSD")
    _, kwargs = mock_get.call_args
    assert kwargs["headers"]["X-Bridge-Token"] == "secret-token-123"


def test_no_auth_header_sent_when_token_not_configured():
    payload = {"ok": True, "contract_size": 100.0, "volume_min": 0.01, "volume_max": 50.0,
               "volume_step": 0.01, "point": 0.01, "digits": 2, "trade_tick_value": 1.0,
               "trade_tick_size": 0.01, "spread": 20, "margin_initial": None}
    client = Mt5BridgeClient("http://127.0.0.1:5001")
    with patch("requests.Session.get", return_value=_fake_response(payload)) as mock_get:
        client.symbol_spec("XAUUSD")
    _, kwargs = mock_get.call_args
    assert "X-Bridge-Token" not in kwargs["headers"]
