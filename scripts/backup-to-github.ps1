# Nightly KegPulse backup to the private GitHub repository.
# Registered as a Windows scheduled task by scripts/register-backup-task.ps1;
# safe to run by hand at any time.
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { $Python = 'python' }
$LogDir = Join-Path $env:LOCALAPPDATA 'KegPulse\KegPulse\logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir 'github-backup.log'
& $Python (Join-Path $RepoRoot 'scripts\backup_to_github.py') 2>&1 | Tee-Object -FilePath $Log -Append
if ($LASTEXITCODE -ne 0) { throw "KegPulse backup failed (exit $LASTEXITCODE); see $Log" }
