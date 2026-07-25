import time
from pathlib import Path

import pandas as pd

from core.backtest import _build_grid_leg, run_backtest
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
    not an acceptable floating-point difference. 1500 bars (not 3000): the
    O(n*600) windowed baseline path is what's slow here by nature (the
    thing this test exists to compare against), and 1500 already produces
    10 trades with exact baseline/fast parity - plenty to catch a real
    divergence, at under half the wall time (~65s vs ~142s)."""
    candles = _real_gold_csv(1500)
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


def test_backtest_daily_loss_cap_actually_cuts_trades_on_real_multi_day_data():
    """Ronda 41: before this, run_backtest() always built its RiskManager
    with max_daily_loss_usd/max_daily_drawdown_pct hardcoded to effectively
    disabled (10**9 / 100.0), AND RiskManager rolled days off date.today()
    (the wall clock) instead of the data - so a multi-day historical
    backtest never crossed a simulated day boundary and these two circuit
    breakers (the same ones core/engine.py enforces live) could never be
    exercised by a backtest at all. data/gold_m1_7d.csv genuinely spans 7
    calendar days, so this is the real regression test: passing a tight
    max_daily_loss_usd must measurably reduce the trades taken relative to
    leaving it unset, because the cap now actually gets to trip and roll
    across those 7 real days."""
    candles = _real_gold_csv(7839)  # full file: needs multiple real days to prove day-rolling works
    spec = _test_spec()

    kwargs = dict(candles=candles, spec=spec, starting_balance=50_000.0, leverage=500,
                  risk_per_trade_usd=6.0, min_tp_usd=0.5, tp_levels=3,
                  assumed_spread_price=0.25, precompute_indicators=True)

    uncapped = run_backtest(**kwargs)
    capped = run_backtest(**kwargs, max_daily_loss_usd=1.0, max_daily_drawdown_pct=100.0)

    assert uncapped.trades > 0, "sanity: this data must produce trades at all for the comparison to mean anything"
    assert capped.trades < uncapped.trades, (
        f"a $1 daily loss cap over 7 real days should block meaningfully more trades than "
        f"no cap, but capped={capped.trades} uncapped={uncapped.trades}"
    )


def test_backtest_without_daily_limits_matches_pre_ronda41_unbounded_behavior():
    """Leaving max_daily_loss_usd/max_daily_drawdown_pct unset (the default)
    must reproduce the exact old hardcoded-disabled behavior (10**9 USD /
    100% drawdown) bit-for-bit - this is the compatibility guarantee for
    every pre-existing caller/test of run_backtest that doesn't pass them."""
    candles = _real_gold_csv(3000)
    spec = _test_spec()

    kwargs = dict(candles=candles, spec=spec, starting_balance=50_000.0, leverage=500,
                  risk_per_trade_usd=6.0, min_tp_usd=0.5, tp_levels=3,
                  assumed_spread_price=0.25, precompute_indicators=True)

    default = run_backtest(**kwargs)
    explicit_old_hardcode = run_backtest(**kwargs, max_daily_loss_usd=10**9, max_daily_drawdown_pct=100.0)

    assert default == explicit_old_hardcode


def test_risk_per_trade_usd_by_regime_none_matches_flat_risk_behavior():
    """Ronda 46: risk_per_trade_usd_by_regime defaults to None, which must
    reproduce the exact flat risk_per_trade_usd behavior bit-for-bit - the
    compatibility guarantee for every pre-existing caller."""
    candles = _real_gold_csv(3000)
    spec = _test_spec()

    kwargs = dict(candles=candles, spec=spec, starting_balance=100.45, leverage=500,
                  risk_per_trade_usd=5.25, min_tp_usd=0.60, tp_levels=8,
                  assumed_spread_price=0.25, precompute_indicators=True)

    default = run_backtest(**kwargs)
    explicit_none = run_backtest(**kwargs, risk_per_trade_usd_by_regime=None)

    assert default == explicit_none


def test_risk_per_trade_usd_by_regime_never_bypasses_the_five_percent_cap():
    """Even when a regime's nominal risk is set far above what a small
    account can afford, RiskManager's MAX_RISK_FRACTION_OF_BALANCE (5% of
    balance, core/risk_manager.py, untouchable) must still be the real
    ceiling on any trade actually taken - this is the safety invariant
    Ronda 46's implementation note claims but doesn't otherwise verify by
    test: a regime map can only ever be capped DOWN by size_position, never
    let a trade risk more than 5% of the account regardless of the nominal
    dict value requested."""
    candles = _real_gold_csv(3000)
    spec = _test_spec()
    balance = 100.45

    kwargs = dict(candles=candles, spec=spec, starting_balance=balance, leverage=500,
                  risk_per_trade_usd=5.25, min_tp_usd=0.60, tp_levels=8,
                  assumed_spread_price=0.25, precompute_indicators=True)

    # An absurdly high nominal for "trend" (far above 5% of this account's
    # balance) must not translate into a real trade risking anywhere near
    # that much - the cap inside RiskManager.size_position still applies.
    huge_regime_risk = run_backtest(
        **kwargs, risk_per_trade_usd_by_regime={"trend": 500.0, "range": 5.25,
                                                 "volatile": 5.25, "quiet": 5.25})
    flat = run_backtest(**kwargs)

    # The 5% cap means no single trade's risk can exceed roughly
    # balance * 0.05 * MAX_RISK_OVERSHOOT_FACTOR (1.5) regardless of the
    # nominal requested - if the huge regime value leaked through
    # uncapped, max_drawdown_pct would blow far past what the flat run
    # shows on the same data.
    assert huge_regime_risk.max_drawdown_pct <= flat.max_drawdown_pct * 3


# ---------------------------------------------------------------------------
# Ronda 47: multi-position (grid/pyramid + recovery) backtest support.
# core/engine.py's _maybe_add_grid_position had existed since ~Ronda 21 but
# was never exercised by a backtest (Ronda 35 confirmed run_backtest only
# ever tracked a single position). _build_grid_leg is the pure decision
# function factored out of run_backtest's loop specifically so these checks
# don't need to fabricate a real trend/range candle sequence - regime_name
# is supplied directly, exactly like _maybe_add_grid_position receives an
# already-computed `regime` from core/regime.py::detect_regime.
# ---------------------------------------------------------------------------

def _grid_window(n=30, tr=2.0):
    """n closed bars with a constant true range of `tr` (high-low, no gaps)
    so (window["high"] - window["low"]).rolling(14).mean() is deterministic:
    exactly `tr` once warm, matching _build_grid_leg's own ATR proxy."""
    rows = []
    t = 1_700_000_000
    for _ in range(n):
        rows.append({"time": t, "open": 100.0, "high": 100.0 + tr / 2,
                     "low": 100.0 - tr / 2, "close": 100.0})
        t += 60
    return pd.DataFrame(rows)


def _grid_strategy():
    return ScalpStrategy(min_tp_usd=0.28, tp_levels=3, value_per_point_per_lot=SPEC.trade_tick_value / SPEC.point)


def _grid_risk(risk_per_trade_usd=5.0):
    return RiskManager(risk_per_trade_usd=risk_per_trade_usd, max_daily_loss_usd=10**9,
                        max_daily_drawdown_pct=100.0, max_trades_per_day=1000)


def _base_grid_kwargs(**overrides):
    kwargs = dict(
        open_positions=[{"side": "BUY", "entry": 100.0, "grid_level": 0}],
        window=_grid_window(),
        bid=103.0, ask=103.2,  # favorable: +3.0 >= atr(2.0) * grid_step_atr(1.0)
        regime_name="trend",
        risk=_grid_risk(),
        spec=SPEC,
        strategy=_grid_strategy(),
        balance=100_000.0,
        leverage=500,
        assumed_spread_price=SPREAD,
        grid_enabled=True,
        grid_max_positions=3,
        grid_step_atr=1.0,
        grid_lot_multiplier=1.0,
        recovery_enabled=False,
        recovery_max_levels=2,
        recovery_min_regime="range",
        strat_sl_atr_multiple=3.5,
        risk_per_trade_usd=5.0,
    )
    kwargs.update(overrides)
    return kwargs


def test_grid_leg_disabled_returns_none():
    assert _build_grid_leg(**_base_grid_kwargs(grid_enabled=False)) is None


def test_grid_leg_returns_none_without_an_open_basket():
    assert _build_grid_leg(**_base_grid_kwargs(open_positions=[])) is None


def test_grid_leg_returns_none_at_max_positions():
    basket = [{"side": "BUY", "entry": 100.0, "grid_level": 0},
              {"side": "BUY", "entry": 101.0, "grid_level": 1},
              {"side": "BUY", "entry": 102.0, "grid_level": 2}]
    assert _build_grid_leg(**_base_grid_kwargs(open_positions=basket, grid_max_positions=3)) is None


def test_grid_leg_returns_none_on_short_history():
    assert _build_grid_leg(**_base_grid_kwargs(window=_grid_window(n=10))) is None


def test_grid_leg_returns_none_for_unknown_volatile_or_quiet_regime():
    for regime_name in ("unknown", "volatile", "quiet"):
        assert _build_grid_leg(**_base_grid_kwargs(regime_name=regime_name)) is None


def test_grid_leg_returns_none_when_move_is_too_small():
    # +0.5 is well under atr(2.0) * grid_step_atr(1.0) = 2.0 in either direction.
    assert _build_grid_leg(**_base_grid_kwargs(bid=100.5, ask=100.7)) is None


def test_grid_leg_added_on_favorable_move_pyramids_same_side():
    leg = _build_grid_leg(**_base_grid_kwargs())
    assert leg is not None
    assert leg["side"] == "BUY"
    assert leg["grid_level"] == 1
    assert leg["entry"] == 103.2  # ask, direction=BUY
    assert leg["remaining_lot"] > 0


def test_grid_leg_adverse_move_without_recovery_enabled_returns_none():
    adverse = _base_grid_kwargs(bid=97.0, ask=97.2, recovery_enabled=False)
    assert _build_grid_leg(**adverse) is None


def test_grid_leg_adverse_move_in_wrong_regime_returns_none():
    # recovery_min_regime defaults to "range" - "trend" must not qualify,
    # even with recovery_enabled=True (mirrors core/engine.py exactly).
    adverse = _base_grid_kwargs(bid=97.0, ask=97.2, recovery_enabled=True,
                                 regime_name="trend", recovery_min_regime="range")
    assert _build_grid_leg(**adverse) is None


def test_grid_leg_adverse_move_in_matching_regime_adds_recovery_leg():
    adverse = _base_grid_kwargs(bid=97.0, ask=97.2, recovery_enabled=True,
                                 regime_name="range", recovery_min_regime="range")
    leg = _build_grid_leg(**adverse)
    assert leg is not None
    assert leg["grid_level"] == 1
    assert leg["entry"] == 97.2  # ask, direction=BUY


def test_grid_leg_adverse_move_past_recovery_max_levels_returns_none():
    basket = [{"side": "BUY", "entry": 100.0, "grid_level": 0},
              {"side": "BUY", "entry": 99.0, "grid_level": 1}]
    adverse = _base_grid_kwargs(open_positions=basket, bid=97.0, ask=97.2,
                                 recovery_enabled=True, regime_name="range",
                                 recovery_min_regime="range", recovery_max_levels=1,
                                 grid_max_positions=5)
    assert _build_grid_leg(**adverse) is None


def test_grid_leg_favorable_pyramid_ignores_recovery_max_levels():
    """recovery_max_levels only bounds ADVERSE (recovery) legs - a
    favorable pyramid leg is bounded solely by grid_max_positions, exactly
    like core/engine.py's `if level > recovery_max_levels and adverse`."""
    leg = _build_grid_leg(**_base_grid_kwargs(recovery_max_levels=0))
    assert leg is not None


def test_grid_leg_returns_none_when_risk_manager_rejects_sizing():
    # A tiny balance caps risk_budget far below what even the broker's
    # minimum lot needs at this stop distance - RiskManager.size_position
    # must reject it, and _build_grid_leg must surface that as None, not a
    # partially-built leg.
    starved = _base_grid_kwargs(balance=1.0, risk=_grid_risk(risk_per_trade_usd=5.0))
    assert _build_grid_leg(**starved) is None


def test_grid_lot_multiplier_growth_is_capped_by_risk_manager_ceiling():
    """Documents real, faithfully-reproduced core/engine.py behavior, not a
    bug introduced here: RiskManager.size_position's risk_budget_usd can
    only ever SHRINK RiskManager's own self.risk_per_trade_usd, never raise
    it (core/risk_manager.py's own docstring). core/engine.py constructs
    its RiskManager with the flat risk_per_trade_usd (never a ceiling
    raised for grid legs), so a grid_lot_multiplier > 1's nominal budget
    growth (risk_per_trade_usd * multiplier**level) is silently clamped
    right back down to the flat value for every level - the multiplier is
    currently a no-op live unless RiskManager itself is built with a higher
    ceiling, which nothing in this codebase does today."""
    # atr and strat_sl_atr_multiple tuned so the risk-based lot lands away
    # from the volume_min floor (which would mask the clamp either way) -
    # verified by hand: both produce a 0.02 lot regardless of multiplier.
    risk = _grid_risk(risk_per_trade_usd=1.0)  # RiskManager's own ceiling, like engine.py
    tuned = _base_grid_kwargs(window=_grid_window(tr=0.5), strat_sl_atr_multiple=1.0,
                               risk=risk, risk_per_trade_usd=1.0)
    flat = _build_grid_leg(**{**tuned, "grid_lot_multiplier": 1.0})
    grown = _build_grid_leg(**{**tuned, "grid_lot_multiplier": 5.0})
    assert flat is not None and grown is not None
    assert flat["remaining_lot"] == grown["remaining_lot"]


def _ronda43_settings(**overrides):
    """Mirrors the real, currently-deployed .env (Ronda 43): mean_reversion
    + ma_grid, RISK_PER_TRADE_USD=5.25, MIN_TP_USD=0.60, TP_LEVELS=8,
    STRAT_SL_ATR_MULTIPLE=3.5, MAX_DAILY_LOSS_USD=25.0,
    MAX_DAILY_DRAWDOWN_PCT=20.0 - the "combo real" baseline this round's
    task description references (TRAIN +$39.05, TEST +$22.07, Ronda 46)."""
    kwargs = dict(
        mt5_login="1", mt5_password="x", mt5_server="s", mt5_is_demo=True,
        bridge_url="http://127.0.0.1:5001", bridge_timeout_ms=8000,
        symbol="XAUUSD", timeframe="M1", risk_per_trade_usd=5.25,
        max_daily_loss_usd=25.0, max_daily_drawdown_pct=20.0, max_trades_per_day=1000,
        min_tp_usd=0.60, tp_levels=8, dry_run=True, db_path=":memory:",
        strat_sl_atr_multiple=3.5, strat_enable_ma_grid=True,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _combo_real_run(candles, **overrides):
    """Builds a FRESH strategy instance and calls run_backtest exactly
    once. Deliberately does NOT share one strategy object across two
    run_backtest calls the way a naive "build kwargs once, call twice"
    helper would: ScalpStrategy/CompositeStrategy are stateful (cooldown
    counters via on_bar_closed/on_trade_opened), so reusing one already-run
    instance for a second call silently makes the second run start from
    the first run's END state instead of a clean slate - confirmed while
    writing these tests: an early version of this helper built the
    strategy once and made "grid disabled, default vs explicit" runs
    diverge (91 vs 89 trades) purely from that state leak, not from any
    real behavior difference."""
    spec = _test_spec()
    settings = _ronda43_settings()
    value_per_point = spec.trade_tick_value / (spec.trade_tick_size or spec.point)
    strategy = build_strategy_from_settings(settings, value_per_point)
    kwargs = dict(candles=candles, spec=spec, starting_balance=100.45, leverage=500,
                  risk_per_trade_usd=settings.risk_per_trade_usd, min_tp_usd=settings.min_tp_usd,
                  tp_levels=settings.tp_levels, assumed_spread_price=0.25, strategy=strategy,
                  precompute_indicators=True, max_daily_loss_usd=settings.max_daily_loss_usd,
                  max_daily_drawdown_pct=settings.max_daily_drawdown_pct)
    kwargs.update(overrides)
    return run_backtest(**kwargs)


def test_grid_and_recovery_disabled_by_default_matches_explicit_off_on_combo_real():
    """The compatibility guarantee for every pre-existing caller of
    run_backtest (dashboard, scripts, every other test in this file): not
    passing the new Ronda 47 kwargs at all must be bit-for-bit identical to
    passing them explicitly disabled, on the real "combo real" config."""
    candles = _real_gold_csv(3000)

    default = _combo_real_run(candles)
    explicit_off = _combo_real_run(candles, grid_enabled=False, recovery_enabled=False)

    assert default == explicit_off
    assert default.trades > 0  # sanity: this scenario actually trades


def test_recovery_without_grid_enabled_is_a_no_op_like_engine_py():
    """core/engine.py::_maybe_add_grid_position gates recovery INSIDE the
    grid_enabled check - RECOVERY_ENABLED=true alone, without
    GRID_ENABLED=true, does nothing live. Must hold here too."""
    candles = _real_gold_csv(1500)

    baseline = _combo_real_run(candles)
    recovery_only = _combo_real_run(candles, grid_enabled=False, recovery_enabled=True,
                                     recovery_max_levels=5, recovery_min_regime="range")

    assert baseline == recovery_only


def test_grid_enabled_actually_adds_extra_legs_on_real_data():
    """Structural sanity: _build_grid_leg must actually engage on real
    data with a low step threshold, not just be inert/safe - proving the
    wiring in run_backtest's loop (regime lookup, leg append) really works
    end to end, not just in the pure-function unit tests above."""
    candles = _real_gold_csv(3000)

    baseline = _combo_real_run(candles)
    grid = _combo_real_run(candles, grid_enabled=True, grid_max_positions=3, grid_step_atr=0.5)

    assert grid.trades > baseline.trades


def test_grid_and_recovery_at_env_example_default_magnitudes_keep_drawdown_bounded():
    """At the .env.example-documented magnitudes (grid_max_positions=3,
    grid_step_atr=1.0, grid_lot_multiplier=1.0, recovery_max_levels=2),
    turning both flags on keeps max_drawdown_pct in the same ballpark as
    the ungridded baseline on real data - every leg still goes through
    RiskManager.size_position's untouched 5%-of-balance cap exactly like
    every other trade in this file."""
    candles = _real_gold_csv(3000)

    baseline = _combo_real_run(candles)
    grid_and_recovery = _combo_real_run(candles, grid_enabled=True, grid_max_positions=3,
                                         grid_step_atr=1.0, grid_lot_multiplier=1.0,
                                         recovery_enabled=True, recovery_max_levels=2,
                                         recovery_min_regime="range")

    assert grid_and_recovery.max_drawdown_pct <= baseline.max_drawdown_pct * 3


def test_recovery_with_many_levels_compounds_basket_risk_past_a_single_leg_cap():
    """Real, important finding from this round's sweep (see the round
    report) - NOT a bug in this backtest harness, a genuine property of
    core/engine.py's already-implemented _maybe_add_grid_position: every
    leg is independently capped at RiskManager's 5%-of-CURRENT-balance
    ceiling, but a new leg's budget is never reduced for risk ALREADY
    committed by other open legs in the same basket. Pushed past the
    .env.example-documented recovery_max_levels=2 (here: 5, with a tighter
    grid_step_atr so more legs actually fire), several adverse legs can
    stack past what a single 5%-capped trade would ever risk alone -
    verified on real TRAIN data: max_drawdown_pct roughly quadruples versus
    the ungridded baseline and a positive baseline PnL turns negative. This
    is exactly why this round does not flip RECOVERY_ENABLED/GRID_ENABLED
    on by default, and why the second-opinion review this report asks for
    should look specifically at basket-level (not just single-leg) risk
    before either flag is ever turned on for the real account."""
    candles = _real_gold_csv(3000)

    baseline = _combo_real_run(candles)
    heavy_recovery = _combo_real_run(candles, grid_enabled=True, grid_max_positions=5,
                                      grid_step_atr=0.5, grid_lot_multiplier=1.0,
                                      recovery_enabled=True, recovery_max_levels=5,
                                      recovery_min_regime="range")

    assert heavy_recovery.max_drawdown_pct > baseline.max_drawdown_pct * 3
    assert heavy_recovery.total_pnl < baseline.total_pnl
