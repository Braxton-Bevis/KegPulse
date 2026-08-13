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
            foreach ($Version in @('-3.11', '-3.12')) {
                try {
                    & $Command.Source $Version -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,13)' 2>$null
                    if ($LASTEXITCODE -eq 0) { $Python = @($Command.Source, $Version); break }
                } catch {}
            }
        } else {
            try {
                & $Command.Source -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,13)' 2>$null
                if ($LASTEXITCODE -eq 0) { $Python = @($Command.Source) }
            } catch {}
        }
        if ($Python) { break }
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
& $VenvPython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('platformio') is None else 1)"
if ($LASTEXITCODE -ne 0) { throw 'The host .venv contains PlatformIO. Remove .venv and rerun setup; PlatformIO belongs only in .pio-venv.' }
Write-Host "KegPulse environment ready at $VenvDir"
if ($Dev) {
    $env:PLATFORMIO_SETTING_ENABLE_TELEMETRY = 'no'
    $FirmwareVenvDir = Join-Path $RepoRoot '.pio-venv'
    if (-not (Test-Path -LiteralPath $FirmwareVenvDir)) {
        $Launcher = $Python[0]
        $LauncherArguments = @()
        if ($Python.Count -gt 1) { $LauncherArguments = $Python[1..($Python.Count - 1)] }
        & $Launcher @LauncherArguments -m venv $FirmwareVenvDir
        if ($LASTEXITCODE -ne 0) { throw 'Isolated PlatformIO virtual environment creation failed.' }
    }
    $FirmwarePython = Join-Path $FirmwareVenvDir 'Scripts\python.exe'
    & $FirmwarePython -m pip install --upgrade 'pip==25.2'
    if ($LASTEXITCODE -ne 0) { throw 'PlatformIO environment pip upgrade failed.' }
    & $FirmwarePython -m pip install --require-hashes -r (Join-Path $RepoRoot 'requirements-firmware.lock')
    if ($LASTEXITCODE -ne 0) { throw 'Locked PlatformIO dependency installation failed.' }
    Write-Host "Isolated PlatformIO environment ready at $FirmwareVenvDir"
}
