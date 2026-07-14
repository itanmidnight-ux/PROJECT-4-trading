#!/usr/bin/env bash
# Manual kill switch: touches the file the engine checks every poll cycle
# (see core/engine.py, KILL_SWITCH_PATH in .env). The engine force-closes
# any open position at market on its next step and halts - it does NOT
# rely on the engine process being reachable via stop.sh/kill, only on the
# filesystem being writable, so this also works if something (Wine hang,
# bridge stuck) is preventing a clean process shutdown.
#
# Usage:
#   ./emergency_stop.sh          activate (stop trading NOW)
#   ./emergency_stop.sh --clear  deactivate (allows ./run.sh to trade again)
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

KILL_SWITCH_PATH="$(grep -E '^KILL_SWITCH_PATH=' "$PROJECT_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
KILL_SWITCH_PATH="${KILL_SWITCH_PATH:-data/EMERGENCY_STOP}"
FLAG="$PROJECT_ROOT/$KILL_SWITCH_PATH"
RUN_STOP_FLAG="$PROJECT_ROOT/data/run/stop.flag"

log()  { printf '\033[1;36m[emergency_stop]\033[0m %s\n' "$*"; }

if [ "${1:-}" = "--clear" ]; then
    rm -f "$FLAG" "$RUN_STOP_FLAG"
    log "Interruptor de emergencia desactivado ($FLAG eliminado)."
    log "Corre ./run.sh de nuevo para reanudar el trading."
    exit 0
fi

mkdir -p "$(dirname "$FLAG")"
touch "$FLAG"
log "ACTIVADO: $FLAG creado."
log "El motor cerrara cualquier posicion abierta a mercado y se detendra en su proximo ciclo de sondeo."
log "Para reanudar despues: ./emergency_stop.sh --clear && ./run.sh"
