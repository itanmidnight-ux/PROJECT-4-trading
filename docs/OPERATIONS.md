# Runbook operativo

## Arranque seguro

1. Comprueba que la cuenta MT5 correcta está abierta en el terminal.
2. Ejecuta `./run.sh doctor` y resuelve cualquier `[FALTA]`.
3. Inicia servicios con `./run.sh --start`.
4. Confirma `Bridge MT5: activo` y la cuenta esperada con `./run.sh --status`.
5. Mantén `DRY_RUN=true` durante la validación inicial.
6. Inicia el motor desde el dashboard sólo después de revisar margen y riesgo.

## Pausa y emergencia

- **Pausar entradas:** botón “Pausar entradas”; las posiciones existentes siguen
  protegidas.
- **Detener motor:** detiene el proceso del motor, no sustituye una emergencia.
- **Emergencia:** `./run.sh emergency-stop`; cierra posiciones al siguiente
  ciclo y bloquea el reinicio automático.

## Diagnóstico rápido

```bash
./run.sh --status
./run.sh doctor
tail -f data/logs/bridge.log
tail -f data/logs/dashboard.log
tail -f data/logs/engine.log
```

Nunca borres `data/trades.db` para “limpiar” un error: conserva primero una
copia y revisa los eventos registrados.
