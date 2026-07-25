"""
Risk manager: position sizing, margin checks, and hard stop conditions.

Nothing here is cosmetic. This module is the only thing standing between
a small demo account and a margin call, so every check is a real gate, not
a suggestion. It never assumes contract size, margin, or leverage - those
come from the broker's live symbol spec (via the bridge) because they
differ per account type and can't be guessed correctly from outside.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class SymbolSpec:
    """Live specification for the traded symbol, as reported by MT5."""
    contract_size: float      # e.g. 100.0 oz per 1.0 lot for standard XAUUSD
    volume_min: float         # smallest tradable lot, e.g. 0.01
    volume_max: float
    volume_step: float
    point: float               # smallest price increment, e.g. 0.01
    trade_tick_value: float    # USD value of one tick move for a 1.0 lot position
    # `trade_tick_value` is the value of `trade_tick_size`, NOT necessarily
    # of `point`.  Many symbols happen to use the same value for both, but
    # assuming that is unsafe and can under/over-size a trade.
    trade_tick_size: float = 0.0
    margin_initial: Optional[float] = None  # USD margin per 1.0 lot, if broker reports it


@dataclass
class AccountState:
    balance: float
    equity: float
    free_margin: float
    leverage: int  # e.g. 1 for 1:1


@dataclass
class SizingResult:
    ok: bool
    lot: float = 0.0
    est_margin_usd: float = 0.0
    reason: str = ""


class RiskManager:
    """
    Guards every trade decision against account reality. Two independent
    caps apply on every order: the requested USD risk, and whatever margin
    is actually free. Whichever is tighter wins - we never trade past
    either one.
    """

    MAX_MARGIN_USAGE_FRACTION = 0.6  # never commit more than 60% of free margin to one trade
    MAX_RISK_OVERSHOOT_FACTOR = 1.5  # tolerate lot-step rounding overshoot; reject anything wider
    # Ronda 13: risk_per_trade_usd is a fixed dollar amount, but on a small
    # account a "reasonable" fixed number can be a dangerous fraction of
    # total capital - and the account only gets SMALLER after losses, so a
    # number sized for the original balance gets proportionally more
    # dangerous over time, not less. Reproduced directly on real recent
    # market data (same strategy/signals, same 3000-bar window, only
    # risk_per_trade_usd changed): $3 (6% of a $50 balance) -> -$0.91 PnL,
    # 13.1% max drawdown; $6 (12% of the same $50 balance) -> -$39.99 PnL,
    # 81.5% max drawdown - doubling the fixed dollar risk alone nearly
    # wiped the account on identical signals. 5% keeps the $3-6 range this
    # project has actually tuned against safely capped, at a $50 balance
    # (verified at $50: risk_per_trade_usd of $1-1.5 on this account's
    # symbol spec produces ZERO trades outright - below the broker's
    # minimum lot's implied risk floor, not a safer number, just an
    # inactive bot). This is a ceiling/backstop, not a per-trade risk
    # target - 5% is aggressive by conventional retail risk-management
    # standards (1-2%) and is only acceptable BECAUSE it only ever
    # shrinks, never sets, the actual risk taken.
    #
    # Known, accepted consequence at very small balances: as the account
    # shrinks (e.g. the live account is currently ~$16, where 5% is
    # ~$0.80), the cap can itself fall below that same $1-1.5 zero-trade
    # floor - the bot goes dormant on a nearly-wiped account instead of
    # continuing to trade a shrunken position. That's the intended
    # fail-safe direction (halt over over-risk), not a bug, but it means
    # "stays capped without silently going dormant" is a $50-balance
    # claim, not a universal one - don't assume trades will keep flowing
    # as balance keeps dropping without re-verifying at the current
    # balance (second-opinion review, Ronda 13 checkpoint, caught this).
    MAX_RISK_FRACTION_OF_BALANCE = 0.05

    def __init__(
        self,
        risk_per_trade_usd: float,
        max_daily_loss_usd: float,
        max_daily_drawdown_pct: float,
        max_trades_per_day: int,
        max_lot: Optional[float] = None,
    ) -> None:
        self.risk_per_trade_usd = risk_per_trade_usd
        self.max_daily_loss_usd = max_daily_loss_usd
        self.max_daily_drawdown_pct = max_daily_drawdown_pct
        self.max_trades_per_day = max_trades_per_day
        self.max_lot = max_lot if max_lot and max_lot > 0 else None

        self._day: date = date.today()
        self._trades_today = 0
        self._pnl_today = 0.0
        self._start_of_day_balance: Optional[float] = None

    # ---------------------------------------------------------------- day
    def _roll_day_if_needed(self, balance: float, current_date: Optional[date] = None) -> None:
        # `current_date` lets a caller (the backtester) drive the day-roll
        # off the historical candle timestamps in the data instead of the
        # machine's wall clock - a backtest replaying a week of 1-minute
        # candles in a few seconds never crosses a real wall-clock midnight,
        # so without this every "day" of a backtest was silently the same
        # single day, and MAX_DAILY_LOSS_USD/MAX_DAILY_DRAWDOWN_PCT could
        # never actually be exercised by historical data (Ronda 41). Default
        # None preserves the exact live behavior in core/engine.py, which
        # never passes this and must keep using the real wall clock.
        today = current_date if current_date is not None else date.today()
        if today != self._day:
            self._day = today
            self._trades_today = 0
            self._pnl_today = 0.0
            self._start_of_day_balance = balance

    def register_trade_closed(self, pnl_usd: float, balance_after: float, current_date: Optional[date] = None) -> None:
        self._roll_day_if_needed(balance_after, current_date)
        self._trades_today += 1
        self._pnl_today += pnl_usd

    # ------------------------------------------------------------- gating
    def can_open_new_trade(self, balance: float, current_date: Optional[date] = None) -> tuple[bool, str]:
        self._roll_day_if_needed(balance, current_date)
        if self._start_of_day_balance is None:
            self._start_of_day_balance = balance

        if self._trades_today >= self.max_trades_per_day:
            return False, f"Limite diario de trades alcanzado ({self.max_trades_per_day})"

        if self._pnl_today <= -abs(self.max_daily_loss_usd):
            return False, f"Perdida diaria maxima alcanzada ({self._pnl_today:.2f} USD)"

        drawdown_pct = 0.0
        if self._start_of_day_balance:
            drawdown_pct = max(0.0, (self._start_of_day_balance - balance) / self._start_of_day_balance * 100)
        if drawdown_pct >= self.max_daily_drawdown_pct:
            return False, f"Drawdown diario maximo alcanzado ({drawdown_pct:.1f}%)"

        return True, "ok"

    @property
    def trades_today(self) -> int:
        return self._trades_today

    @property
    def pnl_today(self) -> float:
        return self._pnl_today

    def check_equity_drawdown(self, equity: float, current_date: Optional[date] = None) -> tuple[bool, str]:
        """
        Real-time check against floating EQUITY (balance + unrealized pnl
        of whatever is open right now), not just realized balance. Meant
        to be called every engine step, not only before opening a new
        trade: `can_open_new_trade`'s balance-based check can't see a
        large floating loss on an already-open position until it's
        realized, and a wide ATR-based stop can let that floating loss
        run well past what a same-day realized check would catch in time.
        Returns (breached, reason) - the caller is responsible for acting
        on a breach (emergency-closing the open position), this only
        detects it.
        """
        self._roll_day_if_needed(equity, current_date)
        if self._start_of_day_balance is None:
            self._start_of_day_balance = equity
        if not self._start_of_day_balance:
            return False, "ok"

        drawdown_pct = max(0.0, (self._start_of_day_balance - equity) / self._start_of_day_balance * 100)
        if drawdown_pct >= self.max_daily_drawdown_pct:
            return True, f"Drawdown de equity en tiempo real alcanzado ({drawdown_pct:.1f}%)"
        return False, "ok"

    # -------------------------------------------------------------- sizing
    def size_position(
        self,
        account: AccountState,
        spec: SymbolSpec,
        sl_distance_price: float,
        current_price: float,
        risk_budget_usd: Optional[float] = None,
    ) -> SizingResult:
        """
        Compute the largest lot size that stays within BOTH the USD risk
        budget for this trade AND a safe fraction of free margin. Returns
        ok=False if even the broker's minimum lot doesn't fit - that is a
        real, expected outcome on a small 1:1-leverage account and must be
        surfaced, not silently rounded away.
        """
        if sl_distance_price <= 0:
            return SizingResult(ok=False, reason="Distancia de stop invalida")

        tick_size = spec.trade_tick_size or spec.point
        value_per_price_per_lot = spec.trade_tick_value / tick_size if tick_size else 0.0
        if value_per_price_per_lot <= 0:
            return SizingResult(ok=False, reason="Spec de simbolo invalida (tick value)")

        # Lot size so that sl_distance_price * value_per_price_per_lot * lot == risk_per_trade_usd.
        risk_budget = self.risk_per_trade_usd if risk_budget_usd is None else min(
            max(float(risk_budget_usd), 0.0), self.risk_per_trade_usd
        )
        # Ceiling, not a floor: only ever shrinks risk_budget, never raises
        # it - an already-conservative risk_per_trade_usd relative to
        # balance is untouched. See MAX_RISK_FRACTION_OF_BALANCE above.
        if account.balance > 0:
            risk_budget = min(risk_budget, account.balance * self.MAX_RISK_FRACTION_OF_BALANCE)
        if risk_budget <= 0:
            return SizingResult(ok=False, reason="Presupuesto de riesgo supervisor invalido")
        risk_based_lot = risk_budget / (sl_distance_price * value_per_price_per_lot)

        # Snap to broker step, respect min/max
        step = spec.volume_step or 0.01
        lot = max(spec.volume_min, self._round_to_step(risk_based_lot, step))
        lot = min(lot, spec.volume_max)
        if self.max_lot is not None:
            lot = min(lot, self.max_lot)

        # The floor above (spec.volume_min) is a REAL bug fix, not a nicety:
        # during a volatility spike the ATR-based stop distance widens, so
        # the risk-appropriate lot can shrink below the broker's minimum
        # tradable size. Silently trading volume_min anyway means the real
        # dollar risk on that one trade can be many times risk_per_trade_usd
        # - reproduced directly against real market data: a $1 risk budget
        # became a real $16 loss this way, and a sequence of those wiped an
        # entire $50 account in hours. Refuse instead of quietly eating it.
        actual_risk_usd = lot * sl_distance_price * value_per_price_per_lot
        max_allowed_risk = risk_budget * self.MAX_RISK_OVERSHOOT_FACTOR
        if actual_risk_usd > max_allowed_risk:
            return SizingResult(
                ok=False,
                reason=(
                    f"Volatilidad demasiado alta para el lote minimo del broker ({spec.volume_min}): "
                    f"con esta distancia de stop ({sl_distance_price:.2f}) ese lote arriesgaria "
                    f"~{actual_risk_usd:.2f} USD, muy por encima del limite efectivo de "
                    f"{risk_budget:.2f} USD por operacion (ya con el cap del {self.MAX_RISK_FRACTION_OF_BALANCE*100:.0f}% "
                    f"del balance aplicado, ver Ronda 13). Se descarta la señal en vez "
                    f"de operar con un riesgo real muy superior al configurado."
                ),
            )

        # Margin required for this lot at this price
        if spec.margin_initial:
            margin_per_lot = spec.margin_initial
        else:
            margin_per_lot = (spec.contract_size * current_price) / max(account.leverage, 1)
        est_margin = margin_per_lot * lot

        margin_budget = account.free_margin * self.MAX_MARGIN_USAGE_FRACTION

        if est_margin > margin_budget:
            # Try shrinking down to the broker minimum before giving up.
            min_margin = margin_per_lot * spec.volume_min
            if min_margin > margin_budget:
                return SizingResult(
                    ok=False,
                    reason=(
                        f"Margen insuficiente: el lote minimo del broker ({spec.volume_min}) "
                        f"requiere ~{min_margin:.2f} USD y solo hay {margin_budget:.2f} USD "
                        f"disponibles con apalancamiento 1:{account.leverage}."
                    ),
                )
            lot = spec.volume_min
            est_margin = min_margin

        if lot <= 0:
            return SizingResult(ok=False, reason="Lote calculado es cero")

        return SizingResult(ok=True, lot=lot, est_margin_usd=est_margin)

    @staticmethod
    def _round_to_step(value: float, step: float) -> float:
        if step <= 0:
            return value
        # Never round UP a risk-derived volume.  Rounding to the nearest
        # step can silently exceed the requested risk before the later
        # tolerance gate notices it; flooring is deterministic and keeps
        # the actual loss cap at or below the configured budget (apart from
        # an unavoidable broker minimum, which is explicitly rejected).
        steps = int(value / step)
        return round(steps * step, 8)
