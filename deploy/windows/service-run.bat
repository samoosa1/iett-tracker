@echo off
REM Supervisor loop: keeps the tracker running and restarts it if it exits.
REM
REM NOTE: install-service.bat REGENERATES this file with Python's absolute path,
REM because the SYSTEM account cannot resolve the `py` launcher. This committed
REM copy is the portable version, fine for running by hand on your own account.
cd /d "%~dp0"
if not exist logs mkdir logs
:loop
REM Do not start a second copy if the port is already served.
netstat -ano | findstr ":8731 " | findstr LISTENING >nul 2>&1
if not errorlevel 1 ( timeout /t 30 /nobreak >nul & goto loop )
echo [%date% %time%] START port 8731 >> "logs\supervisor.log"
py -3 "iett_tracker.py" --map 522 --lan --port 8731 --no-open > "logs\console.log" 2>&1
echo [%date% %time%] EXIT %errorlevel% >> "logs\supervisor.log"
timeout /t 5 /nobreak >nul
goto loop
