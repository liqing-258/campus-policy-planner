#!/usr/bin/env bash
# ============================================================
#  ZhiXiao - one-shot local setup (macOS / Linux / Git Bash)
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "[ERROR] python3 not found"; exit 1; }

if [ ! -x ".venv/bin/python" ] && [ ! -x ".venv/Scripts/python.exe" ]; then
  echo "[1/5] creating venv .venv ..."
  "$PY" -m venv .venv
else
  echo "[1/5] venv already exists, skip"
fi

if [ -x ".venv/Scripts/python.exe" ]; then VENV_PY=".venv/Scripts/python.exe"; else VENV_PY=".venv/bin/python"; fi

echo "[2/5] installing dependencies ..."
"$VENV_PY" -m pip install -q --upgrade pip
"$VENV_PY" -m pip install -q -r requirements.txt

if [ ! -f ".env" ]; then
  echo "[3/5] .env missing, copying from .env.example ..."
  cp .env.example .env
  echo "      NOTE: edit .env and replace JWT_SECRET before real use."
else
  echo "[3/5] .env exists, skip"
fi

echo "[4/5] creating tables ..."
"$VENV_PY" tools/init_db.py

echo "[5/5] seeding demo users ..."
"$VENV_PY" tools/seed_demo.py

echo
echo "[OK] setup finished. Next:"
echo "     scripts/start-backend.sh    # http://127.0.0.1:5000/api/v1/health"
echo "     scripts/start-frontend.sh   # http://127.0.0.1:5173"
