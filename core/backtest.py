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
) -> BacktestResult:
    """candles: columns open, high, low, close, time (oldest -> newest)."""
    value_per_point_per_lot = spec.trade_tick_value / spec.point
    strategy = ScalpStrategy(min_tp_usd=min_tp_usd, tp_levels=tp_levels,
                              value_per_point_per_lot=value_per_point_per_lot)
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
        window = candles.iloc[: i + 1]
        bar = candles.iloc[i]
        mid = bar["close"]
        bid, ask = mid - assumed_spread_price / 2, mid + assumed_spread_price / 2

        account = AccountState(balance=balance, equity=balance, free_margin=balance, leverage=leverage)

        if open_pos:
            direction = 1 if open_pos["side"] == "BUY" else -1
            exit_price = bid if open_pos["side"] == "BUY" else ask
            hit_sl = (direction == 1 and exit_price <= open_pos["sl"]) or (direction == -1 and exit_price >= open_pos["sl"])
            if hit_sl:
                pnl = direction * (exit_price - open_pos["entry"]) * value_per_point_per_lot * open_pos["remaining_lot"]
                balance += pnl
                total_pnl += pnl
                trades += 1
                wins += pnl > 0
                losses += pnl <= 0
                risk.register_trade_closed(pnl, balance)
                open_pos = None
            else:
                while open_pos and open_pos["next_idx"] < len(open_pos["tp_levels"]):
                    level = open_pos["tp_levels"][open_pos["next_idx"]]
                    target = open_pos["entry"] + direction * level.distance_price
                    reached = (direction == 1 and exit_price >= target) or (direction == -1 and exit_price <= target)
                    if not reached:
                        break
                    close_lot = min(open_pos["orig_lot"] * level.close_fraction, open_pos["remaining_lot"])
                    pnl = direction * (exit_price - open_pos["entry"]) * value_per_point_per_lot * close_lot
                    balance += pnl
                    total_pnl += pnl
                    open_pos["remaining_lot"] -= close_lot
                    open_pos["next_idx"] += 1
                    if open_pos["next_idx"] == 1:
                        open_pos["sl"] = open_pos["entry"]
                    if open_pos["remaining_lot"] <= 1e-9:
                        trades += 1
                        wins += 1  # a position that reached TP1+ nets positive by construction
                        risk.register_trade_closed(pnl, balance)
                        open_pos = None
                        break

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
                    tp_levels = strategy.build_tp_ladder(sizing.lot, assumed_spread_price)
                    open_pos = {"side": signal.side, "entry": entry, "sl": sl, "tp_levels": tp_levels,
                                "next_idx": 0, "remaining_lot": sizing.lot, "orig_lot": sizing.lot}
                    strategy.on_trade_opened()

    return BacktestResult(trades=trades, wins=wins, losses=losses, total_pnl=total_pnl,
                           max_drawdown_pct=max_dd, final_balance=balance)
