<#
Telegram notice to the admin when this computer restarts (e.g. after a power cut): creates the
scheduled task <Prefix>-BootNotice, which runs at Windows startup, BEFORE anyone logs in (as
SYSTEM), and says whether the always-on dashboard came back or Windows still waits for a login.
Needs administrator rights once (it asks with the Windows prompt).
  powershell -ExecutionPolicy Bypass -File .\setup_boot_notice.ps1          # create it
  powershell -ExecutionPolicy Bypass -File .\setup_boot_notice.ps1 -Test    # create it and send a test now
  powershell -ExecutionPolicy Bypass -File .\setup_boot_notice.ps1 -Remove
#>
param(
    [string]$Prefix = "JobAlerts",
    [switch]$Test,
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$name = "$Prefix-BootNotice"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Prefix", $Prefix)
    if ($Test) { $argList += "-Test" }
    if ($Remove) { $argList += "-Remove" }
    Start-Process powershell -Verb RunAs -ArgumentList $argList -Wait
    return
}

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    "$name removed"
    Start-Sleep 3
    return
}

$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Virtual environment not found: $pythonw (see README, Installation)" }
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m jobalerts.boot_notice" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 4) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Job alerts: Telegram notice to the admin when the computer restarts" -Force | Out-Null
"$name created: it runs at every Windows startup, before login."
if ($Test) {
    Set-Location $root  # an administrator window starts in C:\Windows\System32
    & (Join-Path $root ".venv\Scripts\python.exe") -m jobalerts.boot_notice --test
    "Test sent: check the admin's Telegram."
}
Start-Sleep 5
