@echo off
REM ====================================================================
REM  Auto-start the tracker as a SYSTEM task at BOOT.
REM
REM  This REPLACES the old per-user "onlogon" task (IETTTracker), which
REM  refused to start while the machine was on battery and stopped when
REM  it went onto battery. That was the single reason the box kept going
REM  dark. schtasks cannot set those flags, so the task is registered
REM  through PowerShell's Register-ScheduledTask instead.
REM
REM  No auto-login required any more: a SYSTEM boot task runs before,
REM  and without, anyone signing in.
REM
REM  Run ONCE, as admin. Close run-iett.bat first if it is open.
REM ====================================================================
title IETT tracker - install SYSTEM boot task

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Asking for administrator permission ^(click "Yes"^)...
  powershell -Command "Start-Process '%~f0' -Verb RunAs"
  exit /b
)
cd /d "%~dp0"
set TASK=IETTTrackerSys

REM --- Python must be an ABSOLUTE path: SYSTEM does not resolve the 'py' launcher
set "PYEXE="
for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
if not defined PYEXE for /f "delims=" %%i in ('where python.exe 2^>nul') do set "PYEXE=%%i"
if not defined PYEXE ( echo. & echo Python not found. Install it with "Add python.exe to PATH" ticked. & pause & exit /b 1 )
echo Using Python: %PYEXE%

echo Writing launcher (absolute paths, port guard, auto-restart loop)...
> "%~dp0service-run.bat" echo @echo off
>>"%~dp0service-run.bat" echo cd /d "%~dp0"
>>"%~dp0service-run.bat" echo if not exist logs mkdir logs
>>"%~dp0service-run.bat" echo :loop
>>"%~dp0service-run.bat" echo netstat -ano ^| findstr ":8731 " ^| findstr LISTENING ^>nul 2^>^&1
>>"%~dp0service-run.bat" echo if not errorlevel 1 ( timeout /t 30 /nobreak ^>nul ^& goto loop )
>>"%~dp0service-run.bat" echo echo [%%date%% %%time%%] START port 8731 ^>^> "logs\supervisor.log"
>>"%~dp0service-run.bat" echo "%PYEXE%" "iett_tracker.py" --map 522 --lan --port 8731 --no-open ^> "logs\console.log" 2^>^&1
>>"%~dp0service-run.bat" echo echo [%%date%% %%time%%] EXIT %%errorlevel%% ^>^> "logs\supervisor.log"
>>"%~dp0service-run.bat" echo timeout /t 5 /nobreak ^>nul
>>"%~dp0service-run.bat" echo goto loop

echo Writing hidden wrapper (no window)...
> "%~dp0service-hidden.vbs" echo CreateObject("WScript.Shell").Run "cmd /c ""%~dp0service-run.bat""", 0, False

echo Registering SYSTEM boot task (battery-allowed)...
> "%TEMP%\iett-task.ps1" echo $a = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument '"%~dp0service-hidden.vbs"'
>>"%TEMP%\iett-task.ps1" echo $t = New-ScheduledTaskTrigger -AtStartup
>>"%TEMP%\iett-task.ps1" echo $p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
>>"%TEMP%\iett-task.ps1" echo $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
>>"%TEMP%\iett-task.ps1" echo Unregister-ScheduledTask -TaskName '%TASK%' -Confirm:$false -ErrorAction SilentlyContinue
>>"%TEMP%\iett-task.ps1" echo Register-ScheduledTask -TaskName '%TASK%' -Action $a -Trigger $t -Principal $p -Settings $s ^| Out-Null
>>"%TEMP%\iett-task.ps1" echo Start-ScheduledTask -TaskName '%TASK%'
powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\iett-task.ps1"
if errorlevel 1 ( echo. & echo Could not register the task. & pause & exit /b 1 )
del "%TEMP%\iett-task.ps1" >nul 2>&1

echo Removing the superseded per-user logon task if present...
schtasks /delete /tn IETTTracker /f >nul 2>&1

echo Keeping the machine awake (lid close and sleep do nothing)...
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /setacvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0
powercfg /setdcvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0
powercfg /setactive SCHEME_CURRENT

timeout /t 8 /nobreak >nul
echo.
echo ============================================================
echo  Done. The tracker starts at BOOT as SYSTEM, hidden, and
echo  auto-restarts if it crashes. No auto-login needed.
echo.
echo  ONE MORE STEP - Tailscale must come up without a login:
echo      tailscale set --unattended
echo  Otherwise the tailnet only returns after someone signs in,
echo  and the phone cannot reach the laptop after a reboot.
echo.
echo  Verify:  schtasks /query /tn %TASK%
echo  Manage:  schtasks /end /tn %TASK%   ^|   /run   ^|   /delete /tn %TASK% /f
echo ============================================================
echo.
pause
