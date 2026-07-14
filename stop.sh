#!/usr/bin/env bash
# Stops a ./run.sh --daemon instance cleanly (bridge + engine + Xvfb).
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$PROJECT_ROOT/data/run"
PIDFILE="$RUN_DIR/supervisor.pid"

log()  { printf '\033[1;36m[stop]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[stop][error]\033[0m %s\n' "$*" >&2; }

if [ ! -f "$PIDFILE" ]; then
    err "No hay una instancia registrada en $PIDFILE (no esta corriendo, o no se inicio con --daemon)."
    exit 1
fi

PID=$(cat "$PIDFILE")
if ! kill -0 "$PID" 2>/dev/null; then
    err "El proceso $PID ya no existe. Limpiando pidfile."
    rm -f "$PIDFILE"
    exit 0
fi

log "Enviando señal de parada al supervisor (PID $PID)..."
kill "$PID" 2>/dev/null || true

for _ in $(seq 1 20); do
    kill -0 "$PID" 2>/dev/null || { log "Detenido."; exit 0; }
    sleep 1
done

err "No se detuvo a tiempo, forzando con SIGKILL."
kill -9 "$PID" 2>/dev/null || true
rm -f "$PIDFILE"
