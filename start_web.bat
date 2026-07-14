@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo AI-cutting 正在启动...

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scripts\launch_web.py
  goto :end
)

where py >nul 2>nul
if not errorlevel 1 (
  py -3 scripts\launch_web.py
  goto :end
)

where python >nul 2>nul
if not errorlevel 1 (
  python scripts\launch_web.py
  goto :end
)

echo [错误] 没有找到 Python 3。请先安装 Python 3.10 或更高版本。

:end
if errorlevel 1 (
  echo.
  echo 启动失败，请查看上方错误，或阅读 docs\TROUBLESHOOTING.md。
  pause
)
