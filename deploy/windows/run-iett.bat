@echo off
title IETT tracker (522)
cd /d "%~dp0"
:loop
py -3 iett_tracker.py --map 522 --lan --port 8731 --no-open
echo restarting in 5s... (close window to stop)
timeout /t 5 /nobreak >nul
goto loop
