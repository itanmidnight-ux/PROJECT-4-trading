# XAUUSD 1m Scalper (FBS / MT5) + Dashboard

Bot de scalping para oro (XAUUSD) en temporalidad 1 minuto, pensado para
correr contra una cuenta MT5 de FBS en Linux, con gestion de riesgo real
y un dashboard nativo para ver resultados.

## Lee esto antes de usarlo

- **No existe una estrategia que gane siempre.** Este bot toma señales
  de reversion a la media (Bandas de Bollinger + RSI en 1m) filtradas por
  spread y volatilidad. Es una estrategia razonable, no una maquina de
  dinero garantizado. Va a perder trades — el objetivo del diseño es que
  cada perdida este acotada (`RISK_PER_TRADE_USD` en `.env`, ~1 USD por
  defecto) y que el sistema se detenga solo si el dia se pone feo
  (`MAX_DAILY_LOSS_USD`, `MAX_DAILY_DRAWDOWN_PCT`).
- **La cuenta de $50 con apalancamiento 1:1 limita mucho cuanto se puede
  operar.** XAUUSD en MT5 normalmente usa un tamaño de contrato de 100 oz
  por lote; con 1:1, el margen requerido para abrir posicion puede superar
  el balance disponible incluso en el lote minimo (0.01), dependiendo de
  como FBS tenga configurado el simbolo. `core/risk_manager.py` consulta
  la especificacion real del simbolo en el broker antes de cada trade y
  **se niega a operar si el margen no alcanza**, en vez de forzar una
  orden que el broker rechazaria. Corre el backtest y una sesion en
  `DRY_RUN=true` primero para ver los numeros reales de tu cuenta.
- **"1000 trades/dia" es un techo, no una meta.** `MAX_TRADES_PER_DAY`
  limita cuantos trades como maximo puede abrir el bot en un dia; cuantos
  realmente abre depende de que aparezcan señales validas y de que haya
  margen disponible. No fuerza operaciones para llegar a un numero.

## Arquitectura

```
run.sh              -> arranca Xvfb (si hace falta), el bridge MT5 (Wine) y el motor
install.sh           -> instala todo: deps de sistema, venv, Wine, terminal MT5, python de Windows
main.py              -> entrypoint del motor (usa core/engine.py)
dashboard.py          -> app nativa (pywebview) con el dashboard

core/
  config.py           -> carga .env
  risk_manager.py      -> sizing de posicion + limites diarios
  strategy.py           -> señal (Bollinger+RSI) + escalera de TP
  engine.py              -> loop principal
  broker.py               -> BridgeBroker (real) / SimulatedBroker (paper)
  market_data.py            -> BridgeMarketData (real) / SyntheticMarketData (pruebas)
  mt5_bridge_client.py       -> cliente HTTP hacia el bridge
  database.py                 -> SQLite (trades, snapshots, eventos)
  backtest.py                  -> backtest reutilizando la misma logica

bridge/
  mt5_bridge_server.py  -> Flask que envuelve el paquete MetaTrader5 (corre bajo Wine)

dashboard/
  index.html, style.css, app.js -> UI del dashboard (SVG a mano, sin CDN)
```

### Por que hay un "bridge" con Wine

El paquete `MetaTrader5` de PyPI solo funciona si puede cargar las DLLs
reales del terminal de Windows. En Linux eso significa correr el terminal
MT5 y un Python de Windows dentro de Wine. `install.sh` deja todo eso
armado; `bridge/mt5_bridge_server.py` corre ahi dentro y expone una API
HTTP simple (`/price`, `/candles`, `/order/open`, ...) que el resto del
sistema (Python normal de Linux) consume por HTTP. Esta separacion es lo
que permite que el motor, el dashboard y el risk manager sean Python
comun y silvestre, sin depender de Wine para nada mas que hablar con MT5.

## Instalacion

```bash
./install.sh
```

Instala dependencias de sistema (apt), crea el venv de Linux, instala
Wine + el terminal MetaTrader 5 + un Python de Windows dentro de Wine con
el paquete `MetaTrader5`, y crea `.env` a partir de `.env.example`
(pidiendo login/password/server de forma interactiva; ese archivo queda
en `.gitignore` y nunca se commitea).

`install.sh` descarga instaladores desde `download.mql5.com` y
`python.org`; necesitas salida a internet para esos dos dominios. Si tu
red los bloquea, descarga `mt5setup.exe` y el instalador de Python 3.11
manualmente y ajusta las rutas al inicio del script.

## Uso

```bash
./run.sh                 # arranca el bridge MT5 + el motor, usando .env (foreground)
./run.sh --synthetic      # sin broker: precios simulados, solo para probar que todo corre
./run.sh --daemon          # igual, pero corre en segundo plano (usar ./stop.sh para pararlo)
./stop.sh                   # detiene una instancia arrancada con --daemon

.venv/bin/python dashboard.py     # abre el dashboard nativo (independiente del motor)

.venv/bin/python scripts/run_backtest.py                 # backtest con datos sinteticos (solo prueba de humo)
.venv/bin/python scripts/run_backtest.py --csv hist.csv    # backtest con historial real exportado del bridge

./scripts/verify.sh       # compila todo, corre los tests y una prueba de humo del motor
```

### Resiliencia

`run.sh` supervisa tanto el bridge MT5 como el motor: si cualquiera de los
dos se cae (crash de Wine, excepcion no manejada, perdida de conexion),
se reinicia solo con backoff exponencial (2s, 4s, 8s... hasta 60s) en vez
de tirar todo el sistema abajo. El cliente del bridge tambien reintenta
llamadas individuales y vuelve a loguearse solo si la sesion de MT5 se
cae sin que el proceso del bridge muera. Los logs quedan en `data/logs/`
(rotan automaticamente, no crecen sin limite).

Para que arranque solo al iniciar el sistema (opcional, no se activa
por defecto): hay una plantilla de servicio systemd de usuario en
`scripts/xauusd-scalper.service.template` con instrucciones de instalacion
en el propio archivo.

### Antes de operar en real

`DRY_RUN=true` en `.env` (valor por defecto) hace que el motor lea
precios **reales** del bridge pero simule los cierres localmente — no
manda ninguna orden a la cuenta. Cambia a `DRY_RUN=false` solo cuando:

1. Corriste `scripts/run_backtest.py` con historial real y los numeros
   tienen sentido para vos.
2. Dejaste el bot en `DRY_RUN=true` un tiempo contra precios en vivo y
   viste en el dashboard que el comportamiento es el esperado.
3. Entendes que aun asi puede perder dinero — la cuenta demo con $50 es
   justamente para probar esto sin que importe.

## Credenciales

Nunca van al repositorio. `install.sh` las guarda en `.env` (con permisos
`600`, excluido por `.gitignore`). Si preferis no guardarlas en disco,
dejalas vacias en `.env` y exportalas como variables de entorno antes de
correr `run.sh` en su lugar.
