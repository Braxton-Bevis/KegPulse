[CmdletBinding()]
param([switch]$SkipBrowser, [switch]$SkipFirmware)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$Ruff = Join-Path $RepoRoot '.venv\Scripts\ruff.exe'
$Mypy = Join-Path $RepoRoot '.venv\Scripts\mypy.exe'
$Pio = Join-Path $RepoRoot '.venv\Scripts\platformio.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Run scripts/setup-windows.ps1 -Dev first.' }
& $Ruff format --check (Join-Path $RepoRoot 'src') (Join-Path $RepoRoot 'tests')
if ($LASTEXITCODE -ne 0) { throw 'Ruff formatting check failed.' }
& $Ruff check (Join-Path $RepoRoot 'src') (Join-Path $RepoRoot 'tests')
if ($LASTEXITCODE -ne 0) { throw 'Ruff lint failed.' }
& $Mypy (Join-Path $RepoRoot 'src\kegpulse')
if ($LASTEXITCODE -ne 0) { throw 'mypy failed.' }
$PytestArgs = @('-m', 'pytest', '--cov=kegpulse', '--cov-branch', '--cov-report=term-missing', '--cov-report=xml:coverage.xml')
if ($SkipBrowser) { $PytestArgs += @('--ignore', (Join-Path $RepoRoot 'tests\e2e')) }
& $Python @PytestArgs
if ($LASTEXITCODE -ne 0) { throw 'pytest failed.' }
if (-not $SkipFirmware) {
    & $Pio pkg install --global --tool 'platformio/toolchain-gccmingw32'
    if ($LASTEXITCODE -ne 0) { throw 'MinGW toolchain installation failed.' }
    $env:Path = "$env:USERPROFILE\.platformio\packages\toolchain-gccmingw32\bin;$env:Path"
    & $Pio test -d (Join-Path $RepoRoot 'firmware') -e native
    if ($LASTEXITCODE -ne 0) { throw 'Native firmware tests failed.' }
    & $Pio run -d (Join-Path $RepoRoot 'firmware') -e nanoatmega328
    if ($LASTEXITCODE -ne 0) { throw 'Nano firmware build failed.' }
}
