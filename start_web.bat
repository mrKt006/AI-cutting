@echo off
setlocal
cd /d "%~dp0"

echo Starting AI-cutting Web...
echo URL: http://127.0.0.1:8000/

start "" http://127.0.0.1:8000/
python -m uvicorn web.app:app --host 127.0.0.1 --port 8000

pause
