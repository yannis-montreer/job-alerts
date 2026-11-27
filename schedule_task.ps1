<#
Creates, updates or removes one daily scheduled task of this tool, for the current user.
Used by the dashboard (Schedule page): one task per profile at its message time, and the
shared backup click check.
  .\schedule_task.ps1 -Name JobAlerts-p2 -Arguments "-m jobalerts.run --profile p2" -At 09:00
  .\schedule_task.ps1 -Name JobAlerts-CatchUp -Arguments "-m jobalerts.run --collect-only" -At 07:40 -EveryMinutes 10 -ForHours 2.85 -LimitMinutes 5
  .\schedule_task.ps1 -Name JobAlerts-p2 -Remove
#>
param(
    [Parameter(Mandatory = $true)][string]$Name,
    [string]$Arguments = "",
    [string]$At = "07:30",
    [int]$EveryMinutes = 0,
    [double]$ForHours = 0,
    [int]$LimitMinutes = 30,
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

if ($Remove) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    "$Name removed"
    return
}

$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Virtual environment not found: $pythonw (see README, Installation)" }
$startAt = [datetime]::Today + [timespan]::Parse($At)
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $Arguments -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $startAt
if ($EveryMinutes -gt 0) {
    $once = New-ScheduledTaskTrigger -Once -At $startAt -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
        -RepetitionDuration (New-TimeSpan -Minutes ([int]($ForHours * 60)))
    $trigger.Repetition = $once.Repetition
}
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes $LimitMinutes) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Job alerts ($Arguments)" -Force | Out-Null
"$Name scheduled at $At"
