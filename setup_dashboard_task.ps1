<#
Always-on dashboard (for phone access through Tailscale): creates the scheduled task
<Prefix>-Dashboard, which starts the dashboard in the background when you log on to Windows
(also after a restart, with automatic Windows login), restarts it within 5 minutes if it ever
stops, and starts it now. It listens on 127.0.0.1:<Port> only (default 8787).
Run from the project folder, in PowerShell:
  powershell -ExecutionPolicy Bypass -File .\setup_dashboard_task.ps1
  powershell -ExecutionPolicy Bypass -File .\setup_dashboard_task.ps1 -Remove
#>
param(
    [string]$Prefix = "JobAlerts",
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$name = "$Prefix-Dashboard"

if ($Remove) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    "$name removed"
    return
}

$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Virtual environment not found: $pythonw (see README, Installation)" }
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m jobalerts.dashboard --service" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Watchdog: every 5 minutes the task is started again; while the dashboard runs, the start is
# ignored (IgnoreNew), and if it stopped for any reason it is back within 5 minutes.
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $name -Action $action -Trigger @($trigger, $watchdog) -Settings $settings -Principal $principal `
    -Description "Job alerts: always-on dashboard (127.0.0.1 only; phone access through Tailscale Serve)" -Force | Out-Null
Start-ScheduledTask -TaskName $name
"$name created and started"
