# Protocolo de backtesting

Un resultado se considera válido sólo si registra símbolo, timeframe, rango,
modelo de fill, spread, capital, apalancamiento, riesgo, número de trades,
win-rate, PnL y drawdown.

## Recomendación

- Usa al menos dos meses cuando el terminal tenga ese historial.
- Divide rangos largos en ventanas pequeñas; el bridge ya lo hace para velas y
  ticks.
- Separa entrenamiento y validación fuera de muestra.
- Repite con spread conservador y tick mode activado.
- Compara siempre contra un escenario OHLC conservador.

El backtest nunca envía órdenes: sólo llama a endpoints de lectura del bridge.
Si MT5 no tiene datos, la prueba debe fallar explícitamente; no se sustituyen
ticks por datos sintéticos sin indicarlo.
