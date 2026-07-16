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
- **Sobre el "apalancamiento 1:1" de la cuenta: probablemente no aplica al
  oro.** Segun la documentacion propia de FBS, el apalancamiento de metales
  (oro incluido) esta **fijo en 1:500 y no se puede cambiar desde el Area
  de Trader** - es independiente del apalancamiento configurado para
  Forex en la cuenta. Con 1:1 literal sobre un contrato estandar de 100 oz,
  ni el lote minimo (0.01) entraria en una cuenta de $50 (~$4000 de
  margen requerido); con el 1:500 fijo que FBS aplica a metales, ese mismo
  lote minimo ronda los $8 de margen - perfectamente viable. El codigo NO
  asume ninguno de los dos numeros: `bridge/mt5_bridge_server.py` le pide
  a MT5 el margen real via `order_calc_margin()` (la misma cuenta que usa
  el broker para decidir si te deja operar) en vez de calcularlo a mano
  con el apalancamiento de la cuenta, y `core/risk_manager.py` **se niega
  a operar si ese margen real no alcanza**, en vez de forzar una orden que
  el broker rechazaria. Corre el backtest y una sesion en `DRY_RUN=true`
  primero para ver los numeros reales de tu cuenta - esto es evidencia de
  documentacion publica de FBS, no una garantia de como esta configurada
  tu cuenta especifica.
- **El bot no asume NINGUN apalancamiento fijo, funciona con el que tenga
  la cuenta.** No hay ningun "1:1" ni "1:500" hardcodeado en el codigo que
  realmente opera: el margen sale de `order_calc_margin()` (el mismo
  calculo que usa el broker) y el sizing por riesgo sale de
  `RISK_PER_TRADE_USD` en dolares, no de un numero de lotes fijo - ninguno
  de los dos depende de que apalancamiento tenga la cuenta. Con poco
  apalancamiento (ej. 1:1) el margen requerido para el lote minimo puede
  superar lo disponible en una cuenta chica; en ese caso el bot **rechaza
  la señal con un mensaje claro** (`core/risk_manager.py`) en vez de
  fallar o forzar una orden invalida - eso no es un bug, es matematica de
  margen real: ningun cambio de codigo hace que $50 alcancen para un
  contrato que necesita $4000 de margen. Si las señales se rechazan
  siempre por margen insuficiente, el diagnostico es el apalancamiento/
  balance de la cuenta, no el bot.
- **"1000 trades/dia" es un techo, no una meta.** `MAX_TRADES_PER_DAY`
  limita cuantos trades como maximo puede abrir el bot en un dia; cuantos
  realmente abre depende de que aparezcan señales validas y de que haya
  margen disponible. No fuerza operaciones para llegar a un numero.
- **`MAX_DAILY_DRAWDOWN_PCT` corta en tiempo real, no solo entre trades.**
  El stop-loss por defecto es ancho (ver la seccion de backtest mas abajo -
  `sl_atr_multiple=4.0`), asi que una posicion abierta puede acumular una
  perdida flotante grande antes de tocar su propio SL. `core/engine.py`
  revisa el equity (no solo el balance realizado) en cada ciclo y, si se
  pasa del limite de drawdown diario, **cierra de inmediato cualquier
  posicion abierta** en vez de esperar a que el SL la alcance por su
  cuenta.
- **Los niveles de take-profit se pueden configurar directamente en
  dolares de ganancia, no en pips.** Por defecto solo el primer escalon
  tiene un objetivo explicito en USD (`MIN_TP_USD`) y el resto son
  multiplos de esa distancia de precio - el comportamiento original, ya
  validado en el backtest de abajo. Si preferis que CADA escalon reserve
  su propio monto en dolares (mas facil de razonar: "este nivel cierra
  cuando llevo $0.60"), configura `TP_TARGETS_USD=0.28,0.60,1.20` en
  `.env` (uno por cada `TP_LEVELS`, separados por coma). Vacio = sin
  cambios respecto a antes. Ver `core/strategy.py::build_tp_ladder`.

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
./emergency_stop.sh         # PARADA DE EMERGENCIA: cierra posiciones abiertas y detiene el motor ya

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

`.github/workflows/ci.yml` corre exactamente `scripts/verify.sh` (mismo
script, no una definicion paralela de "pasa") mas `ruff` y `shellcheck` en
cada push/PR - no necesita Wine ni un broker, asi que corre en cualquier
runner de GitHub Actions estandar.

### Resiliencia

`run.sh` supervisa tanto el bridge MT5 como el motor: si cualquiera de los
dos se cae (crash de Wine, excepcion no manejada, perdida de conexion),
se reinicia solo con backoff exponencial (2s, 4s, 8s... hasta 60s) en vez
de tirar todo el sistema abajo. El cliente del bridge tambien reintenta
llamadas individuales y vuelve a loguearse solo si la sesion de MT5 se
cae sin que el proceso del bridge muera. Los logs quedan en `data/logs/`
(rotan automaticamente, no crecen sin limite). La tabla `account_snapshots`
(una fila por cada poll del motor, no solo por operacion) tampoco crece sin
limite: el motor borra filas mas viejas que `SNAPSHOT_RETENTION_DAYS`
(30 dias por defecto) en un chequeo cada una hora.

**Un cierre de posicion (SL, TP, cierre de emergencia) que falla a nivel
de red nunca deja una posicion fantasma bloqueando el motor.** Antes de
esto, si `close_partial` fallaba justo cuando la orden ya habia llegado al
broker (la orden se ejecuto pero la respuesta HTTP se perdio), el motor
seguia creyendo que la posicion estaba abierta para siempre: cada paso
intentaba cerrarla de nuevo, el broker respondia "no encontrada", y - como
el motor solo sostiene una posicion a la vez - eso bloqueaba cualquier
operacion nueva hasta un reinicio manual. Ahora, ante un fallo de cierre,
el motor primero revisa si el broker todavia tiene esa posicion: si la
tiene, la deja para reintentar en el proximo paso (fallo real, sin
cambios); si no la tiene, la reconcilia como cerrada con PnL marcado
explicitamente como no confirmado y sigue operando en vez de quedar
trabado.

**Lo mismo del lado de abrir una operacion, que es el caso mas peligroso:**
`open_order` es una llamada que modifica estado (a diferencia de una
lectura de precio), asi que un fallo de red justo despues de que la orden
ya se ejecuto en el broker no se puede asumir como "no paso nada" - hacerlo
arriesgaba mandar una SEGUNDA orden real duplicada en el siguiente intento,
doblando el riesgo real sin que nadie lo pidiera. Ahora, si `open_order`
falla, el motor revisa el estado real del broker antes de reintentar: si la
orden si se ejecuto, adopta esa posicion (en vez de abrir una nueva) y
sigue con una sola; si de verdad no se ejecuto, no queda nada rastreado y
la misma señal puede reintentarse limpio en el proximo ciclo.

**Modo de "filling" de la orden calculado dinamicamente, no fijo.**
`bridge/mt5_bridge_server.py` mandaba siempre `ORDER_FILLING_IOC` en cada
orden. MT5 exige que ese modo coincida con lo que el simbolo realmente
acepta (un bitmask que reporta `symbol_info`) - si no coincide, TODAS las
ordenes fallan con el error 10030, dejando el bot sin poder operar nunca
aunque el resto (bridge, sizing, señales) funcione perfecto. Ahora se
pregunta al broker que modos soporta ese simbolo en vez de asumir uno fijo
(IOC > FOK > RETURN, en ese orden de preferencia). Nota de honestidad: esto
sigue el patron documentado oficialmente por MetaTrader5 para este problema
conocido, pero no se pudo probar contra FBS real - este modulo solo corre
bajo el python de Windows dentro de Wine, que no esta disponible en este
entorno de desarrollo. Confirmalo en una sesion real antes de asumir que
resuelve algo que no se sabe si esta roto en tu cuenta especifica.

Para que arranque solo al iniciar el sistema (opcional, no se activa
por defecto): hay una plantilla de servicio systemd de usuario en
`scripts/xauusd-scalper.service.template` con instrucciones de instalacion
en el propio archivo.

### Parada de emergencia (interruptor manual)

`stop.sh` requiere acceso por terminal a la maquina que corre el bot. Para
cubrir el caso en que eso no este disponible (SSH caido, terminal
inaccesible, querés que alguien mas pueda frenarlo), el motor revisa en
cada ciclo si existe un archivo (`KILL_SWITCH_PATH` en `.env`, por defecto
`data/EMERGENCY_STOP`). Si existe:

1. Cierra a mercado cualquier posicion abierta inmediatamente (no espera
   a que el SL/TP la alcance).
2. Se detiene, y evita que `run.sh` lo reinicie solo (a diferencia de un
   crash comun, que si se reintenta con backoff).

```bash
./emergency_stop.sh            # activa: cierra posiciones y detiene el motor
./emergency_stop.sh --clear    # desactiva: borra el interruptor, listo para ./run.sh
```

Tambien alcanza con `touch data/EMERGENCY_STOP` desde cualquier cosa que
pueda escribir al filesystem (no hace falta el script ni una sesion de
shell interactiva). `scripts/doctor.sh` reporta si el interruptor esta
activo.

### Dashboard: hardening y un par de bugs reales

Revision del frontend (`dashboard/app.js`), verificada con Chromium real
via Playwright (no solo lectura de codigo):

- **Los mensajes de eventos ahora se escapan antes de insertarse en el
  DOM.** El manejador generico de errores del motor guarda texto crudo de
  excepciones en la tabla de eventos (`f"{type(exc).__name__}: {exc}"`),
  que el dashboard mostraba con `innerHTML` sin escapar - un mensaje de
  error con caracteres como `<` o `"` se habria interpretado como HTML en
  vez de mostrarse como texto. Probado inyectando un mensaje con
  `<img src=x onerror=...>`: ahora se ve como texto literal, no se
  ejecuta.
- **Los "hitbox" invisibles del grafico de equity (para el tooltip al
  pasar el mouse) usaban un ancho de columna que no coincidia con el
  espaciado real de los puntos**, asi que el tooltip podia no aparecer o
  corresponder al punto equivocado, mas notorio con pocos puntos o un
  grafico angosto. Corregido para usar el mismo espaciado que el trazado
  real.
- **Un solo endpoint del dashboard que fallara tumbaba TODO el refresco**
  (`Promise.all` fallaba entero si cualquiera de las 7 llamadas fallaba).
  Ahora cada seccion (tiles, curva de equity, grafico diario/mensual,
  tabla de trades, eventos) se actualiza de forma independiente - si una
  falla, las demas se siguen actualizando con normalidad en vez de dejar
  todo el dashboard en blanco.

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
movimiento de mercado real en vez de puro ruido sintetico. Esta seccion es
el historial completo y honesto de esa validacion, incluyendo el momento
en que un backtest con mas datos tumbo una conclusion anterior.

**Ronda 1 (5 dias reales de 1m).** Se encontraron y corrigieron dos bugs
del backtester: (a) solo miraba el precio de cierre de cada vela para
decidir si el SL/TP se habian tocado, ignorando el rango intra-vela
(`high`/`low`) - eso escondio una perdida de -$14 contra un limite
configurado de -$1; y (b) clasificaba mal ganadas/perdidas cuando una
operacion aseguraba ganancia en el primer TP y despues cerraba en
breakeven. Con esos bugs corregidos, la configuracion original
(`sl_atr_multiple=1.2`) perdia dinero de forma consistente - el stop era
demasiado ajustado para el ruido normal de 1 minuto en oro. Se agrego un
**trailing stop** despues del primer TP, y se probaron varios valores de
`sl_atr_multiple` con un split train/test 60/40: `4.0` fue el mas ancho
que se mantuvo positivo en ambos tramos, dando +$7.50 sobre $50 en los 5
dias completos (91.7% de operaciones ganadoras).

**Ronda 2 (8 dias reales de 1m - mas datos, misma metodologia) reveló que
la Ronda 1 estaba mal.** Con una ventana un poco mas larga, la MISMA
configuracion (`sl_atr_multiple=4.0`) **quebro la cuenta por completo**:
-$55 sobre $50 iniciales, balance final negativo, 108.8% de drawdown
maximo. Investigando operacion por operacion aparecio la causa real, un
**tercer bug, mas serio que los dos anteriores**: `size_position` siempre
opera como minimo el lote minimo del broker (0.01), incluso cuando ese
lote - dada una distancia de stop ancha por un pico de volatilidad -
implica arriesgar mucho mas que `RISK_PER_TRADE_USD`. Se reprodujo
exacto: un presupuesto de riesgo de $1 se convirtio en una perdida real
de $16 en una sola operacion; una secuencia de esas volo la cuenta en
horas. **Esto explica por que la Ronda 1 se vio bien**: la ventana de 5
dias que se uso para "validar" el parametro simplemente no incluyo ningun
pico de volatilidad lo bastante fuerte como para disparar el bug.

**La correccion:** `core/risk_manager.py` ahora calcula el riesgo real en
dolares del lote minimo antes de operar, y **rechaza la señal** si supera
`RISK_PER_TRADE_USD` por mas de un 50% de margen de redondeo (en vez de
operar igual con el lote minimo y comerse el riesgo real). Con esta
correccion, sobre los mismos 8 dias reales, el resultado a `RISK_PER_TRADE_USD=1.0`
es 0 operaciones (el filtro rechaza casi toda señal dada la volatilidad
real del oro con un lote de 0.01) - subiendo el riesgo por operacion a
$2-5 habilita mas trades pero **el resultado siguio siendo negativo en
todos los niveles probados** (-$5.48 a $2, -$7.80 a $3, -$9.97 a $5).

**Conclusion de la Ronda 2:** la combinacion actual de señal (Bollinger+RSI
en 1m) y gestion de stops **no muestra una ventaja real** sobre esta
muestra de datos reales, mas alla de la correccion de bugs de seguridad
que si son mejoras genuinas y quedan en el codigo. El trabajo pendiente
que quedo anotado: agregar un filtro de tendencia para no operar reversion
a la media en contra de un movimiento fuerte - exactamente lo que produjo
la secuencia de perdidas de la Ronda 2.

**Ronda 3: se agrego el filtro de tendencia (ADX) y se lo puso a prueba
con el mismo rigor.** `core/strategy.py` ahora calcula ADX(14) y descarta
cualquier señal de reversion a la media si ADX >= 35 (umbral estandar de
"tendencia fuerte" en analisis tecnico, no ajustado a este dataset). Con
el filtro fijo en su umbral por defecto, sobre los mismos 8 dias reales
(risk-usd=$3, split 60/40): TRAIN -$0.42 (6 operaciones, 83.3% ganadas),
TEST -$4.74 (5 operaciones, 60% ganadas, 13.2% drawdown maximo). Sin el
filtro (umbral 99, efectivamente desactivado), el mismo split: TRAIN
+$0.38 (7 operaciones), TEST **-$8.19 (7 operaciones, 21.1% drawdown
maximo)**. El filtro reduce la perdida y el drawdown fuera de muestra de
forma clara - hace exactamente lo que se diseño para hacer (evitar las
peores operaciones, las que pelean contra una tendencia confirmada) - pero
**no convierte la estrategia en ganadora**.

Se probaron tambien umbrales de ADX mas ajustados (15-22) buscando un
resultado positivo: en el tramo de entrenamiento llegaron a mostrar 100%
de aciertos, pero eso fue sobre 3 a 6 operaciones - una muestra demasiado
chica para significar nada - y en el tramo de prueba la mayoria no genero
ninguna operacion o perdio la unica que hizo. Se descarto ese resultado
en vez de reportarlo como un hallazgo: es la misma trampa de sobreajuste
que ya aparecio en la Ronda 1, esta vez mas facil de detectar porque el
conteo de operaciones era demasiado bajo para tomarlo en serio.

**Conclusion honesta acumulada (Rondas 1-3):** el objetivo original del
proyecto (miles de operaciones diarias con ganancias altas y perdidas
minimas) sigue sin ser alcanzable con esta estrategia sobre gold real.
Lo que si son mejoras genuinas y quedan en el codigo: los tres bugs de
seguridad corregidos (deteccion de cruces intra-vela, clasificacion de
ganada/perdida, tope de riesgo del lote minimo) y el filtro de tendencia,
que reduce la severidad de las perdidas sin prometer rentabilidad. El
trabajo pendiente real para encontrar una ventaja genuina necesitaria
mucho mas historial (semanas a meses, no dias) del feed real de FBS -
con la cantidad de datos disponible en este entorno, cualquier resultado
mas optimista que este seria, con alta probabilidad, ruido estadistico
maquillado de señal. Antes de operar en real: repeti este proceso con
historial real de FBS (exportado del bridge una vez conectado).

## Credenciales

Nunca van al repositorio. `install.sh` las guarda en `.env` (con permisos
`600`, excluido por `.gitignore`). Si preferis no guardarlas en disco,
dejalas vacias en `.env` y exportalas como variables de entorno antes de
correr `run.sh` en su lugar.
