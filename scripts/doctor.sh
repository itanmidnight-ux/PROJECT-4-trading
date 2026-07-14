#!/usr/bin/env bash
# scripts/doctor.sh - read-only diagnostic for the real Linux deployment.
# Run this when something isn't working to see exactly what's missing,
# instead of re-running the whole install or guessing. Never installs or
# changes anything itself - see install.sh for that.
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT" || exit 1

PASS=0
FAIL=0
WARN=0

ok()   { printf '  \033[1;32m[OK]\033[0m       %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[1;31m[FALTA]\033[0m    %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[1;33m[AVISO]\033[0m    %s\n' "$*"; WARN=$((WARN+1)); }
section() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }

section "1. Entorno Python (Linux)"
if [ -d ".venv" ]; then
    ok ".venv existe"
    if .venv/bin/python3 -c "import pandas, numpy, flask, requests, dotenv, webview" 2>/dev/null; then
        ok "dependencias de requirements.txt importan correctamente"
    else
        bad "faltan dependencias en .venv - corre: .venv/bin/pip install -r requirements.txt"
    fi
else
    bad "no existe .venv - corre ./install.sh"
fi

section "2. Configuracion (.env)"
if [ -f ".env" ]; then
    ok ".env existe"
    # shellcheck disable=SC1091
    set -a; source .env; set +a
    if [ -n "${MT5_LOGIN:-}" ] && [ -n "${MT5_PASSWORD:-}" ]; then
        ok "MT5_LOGIN y MT5_PASSWORD estan configurados"
    else
        bad "MT5_LOGIN o MT5_PASSWORD vacios en .env - editalo o vuelve a correr ./install.sh"
    fi
    if [ "${DRY_RUN:-true}" = "true" ]; then
        ok "DRY_RUN=true (modo seguro: precios reales, sin ordenes reales)"
    else
        warn "DRY_RUN=false: el motor mandara ORDENES REALES a la cuenta ${MT5_LOGIN:-?} (${MT5_SERVER:-?})"
    fi
    if [ -n "${BRIDGE_AUTH_TOKEN:-}" ]; then
        ok "BRIDGE_AUTH_TOKEN configurado (el bridge exige autenticacion)"
    else
        warn "BRIDGE_AUTH_TOKEN vacio - el bridge correra sin autenticacion (protegido solo por estar en 127.0.0.1). Corre ./install.sh para generarlo."
    fi
    perms=$(stat -c '%a' .env 2>/dev/null || echo '?')
    if [ "$perms" != "600" ]; then
        warn ".env tiene permisos $perms (se recomienda 600: chmod 600 .env)"
    fi
    kill_switch="${KILL_SWITCH_PATH:-data/EMERGENCY_STOP}"
    if [ -f "$PROJECT_ROOT/$kill_switch" ]; then
        warn "El interruptor de emergencia esta ACTIVO ($kill_switch existe) - el motor no abrira operaciones. Corre ./emergency_stop.sh --clear para reanudar."
    else
        ok "Interruptor de emergencia inactivo ($kill_switch no existe)"
    fi
else
    bad "no existe .env - corre ./install.sh"
fi

section "3. Wine + MetaTrader 5"
if command -v wine >/dev/null 2>&1; then
    ok "wine instalado ($(wine --version 2>/dev/null || echo 'version desconocida'))"
else
    bad "wine no esta instalado - corre ./install.sh"
fi

WINEPREFIX_DIR="$PROJECT_ROOT/.wine"
if [ -d "$WINEPREFIX_DIR" ]; then
    ok "prefijo de Wine existe en $WINEPREFIX_DIR"
else
    bad "no existe el prefijo de Wine ($WINEPREFIX_DIR) - corre ./install.sh"
fi

MT5_MARKER="$WINEPREFIX_DIR/drive_c/Program Files/MetaTrader 5/terminal64.exe"
if [ -f "$MT5_MARKER" ]; then
    ok "terminal MetaTrader 5 instalado"
else
    bad "no se encontro terminal64.exe - corre ./install.sh"
fi

WIN_PYTHON="$WINEPREFIX_DIR/drive_c/users/$(whoami)/AppData/Local/Programs/Python/Python311/python.exe"
if [ -f "$WIN_PYTHON" ]; then
    ok "python de Windows (para el bridge) instalado"
    export WINEPREFIX="$WINEPREFIX_DIR"
    export WINEDEBUG=-all
    if wine "$WIN_PYTHON" -c "import MetaTrader5" >/dev/null 2>&1; then
        ok "paquete MetaTrader5 importable dentro de Wine"
    else
        bad "el paquete MetaTrader5 no importa en el python de Wine - corre ./install.sh de nuevo"
    fi
else
    bad "no se encontro el python de Windows dentro de Wine - corre ./install.sh"
fi

section "4. Pantalla virtual (Xvfb)"
if command -v Xvfb >/dev/null 2>&1; then
    ok "Xvfb instalado"
else
    warn "Xvfb no encontrado - run.sh lo necesita si no hay DISPLAY configurado"
fi

section "5. Bridge MT5 (proceso en vivo)"
if curl -fsS "http://127.0.0.1:5001/health" >/dev/null 2>&1; then
    health_json=$(curl -fsS "http://127.0.0.1:5001/health" 2>/dev/null)
    if echo "$health_json" | grep -q '"connected": *true'; then
        ok "bridge corriendo y con sesion MT5 activa"
    else
        warn "bridge corriendo pero SIN sesion MT5 activa (falta /login o credenciales invalidas)"
    fi
else
    warn "bridge no responde en 127.0.0.1:5001 (normal si ./run.sh no esta corriendo ahora mismo)"
fi

section "6. Procesos y espacio en disco"
if [ -f "data/run/supervisor.pid" ] && kill -0 "$(cat data/run/supervisor.pid)" 2>/dev/null; then
    ok "hay una instancia de run.sh corriendo (PID $(cat data/run/supervisor.pid))"
else
    warn "no hay ninguna instancia de run.sh corriendo ahora mismo"
fi

avail_kb=$(df -Pk "$PROJECT_ROOT" | awk 'NR==2 {print $4}')
avail_mb=$(( avail_kb / 1024 ))
if [ "$avail_mb" -lt 500 ]; then
    bad "solo quedan ${avail_mb}MB de disco libres - Wine + MT5 necesitan varios GB"
elif [ "$avail_mb" -lt 2000 ]; then
    warn "quedan ${avail_mb}MB de disco libres - puede quedar justo para Wine + MT5"
else
    ok "espacio en disco suficiente (${avail_mb}MB libres)"
fi

section "Resumen"
printf '  \033[1;32m%d OK\033[0m   \033[1;33m%d avisos\033[0m   \033[1;31m%d faltan\033[0m\n' "$PASS" "$WARN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
    echo "  Corre ./install.sh para resolver lo que falta arriba."
    exit 1
fi
exit 0
