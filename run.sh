#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[deprecated] Este proyecto ahora usa run.bat en Windows."
if command -v cmd.exe >/dev/null 2>&1; then
  exec cmd.exe /c run.bat "$@"
fi

if command -v node >/dev/null 2>&1; then
  exec node scripts/run.mjs "$@"
fi

echo "[run] Node.js no está instalado." >&2
exit 1
