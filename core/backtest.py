"""Lightweight backtester: replays historical 1m candles through the exact
same ScalpStrategy + RiskManager logic the live engine uses, so you can
sanity-check expectancy before ever touching the demo account. Feed it
real MT5 history (via the bridge's /candles endpoint, saved to CSV) for a
meaningful result - synthetic data only proves the code runs."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from datetime import date

from core.risk_manager import AccountState, RiskManager, SymbolSpec
from core.strategy import ScalpStrategy, compute_indicators


@dataclass
class BacktestResult:
    trades: int
    wins: int
    losses: int
    total_pnl: float
    max_drawdown_pct: float
    final_balance: float
    # Only populated when run_backtest() is called with brain_fn (Ronda 28
    # harness). Zero/zero when brain_fn is None (default, today's behavior
    # unchanged) - kept as trailing defaulted fields so every pre-existing
    # positional/keyword construction of this dataclass keeps working.
    brain_calls: int = 0
    brain_vetoes: int = 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


def _build_grid_leg(
    *,
    open_positions: list[dict],
    window: pd.DataFrame,
    bid: float,
    ask: float,
    regime_name: str,
    risk: RiskManager,
    spec: SymbolSpec,
    strategy: object,
    balance: float,
    leverage: int,
    assumed_spread_price: float,
    grid_enabled: bool,
    grid_max_positions: int,
    grid_step_atr: float,
    grid_lot_multiplier: float,
    recovery_enabled: bool,
    recovery_max_levels: int,
    recovery_min_regime: str,
    strat_sl_atr_multiple: float,
    risk_per_trade_usd: float,
) -> dict | None:
    """Pure decision function - mirrors core/engine.py::
    TradingEngine._maybe_add_grid_position line for line (same checks, same
    order, same math), factored out so it can be unit-tested directly
    without needing to fabricate a real trend/range candle sequence just to
    exercise it (the caller supplies `regime_name` already resolved, exactly
    like `_maybe_add_grid_position` receives `regime = detect_regime(...)`).

    Returns a new leg dict ready to append to `open_positions`, or None if
    no leg should be opened on this bar. Every early-return mirrors an
    early-return in the engine.py original 1:1:
    - grid_enabled off, or no basket to pyramid/recover into.
    - basket already at grid_max_positions.
    - not enough closed history for a stable ATR.
    - regime is unknown/volatile/quiet (grid+recovery are trend/range-only).
    - the move is neither favorable (pyramid) nor adverse (recovery) by at
      least grid_step_atr.
    - an adverse move without recovery_enabled, or in a regime that isn't
      exactly recovery_min_regime.
    - an adverse move past recovery_max_levels.
    - RiskManager.size_position rejects the new leg outright (same 5% of
      balance cap as every other trade in this file - untouched).
    """
    if not grid_enabled or not open_positions:
        return None
    if len(open_positions) >= max(1, grid_max_positions):
        return None
    if len(window) < 30:
        return None
    if regime_name in {"unknown", "volatile", "quiet"}:
        return None

    pos0 = open_positions[0]
    direction = 1 if pos0["side"] == "BUY" else -1
    price = bid if direction == 1 else ask
    tr = (window["high"] - window["low"]).rolling(14).mean()
    last_tr = tr.iloc[-1]
    atr = float(last_tr) if last_tr == last_tr else 0.0  # NaN check (still-warming rolling window)
    if atr <= 0:
        return None

    favorable = direction * (price - pos0["entry"]) >= atr * grid_step_atr
    adverse = not favorable and direction * (price - pos0["entry"]) <= -atr * grid_step_atr
    # Averaging down is explicitly opt-in and only allowed in the configured regime.
    if adverse and (not recovery_enabled or regime_name != recovery_min_regime):
        return None
    if not favorable and not adverse:
        return None

    level = max(p["grid_level"] for p in open_positions) + 1
    if level > recovery_max_levels and adverse:
        return None

    sl_distance = max(atr * strat_sl_atr_multiple, strategy.min_tp_distance_for_lot(spec.volume_min) * 1.5)
    account = AccountState(balance=balance, equity=balance, free_margin=balance, leverage=leverage)
    budget = risk_per_trade_usd * (grid_lot_multiplier ** level)
    sizing = risk.size_position(account, spec, sl_distance, price, risk_budget_usd=budget)
    if not sizing.ok:
        return None

    fill = ask if direction == 1 else bid
    sl = fill - sl_distance if direction == 1 else fill + sl_distance
    tp_levels = strategy.build_tp_ladder(sizing.lot, assumed_spread_price)
    return {
        "side": pos0["side"], "entry": fill, "sl": sl, "tp_levels": tp_levels,
        "next_idx": 0, "remaining_lot": sizing.lot, "orig_lot": sizing.lot,
        "realized_pnl": 0.0, "trail_distance": tp_levels[0].distance_price,
        "trail_active": False, "best_since_be": 0.0, "bars_held": 0, "grid_level": level,
    }


def run_backtest(
    candles: pd.DataFrame,
    spec: SymbolSpec,
    starting_balance: float,
    leverage: int,
    risk_per_trade_usd: float,
    min_tp_usd: float,
    tp_levels: int,
    assumed_spread_price: float,
    max_trades_per_day: int = 1000,
    strategy_overrides: dict | None = None,
    strategy: object | None = None,
    max_lookback_bars: int = 600,
    max_hold_bars: int = 0,
    ticks: pd.DataFrame | None = None,
    precompute_indicators: bool = False,
    brain_fn=None,
    max_daily_loss_usd: float | None = None,
    max_daily_drawdown_pct: float | None = None,
    risk_per_trade_usd_by_regime: dict[str, float] | None = None,
    grid_enabled: bool = False,
    grid_max_positions: int = 3,
    grid_step_atr: float = 1.0,
    grid_lot_multiplier: float = 1.0,
    recovery_enabled: bool = False,
    recovery_max_levels: int = 2,
    recovery_min_regime: str = "range",
) -> BacktestResult:
    """candles: columns open, high, low, close, time (oldest -> newest).
    strategy_overrides passes extra kwargs straight to ScalpStrategy (e.g.
    sl_atr_multiple, trend_filter_adx_threshold) for sweeping parameters
    without a dedicated CLI flag for every single one - ignored if
    `strategy` is given directly (e.g. a CompositeStrategy from
    core.signals.build_strategy_from_settings), which is used as-is.

    max_lookback_bars caps how much history each strategy call sees (same
    role as Settings.candle_history_count in the live engine, same default)
    - this matters for two reasons, not just speed: it keeps backtest and
    live directly comparable (a strategy never sees more history live than
    it did in backtest), and without it CompositeStrategy's M5 resample
    gets recomputed on a window that grows every single bar for the whole
    replay, which is O(n^2) and turns a multi-thousand-bar backtest into a
    multi-minute one for no accuracy benefit (600 bars is already more than
    enough for every indicator used here, including the slowest, EMA50).

    max_hold_bars (0 = off): time-stop for the pre-TP1 phase only. If a
    position has not reached its FIRST take-profit within this many closed
    bars, the mean-reversion thesis is considered expired and the whole
    position is closed at that bar's close (spread-adjusted) instead of
    waiting for the wide ATR-based stop. Targets exactly the documented
    loss profile (see README Rondas 2-6): the few large losers are trades
    that never revert and ride the full 4xATR distance down. Once TP1 has
    hit, the trailing stop already manages the exit and this does nothing.

    precompute_indicators (default False = today's exact "live_parity"
    behavior, unchanged): when True, computes core/strategy.py's
    compute_indicators() ONCE over the full `candles` series before the
    loop (O(n) total) instead of recomputing it from scratch on a
    max_lookback_bars-sized window every single bar (O(n * 600)) - this is
    the fix for the ~120ms/bar cost that made a 3000-bar backtest take 6+
    minutes. Mathematically safe for compute_indicators' rolling/ewm
    columns (ewm(adjust=False) is causal: value at i depends only on data
    <= i - verified with Codex, EMA50's memory of a seed 600 bars back is
    ~1e-11, negligible). When `strategy` has extra sub-strategies with the
    regime filter enabled, also precomputes core/regime.py's
    detect_regime() globally via detect_regime_series() (same O(n) vs
    O(n * 600) argument - verified equivalent to the original windowed
    detect_regime() by tests/test_regime.py's real-data divergence test).
    Does NOT change any extra strategy's own internal M5/M15 resample logic
    (MACrossGridStrategy etc. still see the same windowed `df` as before -
    this is what keeps this change free of look-ahead risk on the
    resample-based strategies).

    brain_fn (default None = today's behavior, unchanged): optional AI-brain
    gate for the Ronda 28 harness (see scripts/count_ai_brain_signals.py's
    Ronda 18/25/26/27 comments in .env.example for why this stayed
    unimplemented until now - it only makes sense once the account's real
    balance makes the risk cap stop rejecting everything). When given, it
    must be a callable with the exact same signature as
    core/ai_brain.py::OpenRouterBrain.evaluate: (candles, side, spread,
    sl_distance) -> an object with an `.allow` bool attribute (AIDecision
    fits directly). It is called ONLY on bars where `strategy` already
    produced a deterministic signal.side (never on every bar - the same
    condition core/engine.py checks before calling the real brain), and
    ALWAYS before risk.size_position - identical order to core/engine.py
    (brain veto first, RiskManager's cap has the final word after, and can
    still reject a signal the brain approved; the brain can never bypass
    it). Pass a mock here to test the wiring for free; swap in a real
    OpenRouterBrain().evaluate only once the account balance justifies the
    paid-call budget (see Ronda 27's balance/RISK_PER_TRADE_USD table).

    max_daily_loss_usd / max_daily_drawdown_pct (default None = today's
    exact behavior, unchanged): before Ronda 41 this function always
    constructed its RiskManager with these hardcoded to 10**9 / 100.0 (i.e.
    effectively disabled), AND RiskManager._roll_day_if_needed compared
    against date.today() (the wall clock) instead of the candles' own
    timestamps - so a multi-day backtest ran entirely inside one wall-clock
    "today" in seconds, and these two circuit breakers (the same ones
    core/engine.py uses live) could never actually be exercised by
    historical data. Now each bar's real date (from candles['time'], epoch
    seconds) is passed into the RiskManager so day-rolls follow the data,
    and passing these two params here lets a backtest actually test them.
    Leaving both None reproduces the old disabled/unbounded behavior
    exactly, so no existing caller changes results.

    risk_per_trade_usd_by_regime (default None = today's exact behavior,
    unchanged - Ronda 46): optional {regime_name: usd} map (regime names
    from core/regime.py::detect_regime, e.g. "trend"/"range"/"volatile"/
    "quiet"/"unknown") to vary the NOMINAL risk budget per trade by the
    market regime in effect on the signal's bar, instead of the single
    fixed risk_per_trade_usd for every trade. A regime missing from the
    dict falls back to risk_per_trade_usd. This only changes what gets
    PROPOSED to RiskManager.size_position's risk_budget_usd argument - the
    untouched MAX_RISK_FRACTION_OF_BALANCE cap in core/risk_manager.py
    (5% of balance) is still applied inside size_position exactly as
    before, so this can only ever be capped down further by that ceiling,
    never bypass or widen it. Implementation note: size_position's
    risk_budget_usd can only ever shrink RiskManager's own
    self.risk_per_trade_usd (min(risk_budget_usd, self.risk_per_trade_usd)
    at core/risk_manager.py line ~209), so when this dict is given the
    RiskManager below is constructed with the CEILING of risk_per_trade_usd
    and every dict value, and the per-bar regime amount is then passed as
    risk_budget_usd on every call - never risk_manager.py itself. Regime is
    read from the same precomputed_regime_series used by the strategy's own
    regime filter when available (precompute_indicators=True); otherwise it
    falls back to a per-bar detect_regime() call (slower, correctness path
    for precompute_indicators=False).

    grid_enabled / grid_max_positions / grid_step_atr / grid_lot_multiplier /
    recovery_enabled / recovery_max_levels / recovery_min_regime (all
    default to today's exact off behavior - Ronda 47): before this,
    core/engine.py::_maybe_add_grid_position (pyramiding into a winning
    position, or - opt-in, range-regime-only - averaging into a losing one)
    had existed since Ronda ~21 but had NEVER been exercised by a backtest,
    because this function only ever tracked a single `open_pos` dict or
    None (confirmed in Ronda 35). This adds real multi-leg support: `open_pos`
    became `open_positions: list[dict]`, and every leg - the original entry
    and every grid/recovery leg - is tracked and managed (SL/TP ladder,
    breakeven, trailing) completely independently, exactly like
    core/engine.py's list of ManagedPosition. The scope stays bounded
    because core/engine.py itself only ever grows ONE basket at a time (all
    legs share the original entry's side): once `open_positions` is
    non-empty, the entry-signal branch is skipped entirely (mirroring
    engine.py's `if self._open_positions: self._maybe_add_grid_position(...);
    return`) - there is never a second, independent, opposite-side position.
    New-leg sizing goes through the exact same risk.size_position(...) as
    every other trade in this file, with risk_budget_usd = risk_per_trade_usd
    * (grid_lot_multiplier ** level) - nominally larger per level, but
    size_position's own ceiling (min(risk_budget_usd, self.risk_per_trade_usd))
    and the untouched 5% MAX_RISK_FRACTION_OF_BALANCE cap in
    core/risk_manager.py both still apply on top, exactly as in live -
    this can only ever shrink a leg's risk, never let the basket exceed what
    a single trade could risk at that balance. Regime for the grid/recovery
    check is read from precomputed_regime_series when available (added to
    the `needs_regime` condition below), else a per-bar detect_regime() call
    - same fallback pattern as risk_per_trade_usd_by_regime above. All seven
    parameters default to grid_enabled=False, which makes `_build_grid_leg`
    return None on its very first check - byte-for-byte the old single
    `open_pos` behavior, verified by
    test_grid_and_recovery_disabled_by_default_matches_explicit_off_on_combo_real
    (tests/test_backtest.py).
    grid_enabled=True/RECOVERY_ENABLED=True are NOT flipped on by default
    anywhere in this codebase by this change - see docs/superpowers/specs/
    2026-07-25-ronda47-grid-recovery-backtest.md for the numeric results
    this harness produced and why they are not (yet) a default-config
    change."""
    tick_size = spec.trade_tick_size or spec.point
    value_per_point_per_lot = spec.trade_tick_value / tick_size
    if strategy is None:
        strategy = ScalpStrategy(min_tp_usd=min_tp_usd, tp_levels=tp_levels,
                                  value_per_point_per_lot=value_per_point_per_lot,
                                  **(strategy_overrides or {}))
    # Ronda 46: risk_per_trade_usd_by_regime can only ever ask for LESS than
    # RiskManager's own risk_per_trade_usd (size_position clamps
    # risk_budget_usd to min(risk_budget_usd, self.risk_per_trade_usd)) - so
    # when regime amounts exceed the flat risk_per_trade_usd, RiskManager
    # must be constructed with the ceiling of all of them, or higher-regime
    # amounts would be silently clamped back down to the flat value. The
    # untouched 5% MAX_RISK_FRACTION_OF_BALANCE cap inside size_position
    # still applies on top of this regardless.
    risk_per_trade_ceiling = risk_per_trade_usd
    if risk_per_trade_usd_by_regime:
        risk_per_trade_ceiling = max(risk_per_trade_usd, *risk_per_trade_usd_by_regime.values())
    risk = RiskManager(
        risk_per_trade_usd=risk_per_trade_ceiling,
        max_daily_loss_usd=max_daily_loss_usd if max_daily_loss_usd is not None else 10**9,
        max_daily_drawdown_pct=max_daily_drawdown_pct if max_daily_drawdown_pct is not None else 100.0,
        max_trades_per_day=max_trades_per_day,
    )

    precomputed_mr = precomputed_composite = None
    precomputed_regime_series = None
    regime_kwargs = getattr(strategy, "_regime_kwargs", {})
    if precompute_indicators:
        mr_strategy = getattr(strategy, "_mean_reversion", strategy)
        precomputed_mr = compute_indicators(
            candles, bb_period=mr_strategy.bb_period, bb_std=mr_strategy.bb_std,
            rsi_period=mr_strategy.rsi_period, atr_period=mr_strategy.atr_period,
            adx_period=mr_strategy.adx_period)
        extra = getattr(strategy, "_extra", None)
        if extra:
            precomputed_composite = compute_indicators(
                candles, rsi_period=strategy._indicator_rsi_period,
                atr_period=strategy._indicator_atr_period)
        needs_regime = (extra and getattr(strategy, "_regime_filter_enabled", False)) or (
            risk_per_trade_usd_by_regime is not None) or grid_enabled
        if needs_regime:
            from core.regime import detect_regime_series, Regime
            precomputed_regime_series = detect_regime_series(candles, **regime_kwargs)

    balance = starting_balance
    peak = starting_balance
    max_dd = 0.0
    trades = wins = losses = 0
    total_pnl = 0.0
    brain_calls = brain_vetoes = 0

    # Ronda 47: a list, not a single dict-or-None - see _build_grid_leg and
    # the docstring section above for why this is safe to widen (every leg
    # shares the original entry's side; a second, independent, opposite-
    # side position is still impossible, exactly like core/engine.py).
    open_positions: list[dict] = []  # each: side, entry, sl, tp_levels(list), next_idx, remaining_lot, orig_lot, grid_level
    # Derived once (constant for the whole run), not per-bar: the same
    # sl_atr_multiple the mean-reversion strategy itself was built with -
    # core/engine.py's grid/recovery leg sizing always uses
    # self.settings.strat_sl_atr_multiple directly (never a per-signal
    # value), and this is exactly what feeds that setting into the
    # strategy object in the first place (core/signals.py
    # build_strategy_from_settings), so reading it back off the strategy
    # keeps the two impossible to drift apart instead of adding an eighth
    # redundant parameter a caller could forget to keep in sync.
    strat_sl_atr_multiple = getattr(strategy, "_mean_reversion", strategy).sl_atr_multiple
    tick_times = None
    if ticks is not None and not ticks.empty and "time" in ticks:
        tick_times = ticks["time"].astype("int64")

    warmup = 25
    for i in range(warmup, len(candles)):
        window = candles.iloc[max(0, i + 1 - max_lookback_bars): i + 1]
        bar = candles.iloc[i]
        mid = bar["close"]
        bid, ask = mid - assumed_spread_price / 2, mid + assumed_spread_price / 2
        # Drives RiskManager's day-roll off this bar's own historical
        # timestamp (see Ronda 41 note in the docstring above) instead of
        # the wall clock, so a multi-day replay actually crosses simulated
        # days the way live trading crosses real ones.
        bar_date = pd.Timestamp(int(bar["time"]), unit="s").date()

        account = AccountState(balance=balance, equity=balance, free_margin=balance, leverage=leverage)

        if open_positions:
            # All legs in `open_positions` share the original entry's side
            # (grid/recovery only ever pyramids/averages the SAME basket -
            # see _build_grid_leg / the docstring above), so direction and
            # the bar's adverse/favorable extremes are computed ONCE per bar
            # and applied to every leg independently below - not
            # recomputed per leg.
            direction = 1 if open_positions[0]["side"] == "BUY" else -1
            # Crossings are checked against the bar's intrabar HIGH/LOW, not
            # just its close - using close-only would let a position ride
            # straight through the stop during a volatile bar and only get
            # marked down on some much worse later close, understating risk
            # by many multiples of the intended per-trade cap. The SIDE that
            # can hurt the trade is checked first (low for a BUY's stop,
            # high for a SELL's), on the conservative assumption that if a
            # single bar's range could have hit both the stop and a TP
            # target, the adverse move happened first.
            # With a supplied MT5 tick stream, use the actual bid/ask path
            # inside this bar. Otherwise retain the conservative OHLC model.
            bar_ticks = None
            if tick_times is not None:
                start_t = int(bar["time"])
                end_t = int(candles.iloc[i + 1]["time"]) if i + 1 < len(candles) else start_t + 60
                bar_ticks = ticks[(tick_times >= start_t) & (tick_times < end_t)]
            if bar_ticks is not None and not bar_ticks.empty:
                bids = pd.to_numeric(bar_ticks.get("bid"), errors="coerce").dropna()
                asks = pd.to_numeric(bar_ticks.get("ask"), errors="coerce").dropna()
                if direction == 1:
                    adverse_extreme = float(bids.min()) if not bids.empty else float(bar["low"] - assumed_spread_price / 2)
                    favorable_extreme = float(bids.max()) if not bids.empty else float(bar["high"] - assumed_spread_price / 2)
                else:
                    adverse_extreme = float(asks.max()) if not asks.empty else float(bar["high"] + assumed_spread_price / 2)
                    favorable_extreme = float(asks.min()) if not asks.empty else float(bar["low"] + assumed_spread_price / 2)
            else:
                adverse_extreme = (bar["low"] - assumed_spread_price / 2) if direction == 1 else (bar["high"] + assumed_spread_price / 2)
                favorable_extreme = (bar["high"] - assumed_spread_price / 2) if direction == 1 else (bar["low"] + assumed_spread_price / 2)

            still_open: list[dict] = []
            for pos in open_positions:
                hit_sl = (direction == 1 and adverse_extreme <= pos["sl"]) or (direction == -1 and adverse_extreme >= pos["sl"])
                if hit_sl:
                    exit_price = pos["sl"]  # filled at the stop level itself; no slippage/gap modeled
                    pnl = direction * (exit_price - pos["entry"]) * value_per_point_per_lot * pos["remaining_lot"]
                    balance += pnl
                    total_pnl += pnl
                    pos["realized_pnl"] += pnl
                    trades += 1
                    # Win/loss is decided by the trade's TOTAL realized pnl, not
                    # just this final slice - a position that banked profit on
                    # TP1/TP2 and then got stopped at breakeven on the remainder
                    # is still a net winner, even though this last slice was ~0.
                    wins += pos["realized_pnl"] > 0
                    losses += pos["realized_pnl"] <= 0
                    risk.register_trade_closed(pnl, balance, current_date=bar_date)
                    continue  # leg fully closed - dropped from still_open

                closed_fully = False
                while pos["next_idx"] < len(pos["tp_levels"]):
                    level = pos["tp_levels"][pos["next_idx"]]
                    target = pos["entry"] + direction * level.distance_price
                    reached = (direction == 1 and favorable_extreme >= target) or (direction == -1 and favorable_extreme <= target)
                    if not reached:
                        break
                    close_lot = min(pos["orig_lot"] * level.close_fraction, pos["remaining_lot"])
                    pnl = direction * (target - pos["entry"]) * value_per_point_per_lot * close_lot
                    balance += pnl
                    total_pnl += pnl
                    pos["realized_pnl"] += pnl
                    pos["remaining_lot"] -= close_lot
                    pos["next_idx"] += 1
                    if pos["next_idx"] == 1:
                        pos["sl"] = pos["entry"]
                        pos["trail_active"] = True
                        pos["best_since_be"] = target
                    if pos["remaining_lot"] <= 1e-9:
                        trades += 1
                        wins += pos["realized_pnl"] > 0
                        losses += pos["realized_pnl"] <= 0
                        risk.register_trade_closed(pnl, balance, current_date=bar_date)  # this slice only, matches engine.py's per-fill registration
                        closed_fully = True
                        break

                if closed_fully:
                    continue  # leg fully closed - dropped from still_open

                # Trail the stop using this bar's favorable extreme, AFTER
                # TP fills are resolved - so a bar can't ratchet the stop
                # tighter and then "immediately" stop itself out on the same
                # bar's move, which we have no intrabar ordering evidence for.
                if pos["trail_active"]:
                    if direction == 1:
                        pos["best_since_be"] = max(pos["best_since_be"], favorable_extreme)
                        candidate_sl = pos["best_since_be"] - pos["trail_distance"]
                        if candidate_sl > pos["sl"]:
                            pos["sl"] = candidate_sl
                    else:
                        pos["best_since_be"] = min(pos["best_since_be"], favorable_extreme)
                        candidate_sl = pos["best_since_be"] + pos["trail_distance"]
                        if candidate_sl < pos["sl"]:
                            pos["sl"] = candidate_sl

                # Time-stop (see docstring): only pre-TP1, only after SL/TP
                # for this bar are fully resolved (an actual hit this bar
                # takes precedence over expiring the thesis).
                pos["bars_held"] += 1
                if (max_hold_bars > 0 and pos["next_idx"] == 0
                        and pos["bars_held"] >= max_hold_bars):
                    exit_price = (bar["close"] - assumed_spread_price / 2 if direction == 1
                                  else bar["close"] + assumed_spread_price / 2)
                    pnl = direction * (exit_price - pos["entry"]) * value_per_point_per_lot * pos["remaining_lot"]
                    balance += pnl
                    total_pnl += pnl
                    pos["realized_pnl"] += pnl
                    trades += 1
                    wins += pos["realized_pnl"] > 0
                    losses += pos["realized_pnl"] <= 0
                    risk.register_trade_closed(pnl, balance, current_date=bar_date)
                    continue  # leg fully closed by the time-stop - dropped from still_open

                still_open.append(pos)

            open_positions = still_open

        peak = max(peak, balance)
        max_dd = max(max_dd, (peak - balance) / peak * 100 if peak else 0)

        strategy.on_bar_closed()
        can_trade, _ = risk.can_open_new_trade(balance, current_date=bar_date)
        if open_positions and can_trade:
            # Optional bounded basket management (Ronda 47) - mirrors
            # core/engine.py's step(): once a basket is open, this bar can
            # only ever grow it (pyramid/recovery leg), never start an
            # unrelated new entry. _build_grid_leg's own first check
            # (`not grid_enabled`) makes this a no-op whenever grid_enabled
            # is False (today's default), so this branch changes nothing
            # about pre-Ronda-47 behavior on its own.
            regime_name = "unknown"
            if grid_enabled:
                if precomputed_regime_series is not None:
                    regime_name = precomputed_regime_series.iloc[i]["name"]
                else:
                    from core.regime import detect_regime
                    regime_name = detect_regime(window, **regime_kwargs).name
            new_leg = _build_grid_leg(
                open_positions=open_positions, window=window, bid=bid, ask=ask,
                regime_name=regime_name, risk=risk, spec=spec, strategy=strategy,
                balance=balance, leverage=leverage, assumed_spread_price=assumed_spread_price,
                grid_enabled=grid_enabled, grid_max_positions=grid_max_positions,
                grid_step_atr=grid_step_atr, grid_lot_multiplier=grid_lot_multiplier,
                recovery_enabled=recovery_enabled, recovery_max_levels=recovery_max_levels,
                recovery_min_regime=recovery_min_regime, strat_sl_atr_multiple=strat_sl_atr_multiple,
                risk_per_trade_usd=risk_per_trade_usd,
            )
            if new_leg is not None:
                open_positions.append(new_leg)
        elif not open_positions and can_trade:
            if precompute_indicators:
                mr_slice = precomputed_mr.iloc[max(0, i - 2): i + 1]
                if precomputed_composite is not None:
                    composite_slice = precomputed_composite.iloc[max(0, i - 2): i + 1]
                    regime_row = None
                    if precomputed_regime_series is not None:
                        r = precomputed_regime_series.iloc[i]
                        from core.regime import Regime
                        regime_row = Regime(name=r["name"], adx=float(r["adx"]), atr_ratio=float(r["atr_ratio"]), trend=r["trend"])
                    signal = strategy.generate_signal(window, assumed_spread_price, spec.volume_min,
                                                       precomputed_mr_indicators=mr_slice,
                                                       precomputed_composite_indicators=composite_slice,
                                                       precomputed_regime=regime_row)
                elif hasattr(strategy, "_mean_reversion"):
                    signal = strategy.generate_signal(window, assumed_spread_price, spec.volume_min,
                                                       precomputed_mr_indicators=mr_slice)
                else:
                    signal = strategy.generate_signal(window, assumed_spread_price, spec.volume_min,
                                                        precomputed_indicators=mr_slice)
            else:
                signal = strategy.generate_signal(window, assumed_spread_price, spec.volume_min)
            if signal.side:
                brain_ok = True
                if brain_fn is not None:
                    # Same order as core/engine.py: the brain is consulted
                    # ONLY now that a deterministic signal exists, and its
                    # veto is checked BEFORE risk.size_position runs -
                    # size_position below still applies the untouched 5%
                    # cap regardless of what the brain decided, so a brain
                    # "allow" can never bypass RiskManager.
                    decision = brain_fn(window, signal.side, assumed_spread_price, signal.sl_distance_price)
                    brain_calls += 1
                    if not decision.allow:
                        brain_vetoes += 1
                        brain_ok = False
                risk_budget_usd = None
                if risk_per_trade_usd_by_regime is not None:
                    if precomputed_regime_series is not None:
                        bar_regime_name = precomputed_regime_series.iloc[i]["name"]
                    else:
                        from core.regime import detect_regime
                        bar_regime_name = detect_regime(window, **regime_kwargs).name
                    risk_budget_usd = risk_per_trade_usd_by_regime.get(bar_regime_name, risk_per_trade_usd)
                sizing = risk.size_position(account, spec, signal.sl_distance_price, mid,
                                             risk_budget_usd=risk_budget_usd) if brain_ok else None
                if sizing is not None and sizing.ok:
                    entry = ask if signal.side == "BUY" else bid
                    if tick_times is not None:
                        start_t = int(bar["time"])
                        end_t = int(candles.iloc[i + 1]["time"]) if i + 1 < len(candles) else start_t + 60
                        entry_ticks = ticks[(tick_times >= start_t) & (tick_times < end_t)]
                        if not entry_ticks.empty:
                            field = "ask" if signal.side == "BUY" else "bid"
                            values = pd.to_numeric(entry_ticks.get(field), errors="coerce").dropna()
                            if not values.empty:
                                entry = float(values.iloc[-1])
                    direction = 1 if signal.side == "BUY" else -1
                    sl = entry - direction * signal.sl_distance_price
                    tp_levels = strategy.build_tp_ladder(sizing.lot, assumed_spread_price, vol_ratio=signal.vol_ratio)
                    open_positions.append({"side": signal.side, "entry": entry, "sl": sl, "tp_levels": tp_levels,
                                "next_idx": 0, "remaining_lot": sizing.lot, "orig_lot": sizing.lot,
                                "realized_pnl": 0.0, "trail_distance": tp_levels[0].distance_price,
                                "trail_active": False, "best_since_be": 0.0, "bars_held": 0, "grid_level": 0})
                    strategy.on_trade_opened()

    return BacktestResult(trades=trades, wins=wins, losses=losses, total_pnl=total_pnl,
                           max_drawdown_pct=max_dd, final_balance=balance,
                           brain_calls=brain_calls, brain_vetoes=brain_vetoes)
