[CmdletBinding()]
param([switch]$Dev)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$VenvDir = Join-Path $RepoRoot '.venv'
$Python = $null

foreach ($Candidate in @('py', 'python')) {
    $Command = Get-Command $Candidate -ErrorAction SilentlyContinue
    if ($Command) {
        if ($Candidate -eq 'py') {
            try { & $Command.Source -3.11 -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,13)' 2>$null; $Python = @($Command.Source, '-3.11'); break } catch {}
            try { & $Command.Source -3.12 -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,13)' 2>$null; $Python = @($Command.Source, '-3.12'); break } catch {}
        } else {
            try { & $Command.Source -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,13)' 2>$null; $Python = @($Command.Source); break } catch {}
        }
    }
}
if (-not $Python) { throw 'KegPulse requires CPython 3.11 or 3.12. Install it from python.org, then rerun this script.' }

if (-not (Test-Path -LiteralPath $VenvDir)) {
    $Launcher = $Python[0]
    $LauncherArguments = @()
    if ($Python.Count -gt 1) { $LauncherArguments = $Python[1..($Python.Count - 1)] }
    & $Launcher @LauncherArguments -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$Lock = Join-Path $RepoRoot ($(if ($Dev) { 'requirements-dev.lock' } else { 'requirements.lock' }))
& $VenvPython -m pip install --upgrade 'pip==25.2'
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }
& $VenvPython -m pip install --require-hashes -r $Lock
if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
& $VenvPython -m pip install --no-deps -e $RepoRoot
if ($LASTEXITCODE -ne 0) { throw 'KegPulse editable installation failed.' }
Write-Host "KegPulse environment ready at $VenvDir"
