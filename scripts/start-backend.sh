#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -x ".venv/Scripts/python.exe" ]; then VENV_PY=".venv/Scripts/python.exe"; else VENV_PY=".venv/bin/python"; fi
[ -x "$VENV_PY" ] || { echo "[ERROR] .venv not found. Run scripts/setup.sh first."; exit 1; }
exec "$VENV_PY" server.py "$@"
