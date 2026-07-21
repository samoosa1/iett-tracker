@echo off
REM ====================================================================
REM  IETT tracker - one-time Windows setup. Double-click this once.
REM  (Keep this file in the same folder as iett_tracker.py)
REM ====================================================================
title IETT tracker - setup
cd /d "%~dp0"

echo Checking for Python...
py -3 --version
if errorlevel 1 (
  echo.
  echo  Python is not installed.
  echo  1. Go to https://www.python.org/downloads/  and install Python 3.
  echo  2. IMPORTANT: tick "Add python.exe to PATH" on the first screen.
  echo  3. Then double-click this file again.
  echo.
  pause
  exit /b 1
)

echo.
echo Installing dependencies (requests, rich, qrcode)...
py -3 -m pip install --user --upgrade pip
py -3 -m pip install --user requests rich qrcode
if errorlevel 1 ( echo Dependency install failed. & pause & exit /b 1 )

echo.
echo Seeding ETA baselines for line 522 (one-time, downloads ~135 MB of traffic data)...
py -3 iett_tracker.py --seed-traffic 522

echo.
echo ============================================================
echo  Setup complete!
echo  Next: double-click  run-iett.bat  to start the tracker.
echo ============================================================
pause
