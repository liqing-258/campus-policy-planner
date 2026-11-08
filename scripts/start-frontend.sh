#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../frontend"
if [ ! -d node_modules ]; then
  echo "[1/2] installing frontend dependencies ..."
  npm install
fi
echo "[2/2] starting vite ..."
exec npm run dev
