[CmdletBinding()]
param()
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'runtime-paths.ps1')
$statePath = (Get-NyraRuntimePaths).ProcessState
if (Test-Path -LiteralPath $statePath) {
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    foreach ($entry in @(@('backend',$state.backend),@('frontend',$state.frontend),@('tauri',$state.tauri),@('desktop',$state.desktop))) {
        if (-not $entry[1]) { continue }
        $candidate = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $entry[1]) -ErrorAction SilentlyContinue
        $running = [bool]($candidate -and (($candidate.ExecutablePath -and $candidate.ExecutablePath.StartsWith($repoRoot, [StringComparison]::OrdinalIgnoreCase)) -or ($candidate.CommandLine -and $candidate.CommandLine.IndexOf($repoRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0)))
        Write-Host ($entry[0] + ': ' + $(if ($running) {'RUNNING'} else {'STOPPED'}) + ' (PID ' + $entry[1] + ')')
    }
} else { Write-Host 'NYRA: STOPPED' }
$repoProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.IndexOf($repoRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0
})
if ($repoProcesses) { Write-Host ('processos relacionados ativos: ' + (($repoProcesses.ProcessId) -join ', ')) }
try {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 3
    Write-Host ('health: ' + ($health | ConvertTo-Json -Compress))
} catch { Write-Host 'health: indisponível' }
