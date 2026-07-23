from core.risk_manager import AccountState, RiskManager, SymbolSpec

STANDARD_SPEC = SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=1.0,
                            volume_step=0.01, point=0.01, trade_tick_value=1.0)


def make_risk(**overrides):
    defaults = {"risk_per_trade_usd": 1.0, "max_daily_loss_usd": 8.0,
                "max_daily_drawdown_pct": 20.0, "max_trades_per_day": 1000}
    defaults.update(overrides)
    return RiskManager(**defaults)


def test_sizing_rejects_when_margin_insufficient_at_1to1_leverage():
    """This is the exact real-world constraint the account this bot targets
    runs into: $50 balance, 1:1 leverage, standard 100oz XAUUSD contract."""
    rm = make_risk()
    account = AccountState(balance=50.0, equity=50.0, free_margin=50.0, leverage=1)
    result = rm.size_position(account, STANDARD_SPEC, sl_distance_price=0.4, current_price=2400.0)
    assert result.ok is False
    assert "insuficiente" in result.reason.lower()


def test_sizing_uses_broker_reported_margin_over_account_leverage_guess():
    """This is the actual real-world fix: FBS pins metals to a fixed 1:500
    leverage independent of the account's forex leverage - the account
    can report leverage=1 (as the target demo account does) while gold
    itself margins at 1:500. Once the broker tells us the real per-lot
    margin (via order_calc_margin, surfaced as margin_initial), sizing
    must use THAT instead of deriving a wildly wrong number from
    account.leverage."""
    rm = make_risk()
    account = AccountState(balance=50.0, equity=50.0, free_margin=50.0, leverage=1)
    # ~$8 margin per 1.0 lot at 1:500 on ~$4000/oz gold - what order_calc_margin
    # would actually return, vs. contract_size*price/leverage=1 which would
    # demand ~$4000 for even 0.01 lot and reject every trade outright.
    spec_with_real_margin = SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=1.0,
                                        volume_step=0.01, point=0.01, trade_tick_value=1.0,
                                        margin_initial=8.13)
    result = rm.size_position(account, spec_with_real_margin, sl_distance_price=1.0, current_price=4000.0)
    assert result.ok is True
    assert result.lot >= spec_with_real_margin.volume_min


def test_sizing_succeeds_with_realistic_leverage():
    rm = make_risk()
    account = AccountState(balance=50.0, equity=50.0, free_margin=50.0, leverage=100)
    result = rm.size_position(account, STANDARD_SPEC, sl_distance_price=0.4, current_price=2400.0)
    assert result.ok is True
    assert result.lot >= STANDARD_SPEC.volume_min
    assert result.est_margin_usd <= account.free_margin * RiskManager.MAX_MARGIN_USAGE_FRACTION + 1e-9


def test_sizing_respects_volume_step_and_max():
    rm = make_risk(risk_per_trade_usd=1000.0)  # deliberately huge, should clamp to volume_max
    account = AccountState(balance=100_000.0, equity=100_000.0, free_margin=100_000.0, leverage=100)
    result = rm.size_position(account, STANDARD_SPEC, sl_distance_price=0.4, current_price=2400.0)
    assert result.ok is True
    assert result.lot == STANDARD_SPEC.volume_max


def test_sizing_rejects_when_min_lot_risk_far_exceeds_budget_during_high_volatility():
    """Reproduces a real bug found while backtesting against real market
    data: during a volatility spike the ATR-based SL distance widens a lot,
    so the theoretically risk-correct lot shrinks below the broker's
    minimum. Silently trading volume_min anyway let a $1 risk budget turn
    into a real $16 loss on one trade - a sequence of those wiped a full
    $50 account in a few hours in the backtest. Must reject instead."""
    rm = make_risk(risk_per_trade_usd=1.0)
    account = AccountState(balance=50.0, equity=50.0, free_margin=50.0, leverage=500)
    # sl_distance=16 is exactly the width seen in the real trade that
    # produced the -$16.00 loss.
    result = rm.size_position(account, STANDARD_SPEC, sl_distance_price=16.0, current_price=4158.67)
    assert result.ok is False
    assert "volatilidad" in result.reason.lower()


def test_sizing_caps_risk_budget_to_a_fraction_of_balance_on_small_accounts():
    """Ronda 13: risk_per_trade_usd is a fixed dollar amount, but on a
    small account a "reasonable-looking" fixed number can quietly be a
    huge fraction of total capital. Reproduced directly via backtest on
    real recent market data (same strategy, same 3000-bar window, only
    risk_per_trade_usd changed): $3 (6% of a $50 balance) -> -$0.91 PnL,
    13.1% max drawdown. $6 (12% of the same $50 balance) -> -$39.99 PnL,
    81.5% max drawdown - the account was nearly wiped by doubling the
    per-trade dollar risk on the exact same signals. A fixed dollar
    config can't adapt as balance shrinks after losses either - the
    account that hit this in production is now ~$16, where even the
    original $3 would be ~19% of balance per trade. size_position must
    cap the EFFECTIVE risk budget to a fraction of the CURRENT balance,
    regardless of the configured risk_per_trade_usd, so a large fixed
    number stays sane on the small account it's actually protecting
    today, not just the larger account it might have been sized for
    originally."""
    rm = make_risk(risk_per_trade_usd=6.0)
    # margin_initial set low and deliberately generous so margin is never
    # the binding constraint here - this test isolates the risk-BUDGET
    # cap specifically, not margin sizing (a real earlier mistake caught
    # while writing this test: with STANDARD_SPEC's leverage-derived
    # margin estimate, margin bound the lot to volume_min regardless of
    # risk_per_trade_usd, making the risk-budget cap untestable through
    # it - confirmed uncapped risk-budget sizing here is 0.15 lot before
    # any balance-fraction cap is applied).
    spec = SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=1.0,
                       volume_step=0.01, point=0.01, trade_tick_value=1.0,
                       margin_initial=0.5)
    account = AccountState(balance=50.0, equity=50.0, free_margin=50.0, leverage=500)
    result = rm.size_position(account, spec, sl_distance_price=0.4, current_price=2400.0)
    assert result.ok is True
    uncapped_lot = rm._round_to_step(6.0 / (0.4 * 1.0 / 0.01), spec.volume_step)
    assert uncapped_lot == 0.15, f"sanity check on the test's own math failed: {uncapped_lot}"
    # lot sized off the capped budget (balance * MAX_RISK_FRACTION_OF_BALANCE),
    # not the full $6 - i.e. strictly less than what $6 alone would size.
    assert result.lot < uncapped_lot, (
        f"expected the balance cap to shrink the lot below the uncapped ${6.0} sizing "
        f"({uncapped_lot}), got {result.lot} - the balance-fraction cap isn't being applied"
    )


def test_sizing_balance_cap_does_not_affect_already_conservative_risk_settings():
    """The cap must be a ceiling, not a new floor or a universal shrink -
    a risk_per_trade_usd that's already well within the balance fraction
    (e.g. institutional-style 1% risk on a well-capitalized account) must
    size exactly as before, uncapped."""
    rm = make_risk(risk_per_trade_usd=50.0)  # 1% of a 5,000 balance
    account = AccountState(balance=5_000.0, equity=5_000.0, free_margin=5_000.0, leverage=500)
    result = rm.size_position(account, STANDARD_SPEC, sl_distance_price=0.4, current_price=2400.0)
    assert result.ok is True
    uncapped_lot = rm._round_to_step(50.0 / (0.4 * 1.0 / 0.01), STANDARD_SPEC.volume_step)
    assert result.lot == min(uncapped_lot, STANDARD_SPEC.volume_max)


def test_sizing_tolerates_small_rounding_overshoot_near_the_risk_budget():
    """The floor/rounding to the broker's lot step is expected to overshoot
    the exact risk budget a little - only a LARGE overshoot (the bug above)
    should be rejected."""
    rm = make_risk(risk_per_trade_usd=1.0)
    account = AccountState(balance=50_000.0, equity=50_000.0, free_margin=50_000.0, leverage=500)
    # risk-correct lot would be ~0.0125; rounds up to 0.01 min step's
    # neighbor and overshoots only modestly, well inside tolerance.
    result = rm.size_position(account, STANDARD_SPEC, sl_distance_price=0.8, current_price=4000.0)
    assert result.ok is True


def test_sizing_uses_trade_tick_size_not_display_point():
    rm = make_risk(risk_per_trade_usd=1.0)
    account = AccountState(balance=1_000.0, equity=1_000.0, free_margin=1_000.0, leverage=500)
    # $1 is paid per $0.10 tick, so a $1 stop costs $10/lot. Treating the
    # display point (0.01) as the tick would overstate risk by 10x.
    spec = SymbolSpec(100.0, 0.01, 10.0, 0.01, 0.01, 1.0, trade_tick_size=0.10)
    result = rm.size_position(account, spec, sl_distance_price=1.0, current_price=4000.0)
    assert result.ok is True
    assert result.lot == 0.1


def test_supervisor_risk_budget_can_only_reduce_position_size():
    rm = make_risk(risk_per_trade_usd=6.0)
    account = AccountState(balance=1000, equity=1000, free_margin=1000, leverage=500)
    full = rm.size_position(account, STANDARD_SPEC, 1.0, 4000, risk_budget_usd=6.0)
    reduced = rm.size_position(account, STANDARD_SPEC, 1.0, 4000, risk_budget_usd=1.5)
    attempted_increase = rm.size_position(account, STANDARD_SPEC, 1.0, 4000, risk_budget_usd=60.0)
    assert full.ok and reduced.ok and attempted_increase.ok
    assert reduced.lot < full.lot
    assert attempted_increase.lot == full.lot


def test_sizing_rejects_zero_or_negative_sl_distance():
    rm = make_risk()
    account = AccountState(balance=50.0, equity=50.0, free_margin=50.0, leverage=100)
    result = rm.size_position(account, STANDARD_SPEC, sl_distance_price=0.0, current_price=2400.0)
    assert result.ok is False


def test_daily_trade_cap_blocks_further_trading():
    rm = make_risk(max_trades_per_day=2)
    rm.register_trade_closed(0.3, balance_after=50.3)
    rm.register_trade_closed(0.3, balance_after=50.6)
    can_trade, reason = rm.can_open_new_trade(50.6)
    assert can_trade is False
    assert "trades" in reason.lower()


def test_daily_loss_cap_blocks_further_trading():
    rm = make_risk(max_daily_loss_usd=2.0)
    rm.register_trade_closed(-2.5, balance_after=47.5)
    can_trade, reason = rm.can_open_new_trade(47.5)
    assert can_trade is False
    assert "perdida" in reason.lower()


def test_daily_drawdown_cap_blocks_further_trading():
    rm = make_risk(max_daily_drawdown_pct=5.0, max_daily_loss_usd=1000.0)
    can_trade, _ = rm.can_open_new_trade(50.0)  # establishes start-of-day balance
    assert can_trade is True
    can_trade, reason = rm.can_open_new_trade(47.0)  # 6% down
    assert can_trade is False
    assert "drawdown" in reason.lower()


def test_trading_allowed_within_all_limits():
    rm = make_risk()
    can_trade, reason = rm.can_open_new_trade(50.0)
    assert can_trade is True
    assert reason == "ok"


def test_equity_drawdown_breaker_trips_on_floating_loss_alone():
    """The whole point of this check: it must fire from EQUITY (floating
    loss on a still-open position) without any trade ever having closed -
    can_open_new_trade's balance-based check can't see this at all."""
    rm = make_risk(max_daily_drawdown_pct=10.0)
    rm.check_equity_drawdown(50.0)  # establishes start-of-day equity baseline
    breached, reason = rm.check_equity_drawdown(50.0)
    assert breached is False

    breached, reason = rm.check_equity_drawdown(44.0)  # 12% floating drawdown, nothing realized
    assert breached is True
    assert "drawdown" in reason.lower()


def test_equity_drawdown_breaker_does_not_trip_within_limit():
    rm = make_risk(max_daily_drawdown_pct=10.0)
    rm.check_equity_drawdown(50.0)
    breached, _ = rm.check_equity_drawdown(46.0)  # 8%, under the 10% limit
    assert breached is False


def test_equity_and_balance_drawdown_share_the_same_daily_baseline():
    """Whichever check runs first each day sets the start-of-day anchor;
    the other must respect it instead of resetting it."""
    rm = make_risk(max_daily_drawdown_pct=10.0)
    can_trade, _ = rm.can_open_new_trade(50.0)  # balance check runs first today
    assert can_trade is True
    breached, _ = rm.check_equity_drawdown(44.0)  # 12% off the SAME baseline
    assert breached is True
