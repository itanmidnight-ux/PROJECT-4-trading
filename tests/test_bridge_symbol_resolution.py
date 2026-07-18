"""Tests for the FBS-critical parts of bridge/mt5_bridge_server.py that
CAN be tested outside Wine: symbol-suffix resolution (_resolve_symbol)
and retcode translation (_retcode_msg).

The module hard-requires the MetaTrader5 package (Windows-only, lives
inside the Wine prefix), so a fake module is injected into sys.modules
BEFORE import - the fake only needs the names touched at import time
(TIMEFRAME_* constants) plus whatever each test configures.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def bridge(monkeypatch):
    fake_mt5 = MagicMock()
    fake_mt5.SYMBOL_TRADE_MODE_DISABLED = 0
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)
    # A previous test may have imported the module against a different fake -
    # force a fresh import bound to THIS one.
    sys.modules.pop("bridge.mt5_bridge_server", None)
    import bridge.mt5_bridge_server as server
    server._symbol_cache.clear()
    yield server, fake_mt5
    sys.modules.pop("bridge.mt5_bridge_server", None)


def _sym(name: str, trade_mode: int = 4):  # 4 = SYMBOL_TRADE_MODE_FULL
    return SimpleNamespace(name=name, trade_mode=trade_mode)


def test_exact_symbol_name_is_used_directly(bridge):
    server, mt5 = bridge
    mt5.symbol_select.return_value = True
    assert server._resolve_symbol("XAUUSD") == "XAUUSD"
    mt5.symbols_get.assert_not_called()  # no search needed


def test_fbs_suffix_account_resolves_to_the_suffixed_variant(bridge):
    """THE FBS case: on Micro/Cent accounts the symbol is XAUUSDm/XAUUSDc,
    so symbol_select("XAUUSD") fails - the bridge must find and use the
    suffixed variant instead of dying with a 404 forever."""
    server, mt5 = bridge
    mt5.symbol_select.side_effect = lambda name, _enable: name == "XAUUSDm"
    mt5.symbols_get.return_value = (_sym("XAUUSDm"), _sym("XAUUSDmicro.raw"))
    assert server._resolve_symbol("XAUUSD") == "XAUUSDm"  # shortest tradable match wins


def test_disabled_variants_are_skipped(bridge):
    server, mt5 = bridge
    mt5.symbol_select.side_effect = lambda name, _enable: name == "XAUUSDc"
    mt5.symbols_get.return_value = (
        _sym("XAUUSDm", trade_mode=0),  # disabled for trading - must be skipped
        _sym("XAUUSDc"),
    )
    assert server._resolve_symbol("XAUUSD") == "XAUUSDc"


def test_resolution_is_cached_per_requested_name(bridge):
    server, mt5 = bridge
    mt5.symbol_select.side_effect = lambda name, _enable: name == "XAUUSDm"
    mt5.symbols_get.return_value = (_sym("XAUUSDm"),)
    assert server._resolve_symbol("XAUUSD") == "XAUUSDm"
    assert server._resolve_symbol("XAUUSD") == "XAUUSDm"
    assert mt5.symbols_get.call_count == 1  # second call answered from cache


def test_returns_none_when_nothing_matches_anywhere(bridge):
    server, mt5 = bridge
    mt5.symbol_select.return_value = False
    mt5.symbols_get.return_value = ()
    assert server._resolve_symbol("XAUUSD") is None


def test_retcode_translation_makes_common_failures_actionable(bridge):
    server, _mt5 = bridge
    assert "fondos insuficientes" in server._retcode_msg(10019)
    assert "mercado cerrado" in server._retcode_msg(10018)
    assert "AutoTrading" in server._retcode_msg(10027)
    assert server._retcode_msg(99999) == "99999"  # unknown: raw code, no crash
    assert "tuple" not in server._retcode_msg((1, "weird"))  # non-int: stringified
