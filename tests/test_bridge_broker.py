from core.broker import BridgeBroker
from core.risk_manager import SymbolSpec


class FakeBridgeClient:
    def positions(self):
        return [{"ticket": "42", "symbol": "XAUUSDm", "type": "BUY", "volume": 0.10,
                 "price_open": 4000.0}]

    def close_order(self, ticket, lot):
        assert ticket == "42"
        assert lot == 0.04
        return {"price": 4001.5, "volume": 0.04}

    def symbol_spec(self, symbol):
        assert symbol == "XAUUSDm"
        # Value is $1 for a $0.10 tick; it is deliberately NOT equal to point.
        return SymbolSpec(100, 0.01, 100, 0.01, 0.01, 1.0, trade_tick_size=0.10)


def test_bridge_broker_returns_realized_pnl_not_the_raw_fill_price():
    pnl = BridgeBroker(FakeBridgeClient()).close_partial("42", 0.04, fill_price=9999.0)
    # BUY: ($4001.5 - $4000.0) * ($1 / $0.10) * 0.04
    assert pnl == 0.6
