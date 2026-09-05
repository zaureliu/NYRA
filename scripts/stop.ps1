[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'runtime-paths.ps1')
$runtimePaths = Initialize-KazumiRuntimePaths
$stopped = Stop-KazumiOwnedProcesses -RepoRoot $repoRoot -ExcludeProcessId $PID
if (Test-Path -LiteralPath $runtimePaths.ProcessState) {
    Remove-Item -LiteralPath $runtimePaths.ProcessState -Force
}
Write-Host ('Arvores de processos KAZUMI encerradas: ' + $stopped) -ForegroundColor Cyan
