[CmdletBinding()]
param([switch]$SkipModelPreload)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$venvPath = Join-Path $repoRoot '.venv'
$pythonCandidates = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
    (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
if (-not $pythonCandidates) { throw 'Python 3.11 não encontrado. Instale com: winget install Python.Python.3.11' }
$python = $pythonCandidates[0]
& $python --version
if (-not (Test-Path -LiteralPath $venvPath)) { & $python -m venv $venvPath }
$venvPython = Join-Path $venvPath 'Scripts\python.exe'
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $repoRoot 'backend\requirements-dev.txt')
Push-Location (Join-Path $repoRoot 'frontend')
try { & npm.cmd install } finally { Pop-Location }
$envPath = Join-Path $repoRoot '.env'
if (-not (Test-Path -LiteralPath $envPath)) { Copy-Item -LiteralPath (Join-Path $repoRoot '.env.example') -Destination $envPath }
& (Join-Path $PSScriptRoot 'download_tts_models.ps1')
if (-not $SkipModelPreload) {
    & $venvPython (Join-Path $repoRoot 'scripts\preload_stt.py')
}
Write-Host 'Setup concluído. Inicie com .\scripts\start.ps1' -ForegroundColor Cyan
Write-Host 'Chatterbox opcional: .\scripts\setup-chatterbox.ps1' -ForegroundColor DarkGray
