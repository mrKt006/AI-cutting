#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Starting AI-cutting Web..."
echo "URL: http://127.0.0.1:8000/"

if command -v open >/dev/null 2>&1; then
  open "http://127.0.0.1:8000/" || true
fi

if command -v python3 >/dev/null 2>&1; then
  python3 -m uvicorn web.app:app --host 127.0.0.1 --port 8000
else
  python -m uvicorn web.app:app --host 127.0.0.1 --port 8000
fi
