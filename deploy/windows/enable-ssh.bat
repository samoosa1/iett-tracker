@echo off
REM ====================================================================
REM  Lets Claude (from the Mac, over Tailscale) run commands on this PC
REM  so you don't have to type them by hand.
REM  Installs Windows OpenSSH Server + trusts your public key. Run ONCE.
REM  Just double-click and click "Yes" on the admin popup.
REM ====================================================================
title IETT tracker - enable SSH

REM --- self-elevate to Administrator ---
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Asking for administrator permission ^(click "Yes" on the popup^)...
  powershell -Command "Start-Process '%~f0' -Verb RunAs"
  exit /b
)

echo.
echo Installing the SSH server ^(this can take a minute^)...
powershell -NoProfile -Command "Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0"

echo Setting it to start automatically...
powershell -NoProfile -Command "Set-Service -Name sshd -StartupType Automatic; Start-Service sshd"

echo Trusting your key...
REM  Paste the PUBLIC key of the machine that will SSH in (contents of ~/.ssh/id_ed25519.pub).
set "PUB=ssh-ed25519 AAAA...your-public-key... you@your-machine"
set "AUTH=%ProgramData%\ssh\administrators_authorized_keys"
>>"%AUTH%" echo %PUB%
icacls "%AUTH%" /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" >nul

echo.
echo ============================================================
echo  Done. SSH is on and your key is trusted.
echo  Tell Claude "ssh is ready" and it can drive this PC.
echo ============================================================
echo.
pause
