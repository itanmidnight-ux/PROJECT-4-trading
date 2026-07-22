"""Entry point for the trading engine (called by run.sh). Not meant to be
imported - it just wires config -> market data -> broker -> engine and
starts the loop."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.broker import BridgeBroker, SimulatedBroker  # noqa: E402
from core.config import apply_db_overrides, load_settings  # noqa: E402
from core.database import Database  # noqa: E402
from core.engine import EngineHalted, TradingEngine  # noqa: E402
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


def _raise_system_exit(signum: int, frame: object) -> None:
    # engine_supervisor.py sends SIGTERM to stop us and waits a bounded amount
    # of time before escalating to SIGKILL. Relying on the OS default SIGTERM
    # action works most of the time, but under load it can be slow enough to
    # trip that escalation. Raising SystemExit here interrupts any blocking
    # time.sleep() (bridge-wait retries, engine.run_forever()'s poll loop)
    # immediately and unwinds cleanly - it's a BaseException, not an
    # Exception, so it isn't swallowed by the engine's generic error handler.
    raise SystemExit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _raise_system_exit)
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
    settings = apply_db_overrides(settings, db.get_all_settings())

    if args.synthetic:
        logger.warning("SYNTHETIC MODE: no broker connection, prices are simulated.")
        market_data = SyntheticMarketData(seed=None)
        starting_balance = 50.0
        from core.risk_manager import SymbolSpec
        spec = SymbolSpec(contract_size=100.0, volume_min=0.01, volume_max=1.0,
                           volume_step=0.01, point=0.01, trade_tick_value=1.0)
        broker = SimulatedBroker(starting_balance=starting_balance, leverage=1, spec=spec)
    elif settings.broker_kind == "oanda":
        # Direct REST connection - no MT5 terminal, no Wine, no bridge
        # process anywhere. Works identically on any Linux install.
        from core.oanda import OandaClient, OandaError
        if not settings.oanda_api_token or not settings.oanda_account_id:
            logger.error("BROKER_KIND=oanda pero faltan OANDA_API_TOKEN y/o OANDA_ACCOUNT_ID en .env.")
            logger.error("Crea una cuenta practice gratis en oanda.com, genera un token de API en el")
            logger.error("panel 'Manage API Access', y copia token + account ID a .env.")
            sys.exit(1)
        client = OandaClient(settings.oanda_api_token, settings.oanda_account_id,
                              environment=settings.oanda_environment,
                              timeout_ms=settings.bridge_timeout_ms)
        try:
            account = client.account()  # first real call = connectivity + credentials check
        except OandaError as exc:
            logger.error("No se pudo conectar a OANDA (%s, cuenta %s): %s",
                         settings.oanda_environment, settings.oanda_account_id, exc)
            logger.error("Revisa OANDA_API_TOKEN/OANDA_ACCOUNT_ID/OANDA_ENV en .env y la conexion a internet.")
            sys.exit(1)
        logger.info("Conectado a OANDA %s - balance %.2f, apalancamiento 1:%d",
                     settings.oanda_environment, account.balance, account.leverage)
        # BridgeMarketData's incremental candle cache is client-agnostic
        # (anything with price()/candles()) - reused as-is over OANDA.
        market_data = BridgeMarketData(client)
        if settings.dry_run:
            logger.warning("DRY_RUN=true: leyendo precios REALES de OANDA pero simulando ordenes.")
            spec = client.symbol_spec(settings.symbol)
            broker = SimulatedBroker(starting_balance=account.balance, leverage=account.leverage, spec=spec)
        else:
            logger.warning("DRY_RUN=false: ORDENES REALES iran a la cuenta OANDA %s (%s).",
                            settings.oanda_account_id, settings.oanda_environment)
            broker = client
    else:
        client = Mt5BridgeClient(settings.bridge_url, settings.bridge_timeout_ms,
                                  auth_token=settings.bridge_auth_token)
        if not settings.bridge_auth_token:
            logger.warning("BRIDGE_AUTH_TOKEN is not set in .env - the bridge is running without "
                            "authentication. Fine on a trusted single-user machine, but re-run "
                            "install.sh (or set it by hand) to close that gap.")
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
    try:
        engine.run_forever()
    except EngineHalted as exc:
        logger.critical("Motor detenido: %s", exc)
        # core/engine_supervisor.py restarts main.py on ANY exit that
        # wasn't itself asked to stop via SIGTERM - a kill switch halt is
        # a "stop trading now", not a crash, so it must not just get
        # respawned on the next backoff and immediately re-trigger the
        # same halt forever (EMERGENCY_STOP stays in place until someone
        # explicitly clears it). engine_supervisor.py sets
        # ENGINE_SUPERVISOR_PID when it spawns us specifically so we can
        # tell IT (not touch a shared file - that used to double as
        # run.sh's own bridge/dashboard stop flag, which this halt has no
        # business touching) to stop restarting, the same way POST
        # /api/engine/stop does.
        supervisor_pid = os.environ.get("ENGINE_SUPERVISOR_PID")
        if supervisor_pid:
            try:
                os.kill(int(supervisor_pid), signal.SIGTERM)
                logger.critical("Aviso enviado al supervisor (PID %s) para que no reinicie el motor.",
                                 supervisor_pid)
            except (ValueError, ProcessLookupError, PermissionError):
                logger.warning("No se pudo avisar al supervisor (PID %s) - puede reintentar solo.",
                                supervisor_pid)
        logger.critical(
            "Para reanudar: borra el interruptor de emergencia (%s) e inicia el motor de nuevo "
            "desde el dashboard.", settings.kill_switch_path,
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
