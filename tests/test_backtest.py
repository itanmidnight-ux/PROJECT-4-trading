import pandas as pd

from core.backtest import run_backtest
from core.risk_manager import AccountState, RiskManager, SymbolSpec
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
