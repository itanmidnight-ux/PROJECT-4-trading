"""Tests for the Linux-side bridge client, using a fake requests.Session
response instead of a real Wine/MT5 bridge process."""
from unittest.mock import MagicMock, patch

import pytest
import requests

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


def test_open_order_does_not_retry_on_network_timeout():
    """THE duplicate-order guard: a timeout on /order/open is ambiguous -
    the order may have reached MT5 and filled even though the response
    was lost. Before this fix, _call() blindly re-sent it (up to
    max_retries times), so one flaky-Wi-Fi timeout could place two or
    three REAL positions. The client must send it exactly ONCE and raise,
    handing the ambiguity to core/engine.py's reconcile logic (which
    exists precisely for this case but was unreachable while the client
    retried internally)."""
    client = Mt5BridgeClient("http://127.0.0.1:5001", max_retries=3)
    with patch("requests.Session.post", side_effect=requests.Timeout("read timed out")) as mock_post, \
            pytest.raises(BridgeError, match="NO se reintenta"):
        client.open_order("XAUUSD", "BUY", 0.01, sl_price=4000.0)
    assert mock_post.call_count == 1  # exactly one attempt, never re-sent


def test_close_and_modify_do_not_retry_on_network_timeout_either():
    client = Mt5BridgeClient("http://127.0.0.1:5001", max_retries=3)
    for call in (lambda: client.close_order("123", 0.01),
                 lambda: client.modify_sl("123", 4000.0)):
        with patch("requests.Session.post", side_effect=requests.Timeout("read timed out")) as mock_post, \
                pytest.raises(BridgeError):
            call()
        assert mock_post.call_count == 1


def test_read_calls_still_retry_on_network_errors(monkeypatch):
    """The asymmetry's other half: reads (price/candles/account) are
    idempotent, so the existing retry-with-backoff behavior must survive
    the mutating-call fix untouched - a transient Wi-Fi blip on a phone
    polling a remote bridge should NOT bubble up as an engine error if
    the next attempt succeeds."""
    monkeypatch.setattr("core.mt5_bridge_client.time.sleep", lambda _s: None)  # no real backoff waits in tests
    ok_payload = {"ok": True, "bid": 4000.0, "ask": 4000.3, "spread_price": 0.3, "time": 1}
    client = Mt5BridgeClient("http://127.0.0.1:5001", max_retries=3)
    with patch("requests.Session.get",
               side_effect=[requests.ConnectionError("refused"), _fake_response(ok_payload)]) as mock_get:
        tick = client.price("XAUUSD")
    assert mock_get.call_count == 2  # failed once, retried, succeeded
    assert tick.bid == 4000.0


def test_mutating_call_still_relogs_in_after_clean_not_logged_in_refusal():
    """A clean JSON 'not logged in' from the bridge is NOT ambiguous (the
    bridge refused the request - nothing reached MT5), so the transparent
    relogin-and-retry path must keep working for mutating calls: dropping
    it would turn every MT5 session hiccup into a failed close/modify."""
    client = Mt5BridgeClient("http://127.0.0.1:5001", max_retries=3)
    responses = [
        _fake_response({"ok": True}),                                   # initial login()
        _fake_response({"ok": False, "error": "not logged in"}),        # close attempt 1: refused cleanly
        _fake_response({"ok": True}),                                   # automatic re-login
        _fake_response({"ok": True, "closed": True}),                   # close attempt 2: succeeds
    ]
    with patch("requests.Session.post", side_effect=responses) as mock_post:
        client.login("123", "pw", "FBS-Demo")
        result = client.close_order("456", 0.01)
    assert result["closed"] is True
    assert mock_post.call_count == 4
