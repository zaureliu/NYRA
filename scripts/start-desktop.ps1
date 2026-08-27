[CmdletBinding()]
param([switch]$Dev)

if ($Dev) {
    & (Join-Path $PSScriptRoot 'bootstrap.ps1') -Mode Development
} else {
    & (Join-Path $PSScriptRoot 'bootstrap.ps1') -Mode Release
}
exit $LASTEXITCODE
