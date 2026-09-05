[CmdletBinding()]
param()

# One-release compatibility for legacy deployment variables. New names win.
foreach ($legacyVariable in Get-ChildItem Env:NYRA_*) {
    $newVariable = 'KAZUMI_' + $legacyVariable.Name.Substring(5)
    if (-not [Environment]::GetEnvironmentVariable($newVariable, 'Process')) {
        [Environment]::SetEnvironmentVariable($newVariable, $legacyVariable.Value, 'Process')
    }
}

function Get-KazumiRuntimeRoot {
    $configured = [Environment]::GetEnvironmentVariable('KAZUMI_DATA_HOME')
    if ($configured) { return [IO.Path]::GetFullPath($configured) }
    if ($env:LOCALAPPDATA) { return [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Kazumi')) }
    return [IO.Path]::GetFullPath((Join-Path $env:USERPROFILE 'AppData\Local\Kazumi'))
}

function Find-KazumiPythonExecutable {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$RepoRoot)

    $root = [IO.Path]::GetFullPath($RepoRoot)
    foreach ($candidate in @(
        (Join-Path $root '.venv\Scripts\python.exe'),
        (Join-Path $root 'backend\.venv\Scripts\python.exe')
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

function Get-KazumiRuntimePaths {
    $root = Get-KazumiRuntimeRoot
    return [pscustomobject]@{
        Root = $root
        Data = Join-Path $root 'data'
        Logs = Join-Path $root 'logs'
        Temp = Join-Path $root 'tmp'
        Reports = Join-Path $root 'reports'
        ProcessState = Join-Path $root '.kazumi-processes.json'
        BootstrapState = Join-Path $root 'bootstrap'
    }
}

function Initialize-KazumiRuntimePaths {
    $legacyRoot = Join-Path $env:LOCALAPPDATA 'NYRA'
    if (-not $env:KAZUMI_DATA_HOME -and (Test-Path -LiteralPath $legacyRoot -PathType Container)) {
        $migrationRepo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
        $migrationPython = Find-KazumiPythonExecutable -RepoRoot $migrationRepo
        if (-not $migrationPython) { throw 'Execute a migracao do runtime antes de iniciar Kazumi.' }
        & $migrationPython (Join-Path $migrationRepo 'scripts\migrate-user-data.py')
        if ($LASTEXITCODE -ne 0) { throw 'Migracao de dados pendente; startup cancelado para preservar o estado existente.' }
    }
    $paths = Get-KazumiRuntimePaths
    foreach ($directory in @($paths.Root, $paths.Data, $paths.Logs, $paths.Temp, $paths.Reports, $paths.BootstrapState)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    return $paths
}

function Get-KazumiFilesFingerprint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string[]]$InputPaths
    )

    $root = [IO.Path]::GetFullPath($RepoRoot).TrimEnd('\')
    $records = New-Object System.Collections.Generic.List[string]
    $blocked = @('.git', '.venv', 'venv', 'node_modules', 'target', 'dist', 'build', '__pycache__', '.pytest_cache', '.tmp', '.test-temp')
    foreach ($input in $InputPaths) {
        $resolvedInput = Join-Path $root $input
        if (-not (Test-Path -LiteralPath $resolvedInput)) { continue }
        $item = Get-Item -LiteralPath $resolvedInput
        $files = if ($item.PSIsContainer) {
            Get-ChildItem -LiteralPath $resolvedInput -Recurse -File | Where-Object {
                $relative = $_.FullName.Substring($root.Length).TrimStart('\')
                -not (@($relative -split '[\\/]') | Where-Object { $_.ToLowerInvariant() -in $blocked })
            }
        } else { @($item) }
        foreach ($file in @($files | Sort-Object FullName)) {
            $relative = $file.FullName.Substring($root.Length).TrimStart('\').Replace('\', '/')
            $digest = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            $records.Add($relative + ':' + $digest)
        }
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($records -join "`n"))
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Get-KazumiBackendSourceFingerprint {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$RepoRoot)
    return Get-KazumiFilesFingerprint -RepoRoot $RepoRoot -InputPaths @(
        'backend\app', 'backend\run_backend.py', 'backend\requirements.txt',
        'backend\pyproject.toml', 'config', 'identity',
        'packaging\kazumi-backend.spec', 'packaging\build-backend.ps1'
    )
}

function Get-KazumiReleaseSourceFingerprint {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$RepoRoot)
    return Get-KazumiFilesFingerprint -RepoRoot $RepoRoot -InputPaths @(
        'frontend\src', 'frontend\public', 'frontend\index.html',
        'frontend\package.json', 'frontend\package-lock.json',
        'frontend\tsconfig.json', 'frontend\vite.config.ts',
        'desktop\src-tauri\src', 'desktop\src-tauri\Cargo.toml',
        'desktop\src-tauri\Cargo.lock', 'desktop\src-tauri\tauri.conf.json',
        'desktop\src-tauri\build.rs', 'desktop\package.json',
        'desktop\package-lock.json', 'packaging\dist\.kazumi-backend-build.json'
    )
}

function Test-KazumiOwnedProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )
    $name = ([string]$Process.Name).ToLowerInvariant()
    if ($name -notmatch '^(node|cargo|rustc|python|pythonw|kazumi-desktop|cmd|powershell)\.exe$') { return $false }
    $root = [IO.Path]::GetFullPath($RepoRoot).TrimEnd('\')
    $prefix = $root + '\'
    $executablePath = [string]$Process.ExecutablePath
    $commandLine = [string]$Process.CommandLine
    if ($executablePath -and ($executablePath.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or $executablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase))) {
        return $true
    }
    return [bool]($commandLine -and (
        $commandLine.IndexOf($prefix, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
        $commandLine.IndexOf(('"' + $root + '"'), [StringComparison]::OrdinalIgnoreCase) -ge 0
    ))
}

function Get-KazumiOwnedProcesses {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [int[]]$ExcludeProcessId = @()
    )
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessId -notin $ExcludeProcessId -and (Test-KazumiOwnedProcess -Process $_ -RepoRoot $RepoRoot)
    })
}

function Stop-KazumiOwnedProcesses {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [int[]]$ExcludeProcessId = @()
    )
    $targets = @(Get-KazumiOwnedProcesses -RepoRoot $RepoRoot -ExcludeProcessId $ExcludeProcessId)
    $targetIds = @($targets | ForEach-Object { [int]$_.ProcessId })
    $roots = @($targets | Where-Object { [int]$_.ParentProcessId -notin $targetIds } | Sort-Object {
        if ($_.Name -eq 'kazumi-desktop.exe') { 0 } elseif ($_.Name -eq 'node.exe') { 1 } else { 2 }
    })
    $stopped = 0
    foreach ($target in $roots) {
        if (-not (Get-Process -Id $target.ProcessId -ErrorAction SilentlyContinue)) { continue }
        $verified = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $target.ProcessId) -ErrorAction SilentlyContinue
        if (-not $verified -or -not (Test-KazumiOwnedProcess -Process $verified -RepoRoot $RepoRoot)) { continue }
        $startInfo = New-Object Diagnostics.ProcessStartInfo
        $startInfo.FileName = (Join-Path $env:SystemRoot 'System32\taskkill.exe')
        $startInfo.Arguments = '/PID ' + $target.ProcessId + ' /T /F'
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $killer = New-Object Diagnostics.Process
        $killer.StartInfo = $startInfo
        if ($killer.Start()) {
            $null = $killer.StandardOutput.ReadToEndAsync()
            $null = $killer.StandardError.ReadToEndAsync()
            if (-not $killer.WaitForExit(10000)) { $killer.Kill() }
            $killer.Dispose()
        }
        # O alvo pode ter encerrado entre a segunda verificacao e taskkill;
        # isso ja satisfaz o estado desejado e nao transforma stop em falha.
        $stopped++
    }
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        if (-not @(Get-KazumiOwnedProcesses -RepoRoot $RepoRoot -ExcludeProcessId $ExcludeProcessId)) { break }
        Start-Sleep -Milliseconds 250
    }
    return $stopped
}
