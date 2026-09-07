@echo off
REM ====================================================================
REM  Installs the IETTTrackerSys watchdog: every 15 minutes it checks how
REM  old logs\iett.log is and restarts IETTTrackerSys if the collector
REM  heartbeat has stopped. Needed because the supervisor loop in
REM  service-run.bat only catches EXITS; a hung python process keeps the
REM  port open and "Status: Running" while collecting nothing.
REM
REM  Run install-service.bat first. Then run this ONCE, as admin.
REM ====================================================================
title IETT tracker watchdog - install
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Asking for administrator permission ^(click "Yes"^)...
  powershell -Command "Start-Process '%~f0' -Verb RunAs"
  exit /b
)
set TASK=IETTTrackerWatchdog
schtasks /delete /tn %TASK% /f >nul 2>&1
schtasks /create /tn %TASK% /tr "%~dp0watchdog-run.bat" /sc MINUTE /mo 15 /ru SYSTEM /rl HIGHEST /f
REM schtasks defaults are wrong for a laptop (no start on battery, stop on
REM battery, 72 h time limit). Fix settings without touching the trigger.
powershell -NoProfile -Command ^
  "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -Hidden;" ^
  "Set-ScheduledTask -TaskName '%TASK%' -Settings $s | Out-Null"
schtasks /run /tn %TASK%
echo.
schtasks /query /tn %TASK% /fo LIST | findstr /i "TaskName Next Status"
pause
