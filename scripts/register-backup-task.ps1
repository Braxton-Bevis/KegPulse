# Registers (or refreshes) the nightly KegPulse -> GitHub backup as a Windows
# scheduled task for the current user. Run once; safe to re-run.
param([string]$Time = '04:00')

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$Script = Join-Path $RepoRoot 'scripts\backup-to-github.ps1'
$TaskName = 'KegPulse GitHub backup'

$Action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`"" `
    -WorkingDirectory $RepoRoot
# Nightly, plus a catch-up run at logon in case the laptop was asleep overnight.
$Triggers = @(
    (New-ScheduledTaskTrigger -Daily -At $Time),
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)
)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers `
    -Settings $Settings -Description 'Pushes a scrubbed KegPulse database snapshot to the private GitHub backup repository.' `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-List
