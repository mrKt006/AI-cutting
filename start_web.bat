@echo off
setlocal
cd /d "%~dp0"

echo Starting AI-cutting Web...

set "PORT=8000"
for /f %%P in ('python scripts\find_web_port.py') do set "PORT=%%P"

echo URL: http://127.0.0.1:%PORT%/
start "" http://127.0.0.1:%PORT%/
python -m uvicorn web.app:app --host 127.0.0.1 --port %PORT%

pause
