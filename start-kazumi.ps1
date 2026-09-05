[CmdletBinding()]
param([switch]$SourceBackend)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path $PSScriptRoot).Path
# Prefer the verified packaged product. Preserve the source launcher as an
# explicit fallback, including when endpoint protection removed the sidecar.
# Never weaken Defender or silently spawn a second backend on an occupied port.
$sidecar = Join-Path $repoRoot 'packaging\dist\kazumi-backend\kazumi-backend.exe'
if (-not $SourceBackend -and (Test-Path -LiteralPath $sidecar -PathType Leaf)) {
    & (Join-Path $repoRoot 'scripts\bootstrap.ps1') -Mode Release
} else {
    & (Join-Path $repoRoot 'scripts\start-kazumi.ps1')
}
exit $LASTEXITCODE
