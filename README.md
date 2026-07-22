# XAUUSD Scalper · MT5 + Web Dashboard

Bot algorítmico para XAUUSD con ejecución a través de MetaTrader 5, gestión
de riesgo basada en la especificación real del broker, backtesting histórico y
un dashboard web responsive. El proyecto está pensado para Kali/Ubuntu con
Wine + MT5.

> **Advertencia:** operar derivados implica riesgo de pérdida total. Ningún
> resultado histórico garantiza ganancias futuras. Empieza en una cuenta demo,
> usa `DRY_RUN=true` y valida el símbolo, margen y apalancamiento que informa
> tu broker antes de activar órdenes reales.

## Estado del proyecto

- Dashboard: web únicamente (`dashboard.py` ya no abre una ventana nativa).
- Broker principal: MT5 mediante `bridge/mt5_bridge_server.py`.
- Estrategia base: Bollinger + RSI + ATR, con TP escalonado, breakeven y
  trailing stop.
- Opcionales: confluencia Quantum Queen de 12 votos, régimen de mercado,
  grid/piramidación limitada y recuperación controlada. Están apagados por
  defecto y nunca saltan el `RiskManager`.
- Calidad actual: `220 passed, 1 skipped` en la suite local. El test omitido
  requiere un CSV histórico que no está incluido en el repositorio.

## Arquitectura

```text
run.sh ──┬─ MT5 bridge (Wine/Windows Python) ── MetaTrader 5 ── Broker
         └─ dashboard.py (Flask) ── dashboard/{index.html,app.js,style.css}

dashboard/API ── core/backtest.py ── core/signals.py ── RiskManager
                              └── SQLite (data/trades.db)
```

Componentes principales:

| Ruta | Responsabilidad |
|---|---|
| `bridge/mt5_bridge_server.py` | Sesión MT5, velas, ticks, cuenta y órdenes. |
| `core/engine.py` | Ciclo de mercado, señales, sizing y gestión de posiciones. |
| `core/strategy.py` | Estrategia base y escalera de take-profit. |
| `core/signals.py` | CompositeStrategy y señales adicionales. |
| `core/quantum_queen.py` | Port de los 12 votos Quantum Queen. |
| `core/regime.py` | Clasificación `trend/range/volatile/quiet`. |
| `core/risk_manager.py` | Riesgo monetario, margen, drawdown y límites diarios. |
| `core/backtest.py` | Backtest compartido entre dashboard y scripts. |
| `dashboard.py` | API Flask, dashboard y endpoint de backtesting. |
| `install.sh` | Instalación y diagnóstico de dependencias. |
| `run.sh` | Arranque, parada, estado y supervisión de procesos. |

## Instalación

```bash
git clone <URL-del-repositorio>
cd programa2
chmod +x install.sh run.sh
./install.sh
```

En Kali/Ubuntu, el instalador prepara Python, Wine, MetaTrader 5 y el Python
de Windows que importa `MetaTrader5`.

Comandos útiles del instalador:

```bash
./install.sh --help
./install.sh --no-wine       # sólo adaptadores que no necesitan MT5 local
```

El instalador muestra progreso, valida dependencias y ejecuta un diagnóstico
final. Nunca guardes `.env` en Git: ya está excluido por `.gitignore`.

## Configuración segura

Copia `.env.example` a `.env` o deja que `install.sh` lo cree. Las credenciales
se pueden introducir desde **Settings**; las claves OpenRouter se escriben en
`.env` con permisos privados y nunca se muestran completas.

Variables esenciales:

```env
MT5_LOGIN=
MT5_PASSWORD=
MT5_SERVER=FBS-Demo
SYMBOL=XAUUSD
TIMEFRAME=M1
DRY_RUN=true
RISK_PER_TRADE_USD=1
MAX_DAILY_LOSS_USD=8
MAX_DAILY_DRAWDOWN_PCT=20
MAX_TRADES_PER_DAY=100
```

El lote no se fija a ciegas: se calcula con el margen y `SymbolSpec` que
devuelve MT5. Si el lote mínimo excede el riesgo o margen disponibles, la
señal se rechaza de forma explícita.

### Quantum Queen y gestión avanzada

Todos los módulos avanzados son opt-in:

```env
STRAT_ENABLE_QUANTUM_QUEEN=false
STRAT_QUANTUM_PRIMARY=false
REGIME_FILTER_ENABLED=true
GRID_ENABLED=false
GRID_MAX_POSITIONS=3
GRID_STEP_ATR=1.0
GRID_MAX_LOT=0.09
RECOVERY_ENABLED=false
RECOVERY_MAX_LEVELS=2
DYNAMIC_LOT_CAP=0.09
```

`RECOVERY_ENABLED` permite promediar sólo dentro del régimen configurado y
hasta el número máximo de niveles. Cada pierna se vuelve a validar con
`RiskManager`; no hay una autorización implícita para martingala ilimitada.

## Arranque y operación

```bash
./run.sh --start     # bridge + dashboard; no inicia el motor
./run.sh --status    # panel visual con estado real de bridge/cuenta/motor
./run.sh doctor      # diagnóstico de instalación y conectividad
./run.sh verify      # compilación, tests y smoke test
./run.sh --stop      # detiene bridge, dashboard y motor
```

Abre `http://127.0.0.1:9000`. El motor se inicia deliberadamente desde el
botón del dashboard; `--start` nunca comienza a operar por sí solo.

Para parar de emergencia:

```bash
./run.sh emergency-stop
./run.sh emergency-stop --clear
```

El primer comando crea `data/EMERGENCY_STOP`, cierra posiciones en el siguiente
ciclo y evita el reinicio automático del motor.

## Backtesting real con MT5

La pestaña **Backtesting MT5** es de sólo lectura. Permite elegir símbolo,
timeframe, capital, apalancamiento, riesgo, spread, fechas y modo de ticks.
Con ticks usa bid/ask históricos; las señales se calculan únicamente sobre
velas cerradas.

El bridge divide automáticamente rangos grandes en ventanas diarias para
velas y de seis horas para ticks. Esto evita `Invalid params` de
`copy_rates_range`/`copy_ticks_range` y limita el tamaño de cada respuesta.

Ejemplo de API local:

```bash
curl -X POST http://127.0.0.1:9000/api/backtest \
  -H 'Content-Type: application/json' \
  -d '{
    "symbol":"XAUUSD", "timeframe":"M1",
    "date_from":"2026-05-01T00:00:00Z",
    "date_to":"2026-07-01T23:59:59Z",
    "bars":100000, "balance":50, "leverage":500,
    "risk_usd":1, "spread":0.25, "tick_mode":true
  }'
```

Si el broker no tiene historial suficiente, el dashboard devuelve un error
estructurado y conserva el proceso activo. No presenta PnL inventado.

## Cerebros OpenRouter

El filtro de señales sólo puede confirmar o vetar una señal determinista; no
elige lotes ni modifica stops. El supervisor de cuenta puede pausar entradas o
reducir riesgo, pero tampoco puede saltarse límites locales. Ambos tienen
cache, límite diario y comportamiento fail-closed ante errores de red.

Configúralos desde Settings o `.env`:

```env
AI_BRAIN_ENABLED=false
OPENROUTER_API_KEY=
AI_SUPERVISOR_ENABLED=false
OPENROUTER_SUPERVISOR_API_KEY=
```

No subas nunca las claves a GitHub. Si una clave fue expuesta, revócala y
genera otra.

## Calidad, pruebas y desarrollo

```bash
python -m py_compile core/*.py dashboard.py bridge/mt5_bridge_server.py
node --check dashboard/app.js
bash -n install.sh run.sh
pytest -q
```

Las pruebas cubren riesgo, señales, Quantum Queen, backtesting, bridge,
dashboard, configuración y supervisión del motor. El test omitido por falta de
datos se identifica explícitamente en la salida de pytest.

Antes de un cambio importante:

1. Ejecuta `./run.sh verify`.
2. Comprueba `git diff --check`.
3. Ejecuta un backtest con datos del broker y un periodo fuera de muestra.
4. Mantén `DRY_RUN=true` hasta revisar drawdown, margen y número de trades.

## Seguridad y límites conocidos

- Flask es un servidor de desarrollo; usa `127.0.0.1` por defecto.
- Si expones el dashboard o bridge en la red, configura los tokens y usa un
  túnel SSH/VPN.
- MT5, Wine y algunos brokers pueden rechazar rangos enormes o no ofrecer
  ticks antiguos. El chunking reduce el problema, pero no puede crear datos
  que el terminal no tenga.
- Los resultados dependen de spread, comisión, latencia, fill, swap y reglas
  del broker. El objetivo de este proyecto es reproducibilidad y control de
  riesgo, no prometer una ganancia diaria.

## Licencia

Añade aquí la licencia que el propietario del repositorio quiera utilizar
(por ejemplo, MIT) antes de publicar el proyecto.
