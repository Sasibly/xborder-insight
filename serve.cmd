@echo off
REM Windows equivalent of `make serve`, for machines without GNU make.
REM Usage: serve.cmd [port]        (default 8000; use 8018 to match Makefile)
REM Keep this file ASCII-only: non-ASCII in .cmd breaks under some code pages.
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [FATAL] venv not found: %PY%
  echo Create it with: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements-dev.txt
  exit /b 2
)
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"

REM serve reads the real database, so bring it to the latest schema first.
REM db.py up is idempotent: already-applied migrations are skipped.
"%PY%" "%~dp0scripts\db.py" up
if errorlevel 1 (
  echo [FATAL] db.py up failed, refusing to start on a half-migrated schema
  exit /b 3
)

echo [serve] http://127.0.0.1:%PORT%/        (Ctrl+C to stop)
echo [serve] API docs: http://127.0.0.1:%PORT%/docs
"%PY%" -m uvicorn xborder_insight.api:app --app-dir "%~dp0src" --host 127.0.0.1 --port %PORT%
exit /b %ERRORLEVEL%
