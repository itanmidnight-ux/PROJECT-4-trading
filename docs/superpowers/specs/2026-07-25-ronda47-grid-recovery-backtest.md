# Ronda 47: soporte multi-posición en el backtest + primera medición real de grid/recovery

Fecha: 2026-07-25. Contexto: `GRID_ENABLED`/`RECOVERY_ENABLED` y
`core/engine.py::_maybe_add_grid_position` existen desde ~Ronda 21 pero
nunca habían sido medidos por un backtest porque `core/backtest.py::run_backtest`
solo soportaba una posición abierta a la vez (`open_pos: dict | None`,
confirmado en Ronda 35). Esta ronda evaluó el alcance real de agregar
soporte multi-posición, decidió que era abordable, lo implementó de forma
incremental y testeada, y midió grid/recovery por primera vez contra el
baseline real.

## Alcance real (evaluación previa a implementar)

`core/engine.py` solo hace crecer UNA canasta (basket) a la vez: mientras
`self._open_positions` no está vacío, el motor solo evalúa
`_maybe_add_grid_position` (agregar una pierna a la canasta existente,
siempre del mismo lado que la posición original) y nunca evalúa una señal
de entrada nueva e independiente. Esto acota el problema: no hace falta un
backtest con N posiciones verdaderamente independientes (de lados
distintos, entradas distintas) - alcanza con una lista de piernas que
comparten side/dirección. Con esa acotación, el trabajo resultó mediano,
no grande: convertir `open_pos` en `open_positions: list[dict]`, envolver
la gestión de SL/TP/trailing (ya existente) en un loop por pierna
(dirección y extremos intrabar se calculan una sola vez por barra, no por
pierna, porque todas comparten lado), y agregar una función pura nueva
`_build_grid_leg` que replica `_maybe_add_grid_position` línea por línea.

## Qué se implementó (`core/backtest.py`)

- `open_pos: dict | None` → `open_positions: list[dict]`. Cada pierna se
  gestiona de forma completamente independiente (su propio SL, escalera de
  TP, breakeven, trailing) - igual que cada `ManagedPosition` en
  `core/engine.py`.
- `_build_grid_leg(...)`: función pura, testeable sin fabricar datos
  reales de tendencia/rango (recibe `regime_name` ya resuelto). Replica
  cada chequeo y cada fórmula de `_maybe_add_grid_position` en el mismo
  orden: grid_enabled, basket no vacío, `grid_max_positions`, historia
  mínima (30 barras), régimen no unknown/volatile/quiet, movimiento
  favorable (pirámide) o adverso (recovery, solo si `recovery_enabled` Y
  régimen == `recovery_min_regime`), `recovery_max_levels` (solo limita
  piernas adversas), sizing vía `RiskManager.size_position` con
  `risk_budget_usd = risk_per_trade_usd * (grid_lot_multiplier ** level)`.
- 7 parámetros nuevos en `run_backtest`, todos con default = comportamiento
  actual apagado: `grid_enabled=False`, `grid_max_positions=3`,
  `grid_step_atr=1.0`, `grid_lot_multiplier=1.0`, `recovery_enabled=False`,
  `recovery_max_levels=2`, `recovery_min_regime="range"`.
- Ningún llamador existente cambia de comportamiento: los 29 tests de
  `tests/test_backtest.py` (15 previos a esta ronda + 14 nuevos) pasan, y
  un test explícito (`test_grid_and_recovery_disabled_by_default_matches_explicit_off_on_combo_real`)
  verifica que omitir los 7 parámetros nuevos es bit-a-bit idéntico a
  pasarlos explícitamente en False, sobre la config real completa
  ("combo real": mean_reversion+ma_grid, RISK_PER_TRADE_USD=5.25,
  MIN_TP_USD=0.60, TP_LEVELS=8, STRAT_SL_ATR_MULTIPLE=3.5).

## Verificación

`.venv/bin/python -m pytest tests/ -q -n 2` completo (no solo
`test_backtest.py`) - ver el hash del commit para el resultado final.

## Resultado numérico en TRAIN (datos reales, `data/gold_m1_7d_train.csv`)

Baseline reproducido exactamente con este harness (confirma que el
refactor no cambió nada del comportamiento por defecto):

**TRAIN baseline (grid/recovery off): 133 trades, 88.7% acierto, +$39.05,
16.0% drawdown máximo, balance final $139.50** - coincide con el número
citado en el estado del proyecto.

### Pyramid-only (recovery apagado), barrido `grid_max_positions` x `grid_step_atr`

| grid_max_positions | grid_step_atr | trades | win% | PnL | drawdown máx |
|---|---|---|---|---|---|
| 2 | 0.5 | 173 | 84.4% | +$12.01 | 22.0% |
| 2 | 0.75 | 144 | 82.6% | -$5.93 | 26.5% |
| 2 | 1.0 | 129 | 80.6% | -$16.26 | 30.3% |
| 2 | 1.5 | 115 | 81.7% | -$7.55 | 33.9% |
| 3 | 0.5 | 83 | 73.5% | -$41.94 | 42.8% |
| 3 | 0.75 | 124 | 78.2% | -$31.75 | 32.9% |
| 3 | 1.0 (default) | 56 | 67.9% | -$42.87 | 44.9% |
| 3 | 1.5 | 112 | 80.4% | -$17.02 | 33.9% |
| 4 | 0.5 | 91 | 75.8% | -$33.39 | 45.1% |
| 4 | 0.75 | 64 | 70.3% | -$42.21 | 46.5% |
| 4 | 1.0 | 58 | 69.0% | -$40.58 | 44.9% |
| 4 | 1.5 | 115 | 80.9% | -$14.23 | 33.9% |

**Las 12 combinaciones probadas pierden dinero contra el baseline** (la
mejor, gmp=2/gsa=0.5, se queda en +$12.01 vs +$39.05 del baseline - un
69% peor). El patrón es consistente: agregar piernas piramidales SUBE el
número de operaciones y el drawdown, pero BAJA el PnL y el win rate en
todos los casos probados.

### Grid + recovery a las magnitudes ya documentadas en `.env.example`

`grid_max_positions=3, grid_step_atr=1.0, grid_lot_multiplier=1.0,
recovery_enabled=true, recovery_max_levels=2, recovery_min_regime=range`:
**157 trades, 83.4% acierto, -$4.76, 23.9% drawdown** - también pierde
contra el baseline.

## Conclusión: no se activa nada por defecto

Siguiendo la regla de sobreajuste del proyecto (si algo no mejora TRAIN no
se valida en TEST ni se activa), **ninguna configuración de grid/recovery
probada mejoró el baseline en TRAIN** - no hizo falta validar en TEST
porque no hay nada que validar. `GRID_ENABLED` y `RECOVERY_ENABLED` siguen
en `false` por defecto en `.env` y `.env.example`, sin cambios.

## Hallazgo de seguridad adicional (no es un bug, es una propiedad real del diseño ya en producción)

Con `RECOVERY_MAX_LEVELS` empujado por encima del valor ya documentado
(2) - probado con 5, más `grid_step_atr=0.5` para que más piernas
disparen - el drawdown máximo en TRAIN llega a ~44%, casi 4x el del
baseline sin grid, y una ganancia real se vuelve pérdida. La causa: cada
pierna nueva SÍ pasa por el cap del 5% de balance de `RiskManager`
(intocable, verificado), pero ese cap se aplica pierna por pierna contra
el balance ACTUAL - no se reduce el presupuesto de una pierna nueva por el
riesgo que ya está comprometido en piernas hermanas todavía abiertas de la
misma canasta. Esto es una propiedad real de `core/engine.py::_maybe_add_grid_position`
tal como está implementado hoy en producción (con los flags apagados), no
algo introducido por este backtest - reproducida fielmente acá, no
"arreglada" (no se tocó `core/engine.py` ni `core/risk_manager.py` en esta
ronda). A `RECOVERY_MAX_LEVELS=2` (el valor ya documentado) el efecto es
mucho más chico (23.9% vs 16.0% del baseline, ver arriba) - el riesgo real
crece con cuántas piernas adversas se permiten, no es automático con
recovery encendido en general.

## Hallazgo menor: `GRID_MAX_LOT` es código muerto

`GRID_MAX_LOT` está definido en `core/config.py` (`grid_max_lot: float =
0.09`) y documentado en `.env.example`, pero `core/engine.py::_maybe_add_grid_position`
nunca lo lee ni lo usa - no aparece en ningún lado del código fuera de
`core/config.py`. `DYNAMIC_LOT_CAP` (que sí se usa, vía
`RiskManager(max_lot=settings.dynamic_lot_cap)`) ya actúa como cap de lote
para TODAS las posiciones incluyendo piernas de grid, así que en la
práctica no falta protección - pero `GRID_MAX_LOT` promete algo que no
hace, lo cual puede confundir a quien lea el `.env.example` pensando que
es un segundo control real. No se tocó `core/engine.py` en esta ronda (fuera
del alcance de "no tocar el motor en vivo" sin confirmación extra), así
que queda documentado acá para una ronda futura.

## Qué falta si una ronda futura quiere retomar esto

- Explorar per-regime step/multiplier en vez de un único step fijo (el
  patrón de Ronda 46 con `risk_per_trade_usd_by_regime` podría adaptarse).
- Si alguna combinación futura SÍ mejora TRAIN, medir también el efecto de
  `recovery_max_levels` en el drawdown de forma explícita antes de
  proponer cualquier default nuevo - el hallazgo de esta ronda muestra que
  el número de niveles importa más que si recovery está prendido o no.
- Considerar si `core/engine.py::_maybe_add_grid_position` debería reducir
  el presupuesto de una pierna nueva por el riesgo ya comprometido en la
  canasta (cambio a `core/engine.py`/`core/risk_manager.py` - sujeto a la
  regla dura de revisión de esa parte del código).
