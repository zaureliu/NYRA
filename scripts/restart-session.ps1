[CmdletBinding()]
param(
    [int]$Port = 8000,
    [ValidateRange(15, 300)][int]$TimeoutSeconds = 120
)

# Closure Parte 13: restart limpo. Espera a sessao antiga liberar a porta e
# chama o launcher oficial, que cria nova runtime_session_id.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'runtime-paths.ps1')
$runtimePaths = Initialize-NyraRuntimePaths
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$portFree = $false
while ((Get-Date) -lt $deadline) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $listener) { $portFree = $true; break }
    Start-Sleep -Milliseconds 800
}
if (-not $portFree) {
    Add-Content -LiteralPath (Join-Path $runtimePaths.Logs 'restart-session.log') -Value (
        '{0} restart-aborted port={1} reason=PORT_STILL_BUSY' -f (Get-Date -Format o), $Port) -Encoding UTF8
    exit 1
}
& (Join-Path $PSScriptRoot 'start-nyra.ps1')
