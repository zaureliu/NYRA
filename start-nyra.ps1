[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path $PSScriptRoot).Path
& (Join-Path $repoRoot 'scripts\bootstrap.ps1') -Mode Release
exit $LASTEXITCODE
