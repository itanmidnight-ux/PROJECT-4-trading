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
from core.mt5_bridge_client import Tick
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
    sl_distance_price: float  # original ATR-based stop distance, kept for reference/fallback
    trail_distance_price: float  # tighter distance used once trailing - see _manage_open_positions
    next_tp_index: int = 0
    breakeven_moved: bool = False
    trail_active: bool = False
    best_price_since_be: float = 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TradingEngine:
    # No new tick time for this long => assume the market is closed
    # (weekend/holiday) rather than a strategy signal worth chasing.
    STALE_AFTER_SECONDS = 180

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
        self._reconciled = False
        self._last_seen_tick_time: int | None = None
        self._last_tick_change_wallclock: float | None = None
        self._stale_warned = False

    def _ensure_initialized(self) -> None:
        if self._spec is not None:
            return
        spec = self.broker.symbol_spec(self.settings.symbol)
        if not spec.point or not spec.trade_tick_value:
            raise RuntimeError(
                f"Broker returned an invalid symbol spec for {self.settings.symbol} "
                f"(point={spec.point}, trade_tick_value={spec.trade_tick_value}). "
                "Refusing to trade with a spec that would divide by zero."
            )
        value_per_point_per_lot = spec.trade_tick_value / spec.point
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
            rsi_oversold=self.settings.strat_rsi_oversold,
            rsi_overbought=self.settings.strat_rsi_overbought,
            max_spread_price=self.settings.strat_max_spread_price,
            min_atr_price=self.settings.strat_min_atr_price,
            sl_atr_multiple=self.settings.strat_sl_atr_multiple,
            cooldown_bars=self.settings.strat_cooldown_bars,
            bb_period=self.settings.strat_bb_period,
            bb_std=self.settings.strat_bb_std,
            rsi_period=self.settings.strat_rsi_period,
            atr_period=self.settings.strat_atr_period,
        )
        self._spec = spec  # assign last: an exception above must leave state uninitialized

    def run_forever(self) -> None:
        self._ensure_initialized()
        logger.info("Engine started. dry_run=%s symbol=%s", self.settings.dry_run, self.settings.symbol)
        self.db.log_event(ts=_now_iso(), level="INFO", message="Engine started")
        consecutive_errors = 0
        while True:
            try:
                self.step()
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                logger.exception("Error in engine step, continuing after pause")
                try:
                    self.db.log_event(ts=_now_iso(), level="ERROR", message=f"{type(exc).__name__}: {exc}")
                except Exception:
                    logger.exception("Could not even log the error to the database")
                # Back off harder after repeated failures (e.g. bridge down for a while)
                # instead of hammering it every poll_seconds.
                backoff = min(self.poll_seconds * min(consecutive_errors, 10), 60)
                time.sleep(backoff)
                continue
            time.sleep(self.poll_seconds)

    def step(self) -> None:
        self._ensure_initialized()

        if not self._reconciled:
            self._reconcile_open_positions()
            self._reconciled = True

        state = self.market_data.get_state(self.settings.symbol, self.settings.timeframe, 200)
        tick = state.tick
        mid_price = (tick.bid + tick.ask) / 2

        if isinstance(self.broker, SimulatedBroker):
            self.broker.mark_to_market(mid_price)

        account = self.broker.account()
        self.db.record_snapshot(ts=_now_iso(), balance=account.balance,
                                 equity=account.equity, free_margin=account.free_margin)

        # Checked BEFORE normal TP/SL processing, using floating equity
        # (not just realized balance): a wide ATR-based stop can carry a
        # large unrealized loss well before it's ever realized, and the
        # balance-based check in can_open_new_trade can't see that until
        # the position closes on its own. This is the circuit breaker that
        # actually protects the account in real time, not just between
        # trades.
        breached, reason = self._risk.check_equity_drawdown(account.equity)
        if breached:
            self._emergency_close_all_positions(tick, reason)
            return

        self._manage_open_positions(tick.bid, tick.ask)  # always: protect any open position first

        if self._is_market_stale(tick.time):
            return  # market likely closed (weekend/holiday) - don't attempt new signals on dead data

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
            sl_distance_price=signal.sl_distance_price,
            trail_distance_price=tp_levels[0].distance_price,
        ))
        self._strategy.on_trade_opened()
        logger.info("Opened %s %.4f lot @ %.3f (SL %.3f)", signal.side, sizing.lot, open_result.fill_price, sl_price)

    def _manage_open_positions(self, bid: float, ask: float) -> None:
        still_open: list[ManagedPosition] = []
        for pos in self._open_positions:
            exit_price = bid if pos.side == "BUY" else ask
            direction = 1 if pos.side == "BUY" else -1

            # Once past breakeven, trail the stop behind the best price seen
            # instead of leaving it flat at entry. A flat breakeven gives
            # back 100% of any move between TP1 and a reversal. The trail
            # distance deliberately reuses the TP1 spacing (tight, on the
            # same scale as the ladder) rather than the original ATR-based
            # SL distance - that first version traded flat but was a no-op
            # in backtesting: the ATR-based SL distance was consistently
            # several times wider than the gap to TP2/TP3, so price always
            # reached the next TP level (or reversed to exact breakeven)
            # long before a stop that wide could ever ratchet above entry.
            if pos.trail_active:
                ratcheted = False
                if direction == 1:
                    pos.best_price_since_be = max(pos.best_price_since_be, exit_price)
                    candidate_sl = pos.best_price_since_be - pos.trail_distance_price
                    if candidate_sl > pos.sl_price:
                        pos.sl_price = candidate_sl
                        ratcheted = True
                else:
                    pos.best_price_since_be = min(pos.best_price_since_be, exit_price)
                    candidate_sl = pos.best_price_since_be + pos.trail_distance_price
                    if candidate_sl < pos.sl_price:
                        pos.sl_price = candidate_sl
                        ratcheted = True
                if ratcheted:
                    try:
                        self.broker.modify_sl(pos.ticket, pos.sl_price)
                    except Exception:
                        logger.exception("Could not update trailing SL on %s (continuing locally)", pos.ticket)

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
                # breakeven (so a reversal can't turn a winner into a loser)
                # and start trailing from there so a further favorable move
                # keeps getting captured instead of only ever giving it back
                # down to flat.
                if pos.next_tp_index == 1 and not pos.breakeven_moved and not fully_closed:
                    pos.sl_price = pos.entry_price
                    pos.breakeven_moved = True
                    pos.trail_active = True
                    pos.best_price_since_be = exit_price

                if fully_closed:
                    break

            if pos.remaining_lot > 1e-9:
                still_open.append(pos)

        self._open_positions = still_open

    def _emergency_close_all_positions(self, tick: Tick, reason: str) -> None:
        """Force-closes every open position at market, immediately, because
        the real-time equity drawdown breaker tripped. This is the one
        place the engine closes a position for a reason OTHER than its own
        SL/TP - it exists specifically because a wide ATR-based stop can
        otherwise carry a floating loss well past what a same-day realized
        check would catch."""
        message = f"PARADA DE EMERGENCIA: {reason}. Cerrando todas las posiciones abiertas."
        logger.critical(message)
        self.db.log_event(ts=_now_iso(), level="CRITICAL", message=message)

        for pos in self._open_positions:
            exit_price = tick.bid if pos.side == "BUY" else tick.ask
            pnl = self.broker.close_partial(pos.ticket, pos.remaining_lot, exit_price)
            self.db.close_trade_partial(
                pos.trade_id, exit_price=exit_price, closed_at=_now_iso(), pnl_usd=pnl,
                close_fraction=pos.remaining_lot / pos.original_lot, tp_level=-2, fully_closed=True,
            )
            account = self.broker.account()
            self._risk.register_trade_closed(pnl, account.balance)
            logger.critical("Emergency-closed %s, pnl=%.2f", pos.ticket, pnl)

        self._open_positions = []

    def _reconcile_open_positions(self) -> None:
        """
        Runs once, on the first step() after startup. Auto-restart means a
        crash mid-trade can leave a position open at the broker with no
        in-memory ManagedPosition tracking it - this adopts any such
        position instead of silently abandoning it to whatever SL it had
        (or didn't have) at open time.

        Best-effort by nature: we don't know how much of the original TP
        ladder already fired before the crash, so the ladder is rebuilt
        fresh from the current price. That's not identical to the original
        plan, but it's a real risk-managed position instead of an orphan.
        """
        try:
            broker_positions = self.broker.open_positions(self.settings.symbol)
        except Exception:
            logger.exception("Could not check for pre-existing open positions on startup")
            return
        if not broker_positions:
            return

        try:
            state = self.market_data.get_state(self.settings.symbol, self.settings.timeframe, 5)
            spread_price = state.tick.spread_price
        except Exception:
            spread_price = 0.3  # conservative guess if even a price read fails right now

        for bp in broker_positions:
            sl_price = bp.sl
            if not sl_price:
                fallback_distance = max(spread_price * 5, self._strategy.min_tp_distance_for_lot(bp.volume) * 3)
                sl_price = (bp.price_open - fallback_distance if bp.side == "BUY"
                            else bp.price_open + fallback_distance)
                try:
                    self.broker.modify_sl(bp.ticket, sl_price)
                except Exception:
                    logger.exception("Could not set a fallback SL on recovered position %s", bp.ticket)

            tp_levels = self._strategy.build_tp_ladder(bp.volume, spread_price)
            trade_id = self.db.open_trade(
                ticket=bp.ticket, symbol=self.settings.symbol, side=bp.side, lot=bp.volume,
                entry_price=bp.price_open, sl_price=sl_price, opened_at=_now_iso(),
                dry_run=self.settings.dry_run,
            )
            self._open_positions.append(ManagedPosition(
                trade_id=trade_id, ticket=bp.ticket, side=bp.side, entry_price=bp.price_open,
                original_lot=bp.volume, remaining_lot=bp.volume, sl_price=sl_price, tp_levels=tp_levels,
                sl_distance_price=abs(bp.price_open - sl_price),
                trail_distance_price=tp_levels[0].distance_price,
            ))
            message = (
                f"Posicion recuperada tras reinicio: {bp.side} {bp.volume} lote @ {bp.price_open:.3f} "
                f"(ticket {bp.ticket}). Escalera de TP reconstruida desde el precio actual, "
                "puede no coincidir exactamente con el plan original."
            )
            logger.warning(message)
            self.db.log_event(ts=_now_iso(), level="WARN", message=message)

    def _is_market_stale(self, tick_time: int) -> bool:
        now = time.time()
        if self._last_seen_tick_time != tick_time:
            self._last_seen_tick_time = tick_time
            self._last_tick_change_wallclock = now
            if self._stale_warned:
                logger.info("Market data is moving again.")
                self.db.log_event(ts=_now_iso(), level="INFO", message="Datos de mercado activos de nuevo")
                self._stale_warned = False
            return False

        if self._last_tick_change_wallclock is None:
            self._last_tick_change_wallclock = now
            return False

        stale_for = now - self._last_tick_change_wallclock
        if stale_for >= self.STALE_AFTER_SECONDS:
            if not self._stale_warned:
                message = (
                    f"Sin ticks nuevos hace {int(stale_for)}s en {self.settings.symbol} - "
                    "mercado probablemente cerrado (fin de semana/feriado). Pausando nuevas señales "
                    "hasta que vuelva a moverse el precio; las posiciones abiertas se siguen protegiendo."
                )
                logger.warning(message)
                self.db.log_event(ts=_now_iso(), level="WARN", message=message)
                self._stale_warned = True
            return True
        return False
