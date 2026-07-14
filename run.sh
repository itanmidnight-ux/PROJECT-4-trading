#!/usr/bin/env bash
# =====================================================================
# run.sh - single entrypoint that starts everything the trading engine
# needs: a virtual display (if none), the Wine-side MT5 bridge, and the
# engine itself. Run install.sh once before this.
#
# The dashboard is separate on purpose (run `python dashboard.py` in
# another terminal) so you can watch stats without it dying if you
# restart the engine, and vice versa.
# =====================================================================
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

VENV_DIR="$PROJECT_ROOT/.venv"
WINEPREFIX_DIR="$PROJECT_ROOT/.wine"
LOG_DIR="$PROJECT_ROOT/data"
mkdir -p "$LOG_DIR"

log()  { printf '\033[1;36m[run]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[run][error]\033[0m %s\n' "$*" >&2; }

if [ ! -d "$VENV_DIR" ]; then
    err "No se encontro $VENV_DIR. Corre ./install.sh primero."
    exit 1
fi
if [ ! -f "$PROJECT_ROOT/.env" ]; then
    err "No se encontro .env. Corre ./install.sh primero (crea .env desde .env.example)."
    exit 1
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

SYNTHETIC_FLAG=""
if [ "${1:-}" = "--synthetic" ]; then
    SYNTHETIC_FLAG="--synthetic"
    log "Modo --synthetic: sin conexion a broker, precios simulados solo para pruebas."
fi

PIDS=()
cleanup() {
    log "Deteniendo procesos..."
    for pid in "${PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    log "Detenido."
}
trap cleanup INT TERM EXIT

if [ -z "$SYNTHETIC_FLAG" ]; then
    # ---- virtual display for Wine, if none is already available ----
    if [ -z "${DISPLAY:-}" ] && command -v Xvfb >/dev/null 2>&1; then
        log "No hay DISPLAY, iniciando Xvfb en :99"
        Xvfb :99 -screen 0 1024x768x16 >"$LOG_DIR/xvfb.log" 2>&1 &
        PIDS+=($!)
        export DISPLAY=:99
        sleep 2
    fi

    # ---- Wine-side MT5 bridge ----
    WIN_PYTHON="$WINEPREFIX_DIR/drive_c/users/$(whoami)/AppData/Local/Programs/Python/Python311/python.exe"
    if [ ! -f "$WIN_PYTHON" ]; then
        err "No se encontro python de Windows en $WIN_PYTHON. Corre ./install.sh."
        exit 1
    fi

    log "Iniciando bridge MT5 (Wine)..."
    export WINEPREFIX="$WINEPREFIX_DIR"
    export WINEDEBUG=-all
    wine "$WIN_PYTHON" "$PROJECT_ROOT/bridge/mt5_bridge_server.py" --port 5001 \
        >"$LOG_DIR/bridge.log" 2>&1 &
    PIDS+=($!)

    log "Esperando a que el bridge responda en http://127.0.0.1:5001/health ..."
    ready=0
    for _ in $(seq 1 60); do
        if curl -fsS "http://127.0.0.1:5001/health" >/dev/null 2>&1; then
            ready=1
            break
        fi
        sleep 2
    done
    if [ "$ready" -ne 1 ]; then
        err "El bridge no respondio a tiempo. Revisa $LOG_DIR/bridge.log"
        exit 1
    fi
    log "Bridge listo."
fi

log "Iniciando motor de trading..."
python3 "$PROJECT_ROOT/main.py" $SYNTHETIC_FLAG &
PIDS+=($!)

wait "${PIDS[@]}"
