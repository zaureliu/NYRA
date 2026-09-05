[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $repoRoot 'scripts\runtime-paths.ps1')
$runtimePaths = Initialize-KazumiRuntimePaths
$outputRoot = Join-Path $PSScriptRoot 'dist'
$output = Join-Path $outputRoot 'kazumi-backend'
$marker = Join-Path $outputRoot '.kazumi-backend-build.json'
$fingerprint = Get-KazumiBackendSourceFingerprint -RepoRoot $repoRoot

$python = Find-KazumiPythonExecutable -RepoRoot $repoRoot
if (-not $python) { throw 'Ambiente Python ausente. Execute scripts\setup.ps1.' }

$env:KAZUMI_DATA_HOME = $runtimePaths.Root
$buildId = [Guid]::NewGuid().ToString('N')
$stageRoot = Join-Path $runtimePaths.Temp ('pyinstaller-' + $buildId)
$stageDist = Join-Path $stageRoot 'dist'
$stageWork = Join-Path $stageRoot 'work'
New-Item -ItemType Directory -Path $stageDist,$stageWork,$outputRoot -Force | Out-Null

# Cache and work directories belong exclusively to this generated build stage.
# --clean must not remove a shared developer/PyInstaller cache elsewhere.
$previousPyInstallerConfig = $env:PYINSTALLER_CONFIG_DIR
try {
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $stageRoot 'cache'
    & $python -m PyInstaller --clean --noconfirm --distpath $stageDist --workpath $stageWork (Join-Path $PSScriptRoot 'kazumi-backend.spec')
    $buildExitCode = $LASTEXITCODE
} finally {
    $env:PYINSTALLER_CONFIG_DIR = $previousPyInstallerConfig
}
if ($buildExitCode -ne 0) { exit $buildExitCode }

$stagedOutput = Join-Path $stageDist 'kazumi-backend'
$stagedExecutable = Join-Path $stagedOutput 'kazumi-backend.exe'
if (-not (Test-Path -LiteralPath $stagedExecutable -PathType Leaf)) {
    throw 'PyInstaller nao produziu kazumi-backend.exe.'
}

if (Test-Path -LiteralPath $output) {
    $backup = Join-Path $runtimePaths.Temp ('kazumi-backend-previous-' + $buildId)
    Move-Item -LiteralPath $output -Destination $backup
}
Move-Item -LiteralPath $stagedOutput -Destination $output
$hash = (Get-FileHash -LiteralPath (Join-Path $output 'kazumi-backend.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
[ordered]@{
    schema = 1
    source_fingerprint = $fingerprint
    executable_sha256 = $hash
    built_at = (Get-Date).ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8
Write-Host ('Sidecar pronto: ' + (Join-Path $output 'kazumi-backend.exe')) -ForegroundColor Green
Write-Host ('SHA-256: ' + $hash) -ForegroundColor Green
