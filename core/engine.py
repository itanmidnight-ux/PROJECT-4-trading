"""Main trading loop. Wires market data -> strategy -> risk manager ->
broker -> database together. The same code path runs whether the broker
is the real bridge or the paper SimulatedBroker - only the executor
object passed in changes."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.broker import BrokerExecutor, SimulatedBroker
from core.config import Settings
from core.database import Database
from core.market_data import MarketDataSource
from core.risk_manager import RiskManager
from core.strategy import ScalpStrategy, TpLevel

logger = logging.getLogger("engine")


@dataclass
class ManagedPosition:
    trade_id: int
    ticket: str
    side: str
    entry_price: float
    original_lot: float
    remaining_lot: float
    sl_price: float
    tp_levels: list[TpLevel]
    next_tp_index: int = 0
    breakeven_moved: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        market_data: MarketDataSource,
        broker: BrokerExecutor,
        db: Database,
        poll_seconds: float = 2.0,
    ) -> None:
        self.settings = settings
        self.market_data = market_data
        self.broker = broker
        self.db = db
        self.poll_seconds = poll_seconds

        self._open_positions: list[ManagedPosition] = []
        self._risk: RiskManager | None = None
        self._strategy: ScalpStrategy | None = None
        self._spec = None
        self._last_bar_time: int | None = None

    def _ensure_initialized(self) -> None:
        if self._spec is not None:
            return
        self._spec = self.broker.symbol_spec(self.settings.symbol)
        value_per_point_per_lot = self._spec.trade_tick_value / self._spec.point
        self._risk = RiskManager(
            risk_per_trade_usd=self.settings.risk_per_trade_usd,
            max_daily_loss_usd=self.settings.max_daily_loss_usd,
            max_daily_drawdown_pct=self.settings.max_daily_drawdown_pct,
            max_trades_per_day=self.settings.max_trades_per_day,
        )
        self._strategy = ScalpStrategy(
            min_tp_usd=self.settings.min_tp_usd,
            tp_levels=self.settings.tp_levels,
            value_per_point_per_lot=value_per_point_per_lot,
        )

    def run_forever(self) -> None:
        self._ensure_initialized()
        logger.info("Engine started. dry_run=%s symbol=%s", self.settings.dry_run, self.settings.symbol)
        while True:
            try:
                self.step()
            except Exception:
                logger.exception("Error in engine step, continuing after pause")
                time.sleep(self.poll_seconds)
                continue
            time.sleep(self.poll_seconds)

    def step(self) -> None:
        self._ensure_initialized()
        state = self.market_data.get_state(self.settings.symbol, self.settings.timeframe, 200)
        tick = state.tick
        mid_price = (tick.bid + tick.ask) / 2

        if isinstance(self.broker, SimulatedBroker):
            self.broker.mark_to_market(mid_price)

        account = self.broker.account()
        self.db.record_snapshot(ts=_now_iso(), balance=account.balance,
                                 equity=account.equity, free_margin=account.free_margin)

        self._manage_open_positions(tick.bid, tick.ask)

        candles = state.candles
        is_new_bar = not candles.empty and (self._last_bar_time != int(candles.iloc[-1]["time"]))
        if is_new_bar:
            self._last_bar_time = int(candles.iloc[-1]["time"])
            self._strategy.on_bar_closed()

        can_trade, reason = self._risk.can_open_new_trade(account.balance)
        if not can_trade:
            if is_new_bar:
                logger.info("Trading paused: %s", reason)
            return

        if self._open_positions:
            # Keep it simple and low-risk: one position at a time.
            return

        lot_hint = self._spec.volume_min
        signal = self._strategy.generate_signal(candles, tick.spread_price, lot_hint)
        if signal.side is None:
            return

        sizing = self._risk.size_position(account, self._spec, signal.sl_distance_price, mid_price)
        if not sizing.ok:
            logger.warning("Signal found but cannot size trade: %s", sizing.reason)
            self.db.log_event(ts=_now_iso(), level="WARN", message=sizing.reason)
            return

        tp_levels = self._strategy.build_tp_ladder(sizing.lot, tick.spread_price)
        fill_price = tick.ask if signal.side == "BUY" else tick.bid
        sl_price = fill_price - signal.sl_distance_price if signal.side == "BUY" else fill_price + signal.sl_distance_price

        open_result = self.broker.open_order(self.settings.symbol, signal.side, sizing.lot, sl_price, fill_price)
        trade_id = self.db.open_trade(
            ticket=open_result.ticket, symbol=self.settings.symbol, side=signal.side,
            lot=sizing.lot, entry_price=open_result.fill_price, sl_price=sl_price,
            opened_at=_now_iso(), dry_run=self.settings.dry_run,
        )
        self._open_positions.append(ManagedPosition(
            trade_id=trade_id, ticket=open_result.ticket, side=signal.side,
            entry_price=open_result.fill_price, original_lot=sizing.lot,
            remaining_lot=sizing.lot, sl_price=sl_price, tp_levels=tp_levels,
        ))
        self._strategy.on_trade_opened()
        logger.info("Opened %s %.4f lot @ %.3f (SL %.3f)", signal.side, sizing.lot, open_result.fill_price, sl_price)

    def _manage_open_positions(self, bid: float, ask: float) -> None:
        still_open: list[ManagedPosition] = []
        for pos in self._open_positions:
            exit_price = bid if pos.side == "BUY" else ask
            direction = 1 if pos.side == "BUY" else -1

            hit_sl = (direction == 1 and exit_price <= pos.sl_price) or (direction == -1 and exit_price >= pos.sl_price)
            if hit_sl:
                pnl = self.broker.close_partial(pos.ticket, pos.remaining_lot, exit_price)
                self.db.close_trade_partial(pos.trade_id, exit_price=exit_price, closed_at=_now_iso(),
                                             pnl_usd=pnl, close_fraction=pos.remaining_lot / pos.original_lot,
                                             tp_level=-1, fully_closed=True)
                account = self.broker.account()
                self._risk.register_trade_closed(pnl, account.balance)
                logger.info("SL hit on %s, pnl=%.2f", pos.ticket, pnl)
                continue

            moved = False
            while pos.next_tp_index < len(pos.tp_levels):
                level = pos.tp_levels[pos.next_tp_index]
                target_price = pos.entry_price + direction * level.distance_price
                reached = (direction == 1 and exit_price >= target_price) or (direction == -1 and exit_price <= target_price)
                if not reached:
                    break
                close_lot = round(pos.original_lot * level.close_fraction, 8)
                close_lot = min(close_lot, pos.remaining_lot)
                if close_lot <= 0:
                    pos.next_tp_index += 1
                    continue
                pnl = self.broker.close_partial(pos.ticket, close_lot, exit_price)
                pos.remaining_lot = round(pos.remaining_lot - close_lot, 8)
                fully_closed = pos.remaining_lot <= 1e-9
                self.db.close_trade_partial(
                    pos.trade_id, exit_price=exit_price, closed_at=_now_iso(), pnl_usd=pnl,
                    close_fraction=close_lot / pos.original_lot, tp_level=pos.next_tp_index,
                    fully_closed=fully_closed,
                )
                account = self.broker.account()
                self._risk.register_trade_closed(pnl, account.balance)
                logger.info("TP%d hit on %s, closed %.4f lot, pnl=%.2f", pos.next_tp_index + 1, pos.ticket, close_lot, pnl)
                pos.next_tp_index += 1
                moved = True

                # After the first TP locks in profit, move the remainder to
                # breakeven so a reversal can't turn a winner into a loser.
                if pos.next_tp_index == 1 and not pos.breakeven_moved and not fully_closed:
                    pos.sl_price = pos.entry_price
                    pos.breakeven_moved = True

                if fully_closed:
                    break

            if pos.remaining_lot > 1e-9:
                still_open.append(pos)

        self._open_positions = still_open
