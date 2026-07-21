@echo off
REM ====================================================================
REM  Keeps the laptop awake 24/7 so the tracker never stops collecting.
REM  Double-click and click "Yes" on the admin popup. Run it once.
REM ====================================================================
title IETT tracker - keep awake

REM --- self-elevate to Administrator (power settings need admin) ---
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Asking for administrator permission ^(click "Yes" on the popup^)...
  powershell -Command "Start-Process '%~f0' -Verb RunAs"
  exit /b
)

echo.
echo Never sleep / hibernate while plugged in...
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change disk-timeout-ac 0

echo Do nothing when the lid closes ^(while plugged in^)...
powercfg /setacvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0
powercfg /setactive SCHEME_CURRENT

echo.
echo ============================================================
echo  Done. The laptop stays awake on power, even with the lid shut.
echo  Keep it PLUGGED IN. (The screen can still turn off - that's fine.)
echo ============================================================
echo.
pause
