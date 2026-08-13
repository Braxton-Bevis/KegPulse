[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$PyInstaller = Join-Path $RepoRoot '.venv\Scripts\pyinstaller.exe'
$Artifacts = Join-Path $RepoRoot 'artifacts'
$Dist = Join-Path $RepoRoot 'dist'
& $Python -c "import platform, struct, sys; machine = platform.machine().lower(); sys.exit(0 if struct.calcsize('P') == 8 and machine in {'amd64', 'x86_64'} else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Windows packages must be built with 64-bit x86_64/AMD64 CPython.' }
New-Item -ItemType Directory -Force -Path $Artifacts | Out-Null
& $PyInstaller --noconfirm --clean --distpath $Dist --workpath (Join-Path $RepoRoot 'build\pyinstaller') (Join-Path $RepoRoot 'packaging\KegPulse.spec')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
$Executable = Join-Path $Dist 'KegPulse\KegPulse.exe'
& $Python (Join-Path $RepoRoot 'scripts\smoke-package.py') --executable $Executable
if ($LASTEXITCODE -ne 0) { throw "Package smoke test failed with exit code $LASTEXITCODE" }
$Archive = Join-Path $Artifacts 'KegPulse-windows-x64.zip'
if (Test-Path -LiteralPath $Archive) { Remove-Item -LiteralPath $Archive }
Compress-Archive -LiteralPath (Join-Path $Dist 'KegPulse') -DestinationPath $Archive
$Hash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath (Join-Path $Artifacts 'SHA256SUMS.txt') -Value "$Hash  KegPulse-windows-x64.zip" -Encoding ascii
Write-Host "Created $Archive"
