[CmdletBinding()]
param()

function Get-NyraRuntimeRoot {
    $configured = [Environment]::GetEnvironmentVariable('NYRA_DATA_HOME')
    if ($configured) { return [IO.Path]::GetFullPath($configured) }
    if ($env:LOCALAPPDATA) { return [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'NYRA')) }
    return [IO.Path]::GetFullPath((Join-Path $env:USERPROFILE 'AppData\Local\NYRA'))
}

function Find-NyraPythonExecutable {
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

function Get-NyraRuntimePaths {
    $root = Get-NyraRuntimeRoot
    return [pscustomobject]@{
        Root = $root
        Data = Join-Path $root 'data'
        Logs = Join-Path $root 'logs'
        Temp = Join-Path $root 'tmp'
        Reports = Join-Path $root 'reports'
        ProcessState = Join-Path $root '.nyra-processes.json'
        BootstrapState = Join-Path $root 'bootstrap'
    }
}

function Initialize-NyraRuntimePaths {
    $paths = Get-NyraRuntimePaths
    foreach ($directory in @($paths.Root, $paths.Data, $paths.Logs, $paths.Temp, $paths.Reports, $paths.BootstrapState)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    return $paths
}

function Get-NyraFilesFingerprint {
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

function Get-NyraBackendSourceFingerprint {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$RepoRoot)
    return Get-NyraFilesFingerprint -RepoRoot $RepoRoot -InputPaths @(
        'backend\app', 'backend\run_backend.py', 'backend\requirements.txt',
        'backend\pyproject.toml', 'config', 'identity',
        'packaging\nyra-backend.spec', 'packaging\build-backend.ps1'
    )
}

function Get-NyraReleaseSourceFingerprint {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$RepoRoot)
    return Get-NyraFilesFingerprint -RepoRoot $RepoRoot -InputPaths @(
        'frontend\src', 'frontend\public', 'frontend\index.html',
        'frontend\package.json', 'frontend\package-lock.json',
        'frontend\tsconfig.json', 'frontend\vite.config.ts',
        'desktop\src-tauri\src', 'desktop\src-tauri\Cargo.toml',
        'desktop\src-tauri\Cargo.lock', 'desktop\src-tauri\tauri.conf.json',
        'desktop\src-tauri\build.rs', 'desktop\package.json',
        'desktop\package-lock.json', 'packaging\dist\.nyra-backend-build.json'
    )
}

function Test-NyraOwnedProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )
    $name = ([string]$Process.Name).ToLowerInvariant()
    if ($name -notmatch '^(node|cargo|rustc|python|pythonw|nyra-desktop|cmd|powershell)\.exe$') { return $false }
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

function Get-NyraOwnedProcesses {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [int[]]$ExcludeProcessId = @()
    )
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessId -notin $ExcludeProcessId -and (Test-NyraOwnedProcess -Process $_ -RepoRoot $RepoRoot)
    })
}

function Stop-NyraOwnedProcesses {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [int[]]$ExcludeProcessId = @()
    )
    $targets = @(Get-NyraOwnedProcesses -RepoRoot $RepoRoot -ExcludeProcessId $ExcludeProcessId)
    $targetIds = @($targets | ForEach-Object { [int]$_.ProcessId })
    $roots = @($targets | Where-Object { [int]$_.ParentProcessId -notin $targetIds } | Sort-Object {
        if ($_.Name -eq 'nyra-desktop.exe') { 0 } elseif ($_.Name -eq 'node.exe') { 1 } else { 2 }
    })
    $stopped = 0
    foreach ($target in $roots) {
        if (-not (Get-Process -Id $target.ProcessId -ErrorAction SilentlyContinue)) { continue }
        $verified = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $target.ProcessId) -ErrorAction SilentlyContinue
        if (-not $verified -or -not (Test-NyraOwnedProcess -Process $verified -RepoRoot $RepoRoot)) { continue }
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
        if (-not @(Get-NyraOwnedProcesses -RepoRoot $RepoRoot -ExcludeProcessId $ExcludeProcessId)) { break }
        Start-Sleep -Milliseconds 250
    }
    return $stopped
}
