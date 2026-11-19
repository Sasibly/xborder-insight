@echo off
REM Windows equivalent of `make smoke` for machines without GNU make.
REM Keep this file ASCII-only: non-ASCII in .cmd breaks under some code pages.
REM
REM Usage:
REM   smoke.cmd                          local: temp db + temp port
REM   smoke.cmd --target docker          build image and run container
REM   smoke.cmd --target remote --base-url http://host:8000 --api-key xb_xxx
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [FATAL] venv not found: %PY%
  echo Create it with: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements-dev.txt
  exit /b 2
)
"%PY%" "%~dp0scripts\smoke.py" %*
exit /b %ERRORLEVEL%
