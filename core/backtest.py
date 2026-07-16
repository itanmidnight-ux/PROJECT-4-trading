"""Lightweight backtester: replays historical 1m candles through the exact
same ScalpStrategy + RiskManager logic the live engine uses, so you can
sanity-check expectancy before ever touching the demo account. Feed it
real MT5 history (via the bridge's /candles endpoint, saved to CSV) for a
meaningful result - synthetic data only proves the code runs."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.risk_manager import AccountState, RiskManager, SymbolSpec
from core.strategy import ScalpStrategy


@dataclass
class BacktestResult:
    trades: int
    wins: int
    losses: int
    total_pnl: float
    max_drawdown_pct: float
    final_balance: float

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
    enough for every indicator used here, including the slowest, EMA50)."""
    value_per_point_per_lot = spec.trade_tick_value / spec.point
    if strategy is None:
        strategy = ScalpStrategy(min_tp_usd=min_tp_usd, tp_levels=tp_levels,
                                  value_per_point_per_lot=value_per_point_per_lot,
                                  **(strategy_overrides or {}))
    risk = RiskManager(risk_per_trade_usd=risk_per_trade_usd, max_daily_loss_usd=10**9,
                        max_daily_drawdown_pct=100.0, max_trades_per_day=max_trades_per_day)

    balance = starting_balance
    peak = starting_balance
    max_dd = 0.0
    trades = wins = losses = 0
    total_pnl = 0.0

    open_pos = None  # dict: side, entry, sl, tp_levels(list), next_idx, remaining_lot, orig_lot

    warmup = 25
    for i in range(warmup, len(candles)):
        window = candles.iloc[max(0, i + 1 - max_lookback_bars): i + 1]
        bar = candles.iloc[i]
        mid = bar["close"]
        bid, ask = mid - assumed_spread_price / 2, mid + assumed_spread_price / 2

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
                risk.register_trade_closed(pnl, balance)
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
                        risk.register_trade_closed(pnl, balance)  # this slice only, matches engine.py's per-fill registration
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

        peak = max(peak, balance)
        max_dd = max(max_dd, (peak - balance) / peak * 100 if peak else 0)

        strategy.on_bar_closed()
        can_trade, _ = risk.can_open_new_trade(balance)
        if open_pos is None and can_trade:
            signal = strategy.generate_signal(window, assumed_spread_price, spec.volume_min)
            if signal.side:
                sizing = risk.size_position(account, spec, signal.sl_distance_price, mid)
                if sizing.ok:
                    entry = ask if signal.side == "BUY" else bid
                    direction = 1 if signal.side == "BUY" else -1
                    sl = entry - direction * signal.sl_distance_price
                    tp_levels = strategy.build_tp_ladder(sizing.lot, assumed_spread_price, vol_ratio=signal.vol_ratio)
                    open_pos = {"side": signal.side, "entry": entry, "sl": sl, "tp_levels": tp_levels,
                                "next_idx": 0, "remaining_lot": sizing.lot, "orig_lot": sizing.lot,
                                "realized_pnl": 0.0, "trail_distance": tp_levels[0].distance_price,
                                "trail_active": False, "best_since_be": 0.0}
                    strategy.on_trade_opened()

    return BacktestResult(trades=trades, wins=wins, losses=losses, total_pnl=total_pnl,
                           max_drawdown_pct=max_dd, final_balance=balance)
