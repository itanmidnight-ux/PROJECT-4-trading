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

.venv/bin/python scripts/fetch_market_data.py --interval 1m --range 5d   # historial real (proxy) para backtestear
.venv/bin/python scripts/run_backtest.py --csv data/gold_history_1m.csv    # backtest con ese historial

./scripts/verify.sh       # compila todo, corre los tests y una prueba de humo del motor
./scripts/doctor.sh        # diagnostico de la instalacion real (Wine, MT5, bridge, .env, disco...)
```

`scripts/doctor.sh` es el primer comando a correr cuando algo no funciona: no
instala ni cambia nada, solo te dice exactamente que falta (Wine, el
terminal MT5, el python de Wine, credenciales en `.env`, el bridge
corriendo, espacio en disco...) en vez de tener que adivinar o re-correr
`install.sh` a ciegas.

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

## Que dice el backtest con datos reales (y como se llego a esto)

`scripts/fetch_market_data.py` descarga futuros de oro COMEX (GC=F) reales
via Yahoo Finance - no es el feed exacto de FBS, pero es un proxy liquido
y correlacionado, util para revisar que la estrategia tenga sentido sobre
movimiento de mercado real en vez de puro ruido sintetico. Con eso se hizo
esta ronda de analisis (julio 2026, ~5 dias reales de 1m, split 60/40
cronologico en train/test para no reportar un numero sobreajustado):

1. **El backtest original tenia un bug serio**: solo miraba el precio de
   cierre de cada vela para decidir si el SL/TP se habian tocado, ignorando
   el rango intra-vela (`high`/`low`). Eso subestimaba el riesgo real - una
   perdida de -$14 aparecio en los datos cuando el limite configurado era
   -$1. Se corrigio para usar `high`/`low` (con el criterio conservador de
   que, si una vela pudo tocar tanto el SL como un TP, se asume que el
   movimiento adverso ocurrio primero).
2. Con esa correccion, la configuracion original (SL = ATR × 1.2) **perdia
   dinero de forma consistente** en datos reales: el stop era demasiado
   ajustado para el ruido normal de 1 minuto en oro, así que la mayoria de
   los trades se cerraban en perdida antes de que la reversion a la media
   tuviera espacio para funcionar.
3. Se agrego un **trailing stop** despues del primer TP (en vez de dejar el
   stop plano en breakeven) para capturar mas de un movimiento favorable
   que se revierte antes de llegar al segundo/tercer nivel de TP.
4. Se probaron varios valores de `sl_atr_multiple` con el split train/test:
   5.0 se veia mejor en el tramo de entrenamiento pero **se volvia negativo
   en el tramo de prueba** (la firma clasica de sobreajuste). 4.0 fue el
   valor mas ancho que se mantuvo positivo en ambos tramos, y quedo como
   nuevo default.
5. Resultado final sobre los 5 dias completos: **+$7.50 sobre $50 inicial
   (91.7% de operaciones ganadoras, pero 39.3% de drawdown maximo)**.

Esto **no es una garantia de nada**. Es evidencia direccional de una
muestra corta, sobre un instrumento proxy, no sobre el feed real de FBS.
El 39.3% de drawdown maximo sigue siendo alto para una cuenta de $50 -
`MAX_DAILY_DRAWDOWN_PCT` en `.env` (20% por defecto) corta la operativa
mucho antes de llegar ahi en un solo dia, pero en varios dias seguidos con
mala suerte la cuenta si puede sufrir una caida grande. Antes de operar en
real: repeti este proceso con historial real de FBS (exportado del bridge
una vez conectado) y volve a revisar los numeros.

## Credenciales

Nunca van al repositorio. `install.sh` las guarda en `.env` (con permisos
`600`, excluido por `.gitignore`). Si preferis no guardarlas en disco,
dejalas vacias en `.env` y exportalas como variables de entorno antes de
correr `run.sh` en su lugar.
