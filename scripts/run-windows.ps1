[CmdletBinding()]
param(
    [switch]$Demo,
    [switch]$NoBrowser,
    [switch]$Kiosk,
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [string]$DataDir,
    [string]$SerialPort
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Run scripts/setup-windows.ps1 first.' }
$Arguments = @('-m', 'kegpulse')
if ($PSBoundParameters.ContainsKey('Port')) { $Arguments += @('--port', $Port) }
if ($Demo) { $Arguments += '--demo' }
if ($NoBrowser) { $Arguments += '--no-browser' }
if ($Kiosk) { $Arguments += '--kiosk' }
if ($DataDir) { $Arguments += @('--data-dir', $DataDir) }
if ($SerialPort) { $Arguments += @('--serial-port', $SerialPort) }
& $Python @Arguments
exit $LASTEXITCODE
