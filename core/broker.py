"""Order execution. BridgeBroker sends real orders through the Wine/MT5
bridge. SimulatedBroker fills virtually against whatever price feed is
running (live bridge prices or synthetic data) so DRY_RUN=true exercises
the exact same engine/strategy/risk code paths without risking money."""
from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from core.mt5_bridge_client import Mt5BridgeClient
from core.risk_manager import AccountState, SymbolSpec


@dataclass
class OpenResult:
    ticket: str
    fill_price: float


@dataclass
class BrokerPosition:
    """A position as the broker currently sees it - used on startup to
    recover any trade left open by a previous crash/restart, since the
    engine's in-memory TP-ladder state does not survive a process exit."""
    ticket: str
    side: str
    volume: float
    price_open: float
    sl: Optional[float]


@dataclass
class SimPosition:
    ticket: str
    side: str
    lot: float
    entry_price: float
    remaining_lot: float
    sl_price: Optional[float] = None


class BrokerExecutor(ABC):
    @abstractmethod
    def account(self) -> AccountState: ...

    @abstractmethod
    def symbol_spec(self, symbol: str) -> SymbolSpec: ...

    @abstractmethod
    def open_order(self, symbol: str, side: str, lot: float, sl_price: float, fill_price: float) -> OpenResult: ...

    @abstractmethod
    def close_partial(self, ticket: str, lot: float, fill_price: float) -> float:
        """Closes `lot` volume, returns realized PnL in USD for that slice."""
        ...

    @abstractmethod
    def open_positions(self, symbol: str) -> list[BrokerPosition]:
        """Positions the broker currently has open for `symbol`, independent
        of whatever this process remembers - used to recover state after a
        restart."""
        ...

    @abstractmethod
    def modify_sl(self, ticket: str, sl_price: float) -> None: ...


class BridgeBroker(BrokerExecutor):
    def __init__(self, client: Mt5BridgeClient) -> None:
        self.client = client

    def account(self) -> AccountState:
        return self.client.account()

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        return self.client.symbol_spec(symbol)

    def open_order(self, symbol: str, side: str, lot: float, sl_price: float, fill_price: float) -> OpenResult:
        result = self.client.open_order(symbol, side, lot, sl_price)
        return OpenResult(ticket=str(result["ticket"]), fill_price=result["price"])

    def close_partial(self, ticket: str, lot: float, fill_price: float) -> float:
        result = self.client.close_order(ticket, lot)
        # The bridge doesn't compute PnL for us on partial close; the
        # engine computes it from entry/fill price + spec, same as sim.
        return result["price"]

    def open_positions(self, symbol: str) -> list[BrokerPosition]:
        raw = self.client.positions(symbol)
        return [
            BrokerPosition(ticket=str(p["ticket"]), side=p["type"], volume=p["volume"],
                            price_open=p["price_open"], sl=p.get("sl") or None)
            for p in raw
        ]

    def modify_sl(self, ticket: str, sl_price: float) -> None:
        self.client.modify_sl(ticket, sl_price)


class SimulatedBroker(BrokerExecutor):
    """Paper-trading fill engine used whenever DRY_RUN=true."""

    def __init__(self, starting_balance: float, leverage: int, spec: SymbolSpec) -> None:
        self._balance = starting_balance
        self._equity = starting_balance
        self._leverage = leverage
        self._spec = spec
        self._positions: dict[str, SimPosition] = {}
        self._ticket_seq = itertools.count(1)

    def account(self) -> AccountState:
        used_margin = sum(
            (self._spec.contract_size * p.entry_price * p.remaining_lot) / max(self._leverage, 1)
            for p in self._positions.values()
        )
        free_margin = max(self._equity - used_margin, 0.0)
        return AccountState(balance=self._balance, equity=self._equity,
                             free_margin=free_margin, leverage=self._leverage)

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        return self._spec

    def open_order(self, symbol: str, side: str, lot: float, sl_price: float, fill_price: float) -> OpenResult:
        ticket = f"SIM-{next(self._ticket_seq)}"
        self._positions[ticket] = SimPosition(ticket=ticket, side=side, lot=lot,
                                               entry_price=fill_price, remaining_lot=lot,
                                               sl_price=sl_price)
        return OpenResult(ticket=ticket, fill_price=fill_price)

    def open_positions(self, symbol: str) -> list[BrokerPosition]:
        return [
            BrokerPosition(ticket=p.ticket, side=p.side, volume=p.remaining_lot,
                            price_open=p.entry_price, sl=p.sl_price)
            for p in self._positions.values()
        ]

    def modify_sl(self, ticket: str, sl_price: float) -> None:
        pos = self._positions.get(ticket)
        if pos is not None:
            pos.sl_price = sl_price

    def _pnl(self, pos: SimPosition, lot: float, fill_price: float) -> float:
        value_per_point_per_lot = self._spec.trade_tick_value / self._spec.point
        direction = 1 if pos.side == "BUY" else -1
        return direction * (fill_price - pos.entry_price) * value_per_point_per_lot * lot

    def close_partial(self, ticket: str, lot: float, fill_price: float) -> float:
        pos = self._positions.get(ticket)
        if pos is None:
            return 0.0
        lot = min(lot, pos.remaining_lot)
        pnl = self._pnl(pos, lot, fill_price)
        pos.remaining_lot = round(pos.remaining_lot - lot, 8)
        self._balance += pnl
        self._equity = self._balance
        if pos.remaining_lot <= 1e-9:
            del self._positions[ticket]
        return pnl

    def mark_to_market(self, mid_price: float) -> None:
        floating = sum(self._pnl(p, p.remaining_lot, mid_price) for p in self._positions.values())
        self._equity = self._balance + floating
