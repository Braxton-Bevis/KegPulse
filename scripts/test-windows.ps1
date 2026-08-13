[CmdletBinding()]
param([switch]$SkipBrowser, [switch]$SkipFirmware)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$Ruff = Join-Path $RepoRoot '.venv\Scripts\ruff.exe'
$Mypy = Join-Path $RepoRoot '.venv\Scripts\mypy.exe'
$Pio = Join-Path $RepoRoot '.pio-venv\Scripts\platformio.exe'
Set-Location -LiteralPath $RepoRoot
if (-not (Test-Path -LiteralPath $Python)) { throw 'Run scripts/setup-windows.ps1 -Dev first.' }
New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot 'artifacts') | Out-Null
& $Ruff format --check (Join-Path $RepoRoot 'src') (Join-Path $RepoRoot 'tests')
if ($LASTEXITCODE -ne 0) { throw 'Ruff formatting check failed.' }
& $Ruff check (Join-Path $RepoRoot 'src') (Join-Path $RepoRoot 'tests')
if ($LASTEXITCODE -ne 0) { throw 'Ruff lint failed.' }
& $Mypy (Join-Path $RepoRoot 'src\kegpulse')
if ($LASTEXITCODE -ne 0) { throw 'mypy failed.' }
& $Python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('platformio') is None else 1)"
if ($LASTEXITCODE -ne 0) { throw 'PlatformIO must not be installed in the host .venv.' }
$PytestArgs = @('-m', 'pytest', '--ignore', (Join-Path $RepoRoot 'tests\e2e'), '--junitxml=artifacts/pytest-windows.xml', '--cov=kegpulse', '--cov-branch', '--cov-report=term-missing', '--cov-report=xml:coverage.xml', '--cov-report=json:coverage.json')
& $Python @PytestArgs
if ($LASTEXITCODE -ne 0) { throw 'pytest failed.' }
& $Python (Join-Path $RepoRoot 'scripts\check_core_coverage.py') (Join-Path $RepoRoot 'coverage.json') --minimum 90
if ($LASTEXITCODE -ne 0) { throw 'Core branch coverage is below 90%.' }
if (-not $SkipBrowser) {
    & $Python -m pytest (Join-Path $RepoRoot 'tests\e2e') '--junitxml=artifacts/pytest-browser-windows.xml'
    if ($LASTEXITCODE -ne 0) { throw 'Browser tests failed.' }
}
if (-not $SkipFirmware) {
    if (-not (Test-Path -LiteralPath $Pio)) { throw 'Run scripts/setup-windows.ps1 -Dev first.' }
    $env:PLATFORMIO_SETTING_ENABLE_TELEMETRY = 'no'
    & $Pio pkg install --global --tool 'platformio/toolchain-gccmingw32'
    if ($LASTEXITCODE -ne 0) { throw 'MinGW toolchain installation failed.' }
    $env:Path = "$env:USERPROFILE\.platformio\packages\toolchain-gccmingw32\bin;$env:Path"
    & $Pio test -d (Join-Path $RepoRoot 'firmware') -e native
    if ($LASTEXITCODE -ne 0) { throw 'Native firmware tests failed.' }
    & $Pio run -d (Join-Path $RepoRoot 'firmware') -e nanoatmega328
    if ($LASTEXITCODE -ne 0) { throw 'Nano firmware build failed.' }
}
