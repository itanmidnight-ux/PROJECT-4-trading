"""Ronda 28: tests for core/backtest.py's optional brain_fn hook (the
backtest harness for core/ai_brain.py, wired but never fired for real -
see .env.example's Ronda 18/25/26/27 comments for why it stayed
unimplemented until the real account balance makes the risk cap stop
rejecting everything). Every test here uses a MOCKED brain: a plain
Python callable that mimics OpenRouterBrain.evaluate's signature and
returns an AIDecision, with zero HTTP involved. requests.Session.post is
also patched to explode if anything ever tries to reach the network, as a
second, redundant guard that this suite makes zero real OpenRouter calls."""
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.ai_brain import AIDecision
from core.backtest import run_backtest
from core.risk_manager import RiskManager, SymbolSpec
from tests.test_strategy import oversold_candles

SPEC = SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=1.0,
                   volume_step=0.01, point=0.01, trade_tick_value=1.0)
SPREAD = 0.25


def _extend_flat(base: pd.DataFrame, n: int) -> pd.DataFrame:
    """n more bars that gently oscillate around the last close - no fresh
    breakout, so they never produce a new deterministic signal on their
    own (only the position-management path touches them)."""
    import numpy as np
    rows = []
    prev_close = base.iloc[-1]["close"]
    t = int(base.iloc[-1]["time"]) + 60
    for i in range(n):
        c = prev_close + 0.05 * np.sin(i * 0.9)
        rows.append({"time": t, "open": prev_close, "high": max(prev_close, c),
                     "low": min(prev_close, c), "close": c, "tick_volume": 50})
        prev_close = c
        t += 60
    return pd.concat([base, pd.DataFrame(rows)], ignore_index=True)


def _always_allow(candles, side, spread, sl_distance):
    return AIDecision(True, "mock allow")


def _always_veto(candles, side, spread, sl_distance):
    return AIDecision(False, "mock veto")


@patch("requests.Session.post", side_effect=AssertionError("no real network call should ever happen in this suite"))
def test_brain_fn_only_called_on_signal_bars_not_every_bar(_post):
    """oversold_candles() produces exactly one deterministic BUY signal (on
    its last bar); the rest is flat/oscillating filler with no breakout.
    The mocked brain must be consulted exactly once, proving it gates on
    signal bars only - not once per replayed bar."""
    candles = _extend_flat(oversold_candles(), 20)
    brain = MagicMock(side_effect=_always_allow)

    result = run_backtest(
        candles=candles, spec=SPEC, starting_balance=50_000.0, leverage=100,
        risk_per_trade_usd=1.0, min_tp_usd=0.28, tp_levels=3,
        assumed_spread_price=SPREAD, brain_fn=brain,
    )

    assert brain.call_count == 1
    assert result.brain_calls == 1
    # Far fewer brain calls than replayed bars - the whole point of gating
    # on signal.side instead of calling it every step.
    assert len(candles) - 25 > 10 * brain.call_count
    _post.assert_not_called()


@patch("requests.Session.post", side_effect=AssertionError("no real network call should ever happen in this suite"))
def test_brain_veto_blocks_the_trade_and_skips_risk_manager_entirely(_post):
    """core/engine.py calls the brain BEFORE RiskManager.size_position and
    treats a veto as final - size_position must never even run for a
    vetoed candidate. Uses a spy that wraps the REAL size_position so a
    call still behaves correctly if it were (wrongly) invoked."""
    candles = _extend_flat(oversold_candles(), 5)
    with patch.object(RiskManager, "size_position", autospec=True,
                       wraps=RiskManager.size_position) as spy:
        result = run_backtest(
            candles=candles, spec=SPEC, starting_balance=50_000.0, leverage=100,
            risk_per_trade_usd=1.0, min_tp_usd=0.28, tp_levels=3,
            assumed_spread_price=SPREAD, brain_fn=_always_veto,
        )
        spy.assert_not_called()

    # A vetoed candidate never opens a position, so the still-open
    # deterministic condition can keep re-firing on the following bars
    # (each one independently vetoed) - the exact count isn't the point,
    # only that every single call was a veto and none of them traded.
    assert result.trades == 0
    assert result.brain_calls >= 1
    assert result.brain_vetoes == result.brain_calls
    _post.assert_not_called()


@patch("requests.Session.post", side_effect=AssertionError("no real network call should ever happen in this suite"))
def test_brain_allow_still_lets_risk_manager_run_after(_post):
    """The mirror case: when the brain allows, size_position DOES still
    run right after (proves the call order is brain-then-risk, not brain
    replacing risk)."""
    candles = _extend_flat(oversold_candles(), 5)
    with patch.object(RiskManager, "size_position", autospec=True,
                       wraps=RiskManager.size_position) as spy:
        run_backtest(
            candles=candles, spec=SPEC, starting_balance=50_000.0, leverage=100,
            risk_per_trade_usd=1.0, min_tp_usd=0.28, tp_levels=3,
            assumed_spread_price=SPREAD, brain_fn=_always_allow,
        )
        spy.assert_called_once()
    _post.assert_not_called()


@patch("requests.Session.post", side_effect=AssertionError("no real network call should ever happen in this suite"))
def test_brain_approval_cannot_bypass_the_risk_manager_cap(_post):
    """CRITICAL: a signal the (mocked) brain approves must still be
    rejected if RiskManager's untouched 5% cap (core/risk_manager.py,
    MAX_RISK_FRACTION_OF_BALANCE) rejects it afterwards - mirrors the real
    $20.45 account from Ronda 26/27, where every signal at this SL
    distance is capped away regardless of what any brain says."""
    candles = _extend_flat(oversold_candles(), 5)

    result = run_backtest(
        candles=candles, spec=SPEC, starting_balance=20.45, leverage=500,
        risk_per_trade_usd=3.0, min_tp_usd=0.28, tp_levels=3,
        assumed_spread_price=SPREAD, brain_fn=_always_allow,
    )

    assert result.brain_calls == 1
    assert result.brain_vetoes == 0  # the mock never vetoed anything
    assert result.trades == 0        # yet RiskManager's cap still rejected it
    _post.assert_not_called()


@patch("requests.Session.post", side_effect=AssertionError("no real network call should ever happen in this suite"))
def test_mock_brain_runs_end_to_end_without_brain_fn_being_a_noop(_post):
    """Default (brain_fn=None) behavior is completely unaffected: same
    trade count/pnl with and without an always-allowing mock plugged in,
    and brain_calls/brain_vetoes default to zero when the harness isn't
    used - opt-in only, no change to today's plain run_backtest() callers
    (e.g. dashboard.py)."""
    candles = _extend_flat(oversold_candles(), 5)
    kwargs = dict(candles=candles, spec=SPEC, starting_balance=50_000.0, leverage=100,
                  risk_per_trade_usd=1.0, min_tp_usd=0.28, tp_levels=3,
                  assumed_spread_price=SPREAD)

    baseline = run_backtest(**kwargs)
    with_mock_brain = run_backtest(**kwargs, brain_fn=_always_allow)

    assert baseline.brain_calls == 0
    assert baseline.brain_vetoes == 0
    assert baseline.trades == with_mock_brain.trades
    assert baseline.total_pnl == pytest.approx(with_mock_brain.total_pnl)
    assert with_mock_brain.brain_calls == 1
    _post.assert_not_called()
