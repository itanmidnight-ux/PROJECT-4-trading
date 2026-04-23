#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[deprecated] Este proyecto ahora usa stop.bat en Windows."
if command -v cmd.exe >/dev/null 2>&1; then
  exec cmd.exe /c stop.bat "$@"
fi

if command -v node >/dev/null 2>&1; then
  exec node scripts/stop.mjs "$@"
fi

echo "[stop] Node.js no está instalado." >&2
exit 1
