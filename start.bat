@echo off
setlocal
cd /d "%~dp0"
python -X utf8 run.py
if errorlevel 1 (
  echo.
  echo Start failed. Python 3.11+ is required. See README.md.
  pause
)
