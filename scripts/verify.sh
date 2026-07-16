#!/usr/bin/env bash
# scripts/verify.sh - repeatable "does everything still work" check.
# Run this after any code change, before trusting a real (even demo) run.
#
# Does NOT need Wine/MT5/a broker connection - everything here runs
# against synthetic data or an in-memory/temp SQLite DB.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

log()  { printf '\033[1;36m[verify]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[verify][error]\033[0m %s\n' "$*" >&2; }

if [ ! -d ".venv" ]; then
    err "No se encontro .venv. Corre ./install.sh primero (o python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt)."
    exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if ! python3 -c "import pytest" 2>/dev/null; then
    log "Instalando dependencias de test (requirements-dev.txt)..."
    pip install -q -r requirements-dev.txt
fi

log "1/4  Compilando todos los modulos Python..."
python3 -m py_compile core/*.py main.py dashboard.py scripts/*.py
python3 -c "import ast; ast.parse(open('bridge/mt5_bridge_server.py').read())"
log "     OK"

log "2/4  Corriendo tests (pytest)..."
python3 -m pytest tests/ -q
log "     OK"

log "3/4  Prueba de humo: motor en modo --synthetic durante 12s (sin broker)..."
tmp_log="$(mktemp)"
( timeout 12 python3 -u main.py --synthetic --log-level INFO >"$tmp_log" 2>&1 ) || true
if grep -q "Engine started" "$tmp_log"; then
    log "     OK (el motor arranco y corrio el loop sin excepciones no controladas)"
else
    err "El motor no llego a arrancar. Salida:"
    cat "$tmp_log" >&2
    rm -f "$tmp_log"
    exit 1
fi
if grep -qi "Traceback" "$tmp_log"; then
    err "Se encontro un traceback durante la prueba de humo:"
    cat "$tmp_log" >&2
    rm -f "$tmp_log"
    exit 1
fi
rm -f "$tmp_log"

log "4/4  Validando que dashboard.py sirve su API sin errores..."
python3 - <<'PYEOF'
import sys
sys.path.insert(0, ".")
import dashboard as dmod
client = dmod.app.test_client()
for path in ("/", "/app.js", "/style.css", "/api/status", "/api/summary",
             "/api/equity_curve", "/api/pnl_daily", "/api/pnl_monthly",
             "/api/trades", "/api/events", "/api/settings"):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
print("     OK (todas las rutas del dashboard responden 200)")
PYEOF

cat <<'EOF'

=====================================================================
 Todo verificado: compila, los tests pasan, el motor corre sin
 excepciones con datos sinteticos, y el dashboard sirve su API.

 Esto NO prueba que la estrategia sea rentable, ni que la conexion
 real a MT5/FBS funcione (eso necesita Wine + el bridge corriendo en
 tu maquina Linux real). Para eso: ./install.sh, luego ./run.sh y
 revisa data/logs/bridge.log.
=====================================================================
EOF
