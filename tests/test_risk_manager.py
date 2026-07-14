from core.risk_manager import AccountState, RiskManager, SymbolSpec

STANDARD_SPEC = SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=1.0,
                            volume_step=0.01, point=0.01, trade_tick_value=1.0)


def make_risk(**overrides):
    defaults = dict(risk_per_trade_usd=1.0, max_daily_loss_usd=8.0,
                     max_daily_drawdown_pct=20.0, max_trades_per_day=1000)
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
