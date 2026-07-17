# XAUUSD 1m Scalper (FBS / MT5) + Dashboard

Bot de scalping para oro (XAUUSD) en temporalidad 1 minuto, pensado para
correr contra una cuenta MT5 de FBS en Linux, con gestion de riesgo real
y un dashboard nativo para ver resultados.

## Lee esto antes de usarlo

- **No existe una estrategia que gane siempre.** Este bot toma señales
  de reversion a la media (Bandas de Bollinger + RSI en 1m) filtradas por
  spread y volatilidad. Es una estrategia razonable, no una maquina de
  dinero garantizado. Va a perder trades — el objetivo del diseño es que
  cada perdida este acotada (`RISK_PER_TRADE_USD` en `.env`, 3 USD por
  defecto - ver "Ronda 6" mas abajo para por que no es 1) y que el sistema
  se detenga solo si el dia se pone feo
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
run.sh              -> --start/--stop/--status del servidor (bridge + dashboard);
                        el motor (el bot en si) se arranca/para SOLO desde el
                        dashboard, no con run.sh - ver "Uso" mas abajo
install.sh           -> instala todo, auto-detectando la plataforma (Kali/Ubuntu/Termux)
main.py              -> entrypoint del motor (usa core/engine.py)
dashboard.py          -> dashboard: ventana nativa (pywebview) o pagina web (--web, puerto 9000)

core/
  config.py           -> carga .env
  risk_manager.py      -> sizing de posicion + limites diarios
  strategy.py           -> señal (Bollinger+RSI) + escalera de TP
  signals.py             -> señales extra opcionales + CompositeStrategy (ver Ronda 4)
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

### Plataformas soportadas (auto-detectadas)

`install.sh` y `run.sh` detectan solos en que sistema estan corriendo
(`/etc/os-release`, o las variables que Termux define) y ajustan que
instalan/ejecutan - no hace falta pasarles ningun flag de plataforma.

- **Kali Linux y Ubuntu** (o cualquier Debian-like con `apt-get`): soporte
  completo. `install.sh` instala Wine + el terminal MT5 real + un Python
  de Windows dentro de Wine, asi que esta maquina puede correr un bridge
  local y operar en una cuenta real de punta a punta.
- **Termux en Android, sin root**: soporte real pero **deliberadamente
  parcial**, y esto no es una limitacion de este proyecto sino de Wine en
  si - no existe una forma confiable de correr una aplicacion Win32 GUI
  real (el terminal MT5) bajo Wine en Android sin root; las combinaciones
  experimentales con proot/box64 que existen no son algo que este script
  pueda honestamente prometer que funcionan para un bot con dinero real.
  Por eso, en Termux `install.sh` instala **solo el lado Python puro**
  (motor, dashboard, backtest - nada de eso necesita Wine) y configura la
  maquina como **cliente de un bridge remoto**: `run.sh` en el telefono no
  intenta levantar Wine local, sino que le habla por HTTP a un bridge
  corriendo en una Kali/Ubuntu real (`MT5_BRIDGE_URL` en `.env` apuntando
  a esa otra maquina). Lo que SI funciona 100% local en Termux sin ninguna
  otra maquina: el dashboard (`dashboard.py --web`), los backtests, y
  `.venv/bin/python main.py --synthetic` para probar el motor con precios
  simulados - los ultimos dos necesitan `pandas`, que en algunos
  telefonos/toolchains de Termux directamente no compila (PyPI no publica
  wheels precompilados para Android; el compilado desde el codigo fuente
  puede fallar por una incompatibilidad real del toolchain con el codigo
  SIMD de numpy para ARM, no un bug de este script). Si eso pasa,
  `install.sh` no aborta: el dashboard queda funcionando igual (nunca usa
  pandas), solo motor/backtests locales quedan sin disponibles ahi. Un
  fallo se recuerda para no reintentar un compilado de hasta 90 minutos
  en cada corrida - `./install.sh --skip-pandas` lo salta directamente,
  `./install.sh --retry-pandas` fuerza un nuevo intento.
- **Otras distros Linux con `apt-get`** (Debian, Mint, etc.): deberian
  funcionar por el mismo camino que Kali/Ubuntu, sin garantia especifica.
- **Cualquier otra cosa** (Fedora, Arch, sin `apt-get` y no Termux):
  `install.sh` avisa que no reconoce la plataforma, instala igual el lado
  Python puro, y te deja instalar Wine/MT5 a mano antes de reintentar.

`./run.sh doctor` siempre muestra que plataforma detecto en la primera
linea, y ajusta sus propios chequeos (por ejemplo, en Termux no reporta
"falta Wine" como un error - ahi eso es exactamente lo esperado).

## Instalacion

```bash
./install.sh
```

En Kali/Ubuntu: instala dependencias de sistema (apt), crea el venv de
Linux, instala Wine + el terminal MetaTrader 5 + un Python de Windows
dentro de Wine con el paquete `MetaTrader5`, y crea `.env` a partir de
`.env.example` (pidiendo login/password/server de forma interactiva; ese
archivo queda en `.gitignore` y nunca se commitea). En Termux hace lo
mismo pero solo para el lado Python puro (ver arriba).

`install.sh` descarga instaladores desde `download.mql5.com` y
`python.org` (solo en el camino Kali/Ubuntu); necesitas salida a internet
para esos dos dominios. Si tu red los bloquea, descarga `mt5setup.exe` y
el instalador de Python 3.11 manualmente y ajusta las rutas al inicio del
script.

## Uso

`run.sh` arranca el **servidor** (el bridge MT5 + el dashboard) - nunca el
motor (el bot que abre operaciones). El motor se prende y se apaga
**exclusivamente desde el dashboard** (boton "Iniciar motor" / "Detener
motor", o `POST /api/engine/start` / `/api/engine/stop`), a proposito: asi
nunca arranca a operar solo (por ejemplo al reiniciar la maquina con el
systemd template) sin que alguien lo haya prendido mirando el dashboard.

```bash
./run.sh --start          # arranca el bridge MT5 + el dashboard en segundo plano
./run.sh --status          # reporte prolijo: plataforma, bridge, dashboard, motor, cuenta
./run.sh --stop             # apaga TODO (motor si estaba corriendo, dashboard, bridge, Xvfb) - no queda nada corriendo

./run.sh emergency-stop       # PARADA DE EMERGENCIA: cierra posiciones abiertas y detiene el motor ya
./run.sh verify                # compila todo, corre los tests y una prueba de humo del motor
./run.sh doctor                  # diagnostico de la instalacion real (Wine, MT5, bridge, .env, disco...)
```

Con el servidor arriba (`./run.sh --start`), abrí
`http://127.0.0.1:9000` (o la ventana nativa - ver mas abajo) y desde ahi
arrancá el motor cuando estes listo. `./run.sh --status` refleja el estado
real del motor consultando la propia API del dashboard, asi que los dos
nunca se desincronizan.

```bash
.venv/bin/python dashboard.py       # pregunta: ventana nativa o web
.venv/bin/python dashboard.py --web # directo como pagina web en http://127.0.0.1:9000
.venv/bin/python dashboard.py --web --host 0.0.0.0 --port 9000  # accesible desde otro dispositivo en tu red

.venv/bin/python scripts/fetch_market_data.py --interval 1m --range 5d   # historial real (proxy) para backtestear
.venv/bin/python scripts/run_backtest.py --csv data/gold_history_1m.csv    # backtest con ese historial
.venv/bin/python scripts/run_backtest.py --csv data/gold_history_1m.csv --composite --leverage 500  # + señales extra (ver Ronda 4)

.venv/bin/python main.py --synthetic   # motor SIN broker, precios simulados, solo para probar en la terminal
                                        # (no pasa por el dashboard - ./run.sh verify usa esto mismo para su prueba de humo)
```

`install.sh` y `run.sh` son los unicos dos archivos `.sh` del proyecto -
la parada de emergencia, verificar, y el diagnostico son subcomandos de
`run.sh` (`emergency-stop`, `verify`, `doctor`), no scripts separados. Por
ahora `--start`/`--stop`/`--status` son los unicos comandos de arranque del
servidor - cualquier otro (incluido sin argumentos) muestra la ayuda.

`./run.sh doctor` es el primer comando a correr cuando algo no funciona: no
instala ni cambia nada, solo te dice exactamente que falta (Wine, el
terminal MT5, el python de Wine, credenciales en `.env`, el bridge
corriendo, espacio en disco...) en vez de tener que adivinar o re-correr
`install.sh` a ciegas. En Termux ajusta solo sus propios chequeos (ver
"Plataformas soportadas" mas arriba). `install.sh` ya corre este mismo
diagnostico solo al final de instalar, y falla con codigo de salida
distinto de cero si algo sigue faltando - no se queda en "deberia estar
todo bien" sin comprobarlo.

`.github/workflows/ci.yml` corre exactamente `./run.sh verify` (mismo
codigo, no una definicion paralela de "pasa") mas `ruff` y `shellcheck` en
cada push/PR - no necesita Wine ni un broker, asi que corre en cualquier
runner de GitHub Actions estandar.

### Resiliencia

`./run.sh --start` supervisa tanto el bridge MT5 como el dashboard: si
cualquiera de los dos se cae (crash de Wine, excepcion no manejada,
perdida de conexion), se reinicia solo con backoff exponencial (2s, 4s,
8s... hasta 60s) en vez de tirar todo el sistema abajo. El motor, una vez
arrancado desde el dashboard, tiene su propio supervisor independiente con
la misma politica de reintentos (`core/engine_supervisor.py`) - un crash
del motor no tira el dashboard ni el bridge, y viceversa. El cliente del bridge tambien reintenta
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

Tanto `./run.sh --stop` como el boton "Detener motor" del dashboard
requieren acceso a la maquina que corre el bot (terminal o red local). Para
cubrir el caso en que ninguno de los dos este disponible (SSH caido,
dashboard inalcanzable, querés que alguien mas pueda frenarlo con solo
tocar un archivo), el motor revisa en cada ciclo si existe un archivo
(`KILL_SWITCH_PATH` en `.env`, por defecto `data/EMERGENCY_STOP`). Si
existe:

1. Cierra a mercado cualquier posicion abierta inmediatamente (no espera
   a que el SL/TP la alcance).
2. Se detiene, y evita que `core/engine_supervisor.py` lo reinicie solo (a
   diferencia de un crash comun, que si se reintenta con backoff).

```bash
./run.sh emergency-stop            # activa: cierra posiciones y detiene el motor
./run.sh emergency-stop --clear    # desactiva: borra el interruptor, listo para arrancar el motor de nuevo desde el dashboard
```

Tambien alcanza con `touch data/EMERGENCY_STOP` desde cualquier cosa que
pueda escribir al filesystem (no hace falta el subcomando ni una sesion de
shell interactiva). `./run.sh doctor` reporta si el interruptor esta
activo.

### Dashboard: ventana nativa o pagina web

```bash
.venv/bin/python dashboard.py       # terminal interactiva: pregunta que modo usar
.venv/bin/python dashboard.py --native  # fuerza ventana nativa (salta la pregunta)
.venv/bin/python dashboard.py --web     # dashboard web en http://127.0.0.1:9000
.venv/bin/python dashboard.py --web --host 0.0.0.0 --port 9000  # accesible desde otro dispositivo de tu red
```

Sin flags y desde una terminal interactiva, pregunta que modo usar. Sin
flags y sin terminal interactiva (ej. lanzado desde otro script), usa
ventana nativa - el comportamiento original, sin cambios. `--web` corre
exactamente el mismo Flask que ya servia la ventana nativa, pero
escuchando directamente en el puerto indicado (9000 por defecto) en vez
de solo internamente para pywebview - se puede abrir desde cualquier
navegador.

La mayoria de las rutas son de solo lectura (balance, curva de equity,
historial de trades, eventos). Las que no lo son - guardar Settings,
pausar/reanudar, e **iniciar/detener el motor** (`POST
/api/engine/start|stop`, ver mas abajo) - nunca abren ni cierran una
posicion directamente: arrancan o paran el *proceso* del motor, que es
quien decide cuando operar segun la estrategia configurada. Aun asi,
`--host 0.0.0.0` expone esos datos reales de la cuenta y esos controles a
cualquiera en tu red local; el programa avisa esto por consola al
arrancar, y `DASHBOARD_AUTH_TOKEN` (ver mas abajo) es la forma de
protegerlos si vas a usar `0.0.0.0`. Para acceso solo desde esta maquina
(el default, mas seguro), dejá `--host 127.0.0.1` sin tocar.

pywebview (la libreria de la ventana nativa) ahora se importa solo cuando
se usa ese modo, no al cargar el archivo - `--web` funciona incluso en
una maquina sin ningun toolkit grafico instalado (un servidor headless).

### Dashboard: pestaña Settings, y controlar el motor desde ahi

Tres controles, disponibles en los dos modos (nativo y web, mismo
frontend), cada uno resolviendo un problema distinto a proposito:

**Boton "Iniciar motor" / "Detener motor"** (arriba a la derecha, pill
"Motor corriendo"/"Motor detenido" al lado). Es el arranque/parada real
del *proceso* del motor - equivalente a lo que antes hacia `./run.sh` a
secas. `./run.sh --start` deliberadamente NO prende esto: solo levanta el
bridge y el dashboard, y el motor queda apagado hasta que alguien lo
prenda aca, mirando el dashboard. Internamente lanza
`core/engine_supervisor.py` (el mismo proceso detached, crash-resiliente
con backoff que antes manejaba `run.sh`) via `POST /api/engine/start`;
detenerlo manda SIGTERM via `POST /api/engine/stop` y espera hasta ~15s
un cierre limpio antes de forzarlo. El boton "Pausar entradas" de abajo
queda deshabilitado mientras el motor esta detenido - no tiene nada que
pausar.

**Boton "Pausar entradas" / "Reanudar entradas"**. Pausa - no es el
interruptor de emergencia ni apaga el proceso: deja de abrir operaciones
NUEVAS pero sigue gestionando y protegiendo cualquier posicion ya abierta
(SL/TP siguen activos), y el motor sigue corriendo - no hace falta
reiniciar nada, reanudar es instantaneo. Internamente toca/borra un
archivo (`PAUSE_FLAG_PATH` en `.env`, `data/PAUSED` por defecto) que el
motor ya revisa en cada paso. Si necesitas cerrar posiciones abiertas YA
en vez de solo pausar, eso sigue siendo `./run.sh emergency-stop` (ver mas
arriba) - tres controles distintos, cada uno para un caso distinto.

**Pestaña Settings**: cuenta MT5 (login, password, servidor), si es demo,
DRY_RUN, y los limites de riesgo (`RISK_PER_TRADE_USD`,
`MAX_DAILY_LOSS_USD`, `MAX_DAILY_DRAWDOWN_PCT`, `MAX_TRADES_PER_DAY`) -
editables sin tocar `.env` a mano. **Funciona con cualquier broker
compatible con MetaTrader 5, no solo FBS** - el campo "Servidor" es texto
libre (ej. `FBS-Demo`, `ICMarkets-Live07`, `Pepperstone-Demo01`...), y
`bridge/mt5_bridge_server.py` ya aceptaba cualquier servidor desde antes
de que existiera esta pestaña (`mt5.login()` no esta atado a FBS en
ningun lado del codigo).

Detalles importantes:
- **Los cambios se guardan en la base de datos local** (tabla
  `bot_settings`, sobrevive reinicios) y **se aplican la proxima vez que
  arranca el motor** (boton "Iniciar motor" en el dashboard) - a proposito
  NO son en caliente. Cambiar
  de cuenta/servidor mientras el motor ya esta corriendo (y quizas con una
  posicion abierta) es un riesgo real de confundir en que cuenta esta
  parada esa posicion, asi que esto pide un reinicio en vez de intentar un
  hot-reload. `/api/status` (los pills de arriba) siempre muestra la
  cuenta con la que el motor YA esta conectado, nunca un cambio pendiente
  sin aplicar - para eso esta la pestaña Settings.
- **El password nunca se muestra ni se re-envia en texto plano.** El campo
  siempre carga vacio; dejarlo vacio al guardar significa "no lo toques",
  no "borralo". La API solo informa si hay uno guardado (`has_password`),
  jamas su valor.
- **Autenticacion en las rutas que modifican algo.** Igual que el bridge,
  `DASHBOARD_AUTH_TOKEN` (vacio por defecto, `install.sh` NO lo genera
  solo a diferencia de `BRIDGE_AUTH_TOKEN` - ver por que abajo) protege
  `POST /api/settings` y `POST /api/bot/pause|resume` con el header
  `X-Dashboard-Token`; las rutas de solo lectura no lo piden. Si el
  dashboard pide el token (ventana emergente del navegador) y no lo
  configuraste, la accion que intentabas hacer simplemente no se aplica.
  Por que no se genera automaticamente como el del bridge: el bridge
  siempre esta
  expuesto al mismo riesgo (ejecuta ordenes reales) sin importar el modo;
  el dashboard solo necesita el token cuando elegis exponerlo con
  `--web --host 0.0.0.0` - forzarlo siempre agregaria una pregunta de
  token hasta para el uso 100% local (ventana nativa), que no gana nada
  de seguridad real ahi. `./run.sh doctor` reporta si esta configurado.

### Dashboard: rediseño visual

Ventana nativa y dashboard web sirven exactamente el mismo
`dashboard/index.html` + `app.js` + `style.css` desde el mismo Flask -
cualquier mejora visual aplica a los dos por igual, no hay nada que portar
entre uno y otro. Cambios (siguiendo una metodologia de diseño de datos con
validacion de paleta por contraste/daltonismo, no elegida a ojo):

- **Selector de tema claro/oscuro manual**, con boton en el header (antes
  solo seguia la preferencia del sistema operativo). Se guarda en
  `localStorage` y persiste entre sesiones.
- **Colores de estado separados de los colores de texto.** Los badges de
  estado (conectado/dry-run/desconectado, nivel de evento) usan una paleta
  fija que no cambia entre tema claro/oscuro - la severidad significa lo
  mismo en los dos temas. El texto de P&L/deltas usa una paleta aparte que
  si se ajusta por tema (mismo criterio que usan sistemas de diseño
  validados: colores de "identidad/severidad" fijos, colores de "texto"
  adaptados al fondo). Paleta verificada con un validador automatico de
  contraste y separacion por daltonismo, no a ojo.
- **Grafico de equity: crosshair real en vez de cajas de hover diminutas.**
  Antes cada punto tenia su propia caja invisible de hover - con la curva
  mostrando hasta 300 lecturas en un grafico de ~600px, cada caja terminaba
  siendo de un par de pixeles de ancho, virtualmente imposible de acertar
  con el mouse. Ahora hay una sola zona de hover que calcula el punto mas
  cercano al cursor y muestra una linea vertical + tooltip, sin importar
  cuantos puntos tenga la curva.
- **Barras de P&L con esquinas redondeadas solo en el extremo del dato**,
  no en el extremo que toca la linea base (una barra "crece" desde cero;
  redondear ese extremo la hacia parecer flotando en vez de anclada).
- **Tooltips con el valor primero, la etiqueta despues** (el dato es lo que
  se busca al pasar el mouse, no el nombre de la serie).
- El tile de equity ahora muestra el cambio real (▲/▼ en USD) desde el
  inicio del grafico visible - antes ese elemento existia en el HTML pero
  nunca se llenaba con datos.
- Favicon propio, scrollbar con estilo consistente, estados vacios con
  icono en vez de solo texto.

Verificado con capturas de pantalla reales (Chromium headless via
Playwright) en modo claro, oscuro, con datos, vacio, y con el mouse sobre
los graficos - no solo lectura de codigo.

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

**Ronda 4: se probaron 5 señales M1 nuevas, independientes de la reversion a
la media - adaptadas de un EA de referencia (MQL5) que me pasaron, con una
diferencia deliberada e importante.** Ese EA gana confirmaciones (12
filtros direccionales: cruces de EMA multi-timeframe, RSI, ventanas de
sesion, ruptura de rango, estructura de swings) pero su gestion de riesgo
real es un grid/martingale: ninguna orden individual lleva stop-loss, agrega
mas posiciones si el precio sigue en contra y espera un take-profit sobre el
precio promedio de todo el grupo - el mismo patron que ya casi quebro una
cuenta en el backtest de este proyecto (Ronda 2, arriba). Se descarto ese
mecanismo a proposito (decision confirmada explicitamente antes de tocar
codigo) y se tomaron solo las ideas de señal, cada una pasando por el mismo
`RiskManager` y el mismo `RISK_PER_TRADE_USD` que la reversion a la media -
ver `core/signals.py` para el detalle completo de que se adapto y por que.

Las 5 señales (cruce de EMA9/21 en M1 confirmado por la pendiente de EMA50
en M5, cruce de RSI por la linea de 50 con histeresis, vela direccional
fuerte, apertura de sesion Londres/Nueva York, y ruptura del rango
asiatico) se probaron sobre los mismos 7 dias reales de oro COMEX
(`scripts/fetch_market_data.py`, risk-usd=$3, apalancamiento 1:500 fijo de
metales en FBS - ver la nota de apalancamiento mas arriba), cada una sola
encima de la reversion a la media, y las 5 juntas:

| Configuracion | Trades | Trades/dia | Win rate | PnL | Drawdown max |
|---|---|---|---|---|---|
| Solo reversion a la media (baseline) | 17 | ~3.2 | 82.4% | -$0.79 | 12.6% |
| + cruce de EMA (momentum) | 137 | ~25.8 | 75.9% | -$37.22 | 74.9% |
| + histeresis de RSI | 79 | ~14.9 | 73.4% | -$36.85 | 75.3% |
| + vela direccional | 85 | ~16.0 | 67.1% | -$37.02 | 75.3% |
| + apertura de sesion | 82 | ~15.4 | 76.8% | -$24.97 | 54.9% |
| + ruptura de rango asiatico | 51 | ~9.6 | 78.4% | -$11.93 | 34.8% |
| Las 5 juntas | 116 | ~21.8 | 70.7% | -$38.78 | 78.6% |

**El objetivo de frecuencia SI se cumple - cada señal, sola, multiplica los
trades/dia entre ~3x y ~8x la reversion a la media sola.** Pero el
resultado en dolares es peor en las 5, no mejor: el win rate se mantiene
alto (67-78%) pero no compensa perdidas individuales mas grandes,
exactamente el mismo desbalance ganancia/perdida que ya aparecio en las
Rondas 1-3 para la reversion a la media, aca mas marcado porque estas 5
señales son de CONTINUACION (siguen un movimiento que ya empezo) y
comparten el mismo primer nivel de TP chico y dolar-anclado
(`MIN_TP_USD=0.28`) que la reversion a la media, la cual sí puede justificar
un TP chico porque entra EN un extremo estadistico. Una entrada de
continuacion entrando a mitad de un movimiento no tiene esa misma
justificacion para un TP tan ajustado frente a su propio stop.

**Por eso las 5 quedan en el codigo, probadas y documentadas, pero
APAGADAS por defecto** (`STRAT_ENABLE_*=false` en `.env.example`) - activar
una es una decision informada de quien corra su propio backtest despues de
ajustarla, no un cambio de comportamiento por defecto. Esto es exactamente
lo que evita este proyecto en cada ronda: nunca reportar un numero
optimista sin haberlo corrido primero, y nunca activar por defecto algo que
el propio backtest muestra que empeora el resultado.

**Ronda 5: ladder de TP adaptativo a la volatilidad (`vol_ratio`), pedido
explicitamente para intentar arreglar el desbalance encontrado en la Ronda
4.** `core/strategy.py::build_tp_ladder` ahora recibe un `vol_ratio` (ATR
actual dividido por un ATR base mas lento, acotado a 0.5-2.0) que separa
mas los niveles de TP 2+ en mercados volatiles (mas espacio para que un
movimiento real pague mas) y los junta en mercados tranquilos (la escalera
se completa mas rapido, mas operaciones realizadas por dia). El primer
nivel (`MIN_TP_USD`) queda **exactamente igual que antes** - "fijo" en el
pedido del usuario significa eso: ese piso en dolares no se toca, solo el
espaciado de lo que viene despues es "inteligente". Se probo tambien subir
`TP_LEVELS` de 3 a 5 (mas escalones = mas capital que puede salir en
distintos niveles de la escalera en vez de solo 3 cortes).

| Configuracion | Trades | Trades/dia | Win rate | PnL | Drawdown max |
|---|---|---|---|---|---|
| Reversion a la media, ladder adaptativo (TP_LEVELS=3) | 17 | ~3.2 | 82.4% | -$0.95 | 12.6% |
| Reversion a la media, ladder adaptativo (TP_LEVELS=5) | 17 | ~3.2 | 82.4% | **-$0.13** | 12.5% |
| Las 5 señales extra, ladder adaptativo (TP_LEVELS=3) | 116 | ~21.8 | 70.7% | -$38.01 | 77.2% |
| Las 5 señales extra, ladder adaptativo (TP_LEVELS=5) | 123 | ~23.1 | 69.9% | -$39.25 | 79.8% |

**Resultado honesto, en dos partes distintas:**

Para la reversion a la media (la unica estrategia con historial real
validado), el ladder adaptativo con `TP_LEVELS=5` **si ayuda, y bastante**:
de -$0.79 (Ronda 3, ladder fijo) a -$0.13 - practicamente breakeven sobre
esta muestra de 7 dias, sin cambiar ni una señal de entrada, solo como se
reparte la salida. Tiene sentido: una reversion a la media que funciona
tipicamente SI atraviesa varios niveles de TP en su camino de vuelta hacia
el promedio, asi que una escalera con mas escalones y espaciado ajustado a
la volatilidad real aprovecha mejor ese recorrido.

Para las 5 señales extra de la Ronda 4, el mismo ladder **no arregla el
problema real**: -$38.01 y -$39.25 son practicamente lo mismo que el
-$38.78 del ladder fijo original. La razon, investigada en vez de asumida:
estas señales son de continuacion, no de reversion - o el movimiento sigue
de una y atraviesa varios niveles de TP rapido, o se da vuelta y toca el
stop casi de inmediato, sin el recorrido gradual que hace que un ladder
mas fino ayude. El desbalance real (stop proporcionalmente mas ancho que
lo que tarda en llegar CUALQUIER TP) no es un problema de forma de la
escalera, es un problema de que el stop de estas señales sigue siendo
demasiado ancho para su propio patron de resultado binario. Siguen
**apagadas por defecto** - este ladder adaptativo no cambia esa
recomendacion.

**Ronda 6: se encontro (y corrigio) un bug de herramienta que invalidaba
las lecturas de riesgo, y se re-verifico `RISK_PER_TRADE_USD` con datos
reales frescos.** Al pedir una nueva pasada de mejoras se descargaron 7
dias reales de oro COMEX terminados hoy mismo (`scripts/fetch_market_data.py`)
y se corrio un barrido de `RISK_PER_TRADE_USD` con la misma metodologia
train/test 60/40 de siempre. Primer resultado: **0 operaciones en TODOS
los niveles probados, de $1 a $8** - mucho mas severo que lo esperado.
Investigando en vez de asumir que "la estrategia no genera señales" (que
hubiera sido la conclusion facil y equivocada), aparecio la causa real: el
**default de `--leverage` en `scripts/run_backtest.py` estaba en 1**, no en
500 (el apalancamiento real de metales en FBS, ver la nota mas arriba). Con
apalancamiento 1:1 el margen requerido para el lote minimo en oro a ~$4000
es ~$4070 - imposible sobre un balance de $50 - asi que CUALQUIER señal se
rechazaba por margen antes de siquiera llegar al chequeo de riesgo,
indistinguible de "no hay señales" sin mirar el motivo exacto del rechazo.
Este bug era **solo de la herramienta de backtest** (el motor en vivo/
DRY_RUN siempre usa el apalancamiento real que reporta el broker via
`account.leverage`, nunca este default) pero **invalidaba silenciosamente
cualquier barrido de riesgo corrido con el CLI sin pasar `--leverage 500`
a mano** - potencialmente incluyendo lecturas pasadas. Se corrigio el
default a 500 (`scripts/run_backtest.py`).

Con el apalancamiento corregido, el barrido de `RISK_PER_TRADE_USD` (mismo
split, `STRAT_SL_ATR_MULTIPLE=4.0`, `TP_LEVELS=5`) dio:

| RISK_PER_TRADE_USD | TRAIN trades | TRAIN win% | TRAIN PnL | TRAIN DD | TEST trades | TEST win% | TEST PnL | TEST DD |
|---|---|---|---|---|---|---|---|---|
| 1.0 | 0 | - | $0.00 | 0% | 0 | - | $0.00 | 0% |
| 2.0 | 1 | 0% | -$2.88 | 5.8% | 0 | - | $0.00 | 0% |
| 3.0 | 10 | 80.0% | -$1.13 | 13.0% | 10 | 80.0% | -$1.19 | 7.4% |
| 5.0 | 80 | 87.5% | -$9.13 | **54.9%** | 56 | 92.9% | +$11.30 | 15.5% |

**Confirma exactamente lo que la Ronda 2 ya habia encontrado, ahora con
datos de hoy:** con el default publicado hasta ahora (`RISK_PER_TRADE_USD=1.0`),
el bot **no puede operar en absoluto** a los precios actuales del oro - no
es conservador, esta inerte. `2.0` sigue siendo insuficiente (1 sola
operacion en todo el tramo de train, 0 en test - muestra demasiado chica
para significar nada). `5.0` genera volumen real (80+56 operaciones) pero
es **inestable entre mitades**: +$11.30 en test contra un drawdown de
**54.9% en train** - una sola mala racha se comio mas de la mitad de la
cuenta en la mitad de entrenamiento, exactamente el patron de riesgo que
este proyecto evita en cada ronda anterior. `3.0` es el unico nivel
**consistente entre train y test** (10 operaciones en cada mitad, ~80% de
aciertos en ambas, resultado levemente negativo y de magnitud similar en
las dos: -$1.13 y -$1.19, drawdown razonable de 7-13%) - y coincide, en
orden de magnitud, con lo que la Ronda 3 ya habia reportado a este mismo
nivel de riesgo (-$0.42 / -$4.74).

**La decision, honesta:** se sube el default de `RISK_PER_TRADE_USD` de
1.0 a **3.0** en `.env.example` y `core/config.py`. Esto **no es una
afirmacion de que la estrategia ahora es rentable** - sigue siendo
levemente negativa en ambos tramos, igual que en la Ronda 3. Es el piso
minimo para que el bot pueda generar operaciones reales y consistentes en
absoluto con el precio actual del oro y el lote minimo del broker, en vez
de quedarse inerte silenciosamente mientras el operador cree que esta
"probando en modo seguro". `5.0` se probo y se descarta como default por
inestable (el drawdown del 54.9% en una mitad es motivo suficiente,
independientemente de que la otra mitad haya dado ganancia). Sigue
pendiente el mismo trabajo real de las rondas anteriores: mas historial
(semanas/meses del feed real de FBS, no dias de un proxy COMEX) antes de
confiar en que $3.0 - o cualquier otro numero - sea el nivel correcto para
una cuenta real.

## Credenciales

Nunca van al repositorio. `install.sh` las guarda en `.env` (con permisos
`600`, excluido por `.gitignore`). Si preferis no guardarlas en disco,
dejalas vacias en `.env` y exportalas como variables de entorno antes de
correr `run.sh` en su lugar.
