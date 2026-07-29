# Ronda 49: búsqueda de una estrategia SIMPLE (un TP fijo en R, sin escalera) con winrate y ganancia/trade máximos

Fecha: 2026-07-29. Pedido explícito del dueño del proyecto: "mejoralo al
punto maximo en el que el winrate sea el mas alto y las ganancias por
trade sean las mayores... la mejor estrategia es muy simple y facil de
repetir... podes usar estrategias de trading manual o de patrones de
velas... ponela a prueba realmente".

## Limitación del entorno (léase antes que los números)

Esta sesión corre en un contenedor cloud efímero, sin Wine ni
MetaTrader5, sin bridge MT5 vivo y **sin los CSV históricos** que usaron
las Rondas 1-48 (`data/*.csv` está en `.gitignore` — vivían solo en la
máquina local del dueño). No hay forma de reproducir exactamente esos
datasets aquí.

Para poder medir algo real (no sintético) se usó
`scripts/fetch_market_data.py`, que baja **futuros de oro COMEX (GC=F)
vía Yahoo Finance** — un proxy líquido y correlacionado con XAUUSD spot,
pero **no el feed real de FBS**. Sirve para juzgar si un patrón tiene
estructura real de mercado (no ruido), no para prometer el número exacto
que daría la cuenta real. Datasets bajados hoy:

- `data/gold_1m_7d.csv` — 8141 velas, ~7 días reales (mismo rango de días que usaban las Rondas anteriores).
- `data/gold_5m_60d.csv` — 13636 velas, ~60 días.
- `data/gold_15m_60d.csv` — 4554 velas, ~60 días.

## Metodología

Se construyó `scripts/ronda49_simple_strategies.py`: un adaptador que
envuelve señales **ya implementadas y validadas estructuralmente** en
`core/signals.py`/`core/strategy.py` (mean-reversion BB+RSI, cruce
EMA9/21 con filtro de pendiente, engulfing, pin bar, tight pin bar,
micro-range breakout — reusando su `check()`/`generate_signal()` tal
cual, sin reinventar la detección de patrones) y reemplaza su salida
(escalera de TP en dólares) por **un solo TP a un múltiplo R fijo del SL
estructural**, sin escalera, sin breakeven parcial, sin grid — la
definición de "simple" del pedido. Todo corre a través de
`core/backtest.py::run_backtest` sin tocarlo, heredando el mismo modelo
de fill/spread/margen/riesgo que ya usaron las Rondas 1-48 (RiskManager
real, cap del 5%, spread real de Ronda 48 = 0.45).

Split cronológico 60/40 train/test, igual disciplina que Rondas 13-48:
solo cuenta un resultado que mejore en **ambas** mitades.

## Paso 1: barrido amplio a balance real (~$100, RISK_PER_TRADE_USD=$5)

6 estrategias × 3 timeframes (M1, M5, M15) × 4-5 múltiplos de R (0.5 a
2.5) — más de 90 mediciones train/test. Resultado consistente: **todo
negativo o con muestra demasiado chica para confiar (n<15 por mitad)**.

Ejemplo (M1, 7 días, el dataset más parecido a los usados en Rondas
1-48):

| estrategia | tp_r | mitad | trades | win% | PnL | PnL/trade |
|---|---|---|---|---|---|---|
| mean_reversion_bbrsi | 0.8 | TRAIN | 79 | 55.7% | +$8.21 | +$0.10 |
| mean_reversion_bbrsi | 0.8 | TEST | 34 | 35.3% | **-$55.23** | -$1.63 |
| momentum_cross_ema9_21 | 1.5 | TRAIN | 84 | 32.1% | -$31.85 | -$0.38 |
| momentum_cross_ema9_21 | 1.5 | TEST | 60 | 36.7% | +$0.51 | +$0.01 |
| pin_bar_at_extreme | 1.0 | TRAIN | 54 | 33.3% | -$43.74 | -$0.81 |
| pin_bar_at_extreme | 1.0 | TEST | 32 | 21.9% | -$48.27 | -$1.51 |

En M15 (60 días) casi todo dio **0 trades** — no por falta de señal, sino
por sizing: se verificó directamente (`RiskManager.size_position`) que
145 señales de mean-reversion en TRAIN generaron 145 rechazos por
"volatilidad demasiado alta para el lote mínimo del broker" — a M15 el
ATR mediano es ~$9.8 (vs ~$1.7 en M1), el SL estructural (3.5×ATR ≈ $35)
implica un riesgo real por encima del presupuesto de $5 incluso con el
lote mínimo de 0.01. **Es el mismo cuello de botella de cuenta chica que
Rondas 23-27 ya habían diagnosticado para mean-reversion en M1 — acá se
confirma que también bloquea timeframes más altos, no solo señales
nuevas.**

Conclusión del Paso 1: al balance real de hoy, ninguna estrategia simple
de un solo TP/SL, en ningún timeframe probado, tiene evidencia de ventaja
bajo el spread real.

## Paso 2: ¿hay señal real detrás del cuello de botella de sizing?

Para separar "no hay ventaja" de "la cuenta es demasiado chica para
correr la estrategia", se repitió el barrido con balances hipotéticos
más grandes (para que `RiskManager` deje de rechazar el SL ancho de M15)
y riesgo escalado al 2% del balance.

**Hallazgo parcial: `momentum_cross_ema9_21` (cruce EMA9/21 con filtro de
pendiente) en M15 generaliza train→test, consistente en varios balances y
varios múltiplos de R:**

| Balance | risk_usd | tp_r | TRAIN PnL (n) | TEST PnL (n) |
|---|---|---|---|---|
| $1000 | $20 | 1.5 | +$35.54 (52) | +$151.69 (38) |
| $2000 | $40 | 1.5 | +$80.53 (57) | +$149.31 (37) |
| $2000 | $40 | 2.0 | +$18.80 (48) | +$278.95 (27) |
| $2000 | $40 | 2.5 | +$92.36 (47) | +$310.78 (25) |
| $5000 | $100 | 1.5 | +$163.39 (57) | +$635.09 (37) |
| $5000 | $100 | 2.0 | +$66.76 (48) | +$927.98 (27) |

Positivo en TRAIN y TEST, en 6/6 combinaciones balance×R probadas. Con la
misma disciplina de rondas anteriores (nunca aceptar un TEST bueno solo
porque gusta) esto pasaría el filtro — **pero no se activa, por lo que
sigue en el próximo párrafo.**

## Paso 3: por qué este hallazgo NO se declara confirmado (chequeo de fragilidad)

La misma estrategia (`momentum_cross_ema9_21`), mismo activo, mismo
período, mismo balance grande ($2000/$40), **en M5 y M1**:

| Timeframe | tp_r | TRAIN PnL | TEST PnL |
|---|---|---|---|
| M15 | 1.5 | +$80.53 | **+$149.31** |
| M5 | 1.5 | +$412.89 | **-$815.59** |
| M1 | 1.5 | -$533.87 | -$84.03 |

Un edge estructural real de "seguir la tendencia con cruce de EMAs"
debería sobrevivir, aunque sea débil, en timeframes vecinos del mismo
activo y período — no cambiar de signo así. Que sea positivo SOLO en M15
y claramente negativo (o no generalice) en M5 y M1 sobre el mismo tramo
de 60 días es la firma típica de "esta ventana de 60 días tuvo 2-3 tramos
de tendencia grandes que una estrategia lenta (pocos trades/día) en M15
alcanzó a capturar", no una ventaja repetible. Nota técnica adicional: el
filtro de "pendiente M5" de `MomentumCrossStrategy` se degenera cuando la
serie base ya es M15 (`resample_m1_to_tf(df, 5)` sobre datos ya
espaciados 15 min termina reconstruyendo básicamente la misma serie) —
en la práctica el filtro actúa como una EMA50 lenta sobre M15, no como
una confirmación de timeframe realmente superior. No invalida el
resultado, pero significa que el mecanismo no es exactamente el que
describe el código.

**Conclusión honesta del Paso 3, siguiendo la misma vara de Rondas 15/19/24
(nunca confiar en una mejora que no se sostiene de forma consistente):
este hallazgo NO se declara una ventaja confirmada.** Necesitaría, como
mínimo, un segundo período de 60 días completamente independiente (otro
tramo de fechas) y, para tener algún valor real, datos reales de FBS via
bridge MT5 en vez del proxy GC=F — ninguno de los dos está disponible en
este entorno.

## Qué NO se tocó (a propósito)

- Ningún default de `.env`/`.env.example` cambió.
- Ningún flag `STRAT_ENABLE_*` se activó.
- El motor en vivo nunca se tocó (guardrail no negociable del proyecto).
- No se declaró ninguna estrategia "ganadora" — el hallazgo del Paso 2 es
  interesante pero falló su propio chequeo de robustez en el Paso 3.

## Conclusión honesta (extiende Ronda 48, no la contradice)

Con el spread real (0.45) y el tamaño de cuenta real, **ninguna
estrategia simple de una sola entrada/un SL/un TP — mean-reversion,
tendencia (cruce de EMAs), o patrones de vela clásicos (engulfing, pin
bar, micro-range breakout) — tiene evidencia robusta de ventaja** en los
tres timeframes probados (M1/M5/M15), sobre el único dataset real
disponible en este entorno (proxy GC=F, no el feed de FBS). El cuello de
botella de cuenta chica identificado en Rondas 23-27 no es exclusivo de
mean-reversion en M1: se reconfirmó que también bloquea el sizing en M15
para cualquier señal con SL ancho. El único candidato con un patrón
train/test positivo y repetido en varios balances (`momentum_cross` en
M15) no sobrevivió el chequeo de consistencia entre timeframes vecinos,
así que queda documentado como pista para investigar más adelante, no
como una mejora.

## Qué haría falta para seguir esto en serio

1. Datos reales de FBS (via bridge MT5, `GET /candles`) en vez del proxy
   GC=F — el único dato "real" disponible hoy para esto es una
   aproximación, no el feed que va a ejecutar las órdenes.
2. Un segundo tramo de 60+ días completamente independiente (no solo
   train/test dentro de la misma ventana) antes de confiar en cualquier
   resultado positivo de M15.
3. Balance real ≥ ~$1000-2000 antes de que `RiskManager` pueda siquiera
   dimensionar una posición en M15 con el SL estructural que este
   backtest asume — no aplica a la cuenta actual (~$20-100), coherente
   con la tabla de Ronda 27.
4. Si en algún momento se decide perseguir M15 en serio, el motor
   (`core/engine.py`) hoy solo sondea M1 — correr una señal M15 en vivo
   necesitaría wiring nuevo, no solo prender un flag existente.

## Reproducir

```bash
.venv/bin/python scripts/fetch_market_data.py --interval 1m --range 7d --out data/gold_1m_7d.csv
.venv/bin/python scripts/fetch_market_data.py --interval 5m --range 60d --out data/gold_5m_60d.csv
.venv/bin/python scripts/fetch_market_data.py --interval 15m --range 60d --out data/gold_15m_60d.csv
.venv/bin/python scripts/ronda49_simple_strategies.py --csv data/gold_1m_7d.csv --tp-r 0.5 0.8 1.0 1.5 2.0
.venv/bin/python scripts/ronda49_simple_strategies.py --csv data/gold_15m_60d.csv --tp-r 1.5 2.0 2.5 --balance 2000 --risk-usd 40 --only momentum_cross_ema9_21
```
