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
    exactly, so no existing caller changes results."""
    tick_size = spec.trade_tick_size or spec.point
    value_per_point_per_lot = spec.trade_tick_value / tick_size
    if strategy is None:
        strategy = ScalpStrategy(min_tp_usd=min_tp_usd, tp_levels=tp_levels,
                                  value_per_point_per_lot=value_per_point_per_lot,
                                  **(strategy_overrides or {}))
    risk = RiskManager(
        risk_per_trade_usd=risk_per_trade_usd,
        max_daily_loss_usd=max_daily_loss_usd if max_daily_loss_usd is not None else 10**9,
        max_daily_drawdown_pct=max_daily_drawdown_pct if max_daily_drawdown_pct is not None else 100.0,
        max_trades_per_day=max_trades_per_day,
    )

    precomputed_mr = precomputed_composite = None
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
        precomputed_regime_series = None
        if extra and getattr(strategy, "_regime_filter_enabled", False):
            from core.regime import detect_regime_series, Regime
            regime_kwargs = getattr(strategy, "_regime_kwargs", {})
            precomputed_regime_series = detect_regime_series(candles, **regime_kwargs)

    balance = starting_balance
    peak = starting_balance
    max_dd = 0.0
    trades = wins = losses = 0
    total_pnl = 0.0
    brain_calls = brain_vetoes = 0

    open_pos = None  # dict: side, entry, sl, tp_levels(list), next_idx, remaining_lot, orig_lot
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

        if open_pos:
            direction = 1 if open_pos["side"] == "BUY" else -1
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

            hit_sl = (direction == 1 and adverse_extreme <= open_pos["sl"]) or (direction == -1 and adverse_extreme >= open_pos["sl"])
            if hit_sl:
                exit_price = open_pos["sl"]  # filled at the stop level itself; no slippage/gap modeled
                pnl = direction * (exit_price - open_pos["entry"]) * value_per_point_per_lot * open_pos["remaining_lot"]
                balance += pnl
                total_pnl += pnl
                open_pos["realized_pnl"] += pnl
                trades += 1
                # Win/loss is decided by the trade's TOTAL realized pnl, not
                # just this final slice - a position that banked profit on
                # TP1/TP2 and then got stopped at breakeven on the remainder
                # is still a net winner, even though this last slice was ~0.
                wins += open_pos["realized_pnl"] > 0
                losses += open_pos["realized_pnl"] <= 0
                risk.register_trade_closed(pnl, balance, current_date=bar_date)
                open_pos = None
            else:
                while open_pos and open_pos["next_idx"] < len(open_pos["tp_levels"]):
                    level = open_pos["tp_levels"][open_pos["next_idx"]]
                    target = open_pos["entry"] + direction * level.distance_price
                    reached = (direction == 1 and favorable_extreme >= target) or (direction == -1 and favorable_extreme <= target)
                    if not reached:
                        break
                    close_lot = min(open_pos["orig_lot"] * level.close_fraction, open_pos["remaining_lot"])
                    pnl = direction * (target - open_pos["entry"]) * value_per_point_per_lot * close_lot
                    balance += pnl
                    total_pnl += pnl
                    open_pos["realized_pnl"] += pnl
                    open_pos["remaining_lot"] -= close_lot
                    open_pos["next_idx"] += 1
                    if open_pos["next_idx"] == 1:
                        open_pos["sl"] = open_pos["entry"]
                        open_pos["trail_active"] = True
                        open_pos["best_since_be"] = target
                    if open_pos["remaining_lot"] <= 1e-9:
                        trades += 1
                        wins += open_pos["realized_pnl"] > 0
                        losses += open_pos["realized_pnl"] <= 0
                        risk.register_trade_closed(pnl, balance, current_date=bar_date)  # this slice only, matches engine.py's per-fill registration
                        open_pos = None
                        break

                # Trail the stop using this bar's favorable extreme, AFTER
                # TP fills are resolved - so a bar can't ratchet the stop
                # tighter and then "immediately" stop itself out on the same
                # bar's move, which we have no intrabar ordering evidence for.
                if open_pos and open_pos["trail_active"]:
                    if direction == 1:
                        open_pos["best_since_be"] = max(open_pos["best_since_be"], favorable_extreme)
                        candidate_sl = open_pos["best_since_be"] - open_pos["trail_distance"]
                        if candidate_sl > open_pos["sl"]:
                            open_pos["sl"] = candidate_sl
                    else:
                        open_pos["best_since_be"] = min(open_pos["best_since_be"], favorable_extreme)
                        candidate_sl = open_pos["best_since_be"] + open_pos["trail_distance"]
                        if candidate_sl < open_pos["sl"]:
                            open_pos["sl"] = candidate_sl

                # Time-stop (see docstring): only pre-TP1, only after SL/TP
                # for this bar are fully resolved (an actual hit this bar
                # takes precedence over expiring the thesis).
                if open_pos is not None:
                    open_pos["bars_held"] += 1
                    if (max_hold_bars > 0 and open_pos["next_idx"] == 0
                            and open_pos["bars_held"] >= max_hold_bars):
                        exit_price = (bar["close"] - assumed_spread_price / 2 if direction == 1
                                      else bar["close"] + assumed_spread_price / 2)
                        pnl = direction * (exit_price - open_pos["entry"]) * value_per_point_per_lot * open_pos["remaining_lot"]
                        balance += pnl
                        total_pnl += pnl
                        open_pos["realized_pnl"] += pnl
                        trades += 1
                        wins += open_pos["realized_pnl"] > 0
                        losses += open_pos["realized_pnl"] <= 0
                        risk.register_trade_closed(pnl, balance, current_date=bar_date)
                        open_pos = None

        peak = max(peak, balance)
        max_dd = max(max_dd, (peak - balance) / peak * 100 if peak else 0)

        strategy.on_bar_closed()
        can_trade, _ = risk.can_open_new_trade(balance, current_date=bar_date)
        if open_pos is None and can_trade:
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
                sizing = risk.size_position(account, spec, signal.sl_distance_price, mid) if brain_ok else None
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
                    open_pos = {"side": signal.side, "entry": entry, "sl": sl, "tp_levels": tp_levels,
                                "next_idx": 0, "remaining_lot": sizing.lot, "orig_lot": sizing.lot,
                                "realized_pnl": 0.0, "trail_distance": tp_levels[0].distance_price,
                                "trail_active": False, "best_since_be": 0.0, "bars_held": 0}
                    strategy.on_trade_opened()

    return BacktestResult(trades=trades, wins=wins, losses=losses, total_pnl=total_pnl,
                           max_drawdown_pct=max_dd, final_balance=balance,
                           brain_calls=brain_calls, brain_vetoes=brain_vetoes)
