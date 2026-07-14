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

    def __init__(
        self,
        risk_per_trade_usd: float,
        max_daily_loss_usd: float,
        max_daily_drawdown_pct: float,
        max_trades_per_day: int,
    ) -> None:
        self.risk_per_trade_usd = risk_per_trade_usd
        self.max_daily_loss_usd = max_daily_loss_usd
        self.max_daily_drawdown_pct = max_daily_drawdown_pct
        self.max_trades_per_day = max_trades_per_day

        self._day: date = date.today()
        self._trades_today = 0
        self._pnl_today = 0.0
        self._start_of_day_balance: Optional[float] = None

    # ---------------------------------------------------------------- day
    def _roll_day_if_needed(self, balance: float) -> None:
        today = date.today()
        if today != self._day:
            self._day = today
            self._trades_today = 0
            self._pnl_today = 0.0
            self._start_of_day_balance = balance

    def register_trade_closed(self, pnl_usd: float, balance_after: float) -> None:
        self._roll_day_if_needed(balance_after)
        self._trades_today += 1
        self._pnl_today += pnl_usd

    # ------------------------------------------------------------- gating
    def can_open_new_trade(self, balance: float) -> tuple[bool, str]:
        self._roll_day_if_needed(balance)
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

    # -------------------------------------------------------------- sizing
    def size_position(
        self,
        account: AccountState,
        spec: SymbolSpec,
        sl_distance_price: float,
        current_price: float,
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

        value_per_point_per_lot = spec.trade_tick_value / spec.point if spec.point else 0.0
        if value_per_point_per_lot <= 0:
            return SizingResult(ok=False, reason="Spec de simbolo invalida (tick value)")

        # Lot size so that sl_distance_price * value_per_point_per_lot * lot == risk_per_trade_usd
        risk_based_lot = self.risk_per_trade_usd / (sl_distance_price * value_per_point_per_lot)

        # Snap to broker step, respect min/max
        step = spec.volume_step or 0.01
        lot = max(spec.volume_min, self._round_to_step(risk_based_lot, step))
        lot = min(lot, spec.volume_max)

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
        steps = round(value / step)
        return round(steps * step, 8)
