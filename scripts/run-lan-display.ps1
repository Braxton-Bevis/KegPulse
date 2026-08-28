# Serve KegPulse on the LAN with a read-only wall display.
#
# Pouring, money, settings, calibration, and backups still require the
# administrator PIN. Remote viewers can only watch status, people, and history.
[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [string[]]$ExtraHost = @(),
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Run scripts/setup-windows.ps1 first.' }

# Every IPv4 address this machine answers on, plus its hostname.
$addresses = @(
    Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254*' } |
        Select-Object -ExpandProperty IPAddress
)
$names = @($env:COMPUTERNAME.ToLower(), "$($env:COMPUTERNAME.ToLower()).local") + $addresses + $ExtraHost |
    Where-Object { $_ } | Select-Object -Unique

$arguments = @('-m', 'kegpulse', '--lan', '--lan-display', '--port', $Port)
if ($NoBrowser) { $arguments += '--no-browser' }
foreach ($name in $names) {
    $arguments += @('--allowed-host', $name)
    $arguments += @('--allowed-origin', "http://${name}:$Port")
}

Write-Host 'KegPulse LAN display' -ForegroundColor Cyan
foreach ($address in $addresses) {
    Write-Host "  Wall display: http://${address}:$Port/#/display"
}
Write-Host '  Mutations still require the administrator PIN.' -ForegroundColor Yellow

Set-Location -LiteralPath $RepoRoot
& $Python @arguments
exit $LASTEXITCODE
