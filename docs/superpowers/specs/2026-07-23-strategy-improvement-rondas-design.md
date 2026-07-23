# Fase 2: mejora del bot vía backtesting (Rondas 13+)

Fecha: 2026-07-23

## Contexto

Con el dashboard/backtesting arreglado (Fase 1, mergeado a `main`) y el bug
del loop de emergencia corregido, el dueño del proyecto pidió Fase 2:
usar el backtesting (ahora rápido, ~4s en vez de 6+ minutos) para mejorar
el bot en sí — estrategias, sistemas de riesgo/régimen, y los dos
"cerebros" IA (`core/ai_brain.py`, `core/account_supervisor.py`,
OpenRouter, claves ya en `.env`). Alcance confirmado: todo — parámetros,
señales nuevas, cerebros, hasta cuándo/cómo se invocan.

El proyecto ya tiene un patrón establecido: 12 "Rondas" documentadas en
git log (`git log --oneline | grep -i ronda`), cada una un cambio chico
y concreto, probado, con resultado (mejoró / no ayudó) documentado en el
mensaje de commit. Varias Rondas anteriores terminaron en "no ayuda,
queda apagado" — el bot es honesto sobre lo que no funciona, no se
esconden los resultados negativos. Esta fase continúa esa numeración
desde Ronda 13.

Datos de entrenamiento/prueba ya existen y están separados:
- `data/gold_m1_7d_train.csv` / `_test.csv`
- `data/gold_history_m15_train.csv` / `_test.csv`
- (`data/gold_m5_60d.csv`, `data/gold_history.csv`, `data/xauusd_m1_live.csv`
  disponibles como series completas sin split, para contexto/diagnóstico)

Codex (segunda opinión pedida explícitamente por el dueño del proyecto)
está inaccesible — agotó su límite de uso hasta el 21 de agosto. Se
sustituye por un agente Claude independiente en cada checkpoint, mismo
patrón ya usado y aceptado en la Fase 1.

## Objetivo

Mejorar la expectativa (win rate, PnL, drawdown) del bot de forma
verificable, sin overfitting, documentando cada intento — exitoso o no —
con la misma disciplina que las Rondas 1-12 ya establecieron.

## Mecánica por ronda (confirmada)

1. **Hipótesis chica y concreta.** Un cambio, no un combo de cinco cosas
   a la vez — si mejora o empeora, tiene que quedar claro CUÁL cambio lo
   causó. Ejemplos de "tamaño de hipótesis" correcto: un umbral RSI, un
   filtro ADX, prender una estrategia extra, ajustar el prompt de un
   cerebro, cambiar un multiplicador de SL.
2. **Backtest en TRAIN.** `core/backtest.py::run_backtest(precompute_indicators=True)`
   contra `data/*_train.csv`. Si no mejora medible/consistentemente acá,
   se descarta ahí mismo — no pasa a test.
3. **Validar en TEST** (datos que el cambio nunca vio). Si la mejora no
   se sostiene en test, es overfitting al período de train — se descarta,
   se documenta por qué.
4. **Decisión:**
   - Mejora en ambos → se deja prendido, commit chico con el resultado
     numérico en el mensaje (mismo estilo que las Rondas anteriores:
     `Ronda N: <qué> (<resultado numérico train/test>)`).
   - No mejora, o mejora en train pero no en test → se revierte (o queda
     apagado por config si es una feature opt-in), se documenta la razón
     en el commit igual — "Ronda N: <qué probé>, no ayuda, queda apagado
     (<por qué, con números>)".
5. **Checkpoint de segunda opinión** (agente Claude sustituyendo a Codex)
   en cambios grandes — no en cada ajuste de un solo parámetro, pero sí
   antes de: activar una estrategia nueva por defecto, tocar la lógica de
   invocación de un cerebro, o cualquier cambio a `core/risk_manager.py`
   (superficie de seguridad, más escrutinio).

## Alcance de los cerebros (confirmado: todo)

- Prompts enviados a `ai_brain.py` (filtro de señales) y
  `account_supervisor.py` (supervisor de cuenta/riesgo).
- Umbrales de cuándo confiar en la respuesta de la IA vs. ignorarla.
- Qué modelo de OpenRouter usa cada uno (`OPENROUTER_MODEL`,
  `OPENROUTER_SUPERVISOR_MODEL` en `.env`).
- Cuándo/cómo se invocan desde `core/engine.py` (hoy: el brain filtra
  señales deterministas ya generadas, nunca elige lotes ni modifica
  stops por su cuenta — ver README "Cerebros OpenRouter"; ese invariante
  de seguridad — la IA nunca puede saltarse al RiskManager — se preserva
  SIEMPRE, cambiar CUÁNDO se la consulta está permitido, cambiar que
  pueda operar fuera del RiskManager no).
- Nota práctica: los cerebros no participan en `core/backtest.py` hoy
  (confirmado en la Fase 1 — solo se usan en `core/engine.py`, el motor
  en vivo). Para poder iterar sobre ellos con el mismo rigor train/test,
  esta fase probablemente necesita agregar un modo de backtest que sí
  los invoque (o un harness separado que replaye señales + llame al
  cerebro contra el historial), ya que hoy no hay forma de medir el
  impacto de un cambio de prompt sin correrlo en DRY_RUN real. Se decide
  el enfoque exacto en la primera ronda que toque un cerebro, no de
  antemano — parte del descubrimiento de esta fase, no algo fijo hoy.

## Cuándo parar

Rondas mientras cada una encuentre una mejora real y verificada out-of-
sample. Se para cuando 2-3 rondas seguidas no encuentran nada — patrón
"loop-until-dry", igual al que ya se ve en las Rondas 1-12 originales
(quedaron varias apagadas seguidas antes de que el dueño del proyecto
pausara esa fase la primera vez).

## Barandas de seguridad (no negociables)

- **Nunca tocar el motor en vivo sin confirmación explícita.** Todo el
  trabajo de esta fase es backtest-only contra CSVs locales. Si algún
  punto necesita probarse en DRY_RUN real (p. ej. un cambio de cerebro
  que no se puede medir solo con backtest), se pide confirmación
  explícita antes de arrancar el motor — nunca se hace solo.
- **`core/risk_manager.py` es superficie de alto escrutinio.** Cualquier
  cambio ahí pasa por el checkpoint de segunda opinión sin excepción,
  incluso si es un ajuste de un solo número.
- **El invariante "la IA nunca opera fuera del RiskManager" no se toca.**
  Es la garantía de seguridad central del proyecto (ver README).
- **`./run.sh verify` (o el equivalente de la suite completa) pasa antes
  de cada commit de ronda.**
- Cuenta real conectada (FBS-Demo, plata demo pero mecánica real de
  MT5) — nada de esto se ejecuta contra ella sin decisión explícita.

## Entregable de esta fase

No es un plan de tareas fijo (el trabajo es exploratorio por diseño — no
se sabe de antemano cuántas rondas ni cuáles hipótesis van a salir bien).
En vez de `writing-plans` con tareas enumeradas, se ejecuta directamente
en rondas siguiendo la mecánica de arriba, cada ronda como su propio
ciclo hipótesis→backtest→decisión→commit, con checkpoints de segunda
opinión en los cambios grandes. Un resumen de qué se probó y qué quedó
prendido se mantiene igual que el `README.md`/historial de commits ya
lo viene haciendo.
