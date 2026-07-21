@echo off
REM ====================================================================
REM  Opens Windows Firewall so your phone can reach the tracker over
REM  Tailscale (port 8731). Just double-click it and click "Yes" on the
REM  admin popup. No typing needed.
REM ====================================================================
title IETT tracker - firewall fix

REM --- self-elevate to Administrator (firewall changes need admin) ---
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Asking for administrator permission ^(click "Yes" on the popup^)...
  powershell -Command "Start-Process '%~f0' -Verb RunAs"
  exit /b
)

echo.
echo Opening port 8731 for incoming connections...
netsh advfirewall firewall delete rule name="IETT tracker" >nul 2>&1
netsh advfirewall firewall add rule name="IETT tracker" dir=in action=allow protocol=TCP localport=8731 profile=any

echo.
echo Making sure Python is allowed (clears any hidden block)...
for /f "delims=" %%P in ('where python.exe 2^>nul') do (
  netsh advfirewall firewall delete rule name=all program="%%P" dir=in >nul 2>&1
  netsh advfirewall firewall add rule name="IETT Python" dir=in action=allow program="%%P" enable=yes profile=any >nul 2>&1
)

echo.
echo ============================================================
echo  Done. Port 8731 is open.
echo  Now retry  http://^<TAILSCALE-IP^>:8731/  on your phone.
echo  ^(find it with:  tailscale ip -4^)
echo ============================================================
echo.
pause
