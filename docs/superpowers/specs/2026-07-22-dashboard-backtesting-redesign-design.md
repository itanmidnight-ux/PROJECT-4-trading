# Rediseño del dashboard + fix de backtesting (Fase 1)

Fecha: 2026-07-22
Fase: 1 de 2 (Fase 2 = mejora de estrategias vía backtest, spec separado, después de esta)

## Contexto

Auditoría del dashboard web (`dashboard.py`, `dashboard/{index.html,app.js,style.css}`)
y su pestaña de Backtesting MT5, pedida por el dueño del proyecto. Durante la
auditoría, con el motor LIVE corriendo en la cuenta real (106049158 @ FBS-Demo,
`DRY_RUN=false`), se confirmaron con datos reales:

- No hay datos fake/mock en `dashboard.py` ni `app.js` — todo sale de
  `data/trades.db` / la API del bridge. Esto se mantiene como invariante, no
  como algo a arreglar.
- La barra `.live-progress-wrap` (header, "Motor activo · datos en vivo")
  reinicia su animación de sliding cada `POLL_MS` (3000ms) porque
  `refresh()` llama `setLiveState('loading', ...)` en cada poll, incluso
  cuando el round-trip real tarda ~80-180ms. Se percibe como parpadeo
  constante — molesto de ver, sin aportar información real (la carga real es
  casi instantánea).
- **Bug funcional real, no sólo cosmético**: `POST /api/backtest` con los
  defaults de la UI (3000 velas) tarda 6+ minutos y típicamente termina en
  timeout/cancelación — coincide con los `Backtest MT5 no ejecutado:
  BridgeError` que aparecen repetidos en el log de eventos del motor.
  Reproducido **offline** (sin bridge, sin red, CSV local
  `data/gold_m1_7d.csv`) y perfilado con `cProfile`: ~120ms por vela.
  Causa raíz confirmada (ver sección Backtesting abajo).

El dueño del proyecto también pidió, a mitad de esta conversación, escalar el
alcance visual de "pulido incremental" a **reconstrucción completa** del
dashboard: más creatividad, mejor diseño visual, usando la skill
`ui-ux-pro-max`. Ese cambio de alcance está incorporado en este spec.

## Objetivo

1. Backtesting MT5 funciona de verdad: corre en segundos (no minutos) para
   el rango completo que la UI ofrece (100–200000 velas), sin cambiar el
   comportamiento observable de la estrategia en vivo.
2. Dashboard reconstruido visualmente: paleta, tipografía y motion
   coherentes, sin animaciones que compitan por atención sin razón, 100%
   datos reales (ya lo es — se preserva como invariante con guardas).
3. Codex (`codex` MCP, ya conectado) revisa cada cambio grande antes de
   darlo por cerrado — ya se usó para validar el approach de la sección
   Backtesting de abajo.

## Fuera de alcance (Fase 2, spec separado después)

Mejorar las estrategias en sí (parámetros, señales nuevas, cerebros IA)
usando resultados de backtest. Esta fase sólo deja el backtesting
**utilizable** para que la Fase 2 pueda apoyarse en él.

## Diseño visual

**Stack:** se mantiene vanilla HTML/CSS/JS servido directo por Flask, sin
build step (`dashboard/index.html` + `app.js` + `style.css`, como hoy) —
decisión explícita del dueño del proyecto para no meter npm/bundler a un
proyecto que hoy es "clonar y correr".

**Paleta** (terminal/hacker, dark-mode primero — confirmada con el dueño del
proyecto vía `ui-ux-pro-max --domain color`, perfil "Autonomous Drone Fleet
Manager" como base):

| Token | Valor | Uso |
|---|---|---|
| `--bg` | `#0D1117` | fondo base |
| `--card` | `#182424` | superficies/cards |
| `--primary` (verde terminal) | `#00FF41` | acciones, ganancias, pulso "vivo" |
| `--primary-dim` | `#008F11` | verde secundario/hover |
| `--destructive` | `#FF3333` | pérdidas, acciones peligrosas (detener motor) |
| `--foreground` | `#E6EDF3` | texto principal |
| `--muted` | `#94A3B8` | texto secundario |
| `--border` | `#30363D` | bordes/separadores |
| `--gold-accent` | `#F59E0B` | **sólo** el logo/marca — guiño a XAUUSD=oro, no se usa en el resto de la UI |

Modo claro: variante con los mismos tokens semánticos invertidos
(fondo claro, mismo verde/rojo con contraste AA verificado) — el toggle
sol/luna que ya existe en el header se conserva.

**Tipografía:**
- `JetBrains Mono` — números y datos: precios, PnL, equity, tablas de
  trades, contadores. `font-variant-numeric: tabular-nums` para que las
  cifras no salten de ancho al actualizar.
- `IBM Plex Sans` — texto de UI: labels, botones, mensajes, Settings.

**Motion:**
- Micro-interacciones 150–300ms, `ease-out` al entrar / `ease-in` al
  salir, nunca decorativas — cada animación tiene que representar un
  cambio real de estado.
- `prefers-reduced-motion` respetado (desactiva las no esenciales).
- **Fix de la barra de progreso**: se reemplaza el sliding-indeterminate
  en cada poll rutinario por un indicador de pulso sutil (ej. un punto que
  respira, no una barra que se desliza) que sólo se anima cuando el
  round-trip realmente tarda más que un umbral (ej. >400ms) — la mayoría
  de los polls (80-180ms) no deberían mostrar ningún movimiento, sólo
  actualizar el timestamp "hace Xs".

**Charts:** equity curve sigue siendo un area/line chart (no candlestick —
no es el tipo de dato que se muestra hoy), fill al 20%, colores
ganancia/pérdida del token table de arriba, sin gradientes pesados que
opaquen la tendencia.

**Estructura:** se reconstruyen `index.html`/`app.js`/`style.css` sobre la
misma arquitectura de datos actual (mismos endpoints, mismo flujo
fetch→render), reorganizando layout/jerarquía visual y componentizando el
CSS con los tokens de arriba. No se tocan los endpoints de `dashboard.py`
salvo el fix de backtesting de abajo.

## Backtesting: causa raíz y fix (validado con Codex)

**Causa raíz** (perfilado con `cProfile`, 300 velas → 33s, 85% del tiempo en
`compute_indicators`):

`CompositeStrategy.generate_signal` (core/signals.py:724) recalcula
indicadores desde cero, por barra, sobre una ventana de hasta
`max_lookback_bars` (600) filas — y los recalcula **dos veces por barra**:
una vez dentro de `self._mean_reversion.generate_signal(...)` (que ya llama
`compute_indicators` internamente) y otra vez explícitamente en la rama de
`extra_strategies` (línea 734) cuando la señal mean-reversion no dispara.
Además, cuando ninguna extra dispara, la señal mean-reversion se vuelve a
calcular una TERCERA vez en el `return` final (bug adicional detectado por
Codex). El resultado: ~120ms/vela → 3000 velas = 6+ minutos.

**Fix, en dos partes (ambas necesarias, orden importa):**

**Parte A — eliminar la redundancia** (siempre, bajo riesgo):
- `CompositeStrategy.generate_signal` calcula la señal mean-reversion UNA
  vez, la guarda, y la retorna directamente si ninguna extra dispara (sin
  recalcularla).
- `compute_indicators()`/`detect_regime()` se calculan UNA vez por barra y
  se pasan tanto a mean-reversion como a las extra strategies — requiere
  que `ScalpStrategy.generate_signal` (core/strategy.py:268) acepte
  indicadores precomputados opcionalmente en vez de siempre recalcularlos.

**Parte B — precómputo global vectorizado** (el fix de fondo, mayor
impacto): calcular `compute_indicators()` **una sola vez sobre la serie
completa** de velas antes del loop principal (O(n) en vez de O(n·600)),
indexando la fila `i` ya calculada en cada iteración en vez de recortar una
ventana y recalcular desde cero. Confirmado con Codex: `rolling()` y
`ewm(adjust=False)` ya son causales (el valor en `i` sólo depende de datos
`<= i`), así que esto es matemáticamente válido, no un truco.

- Para los indicadores M1 puros (Bollinger, RSI, ATR, ADX, EMA9/21) la
  diferencia contra el modelo actual de ventana-de-600 es despreciable
  después del arranque (EMA50 con semilla de hace 600 barras influye
  `~1e-11`).
- **Cuidado real**: las extra strategies que resamplean a M5/M15 (ej.
  `MomentumCrossStrategy`, `MACrossGridStrategy`) SÍ pueden divergir más
  (EMA50 sobre M5 con ~120 muestras de contexto tiene memoria residual
  ~0.8%) y, más importante, un resample global ingenuo puede meter
  **look-ahead bias** (una vela M5/M15 que en el momento `i` todavía no
  había cerrado). Regla no negociable: al resamplear a timeframe superior,
  sólo usar velas HTF ya CERRADAS al momento `i`, alineando cada M1 con la
  última vela HTF completa disponible — nunca una vela HTF parcial que
  contenga minutos futuros a `i`.
- Se mantiene un modo `live_parity` (el comportamiento actual, ventana de
  `candle_history_count`) disponible para pruebas de regresión y
  comparación puntual — no como ruta por defecto de la UI, que usa el modo
  rápido.

**Validación antes de reemplazar el modo por defecto**: comparar
señales/trades resultantes entre el modo actual (ventana) y el nuevo
(global) sobre el mismo CSV real, especialmente con `STRAT_ENABLE_MA_GRID`
u otras extra strategies M5/M15 activadas (está `true` en el `.env` real
ahora mismo) — si diverge más de lo esperado, se investiga antes de
adoptarlo como default.

## Validación / testing

- `./run.sh verify` (compila + 220 tests + smoke synthetic + dashboard API
  check) debe seguir pasando.
- Nuevos tests: backtest de N velas termina en tiempo acotado (ej. <5s para
  3000 velas); comparación de trades modo-ventana vs modo-global sobre CSV
  real, con assert de divergencia máxima aceptable.
- Prueba visual real vía `.claude/skills/run-xauusd-scalper/driver.py`
  (Playwright) contra el dashboard real corriendo — screenshots de
  Dashboard/Backtesting/Settings en ambos temas (claro/oscuro), antes y
  después.
- Codex revisa el diff de: (1) el fix de backtesting (A+B), (2) el
  rediseño visual completo, antes de darlos por terminados.
- **Nunca** tocar el motor/bridge en vivo para probar esto — el backtest
  nuevo se prueba con CSVs locales (`data/*.csv`) primero; sólo al final
  una prueba end-to-end contra el bridge real, de sólo lectura (el
  endpoint ya es read-only por diseño).

## Riesgos conocidos

- El precómputo global (Parte B) es un cambio de comportamiento numérico
  real, aunque pequeño — se documenta explícitamente en vez de esconderlo,
  y se valida con datos reales antes de convertirlo en default.
- El sistema tiene el motor LIVE corriendo con dinero real durante todo
  este trabajo — ningún paso de esta fase reinicia bridge/dashboard/motor
  sin confirmación explícita (ver gotcha ya documentado en
  `.claude/skills/run-xauusd-scalper/SKILL.md` sobre no lanzar una segunda
  sesión de `./run.sh --start`).
