<#
Creates the two Windows scheduled tasks for the current user:
  <Prefix>-Morning  : full run every day at -MorningTime (default 07:30)
  <Prefix>-CatchUp  : collects Telegram clicks every 10 minutes from -CatchUpStart for -CatchUpHours
Run from the project folder, in PowerShell:
  powershell -ExecutionPolicy Bypass -File .\setup_tasks.ps1
Re-running it updates the tasks. The tasks run only while your Windows session is open
(a locked screen or a disconnected remote session is fine; a reboot without logging in is not).
#>
param(
    [string]$Prefix = "JobAlerts",
    [string]$MorningTime = "07:30",
    [string]$CatchUpStart = "07:40",
    [double]$CatchUpHours = 2.85,
    [int]$CatchUpMinutes = 10
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Virtual environment not found: $pythonw (see README, Installation)" }

$morningAt = [datetime]::Today + [timespan]::Parse($MorningTime)
$catchUpAt = [datetime]::Today + [timespan]::Parse($CatchUpStart)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

# Full run, once a day.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m jobalerts.run" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $morningAt
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "$Prefix-Morning" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Job alerts: full run (sources, sorting, Telegram)" -Force | Out-Null

# Catch-up: Telegram clicks every N minutes during the morning window.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m jobalerts.run --collect-only" -WorkingDirectory $root
$once = New-ScheduledTaskTrigger -Once -At $catchUpAt -RepetitionInterval (New-TimeSpan -Minutes $CatchUpMinutes) `
    -RepetitionDuration (New-TimeSpan -Minutes ([int]($CatchUpHours * 60)))
$trigger = New-ScheduledTaskTrigger -Daily -At $catchUpAt
$trigger.Repetition = $once.Repetition
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "$Prefix-CatchUp" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Job alerts: collect Telegram clicks" -Force | Out-Null

Get-ScheduledTask -TaskName "$Prefix-*" | ForEach-Object {
    $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
    "{0,-22} next run: {1}" -f $_.TaskName, $info.NextRunTime
}
