# Fase 2: resumen de Rondas 13-29

Fecha: 2026-07-24. Cierre del objetivo "hasta Ronda 30" pedido explícitamente
por el dueño del proyecto. Ver `2026-07-23-strategy-improvement-rondas-design.md`
para la metodología completa que gobernó cada ronda.

## Cambios reales que quedaron activos (mejoran el bot hoy)

- **Ronda 13** — `RiskManager` cap del 5% de balance por trade
  (`MAX_RISK_FRACTION_OF_BALANCE`, `core/risk_manager.py`). Bug real: riesgo
  fijo en dólares se vuelve proporcionalmente más peligroso a medida que el
  balance baja. Fix, no ajuste — protege la cuenta en cualquier balance.
- **Ronda 15** — `STRAT_SL_ATR_MULTIPLE` 4.0 → 3.5. Mejora validada train+test.
- **Ronda 20** — `MIN_TP_USD` 0.28 → 0.60. Atacó el techo estructural (TP
  chico vs SL ancho) que hundía toda señal nueva probada hasta ese punto.
  Mejora limpia en ambas mitades.
- **Ronda 21** — `STRAT_ENABLE_MA_GRID` activada. Rescatada por el fix de
  Ronda 20 — pasó de perder $29 a ganar en ambas mitades.
- **Ronda 22** — `TP_LEVELS` 5 → 8. Mejora limpia, sin confirmar más allá
  de 8 en TEST (se probó hasta 30).
- **Ronda 28** — harness de backtest del cerebro IA construido y probado
  (con mock, cero llamadas reales), listo para conectar cuando aplique.

## Hallazgos negativos, documentados con la misma honestidad (nada se esconde)

- **Ronda 14** — 6 señales extra (momentum_cross, rsi_hysteresis,
  directional_candle, session_open, asian_breakout, quantum_queen) pierden
  dinero sin excepción. Apagadas.
- **Ronda 17** — las mismas 6, medidas por régimen de mercado. Ningún
  régimen las salva.
- **Ronda 19** — 2 patrones de vela nuevos (engulfing, pin bar). Engulfing
  casi se salva en TEST pero TRAIN quedó negativo (descartado por
  metodología, no por capricho). Pin bar pierde claro.
- **Ronda 24** — achicar el SL para esquivar el cap de riesgo: sube el %
  de señales aceptadas pero empeora el PnL en todos los valores probados,
  sin excepción. 3.5 sigue siendo el pico real.
- **Ronda 16** — `RISK_PER_TRADE_USD` resultó cosmético en el contexto de
  ese momento (el cap del 5% domina primero a balances chicos).

## El hallazgo que cambia el panorama (Rondas 23, 24, 26, 27)

Después de 6 rondas de señales/parámetros nuevos, todas chocando el mismo
techo, **Ronda 23 identificó la causa raíz real**: no es un problema de
calidad de señal, es aritmética de cuenta chica. El lote mínimo del broker
(0.01) implica más riesgo real del que el presupuesto (5% del balance)
permite a la distancia de stop-loss que de verdad funciona (3.5x ATR).

**Ronda 26 calculó el número exacto**: a la ATR mediana real (1.7225), el
cap del 5% dejaría de rechazar señales recién en balance=$120.58 — pero
`RISK_PER_TRADE_USD=3.0` (fijo en dólares) lo topea antes, desde balance≥$60,
en un plateau de 18.34% de aceptación para siempre. **Al balance real actual
($20.45), el accept-rate medido es 0.00%.**

**Ronda 27 convirtió esto en una regla práctica** (tabla balance →
`RISK_PER_TRADE_USD` sugerido → accept-rate resultante):

| Balance | RISK_PER_TRADE_USD sugerido | % de señales que pasarían el cap |
|---|---|---|
| $20 (real hoy) | 3.0 (sin cambiar) | 0.00% |
| $60 | $3.00 (default actual) | 18.3% |
| $80 | $4.00 | 49.4% |
| **$100** | **$5.00** | **70.1%** ← acá vale la pena el cerebro IA |
| $150 | $7.50 | 92.6% |
| $200 | $10.00 | 97.8% |

**Conclusión honesta**: ya no hay más ganancia real disponible ajustando
estrategias/parámetros al balance actual — el cuello de botella es el
tamaño de cuenta, no el código. Seguir puliendo TP/SL/señales sin que el
balance crezca es optimizar un número que no es el limitante.

## Qué hacer cuando el balance crezca (nada de esto se hizo todavía)

1. Balance ≈ $60-80: nada urgente, el accept-rate sigue bajo.
2. **Balance ≈ $100**: subir `RISK_PER_TRADE_USD` a $5.00 en el `.env` real
   (no en `.env.example`, ese ya documenta la regla). Conectar el harness
   de Ronda 28 (cambiar el `brain_fn` mock por `OpenRouterBrain.evaluate`
   real) — recién ahí las 304 llamadas/día al cerebro IA valen su costo.
3. El cap del 5% (`core/risk_manager.py`) sigue intocable en todos los
   escenarios de arriba — la regla de Ronda 27 nunca lo supera, solo deja
   de ser el cuello de botella *adicional* que es `RISK_PER_TRADE_USD` hoy.

## Infraestructura (no es una "ronda" de estrategia, pero ayuda a todas)

Suite de tests completa: ~270-300s → 93s (pytest-xdist + reducción de un
test de paridad de 3000 a 1500 velas sin perder cobertura real). Usar
siempre `.venv/bin/python -m pytest tests/ -q -n 2` de acá en adelante.

## Estado final verificado (Ronda 29)

Dashboard, bridge y backtest funcionando de punta a punta vía la UI real
(no solo scripts), motor de trading sigue apagado (nunca se tocó sin
confirmación explícita, como pide la baranda de seguridad). Cuenta real:
$20.45, FBS-Demo, 106049158.
