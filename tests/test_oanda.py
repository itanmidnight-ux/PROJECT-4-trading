"""Tests for core/oanda.py - the direct (no MetaTrader, no Wine) OANDA
v20 REST broker adapter. Uses faked requests.Session responses shaped
exactly like OANDA's documented v20 payloads; no network involved."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from core.oanda import OandaClient, OandaError, to_oanda_instrument


def make_client(**kwargs) -> OandaClient:
    return OandaClient("fake-token", "101-001-1234567-001", environment="practice", **kwargs)


def _resp(payload: dict, status: int = 200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = str(payload)
    return r


def test_instrument_name_mapping():
    assert to_oanda_instrument("XAUUSD") == "XAU_USD"
    assert to_oanda_instrument("EURUSD") == "EUR_USD"
    assert to_oanda_instrument("XAU_USD") == "XAU_USD"  # already native: untouched


def test_rejects_unknown_environment():
    with pytest.raises(OandaError, match="OANDA_ENV"):
        OandaClient("t", "a", environment="staging")


def test_price_parses_bid_ask_and_time():
    payload = {"prices": [{
        "time": "2026-07-18T14:03:00.123456789Z",
        "bids": [{"price": "4071.250", "liquidity": 100}],
        "asks": [{"price": "4071.550", "liquidity": 100}],
    }]}
    client = make_client()
    with patch("requests.Session.request", return_value=_resp(payload)):
        tick = client.price("XAUUSD")
    assert tick.bid == 4071.25
    assert tick.ask == 4071.55
    assert tick.spread_price == pytest.approx(0.30)
    assert tick.time == 1784383380  # 2026-07-18T14:03:00Z (verificado con calendar.timegm)


def test_candles_convert_to_the_engines_dataframe_shape():
    payload = {"candles": [
        {"time": "2026-07-18T14:02:00.000000000Z", "volume": 42, "complete": True,
         "mid": {"o": "4070.1", "h": "4071.9", "l": "4069.8", "c": "4071.2"}},
        {"time": "2026-07-18T14:03:00.000000000Z", "volume": 7, "complete": False,
         "mid": {"o": "4071.2", "h": "4071.6", "l": "4071.0", "c": "4071.5"}},
    ]}
    client = make_client()
    with patch("requests.Session.request", return_value=_resp(payload)) as mock_req:
        df = client.candles("XAUUSD", "M1", 600)
    assert list(df.columns) == ["time", "open", "high", "low", "close", "tick_volume"]
    assert df["time"].tolist() == [1784383320, 1784383380]
    assert df["close"].iloc[-1] == 4071.5  # the forming candle IS included, like MT5
    _, kwargs = mock_req.call_args
    assert kwargs["params"]["granularity"] == "M1"


def test_account_maps_summary_and_derives_leverage_from_margin_rate():
    payload = {"account": {"balance": "50.0000", "NAV": "51.2300",
                            "marginAvailable": "48.1000", "marginRate": "0.05"}}
    client = make_client()
    with patch("requests.Session.request", return_value=_resp(payload)):
        acc = client.account()
    assert acc.balance == 50.0
    assert acc.equity == 51.23
    assert acc.free_margin == 48.10
    assert acc.leverage == 20  # 1 / 0.05


def test_symbol_spec_maps_units_semantics_so_engine_math_is_1usd_per_point_per_unit():
    payload = {"instruments": [{
        "name": "XAU_USD", "displayPrecision": 3, "pipLocation": -2,
        "tradeUnitsPrecision": 0, "minimumTradeSize": "1",
        "maximumOrderUnits": "10000", "marginRate": "0.05",
    }]}
    client = make_client()
    with patch("requests.Session.request", return_value=_resp(payload)):
        spec = client.symbol_spec("XAUUSD")
    # The invariant the whole engine's dollar math rests on:
    assert spec.trade_tick_value / spec.point == pytest.approx(1.0)
    assert spec.volume_min == 1.0
    assert spec.volume_step == 1.0  # tradeUnitsPrecision 0 -> whole units


def test_open_order_sends_negative_units_for_sell_and_formatted_sl():
    fill = {"orderFillTransaction": {"id": "77", "price": "4071.520",
                                       "tradeOpened": {"tradeID": "78", "units": "-1"}}}
    client = make_client()
    client._price_precision["XAU_USD"] = 3
    with patch("requests.Session.request", return_value=_resp(fill)) as mock_req:
        result = client.open_order("XAUUSD", "SELL", 1.0, sl_price=4080.123456, fill_price=4071.5)
    body = mock_req.call_args.kwargs["json"]["order"]
    assert body["units"] == "-1"
    assert body["stopLossOnFill"]["price"] == "4080.123"  # displayPrecision respected
    assert body["timeInForce"] == "FOK"
    assert result.ticket == "78"
    assert result.fill_price == 4071.52


def test_open_order_raises_clearly_when_oanda_cancels_instead_of_filling():
    cancel = {"orderCancelTransaction": {"reason": "INSUFFICIENT_MARGIN"}}
    client = make_client()
    with patch("requests.Session.request", return_value=_resp(cancel)), \
            pytest.raises(OandaError, match="INSUFFICIENT_MARGIN"):
        client.open_order("XAUUSD", "BUY", 1.0, sl_price=4000.0, fill_price=4071.5)


def test_close_partial_returns_oandas_realized_pl():
    payload = {"orderFillTransaction": {"price": "4072.100", "pl": "0.5800"}}
    client = make_client()
    with patch("requests.Session.request", return_value=_resp(payload)):
        pnl = client.close_partial("78", 1.0, fill_price=4072.1)
    assert pnl == pytest.approx(0.58)


def test_open_positions_maps_signed_units_to_side_and_filters_other_instruments():
    payload = {"trades": [
        {"id": "78", "instrument": "XAU_USD", "currentUnits": "-2", "price": "4071.520",
         "stopLossOrder": {"price": "4080.000"}},
        {"id": "79", "instrument": "EUR_USD", "currentUnits": "100", "price": "1.0900"},
    ]}
    client = make_client()
    with patch("requests.Session.request", return_value=_resp(payload)):
        positions = client.open_positions("XAUUSD")
    assert len(positions) == 1
    p = positions[0]
    assert (p.side, p.volume, p.sl) == ("SELL", 2.0, 4080.0)


def test_mutating_calls_do_not_retry_on_network_errors():
    """Same duplicate-order rule as the MT5 client: a timed-out order may
    have executed anyway - exactly ONE send, then raise for the engine's
    reconcile logic."""
    client = make_client(max_retries=3)
    with patch("requests.Session.request", side_effect=requests.Timeout("read timed out")) as mock_req, \
            pytest.raises(OandaError, match="NO se reintenta"):
        client.open_order("XAUUSD", "BUY", 1.0, sl_price=4000.0, fill_price=4071.5)
    assert mock_req.call_count == 1


def test_read_calls_retry_network_errors_with_backoff(monkeypatch):
    monkeypatch.setattr("core.oanda.time.sleep", lambda _s: None)
    ok = {"prices": [{"time": "2026-07-18T14:03:00.000000000Z",
                       "bids": [{"price": "4071.2"}], "asks": [{"price": "4071.5"}]}]}
    client = make_client(max_retries=3)
    with patch("requests.Session.request",
               side_effect=[requests.ConnectionError("refused"), _resp(ok)]) as mock_req:
        tick = client.price("XAUUSD")
    assert mock_req.call_count == 2
    assert tick.ask == 4071.5


def test_http_error_surfaces_oandas_own_message():
    client = make_client()
    with patch("requests.Session.request",
               return_value=_resp({"errorMessage": "Insufficient authorization to perform request."}, status=401)), \
            pytest.raises(OandaError, match="Insufficient authorization"):
        client.account()


def test_client_works_under_bridge_market_datas_incremental_cache():
    """The whole point of matching Mt5BridgeClient's price()/candles()
    shape: BridgeMarketData's incremental candle sync must wrap an
    OandaClient without modification."""
    from core.market_data import BridgeMarketData

    candles = {"candles": [
        {"time": "2026-07-18T14:02:00.000000000Z", "volume": 1, "complete": True,
         "mid": {"o": "1", "h": "2", "l": "0.5", "c": "1.5"}},
    ]}
    price = {"prices": [{"time": "2026-07-18T14:03:00.000000000Z",
                          "bids": [{"price": "4071.2"}], "asks": [{"price": "4071.5"}]}]}
    client = make_client()
    md = BridgeMarketData(client)
    with patch("requests.Session.request", side_effect=[_resp(price), _resp(candles)]):
        state = md.get_state("XAUUSD", "M1", 600)
    assert state.tick.bid == 4071.2
    assert state.candles["close"].iloc[-1] == 1.5
