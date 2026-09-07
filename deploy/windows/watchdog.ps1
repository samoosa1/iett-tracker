# ====================================================================
#  IETTTrackerSys watchdog.
#
#  service-run.bat only respawns the tracker when python EXITS. A hung
#  process (collector thread stuck, port 8731 still listening) never exits,
#  so the supervisor waits forever and schtasks keeps saying "Running".
#
#  The only trustworthy signal is the age of logs\iett.log: the collector
#  thread writes a heartbeat every 5 min by day and every 15 min during the
#  overnight 900s nap. Anything past 40 min means the collector is stuck.
# ====================================================================
param(
    [string]$Root = $PSScriptRoot,
    [int]$StaleMinutes = 40,          # > 2x the 15 min night heartbeat
    [string]$TaskName = "IETTTrackerSys"
)

$log = Join-Path $Root "logs\watchdog.log"
function Say($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $log -Value $line
    Write-Output $line
}

$hb = Get-Item (Join-Path $Root "logs\iett.log") -ErrorAction SilentlyContinue
if ($null -eq $hb) {
    $age = [double]::PositiveInfinity
    $desc = "logs\iett.log missing"
} else {
    $age = ((Get-Date) - $hb.LastWriteTime).TotalMinutes
    $desc = "last heartbeat {0:N0} min old" -f $age
}

if ($age -le $StaleMinutes) {
    Say ("ok, {0}" -f $desc)
} else {
    Say ("STALE: {0}, threshold {1} min -- restarting {2}" -f $desc, $StaleMinutes, $TaskName)

    schtasks /end /tn $TaskName 2>&1 | Out-Null

    # Kill only the tracker, never every python on the box: other daemons
    # may share the machine.
    $killed = 0
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*iett_tracker.py*" } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $killed++
        }
    Say ("killed {0} tracker python process(es)" -f $killed)

    Start-Sleep -Seconds 3
    schtasks /run /tn $TaskName 2>&1 | Out-Null
    Say ("issued schtasks /run for {0}" -f $TaskName)
}

# Keep the log from growing without bound.
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 512KB)) {
    $keep = Get-Content $log -Tail 500
    Set-Content -Path $log -Value $keep
}
