@echo off
cd /d "%~dp0"
title NBE Call Log server
where py >nul 2>nul && (set PY=py -3) || (set PY=python)

if not exist ".venv\Scripts\python.exe" (
  echo First start: setting things up ^(needs internet once to download Flask^)...
  %PY% -m venv .venv || goto :fail
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :fail
)

".venv\Scripts\python.exe" run.py
pause
exit /b 0

:fail
echo.
echo Setup failed. Install Python 3.9 or newer from python.org ^(tick "Add python.exe to PATH"^) and try again.
pause
exit /b 1
