#!/usr/bin/env bash
set -euo pipefail

# One-project, one-venv runner. Creates .venv if missing, installs deps, then runs the server

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VENV_DIR="$ROOT_DIR/.venv"
PYTHON_BIN=""

if [[ ! -d "$VENV_DIR" ]]; then
  echo "[dev] Creating virtualenv at .venv" >&2
  if command -v python3 >/dev/null 2>&1; then PYTHON_BIN="python3"; else PYTHON_BIN="python"; fi
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

echo "[dev] Using python: $(python -c 'import sys; print(sys.executable)')" >&2

echo "[dev] Ensuring dependencies (from requirements.txt)" >&2
python -m pip install -U pip >/dev/null
python -m pip install -r requirements.txt >/dev/null

export PYTHONPATH="$ROOT_DIR:$PYTHONPATH"

echo "[dev] Starting server on ${APP_HOST:-127.0.0.1}:${APP_PORT:-8000}" >&2
exec python -m uvicorn app.server:app --reload --host "${APP_HOST:-127.0.0.1}" --port "${APP_PORT:-8000}"

