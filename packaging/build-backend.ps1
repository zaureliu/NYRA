[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $repoRoot 'scripts\runtime-paths.ps1')
$runtimePaths = Initialize-NyraRuntimePaths
$outputRoot = Join-Path $PSScriptRoot 'dist'
$output = Join-Path $outputRoot 'nyra-backend'
$marker = Join-Path $outputRoot '.nyra-backend-build.json'
$fingerprint = Get-NyraBackendSourceFingerprint -RepoRoot $repoRoot

$python = Find-NyraPythonExecutable -RepoRoot $repoRoot
if (-not $python) { throw 'Ambiente Python ausente. Execute scripts\setup.ps1.' }

$env:NYRA_DATA_HOME = $runtimePaths.Root
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
    & $python -m PyInstaller --clean --noconfirm --distpath $stageDist --workpath $stageWork (Join-Path $PSScriptRoot 'nyra-backend.spec')
    $buildExitCode = $LASTEXITCODE
} finally {
    $env:PYINSTALLER_CONFIG_DIR = $previousPyInstallerConfig
}
if ($buildExitCode -ne 0) { exit $buildExitCode }

$stagedOutput = Join-Path $stageDist 'nyra-backend'
$stagedExecutable = Join-Path $stagedOutput 'nyra-backend.exe'
if (-not (Test-Path -LiteralPath $stagedExecutable -PathType Leaf)) {
    throw 'PyInstaller nao produziu nyra-backend.exe.'
}

if (Test-Path -LiteralPath $output) {
    $backup = Join-Path $runtimePaths.Temp ('nyra-backend-previous-' + $buildId)
    Move-Item -LiteralPath $output -Destination $backup
}
Move-Item -LiteralPath $stagedOutput -Destination $output
$hash = (Get-FileHash -LiteralPath (Join-Path $output 'nyra-backend.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
[ordered]@{
    schema = 1
    source_fingerprint = $fingerprint
    executable_sha256 = $hash
    built_at = (Get-Date).ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8
Write-Host ('Sidecar pronto: ' + (Join-Path $output 'nyra-backend.exe')) -ForegroundColor Green
Write-Host ('SHA-256: ' + $hash) -ForegroundColor Green
