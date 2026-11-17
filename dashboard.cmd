@echo off
rem Double-click to open the Job Alerts dashboard. Close this window to stop it.
cd /d "%~dp0"
title Job Alerts dashboard - close this window to stop
if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. See README, step 1.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m jobalerts.dashboard
pause
