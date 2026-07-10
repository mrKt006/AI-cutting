#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Starting AI-cutting Web..."

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN=python3
else
  PYTHON_BIN=python
fi

PORT="$("$PYTHON_BIN" scripts/find_web_port.py)"

echo "URL: http://127.0.0.1:${PORT}/"

if command -v open >/dev/null 2>&1; then
  open "http://127.0.0.1:${PORT}/" || true
fi

"$PYTHON_BIN" -m uvicorn web.app:app --host 127.0.0.1 --port "$PORT"
