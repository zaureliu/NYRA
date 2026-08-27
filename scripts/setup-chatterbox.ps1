[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$basePython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$venv = Join-Path $repoRoot '.venv-chatterbox'
if (-not (Test-Path -LiteralPath $basePython)) { throw 'Execute scripts/setup.ps1 antes.' }
if (-not (Test-Path -LiteralPath $venv)) { & $basePython -m venv $venv }
$python = Join-Path $venv 'Scripts\python.exe'
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $repoRoot 'backend\requirements-chatterbox.txt')
& $python -c "import torch; from chatterbox.mtl_tts import ChatterboxMultilingualTTS; print('CHATTERBOX_IMPORT=OK'); print('TORCH=' + torch.__version__); print('CUDA=' + str(torch.cuda.is_available()))"
Write-Host 'Chatterbox instalado em venv isolada.' -ForegroundColor Cyan
