@echo off
REM Windows equivalent of `make check` for machines without GNU make.
REM Keep this file ASCII-only: non-ASCII in .cmd breaks under some code pages.
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [FATAL] venv not found: %PY%
  echo Create it with: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements-dev.txt
  exit /b 2
)
"%PY%" "%~dp0scripts\check.py" %*
exit /b %ERRORLEVEL%
