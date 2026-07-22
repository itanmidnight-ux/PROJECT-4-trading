import time
from pathlib import Path

import pandas as pd

from core.backtest import run_backtest
from core.config import Settings
from core.risk_manager import AccountState, RiskManager, SymbolSpec
from core.signals import build_strategy_from_settings
from core.strategy import ScalpStrategy
from tests.test_strategy import oversold_candles

SPEC = SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=1.0,
                   volume_step=0.01, point=0.01, trade_tick_value=1.0)
SPREAD = 0.25


def _extend(base: pd.DataFrame, closes: list[float]) -> pd.DataFrame:
    rows = []
    prev_close = base.iloc[-1]["close"]
    t = int(base.iloc[-1]["time"]) + 60
    for c in closes:
        rows.append({"time": t, "open": prev_close, "high": max(prev_close, c),
                     "low": min(prev_close, c), "close": c, "tick_volume": 50})
        prev_close = c
        t += 60
    return pd.concat([base, pd.DataFrame(rows)], ignore_index=True)


def test_backtest_counts_partial_tp_then_breakeven_stop_as_a_win():
    """Regression test: a position that locks in profit at TP1 and then
    gets stopped at breakeven on the remainder is a net winner - it must
    not be counted as a loss just because the SL branch closed it."""
    base = oversold_candles()
    probe = ScalpStrategy(min_tp_usd=0.28, tp_levels=3,
                           value_per_point_per_lot=SPEC.trade_tick_value / SPEC.point)
    signal = probe.generate_signal(base, SPREAD, SPEC.volume_min)
    assert signal.side == "BUY"

    # Replicate run_backtest's own sizing step instead of assuming
    # volume_min: the actual TP ladder is built from the RISK-SIZED lot,
    # which can differ from volume_min once the SL distance changes (as it
    # did when sl_atr_multiple's default moved - this test used to
    # hardcode volume_min here and broke silently on that unrelated change).
    risk = RiskManager(risk_per_trade_usd=1.0, max_daily_loss_usd=10**9,
                        max_daily_drawdown_pct=100.0, max_trades_per_day=1000)
    account = AccountState(balance=50_000.0, equity=50_000.0, free_margin=50_000.0, leverage=100)
    entry_price = base.iloc[-1]["close"] + SPREAD / 2
    sizing = risk.size_position(account, SPEC, signal.sl_distance_price, entry_price)
    assert sizing.ok
    tp1_distance = probe.build_tp_ladder(sizing.lot, SPREAD)[0].distance_price

    # Rally well past TP1 (locks in profit + moves SL to breakeven), then
    # fall back to exactly the entry price (breakeven stop on the rest).
    # The margin here has to clear TWO spread/2 deductions (favorable
    # extreme uses high - spread/2, and the target itself already has a
    # spread buffer baked in) - 0.3 is comfortably more than the 0.125
    # (spread/2) that intrabar accounting subtracts.
    up = entry_price + tp1_distance + 0.3
    candles = _extend(base, [up, up, entry_price, entry_price])

    result = run_backtest(
        candles=candles, spec=SPEC, starting_balance=50_000.0, leverage=100,
        risk_per_trade_usd=1.0, min_tp_usd=0.28, tp_levels=3,
        assumed_spread_price=SPREAD,
    )

    assert result.trades == 1
    assert result.total_pnl > 0, "TP1 banked real profit - the trade must net positive"
    assert result.wins == 1, "a net-positive trade must be counted as a win, not a loss"
    assert result.losses == 0


def test_backtest_time_stop_closes_a_stalled_position_before_the_wide_sl():
    """max_hold_bars: a trade that neither reverts to TP1 nor falls to the
    (wide, 4xATR) stop must be force-closed after N closed bars - and
    counted with its real (small) loss, not the full stop-distance loss."""
    base = oversold_candles()
    probe = ScalpStrategy(min_tp_usd=0.28, tp_levels=3,
                           value_per_point_per_lot=SPEC.trade_tick_value / SPEC.point)
    signal = probe.generate_signal(base, SPREAD, SPEC.volume_min)
    assert signal.side == "BUY"

    entry_price = base.iloc[-1]["close"] + SPREAD / 2
    # Drift very slightly down and STAY there: never reaches TP1, never
    # reaches the ATR stop - without a time-stop this position would sit
    # open to the end of the data.
    stalled = entry_price - 0.4
    candles = _extend(base, [stalled] * 12)

    with_stop = run_backtest(candles=candles, spec=SPEC, starting_balance=50_000.0, leverage=100,
                              risk_per_trade_usd=1.0, min_tp_usd=0.28, tp_levels=3,
                              assumed_spread_price=SPREAD, max_hold_bars=5)
    without = run_backtest(candles=candles, spec=SPEC, starting_balance=50_000.0, leverage=100,
                            risk_per_trade_usd=1.0, min_tp_usd=0.28, tp_levels=3,
                            assumed_spread_price=SPREAD, max_hold_bars=0)

    assert with_stop.trades == 1          # time-stop realized the exit
    assert with_stop.losses == 1
    assert -1.5 < with_stop.total_pnl < 0  # small drift loss, NOT a full stop-out
    assert without.trades == 0            # default off: position still open at data end


def test_backtest_runs_clean_on_real_style_data_without_crashing():
    """Not a profitability assertion - just proves the backtester handles
    a longer, more realistic price path without blowing up (NaNs, index
    errors, etc.), the way a real MT5/Yahoo export would look."""
    import numpy as np
    rng = np.random.default_rng(7)
    closes = [2400.0]
    for _ in range(600):
        closes.append(closes[-1] + rng.normal(0, 0.4))
    from tests.test_strategy import build_candles
    candles = build_candles(closes)

    result = run_backtest(
        candles=candles, spec=SPEC, starting_balance=50_000.0, leverage=100,
        risk_per_trade_usd=1.0, min_tp_usd=0.28, tp_levels=3,
        assumed_spread_price=SPREAD,
    )
    assert result.trades >= 0  # just needs to complete without raising
    assert result.wins + result.losses == result.trades


def test_backtest_accepts_real_style_bid_ask_ticks_for_intrabar_path():
    base = oversold_candles()
    ticks = pd.DataFrame([
        {"time": int(row.time) + 10, "bid": float(row.close) - 0.10,
         "ask": float(row.close) + 0.10, "last": float(row.close), "volume": 1}
        for row in base.itertuples()
    ])
    result = run_backtest(candles=base, ticks=ticks, spec=SPEC,
                          starting_balance=50_000.0, leverage=100,
                          risk_per_trade_usd=1.0, min_tp_usd=0.28, tp_levels=3,
                          assumed_spread_price=SPREAD)
    assert result.trades >= 0


def _real_gold_csv(n=3000):
    path = Path(__file__).resolve().parent.parent / "data" / "gold_m1_7d.csv"
    if not path.exists():
        import pytest
        pytest.skip("data/gold_m1_7d.csv not present in this checkout")
    return pd.read_csv(path).tail(n).reset_index(drop=True)


def _test_spec():
    return SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=100, volume_step=0.01,
                       point=0.01, trade_tick_value=1.0, trade_tick_size=0.01, margin_initial=None)


def _test_settings():
    return Settings(
        mt5_login="1", mt5_password="x", mt5_server="s", mt5_is_demo=True,
        bridge_url="http://127.0.0.1:5001", bridge_timeout_ms=8000,
        symbol="XAUUSD", timeframe="M1", risk_per_trade_usd=6.0,
        max_daily_loss_usd=40.0, max_daily_drawdown_pct=20.0, max_trades_per_day=1000,
        min_tp_usd=0.5, tp_levels=3, dry_run=True, db_path=":memory:",
        strat_enable_ma_grid=True,
    )


def test_backtest_with_precompute_is_fast():
    """3000 real M1 candles must complete in well under the ~180s+ the
    windowed default takes today for the compute_indicators() cost this
    task actually fixes.

    Bound note: with _test_settings()'s strat_enable_ma_grid=True (matching
    the real deployed .env, not the code's bare dataclass default) this
    measures ~20s on this machine, not the "low single digits" originally
    hoped for - profiling shows ~80% of that remaining time is
    detect_regime() in core/regime.py, called from CompositeStrategy's
    extra-strategy branch on the still-windowed 600-bar slice every bar it
    fires (~95% of bars, since the mean-reversion signal rarely fires).
    That is NOT part of this task's fix - see run_backtest's
    precompute_indicators docstring ("Does NOT change: detect_regime()...
    left as a known follow-up") and the spec doc's "Riesgos conocidos".
    Isolated with strat_enable_ma_grid=False (no extra strategy, so the
    regime branch never runs) this same call completes in ~1.1s, confirming
    the actual precompute_indicators mechanism under test here is correct
    and fast; 30s leaves comfortable margin over the observed ~20-21s while
    still failing hard (300x+) if compute_indicators regresses back to the
    O(n * 600) per-bar recompute this task eliminates."""
    candles = _real_gold_csv(3000)
    spec = _test_spec()
    settings = _test_settings()
    value_per_point = spec.trade_tick_value / (spec.trade_tick_size or spec.point)
    strategy = build_strategy_from_settings(settings, value_per_point)

    t0 = time.time()
    result = run_backtest(candles=candles, spec=spec, starting_balance=50, leverage=500,
                           risk_per_trade_usd=6, min_tp_usd=settings.min_tp_usd,
                           tp_levels=settings.tp_levels, assumed_spread_price=0.25,
                           max_trades_per_day=settings.max_trades_per_day,
                           strategy=strategy, precompute_indicators=True)
    elapsed = time.time() - t0
    assert elapsed < 30.0, f"precompute_indicators=True took {elapsed:.1f}s for 3000 bars, expected <30s"
    assert result.trades >= 0  # sanity: it actually ran, not a silent no-op


def test_precompute_matches_live_parity_trade_count():
    """The fast path must produce the same (or near-identical) trades as
    today's windowed default on real data - if this diverges by more than
    a handful of trades, something is wrong with the precompute wiring,
    not an acceptable floating-point difference."""
    candles = _real_gold_csv(3000)
    spec = _test_spec()
    settings = _test_settings()
    value_per_point = spec.trade_tick_value / (spec.trade_tick_size or spec.point)

    kwargs = dict(candles=candles, spec=spec, starting_balance=50, leverage=500,
                  risk_per_trade_usd=6, min_tp_usd=settings.min_tp_usd,
                  tp_levels=settings.tp_levels, assumed_spread_price=0.25,
                  max_trades_per_day=settings.max_trades_per_day)

    baseline = run_backtest(**kwargs, strategy=build_strategy_from_settings(settings, value_per_point),
                             precompute_indicators=False)
    fast = run_backtest(**kwargs, strategy=build_strategy_from_settings(settings, value_per_point),
                         precompute_indicators=True)

    assert abs(fast.trades - baseline.trades) <= 2, (
        f"trade count diverged too much: baseline={baseline.trades} fast={fast.trades}")
