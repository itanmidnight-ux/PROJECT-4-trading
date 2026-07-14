"""Entry point for the trading engine (called by run.sh). Not meant to be
imported - it just wires config -> market data -> broker -> engine and
starts the loop."""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.broker import BridgeBroker, SimulatedBroker  # noqa: E402
from core.config import load_settings  # noqa: E402
from core.database import Database  # noqa: E402
from core.engine import TradingEngine  # noqa: E402
from core.market_data import BridgeMarketData, SyntheticMarketData  # noqa: E402
from core.mt5_bridge_client import Mt5BridgeClient  # noqa: E402

ROOT = Path(__file__).resolve().parent


def _setup_logging(level: str) -> None:
    log_dir = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(log_dir / "engine.log", maxBytes=5_000_000, backupCount=5)
    file_handler.setFormatter(fmt)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="XAUUSD 1m scalper engine")
    parser.add_argument("--synthetic", action="store_true",
                         help="Use synthetic price data instead of the MT5 bridge "
                              "(for local testing with no broker connection at all).")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    _setup_logging(args.log_level)
    logger = logging.getLogger("main")

    settings = load_settings()
    db = Database(settings.db_path)

    if args.synthetic:
        logger.warning("SYNTHETIC MODE: no broker connection, prices are simulated.")
        market_data = SyntheticMarketData(seed=None)
        starting_balance = 50.0
        from core.risk_manager import SymbolSpec
        spec = SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=1.0,
                           volume_step=0.01, point=0.01, trade_tick_value=1.0)
        broker = SimulatedBroker(starting_balance=starting_balance, leverage=1, spec=spec)
    else:
        client = Mt5BridgeClient(settings.bridge_url, settings.bridge_timeout_ms)
        logger.info("Waiting for MT5 bridge at %s ...", settings.bridge_url)
        import time
        for _ in range(60):
            if client.health():
                break
            time.sleep(2)
        else:
            logger.error("Bridge never became healthy. Is run.sh's Wine bridge process up?")
            sys.exit(1)

        if not settings.mt5_login or not settings.mt5_password:
            logger.error("MT5_LOGIN/MT5_PASSWORD missing in .env. Run install.sh or edit .env.")
            sys.exit(1)

        from core.mt5_bridge_client import BridgeError
        try:
            client.login(settings.mt5_login, settings.mt5_password, settings.mt5_server)
        except BridgeError as exc:
            logger.error("Could not log in to MT5 (login=%s server=%s): %s",
                         settings.mt5_login, settings.mt5_server, exc)
            logger.error("Check MT5_LOGIN/MT5_PASSWORD/MT5_SERVER in .env - the password "
                         "resets in 2 days on the demo account provided, this is a common cause.")
            sys.exit(1)
        market_data = BridgeMarketData(client)

        if settings.dry_run:
            logger.warning("DRY_RUN=true: reading REAL prices from the bridge but simulating fills, "
                            "no real orders will be sent.")
            account = client.account()
            spec = client.symbol_spec(settings.symbol)
            broker = SimulatedBroker(starting_balance=account.balance, leverage=account.leverage, spec=spec)
        else:
            logger.warning("DRY_RUN=false: LIVE ORDERS will be sent to account %s (%s).",
                            settings.mt5_login, settings.mt5_server)
            broker = BridgeBroker(client)

    engine = TradingEngine(settings, market_data, broker, db, poll_seconds=settings.poll_seconds)
    engine.run_forever()


if __name__ == "__main__":
    main()
