# Ronda 48: el spread real (0.45) es casi el doble del hardcodeado en todo backtest de la Fase 2 (0.25) — el combo real se vuelve NEGATIVO

Fecha: 2026-07-25. Prioridad máxima (pedido explícito del dueño: resultados
lo más realistas posibles porque va a operar con dinero real). Hallazgo
crítico detectado por el thread principal: prácticamente todos los scripts
de backtest desde el inicio de la Fase 2 (Ronda 13+), incluida la medición
insignia de Ronda 43, llaman a `run_backtest(..., assumed_spread_price=0.25)`
hardcodeado, sin nunca haberlo verificado contra el bridge MT5 real.

## Spread real medido

`GET /price/XAUUSD` contra el bridge MT5 en vivo (127.0.0.1:5001, con
`X-Bridge-Token`), 2026-07-25 ~11:41 hora del sistema:

```
{"ask":4053.57,"bid":4053.12,"ok":true,"spread_price":0.45,"time":1784937299}
```

`spread_price=0.45` — casi el doble del 0.25 usado en todos los backtests
anteriores. Cinco lecturas espaciadas por 3s devolvieron el mismo valor
exacto (mismo `time`): el mercado estaba cerrado (viernes 24-jul 18:54,
fin de semana) y el bridge devuelve la última cotización conocida, no una
serie viva. Es una FOTO de un momento (cierre de viernes), no una
constante — el spread real de XAUUSD varía con sesión/liquidez y debería
re-medirse en vivo antes de decisiones de producción futuras. `GET
/symbol/XAUUSD` confirma lo mismo: `"spread":45` (en puntos de 0.01).

## Metodología (para que el número sea confiable)

Se reprodujo primero el harness EXACTO de
`tests/test_backtest.py::_combo_real_run` / `_ronda43_settings` /
`_test_spec` (mismo `starting_balance=100.45`, `leverage=500`,
`precompute_indicators=True`, misma construcción de estrategia vía
`build_strategy_from_settings`) para confirmar que el script de esta ronda
mide exactamente lo mismo que Ronda 43 antes de cambiar el spread. Combo
real: mean_reversion + ma_grid, `RISK_PER_TRADE_USD=5.25`,
`MIN_TP_USD=0.60`, `TP_LEVELS=8`, `STRAT_SL_ATR_MULTIPLE=3.5`,
`MAX_DAILY_LOSS_USD=25.0`, `MAX_DAILY_DRAWDOWN_PCT=20.0`. Datos:
`data/gold_m1_7d.csv` (7 días reales completos, 7839 velas) y sus mitades
`data/gold_m1_7d_train.csv` / `data/gold_m1_7d_test.csv`.

Scripts: `scripts/ronda48_spread_real.py` (reproducción + comparación
0.25 vs 0.45), `scripts/ronda48_sweep_spread_real.py` y
`scripts/ronda48_sweep2_combo.py` (rebarrido bajo spread real),
`scripts/ronda48_validate_test.py` (validación TRAIN vs TEST de los
mejores candidatos).

## Paso 2: reproducción de Ronda 43 (spread=0.25) — CONFIRMADA

| | trades | wins | win rate | PnL total | balance final | max DD |
|---|---|---|---|---|---|---|
| FULL 7d, spread=0.25 | 225 | 199 | 88.4% | **+$63.40** | $163.85 | 16.02% |

Coincide exacto con el número insignia citado (225 trades, 88.4%, +$63.40).
El método de esta ronda mide igual que Ronda 43 — el cambio de resultado
que sigue es 100% atribuible al spread, no a un error de medición.

## Paso 3: mismo combo con spread REAL (0.45) — RESULTADO NEGATIVO

| | trades | wins | win rate | PnL total | balance final | max DD |
|---|---|---|---|---|---|---|
| FULL 7d, spread=0.25 (Ronda 43) | 225 | 199 | 88.4% | +$63.40 | $163.85 | 16.02% |
| FULL 7d, spread=0.45 (real) | 119 | 93 | 78.2% | **-$32.45** | $68.00 | 33.64% |
| TRAIN, spread=0.25 | 133 | 118 | 88.7% | +$39.05 | $139.50 | 16.02% |
| TRAIN, spread=0.45 (real) | 88 | 69 | 78.4% | **-$21.78** | $78.67 | 31.25% |
| TEST, spread=0.25 | 91 | 80 | 87.9% | +$22.07 | $122.52 | 10.73% |
| TEST, spread=0.45 (real) | 67 | 55 | 82.1% | **-$4.41** | $96.04 | 19.58% |

Con el spread real, el número insignia de +$63.40/225 trades/88.4% en 7
días se convierte en **-$32.45 en el archivo completo**, negativo también
en TRAIN (-$21.78) y casi negativo en TEST (-$4.41). El trade count cae
~47% (el spread más ancho hace que menos setups superen `MIN_TP_USD` neto
de costo) y el win rate cae ~10 puntos (más operaciones que hubieran sido
ganadoras con spread=0.25 terminan siendo perdedoras o de breakeven con el
costo real). El drawdown máximo también empeora sensiblemente (16%→34% en
el archivo completo). Este es el hallazgo serio que preveía el punto 5 de
la tarea.

## Paso 5: intento de retuneo bajo spread real — NINGUNO GENERALIZÓ (hallazgo negativo honesto)

Se rebarrió `MIN_TP_USD` (0.60 a 5.00) y `STRAT_SL_ATR_MULTIPLE` (2.0 a
5.0) por separado y en grid combinado (36 combinaciones), **solo sobre
TRAIN**, bajo spread=0.45. Varias combinaciones mejoraron TRAIN de forma
notable:

| Combinación | TRAIN PnL |
|---|---|
| MIN_TP_USD=3.00, SL=4.0 | **+$40.23** |
| MIN_TP_USD=1.20, SL=4.0 | +$27.42 |
| MIN_TP_USD=1.00, SL=4.0 | +$25.48 |
| MIN_TP_USD=1.20, SL=2.5 | +$22.72 |

Los 4 candidatos se validaron después contra TEST (held-out, nunca mirado
durante el sweep) — **ninguno generalizó**:

| Combinación | TRAIN | TEST | FULL |
|---|---|---|---|
| Baseline (0.60 / 3.5, default actual) | -$21.78 | -$4.41 | -$32.45 |
| MIN_TP=3.00, SL=4.0 | +$40.23 | **-$23.19** | +$26.30 |
| MIN_TP=1.20, SL=4.0 | +$27.42 | **-$12.56** | +$19.65 |
| MIN_TP=1.00, SL=4.0 | +$25.48 | **-$16.05** | +$7.88 |
| MIN_TP=1.20, SL=2.5 | +$22.72 | **-$34.83** (peor que baseline) | -$20.99 |

Los cuatro candidatos con mejor TRAIN se vuelven negativos en TEST, en
algunos casos peor que el baseline. Con spread=0.45 el trade count por
mitad ya es chico (67-119 en el archivo completo, bajando a 30-50 en TEST
tras filtrar por `MIN_TP_USD` más alto) — suficiente para que las mejoras
de TRAIN sean ruido de sobreajuste, no señal real. Siguiendo la misma
disciplina de rondas anteriores (nunca elegir el número de TEST que más
guste si TRAIN no mejora primero, y aquí ninguna combinación mejoró
*ambos* lados a la vez), **no se cambió ningún default de estrategia**:
`MIN_TP_USD` sigue en 0.60, `STRAT_SL_ATR_MULTIPLE` sigue en 3.5 en
`.env`.

## Qué se corrigió (documentación/UI, no estrategia)

- `dashboard.py` (`/api/backtest`): el fallback `spread = float(data.get("spread", 0.25))`
  pasó a `0.45`, con comentario explicando el origen y que es una foto de
  un momento.
- `dashboard/index.html`: el campo "Spread asumido (precio)" del
  formulario de Backtesting pasó de `value="0.25"` a `value="0.45"`, con
  `title` explicando que es una medición del 2026-07-25 y que el campo
  sigue siendo editable porque el spread real varía.
- `scripts/run_backtest.py`: `--spread` default pasó de `0.25` a `0.45`,
  mismo comentario.
- `tests/test_backtest.py` y `tests/test_backtest_brain.py` (`SPREAD =
  0.25`, `_combo_real_run`'s `assumed_spread_price=0.25`) se dejaron
  intactos a propósito: ahí `0.25` es una constante de fixture para
  ejercitar mecánica de código de forma determinística (paridad
  precompute/live, ladder de TP, etc.), no una afirmación de costo real de
  producción — cambiarla no aporta nada y arriesga romper aserciones que
  no dependen del valor exacto de forma intencional.
- `scripts/count_ai_brain_signals.py` deriva su spread de
  `STRAT_MAX_SPREAD_PRICE / 2` (no de un `0.25` hardcodeado) — no hacía
  falta tocarlo.

## Otro hallazgo relacionado (no se tocó, solo se documenta)

`STRAT_MAX_SPREAD_PRICE=0.5` (`core/config.py`, filtro de seguridad que
descarta cualquier señal si el spread en vivo supera ese umbral) está muy
cerca del spread real medido (0.45) — con un margen de apenas 0.05. No es
el mismo mecanismo que `assumed_spread_price` (ese es el costo asumido en
el backtest; `STRAT_MAX_SPREAD_PRICE` es un filtro de aceptación en vivo)
y no hay evidencia de que haya que cambiarlo, pero vale la pena que quede
anotado: si el spread real sube un poco más en otro momento del día, el
bot en producción empezaría a rechazar señales por este filtro, no
silenciosamente.

## Conclusión honesta

El resultado insignia de Ronda 43 (+$63.40/225 trades/7 días, ~11.7%/día)
estaba construido sobre un costo de spread subestimado a la mitad. Con el
spread real medido hoy (0.45), el mismo combo produce **-$32.45 en el
archivo completo de 7 días** (-$4.61%/día), negativo también en TRAIN
(-$21.78) y muy cerca de cero en TEST (-$4.41, -1.10%/día). Ningún
retuneo de `MIN_TP_USD`/`STRAT_SL_ATR_MULTIPLE` probado en esta ronda
generaliza de TRAIN a TEST — la mejora en TRAIN es sobreajuste, no señal
real, dado el trade count ya reducido por el spread más ancho. El combo
real actual (mean_reversion + ma_grid con los defaults vigentes) **no
tiene evidencia de ser rentable una vez que se cuenta el costo de spread
real** — es un hallazgo negativo que hay que tener en cuenta antes de
operar con dinero real, no un ajuste cosmético.
