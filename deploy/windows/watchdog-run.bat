@echo off
REM Thin wrapper so the scheduled task needs no nested quoting.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0watchdog.ps1" >nul 2>&1
