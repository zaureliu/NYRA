[CmdletBinding()]
param(
    [switch]$SkipDesktop,
    [ValidateRange(5, 180)][int]$OllamaTimeoutSeconds = 45,
    [ValidateRange(10, 300)][int]$BackendTimeoutSeconds = 90,
    [ValidateRange(30, 900)][int]$WarmupTimeoutSeconds = 300
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'runtime-paths.ps1')
$runtimePaths = Initialize-KazumiRuntimePaths
$logDir = $runtimePaths.Logs
$launcherLog = Join-Path $logDir 'launcher.log'
$runtimeState = $runtimePaths.ProcessState
$releaseExecutable = Join-Path $repoRoot 'desktop\src-tauri\target\release\kazumi-desktop.exe'
$pythonExecutable = Find-KazumiPythonExecutable -RepoRoot $repoRoot
$startupTimer = [Diagnostics.Stopwatch]::StartNew()
$launcherMutex = $null
$hasMutex = $false
$pullProcess = $null
$backendProcess = $null
$desktopProcess = $null

New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Write-LauncherLog {
    param(
        [ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level,
        [string]$Message
    )
    $line = '{0} [{1}] session={2} elapsed_ms={3} {4}' -f (
        Get-Date -Format 'yyyy-MM-ddTHH:mm:ss.fffK'
    ), $Level, $PID, $startupTimer.ElapsedMilliseconds, $Message
    Add-Content -LiteralPath $launcherLog -Value $line -Encoding UTF8
    Write-Verbose $line
}

function Show-LauncherError {
    param([string]$Message)
    try {
        $shell = New-Object -ComObject WScript.Shell
        # A finite timeout prevents an unattended error dialog from retaining
        # the single-start mutex forever.
        $null = $shell.Popup("$Message`n`nConsulte: $launcherLog", 15, 'KAZUMI', 16)
    } catch {
        Write-LauncherLog -Level WARN -Message ('error_dialog_unavailable type=' + $_.Exception.GetType().Name)
    }
}

function Get-DotEnvValue {
    param([string]$Name)
    $envPath = Join-Path $repoRoot '.env'
    if (-not (Test-Path -LiteralPath $envPath)) { return $null }
    $escaped = [Regex]::Escape($Name)
    $line = Get-Content -LiteralPath $envPath | Where-Object { $_ -match "^\s*$escaped\s*=" } | Select-Object -Last 1
    if (-not $line) { return $null }
    $value = ($line -split '=', 2)[1].Trim()
    return $value.Trim('"').Trim("'")
}

function Get-ConfiguredValue {
    param([string]$EnvironmentName, [string]$DefaultValue)
    $processValue = [Environment]::GetEnvironmentVariable($EnvironmentName)
    if ($processValue) { return $processValue }
    $fileValue = Get-DotEnvValue -Name $EnvironmentName
    if ($fileValue) { return $fileValue }
    return $DefaultValue
}

function Get-OfficialModel {
    $configured = Get-ConfiguredValue -EnvironmentName 'KAZUMI_LLM_MODEL' -DefaultValue 'qwen3:8b'
    $settingsPath = Join-Path $runtimePaths.Data 'brain-settings.json'
    if (Test-Path -LiteralPath $settingsPath) {
        try {
            $saved = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
            if ($saved.official_model) { return [string]$saved.official_model }
        } catch {
            Write-LauncherLog -Level WARN -Message 'brain_settings_invalid using_configured_model=true'
        }
    }
    return $configured
}

function Get-OllamaTags {
    param([string]$BaseUrl)
    try {
        return Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/') + '/api/tags') -TimeoutSec 3
    } catch {
        return $null
    }
}

function Find-OllamaExecutable {
    $command = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { return $command.Source }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
        (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Wait-Ollama {
    param([string]$BaseUrl, [int]$TimeoutSeconds)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $tags = Get-OllamaTags -BaseUrl $BaseUrl
        if ($tags) { return $tags }
        Start-Sleep -Milliseconds 350
    } while ((Get-Date) -lt $deadline)
    return $null
}

function Get-KazumiHealth {
    param([string]$Uri)
    try {
        $health = Invoke-RestMethod -Uri $Uri -TimeoutSec 4
        if ($health.character -eq 'KAZUMI') { return $health }
    } catch {
        return $null
    }
    return $null
}

function Wait-KazumiHealth {
    param([string]$Uri, [int]$TimeoutSeconds)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $health = Get-KazumiHealth -Uri $Uri
        if ($health) { return $health }
        if ($backendProcess -and $backendProcess.HasExited) { return $null }
        Start-Sleep -Milliseconds 400
    } while ((Get-Date) -lt $deadline)
    return $null
}

function Save-RuntimeState {
    $state = [ordered]@{
        mode = 'release'
        launcher = $PID
        backend = if ($backendProcess) { $backendProcess.Id } else { $null }
        desktop = if ($desktopProcess) { $desktopProcess.Id } else { $null }
        started = (Get-Date).ToString('o')
        root = $repoRoot
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $runtimeState -Encoding UTF8
}

try {
    $createdNew = $false
    $launcherMutex = New-Object System.Threading.Mutex($false, 'Local\KAZUMI.Launcher.Startup', [ref]$createdNew)
    try { $hasMutex = $launcherMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $hasMutex = $true }

    if (-not $hasMutex) {
        Write-LauncherLog -Level INFO -Message 'launcher_already_running action=focus_when_ready'
        if ((-not $SkipDesktop) -and (Test-Path -LiteralPath $releaseExecutable)) {
            $runningDesktop = Get-Process -Name 'kazumi-desktop' -ErrorAction SilentlyContinue | Where-Object {
                try { $_.Path -and $_.Path.Equals($releaseExecutable, [StringComparison]::OrdinalIgnoreCase) } catch { $false }
            } | Select-Object -First 1
            if (-not $runningDesktop) {
                Start-Process -FilePath $releaseExecutable -WorkingDirectory $repoRoot | Out-Null
            }
        }
        return
    }

    Write-LauncherLog -Level INFO -Message 'startup_begin mode=release vite=false'

    $ollamaUrl = Get-ConfiguredValue -EnvironmentName 'KAZUMI_OLLAMA_URL' -DefaultValue 'http://127.0.0.1:11434'
    $officialModel = Get-OfficialModel
    $portValue = Get-ConfiguredValue -EnvironmentName 'KAZUMI_BACKEND_PORT' -DefaultValue '8000'
    if ($portValue -notmatch '^\d{2,5}$') { throw 'KAZUMI_BACKEND_PORT invalida.' }
    $backendHealthUri = "http://127.0.0.1:$portValue/health"

    $ollamaStartedMs = $startupTimer.ElapsedMilliseconds
    $tags = Get-OllamaTags -BaseUrl $ollamaUrl
    $ollamaExecutable = Find-OllamaExecutable
    if ($tags) {
        Write-LauncherLog -Level INFO -Message ("ollama_online reused=true ready_ms=" + ($startupTimer.ElapsedMilliseconds - $ollamaStartedMs))
    } elseif ($ollamaExecutable) {
        Write-LauncherLog -Level INFO -Message ("ollama_start executable=" + [IO.Path]::GetFileName($ollamaExecutable))
        Start-Process -FilePath $ollamaExecutable -ArgumentList @('serve') -WorkingDirectory (Split-Path $ollamaExecutable) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'ollama.stdout.log') -RedirectStandardError (Join-Path $logDir 'ollama.stderr.log') | Out-Null
        $tags = Wait-Ollama -BaseUrl $ollamaUrl -TimeoutSeconds $OllamaTimeoutSeconds
        if ($tags) {
            Write-LauncherLog -Level INFO -Message ("ollama_online reused=false ready_ms=" + ($startupTimer.ElapsedMilliseconds - $ollamaStartedMs))
        }
    } else {
        Write-LauncherLog -Level ERROR -Message 'ollama_executable_not_found'
    }

    if ($tags) {
        $installedModels = @($tags.models | ForEach-Object { [string]$_.name })
        if ($officialModel -in $installedModels) {
            Write-LauncherLog -Level INFO -Message ("ollama_model_found model=$officialModel")
            Write-LauncherLog -Level INFO -Message ("ollama_preload_owner backend=true model=$officialModel")
        } elseif ($ollamaExecutable) {
            Write-LauncherLog -Level WARN -Message ("ollama_model_missing model=$officialModel action=pull")
            $pullProcess = Start-Process -FilePath $ollamaExecutable -ArgumentList @('pull', $officialModel) -WorkingDirectory (Split-Path $ollamaExecutable) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'ollama-pull.stdout.log') -RedirectStandardError (Join-Path $logDir 'ollama-pull.stderr.log') -PassThru
        }
    } else {
        Write-LauncherLog -Level ERROR -Message ("ollama_unavailable timeout_s=$OllamaTimeoutSeconds")
    }

    $backendStartedMs = $startupTimer.ElapsedMilliseconds
    $health = Get-KazumiHealth -Uri $backendHealthUri
    if ($health) {
        Write-LauncherLog -Level INFO -Message ("backend_online reused=true status=" + $health.status + " ready_ms=" + ($startupTimer.ElapsedMilliseconds - $backendStartedMs))
    } elseif ($SkipDesktop) {
        # Backend-only diagnostic mode has no desktop process to own the child,
        # so it deliberately keeps the source/uvicorn launch path.
        if (-not $pythonExecutable) { throw 'Ambiente Python ausente em .venv e backend\.venv.' }
        $cleanPath = $env:Path
        Remove-Item Env:PATH -ErrorAction SilentlyContinue
        Remove-Item Env:Path -ErrorAction SilentlyContinue
        $env:Path = $cleanPath
        $pythonLayout = if ($pythonExecutable.IndexOf('\backend\.venv\', [StringComparison]::OrdinalIgnoreCase) -ge 0) { 'backend/.venv' } else { '.venv' }
        Write-LauncherLog -Level INFO -Message ("backend_start port=$portValue python=$pythonLayout")
        $backendProcess = Start-Process -FilePath $pythonExecutable -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', $portValue) -WorkingDirectory (Join-Path $repoRoot 'backend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'backend.stdout.log') -RedirectStandardError (Join-Path $logDir 'backend.stderr.log') -PassThru
        $health = Wait-KazumiHealth -Uri $backendHealthUri -TimeoutSeconds $BackendTimeoutSeconds
        if ($health) {
            Write-LauncherLog -Level INFO -Message ("backend_online reused=false status=" + $health.status + " ready_ms=" + ($startupTimer.ElapsedMilliseconds - $backendStartedMs))
        } else {
            Write-LauncherLog -Level ERROR -Message ("backend_unavailable timeout_s=$BackendTimeoutSeconds")
        }
    } else {
        # In the official release the Tauri backend manager must be the sole
        # owner. This gives Tray/UI Exit the token and process handle required
        # to stop the frozen sidecar and release port 8000 deterministically.
        Write-LauncherLog -Level INFO -Message 'backend_start owner=desktop sidecar=frozen'
    }

    if (-not $SkipDesktop) {
        if (-not (Test-Path -LiteralPath $releaseExecutable)) { throw 'Executavel release ausente. Execute build-kazumi.ps1.' }
        $existingDesktop = Get-Process -Name 'kazumi-desktop' -ErrorAction SilentlyContinue | Where-Object {
            try { $_.Path -and $_.Path.Equals($releaseExecutable, [StringComparison]::OrdinalIgnoreCase) } catch { $false }
        } | Select-Object -First 1
        if ($existingDesktop) {
            $desktopProcess = $existingDesktop
            Write-LauncherLog -Level INFO -Message ("desktop_reused pid=" + $desktopProcess.Id + " action=preserve_single_instance")
        } else {
            $desktopProcess = Start-Process -FilePath $releaseExecutable -WorkingDirectory $repoRoot -PassThru
            Start-Sleep -Milliseconds 900
            if ($desktopProcess.HasExited) { throw 'KAZUMI Desktop encerrou durante a inicializacao.' }
            Write-LauncherLog -Level INFO -Message ("desktop_started pid=" + $desktopProcess.Id + " ready_ms=" + $startupTimer.ElapsedMilliseconds)
        }

        if (-not $health) {
            $health = Wait-KazumiHealth -Uri $backendHealthUri -TimeoutSeconds $BackendTimeoutSeconds
            if ($health) {
                Write-LauncherLog -Level INFO -Message ("backend_online reused=false owner=desktop status=" + $health.status + " ready_ms=" + ($startupTimer.ElapsedMilliseconds - $backendStartedMs))
            } else {
                Write-LauncherLog -Level ERROR -Message ("backend_unavailable owner=desktop timeout_s=$BackendTimeoutSeconds")
            }
        }
    }

    Save-RuntimeState

    if (-not $health) {
        Show-LauncherError -Message 'KAZUMI abriu, mas o backend local nao ficou disponivel.'
    } elseif (-not $tags) {
        Write-LauncherLog -Level WARN -Message 'startup_degraded reason=ollama_unavailable desktop_open=true'
    }

    if ($pullProcess) {
        $pullDeadline = (Get-Date).AddSeconds($WarmupTimeoutSeconds)
        while (-not $pullProcess.HasExited -and (Get-Date) -lt $pullDeadline) { Start-Sleep -Milliseconds 500 }
        if ($pullProcess.HasExited -and $pullProcess.ExitCode -eq 0) {
            Write-LauncherLog -Level INFO -Message ("ollama_model_pulled model=$officialModel")
        } else {
            Write-LauncherLog -Level ERROR -Message ("ollama_model_pull_incomplete model=$officialModel timeout_s=$WarmupTimeoutSeconds")
        }
    }

    Write-LauncherLog -Level INFO -Message ("startup_complete desktop_open=" + (-not $SkipDesktop) + " backend_ready=" + [bool]$health + " ollama_ready=" + [bool]$tags + " total_ms=" + $startupTimer.ElapsedMilliseconds)
} catch {
    Write-LauncherLog -Level ERROR -Message ("startup_failed type=" + $_.Exception.GetType().Name + " message=" + ($_.Exception.Message -replace '[\r\n]+', ' '))
    if ($hasMutex -and $launcherMutex) {
        try { $launcherMutex.ReleaseMutex() } catch {}
        $hasMutex = $false
    }
    Show-LauncherError -Message ('KAZUMI nao conseguiu concluir a inicializacao: ' + $_.Exception.Message)
    exit 1
} finally {
    if ($hasMutex -and $launcherMutex) { try { $launcherMutex.ReleaseMutex() } catch {} }
    if ($launcherMutex) { $launcherMutex.Dispose() }
}
