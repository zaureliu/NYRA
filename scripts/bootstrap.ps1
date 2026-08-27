[CmdletBinding()]
param(
    [ValidateSet('Development', 'Build', 'Release')][string]$Mode = 'Development',
    [switch]$ForceBuild,
    [ValidateRange(30, 600)][int]$BackendTimeoutSeconds = 180,
    [ValidateRange(30, 600)][int]$FrontendTimeoutSeconds = 120,
    [ValidateRange(60, 1200)][int]$DesktopTimeoutSeconds = 600,
    [ValidateRange(120, 2400)][int]$BuildTimeoutSeconds = 1200
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'runtime-paths.ps1')
$runtimePaths = Initialize-NyraRuntimePaths
$logDir = $runtimePaths.Logs
$statePath = $runtimePaths.ProcessState
$bootstrapLog = Join-Path $logDir 'bootstrap.log'
$node = (Get-Command node.exe -ErrorAction Stop).Source
$npmCli = Join-Path (Split-Path $node) 'node_modules\npm\bin\npm-cli.js'
$npmCliArgument = '"' + $npmCli + '"'
$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
$script:DependencyInstalls = 0
$mutex = $null
$mutexOwned = $false

# Windows PowerShell 5 falha no Start-Process quando o host fornece PATH e
# Path simultaneamente. Normalize uma vez antes de criar qualquer filho.
$cleanPath = $env:Path
Remove-Item Env:PATH -ErrorAction SilentlyContinue
Remove-Item Env:Path -ErrorAction SilentlyContinue
$env:Path = $cleanPath
if (-not (Test-Path -LiteralPath $npmCli -PathType Leaf)) { throw 'npm-cli.js nao encontrado ao lado do Node.js.' }

function Write-BootstrapLog {
    param([ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level, [string]$Message)
    $line = '{0} [{1}] mode={2} pid={3} {4}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss.fffK'), $Level, $Mode, $PID, $Message
    Add-Content -LiteralPath $bootstrapLog -Value $line -Encoding UTF8
    Write-Host $Message -ForegroundColor $(if ($Level -eq 'ERROR') { 'Red' } elseif ($Level -eq 'WARN') { 'Yellow' } else { 'Cyan' })
}

function Read-TextTail {
    param([string]$Path, [int]$Lines = 30)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    return ((Get-Content -LiteralPath $Path -Tail $Lines -ErrorAction SilentlyContinue) -join "`n").Trim()
}

function Invoke-BoundedProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $id = [Guid]::NewGuid().ToString('N')
    $stdout = Join-Path $runtimePaths.Temp ($Label + '-' + $id + '.stdout.log')
    $stderr = Join-Path $runtimePaths.Temp ($Label + '-' + $id + '.stderr.log')
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = ($ArgumentList -join ' ')
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "$Label nao iniciou." }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
        throw "$Label excedeu o timeout de $TimeoutSeconds segundos."
    }
    $process.WaitForExit()
    $stdoutText = $stdoutTask.Result
    $stderrText = $stderrTask.Result
    Set-Content -LiteralPath $stdout -Value $stdoutText -Encoding UTF8
    Set-Content -LiteralPath $stderr -Value $stderrText -Encoding UTF8
    $result = [pscustomobject]@{
        ExitCode = $process.ExitCode
        Stdout = (($stdoutText -split '[\r\n]+' | Select-Object -Last 60) -join "`n").Trim()
        Stderr = (($stderrText -split '[\r\n]+' | Select-Object -Last 60) -join "`n").Trim()
    }
    $process.Dispose()
    return $result
}

function Start-NyraManagedProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$LogName
    )
    $stdinIdle = Join-Path $logDir 'stdin.idle'
    if (-not (Test-Path -LiteralPath $stdinIdle)) { New-Item -ItemType File -Path $stdinIdle -Force | Out-Null }
    return Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -RedirectStandardInput $stdinIdle -RedirectStandardOutput (Join-Path $logDir ($LogName + '.stdout.log')) -RedirectStandardError (Join-Path $logDir ($LogName + '.stderr.log')) -PassThru
}

function Test-DependencyProbe {
    param([ValidateSet('frontend', 'desktop')][string]$Component)
    if ($Component -eq 'frontend') {
        $entry = Join-Path $repoRoot 'frontend\node_modules\vite\bin\vite.js'
        if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { return $false }
        $probe = Invoke-BoundedProcess -FilePath $node -ArgumentList @($entry, '--version') -WorkingDirectory (Join-Path $repoRoot 'frontend') -TimeoutSeconds 30 -Label 'probe-vite'
    } else {
        $entry = Join-Path $repoRoot 'desktop\node_modules\@tauri-apps\cli\tauri.js'
        if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { return $false }
        $probe = Invoke-BoundedProcess -FilePath $node -ArgumentList @($entry, '--version') -WorkingDirectory (Join-Path $repoRoot 'desktop') -TimeoutSeconds 30 -Label 'probe-tauri'
    }
    return $probe.ExitCode -eq 0
}

function Test-NpmDependencies {
    param([ValidateSet('frontend', 'desktop')][string]$Component, [switch]$AllowAdopt)
    $componentRoot = Join-Path $repoRoot $Component
    $lockPath = Join-Path $componentRoot 'package-lock.json'
    $packagePath = Join-Path $componentRoot 'package.json'
    $modulesPath = Join-Path $componentRoot 'node_modules'
    $markerPath = Join-Path $modulesPath '.nyra-lock.sha256'
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf) -or -not (Test-Path -LiteralPath $packagePath -PathType Leaf) -or -not (Test-Path -LiteralPath $modulesPath -PathType Container)) { return $false }
    $lockHash = (Get-FileHash -LiteralPath $lockPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $dependencyFingerprint = Get-NyraFilesFingerprint -RepoRoot $repoRoot -InputPaths @(
        ($Component + '\package.json'), ($Component + '\package-lock.json')
    )
    $markerHash = if (Test-Path -LiteralPath $markerPath) { (Get-Content -LiteralPath $markerPath -Raw).Trim().ToLowerInvariant() } else { '' }
    if ($markerHash -eq $dependencyFingerprint) { return Test-DependencyProbe -Component $Component }
    # Migra marcadores da versao inicial (somente lock) sem reinstalar uma
    # arvore que npm ls e o probe nativo confirmam como saudavel.
    $legacyMarker = $markerHash -eq $lockHash
    if ($markerHash -and -not $legacyMarker) { return $false }
    if (-not $AllowAdopt) { return $false }
    $listing = Invoke-BoundedProcess -FilePath $node -ArgumentList @($npmCliArgument, 'ls', '--depth=0', '--no-audit', '--no-fund') -WorkingDirectory $componentRoot -TimeoutSeconds 60 -Label ('npm-ls-' + $Component)
    if ($listing.ExitCode -ne 0 -or -not (Test-DependencyProbe -Component $Component)) { return $false }
    Set-Content -LiteralPath $markerPath -Value $dependencyFingerprint -Encoding ASCII
    Write-BootstrapLog -Level INFO -Message ("dependencias $Component existentes validadas; npm_ci=false")
    return $true
}

function Ensure-NpmDependencies {
    param([ValidateSet('frontend', 'desktop')][string]$Component)
    if (Test-NpmDependencies -Component $Component -AllowAdopt) {
        Write-BootstrapLog -Level INFO -Message ("dependencias $Component saudaveis; npm_ci=false")
        return
    }
    Write-BootstrapLog -Level WARN -Message ("dependencias $Component ausentes ou inconsistentes; reparo necessario")
    $null = Stop-NyraOwnedProcesses -RepoRoot $repoRoot -ExcludeProcessId $PID
    $componentRoot = Join-Path $repoRoot $Component
    $install = Invoke-BoundedProcess -FilePath $node -ArgumentList @($npmCliArgument, 'ci', '--no-audit', '--no-fund') -WorkingDirectory $componentRoot -TimeoutSeconds 600 -Label ('npm-ci-' + $Component)
    if ($install.ExitCode -ne 0) {
        throw ("npm ci falhou em $Component. " + $install.Stderr)
    }
    $script:DependencyInstalls++
    $dependencyFingerprint = Get-NyraFilesFingerprint -RepoRoot $repoRoot -InputPaths @(
        ($Component + '\package.json'), ($Component + '\package-lock.json')
    )
    Set-Content -LiteralPath (Join-Path $componentRoot 'node_modules\.nyra-lock.sha256') -Value $dependencyFingerprint -Encoding ASCII
    if (-not (Test-NpmDependencies -Component $Component)) { throw "Dependencias de $Component continuaram inconsistentes apos npm ci." }
    Write-BootstrapLog -Level INFO -Message ("dependencias $Component reparadas; npm_ci=true")
}

function Test-BackendPackageCurrent {
    $executable = Join-Path $repoRoot 'packaging\dist\nyra-backend\nyra-backend.exe'
    $markerPath = Join-Path $repoRoot 'packaging\dist\.nyra-backend-build.json'
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf) -or -not (Test-Path -LiteralPath $markerPath -PathType Leaf)) { return $false }
    try {
        $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
        $source = Get-NyraBackendSourceFingerprint -RepoRoot $repoRoot
        $binary = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
        return $marker.source_fingerprint -eq $source -and $marker.executable_sha256 -eq $binary
    } catch { return $false }
}

function Test-NyraTtsModels {
    $expected = [ordered]@{
        'kokoro-v1.0.int8.onnx' = '6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb'
        'voices-v1.0.bin' = 'bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d'
    }
    $modelDir = Join-Path $runtimePaths.Data 'models'
    foreach ($entry in $expected.GetEnumerator()) {
        $path = Join-Path $modelDir $entry.Key
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) { return $false }
    }
    return $true
}

function Ensure-NyraTtsModels {
    if (Test-NyraTtsModels) {
        Write-BootstrapLog -Level INFO -Message 'modelos TTS locais validados'
        return
    }
    Write-BootstrapLog -Level WARN -Message 'modelos TTS ausentes ou invalidos; download oficial necessario'
    $downloader = Join-Path $repoRoot 'scripts\download_tts_models.ps1'
    $result = Invoke-BoundedProcess -FilePath $powershell -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $downloader) -WorkingDirectory $repoRoot -TimeoutSeconds 900 -Label 'download-tts-models'
    if ($result.ExitCode -ne 0 -or -not (Test-NyraTtsModels)) {
        throw ('Preparacao dos modelos TTS falhou. ' + $result.Stderr)
    }
    Write-BootstrapLog -Level INFO -Message 'modelos TTS baixados e validados'
}

function Ensure-BackendPackage {
    if (Test-BackendPackageCurrent) {
        Write-BootstrapLog -Level INFO -Message 'backend PyInstaller atual; rebuild=false'
        return
    }
    Write-BootstrapLog -Level WARN -Message 'backend PyInstaller ausente ou stale; rebuild=true antes do Tauri'
    $null = Stop-NyraOwnedProcesses -RepoRoot $repoRoot -ExcludeProcessId $PID
    $builder = Join-Path $repoRoot 'packaging\build-backend.ps1'
    $result = Invoke-BoundedProcess -FilePath $powershell -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $builder) -WorkingDirectory $repoRoot -TimeoutSeconds $BuildTimeoutSeconds -Label 'build-backend'
    if ($result.ExitCode -ne 0 -or -not (Test-BackendPackageCurrent)) {
        throw ('Build PyInstaller falhou. ' + $result.Stderr)
    }
    Write-BootstrapLog -Level INFO -Message 'backend PyInstaller reconstruido e validado'
}

function Get-NyraHealth {
    try { return Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 4 }
    catch { return $null }
}

function Wait-NyraHealth {
    param([int]$TimeoutSeconds, $OwnerProcess = $null)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $last = $null
    while ((Get-Date) -lt $deadline) {
        $last = Get-NyraHealth
        if ($last -and $last.character -eq 'NYRA' -and $last.status -eq 'online') { return $last }
        if ($OwnerProcess) {
            $OwnerProcess.Refresh()
            if ($OwnerProcess.HasExited) { break }
        }
        Start-Sleep -Milliseconds 500
    }
    $status = if ($last) { [string]$last.status } else { 'unavailable' }
    throw "Backend nao ficou HEALTHY em /api/health dentro de $TimeoutSeconds segundos (status=$status)."
}

function Test-NyraUi {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:5173/' -TimeoutSec 3
        return $response.StatusCode -eq 200 -and $response.Content -match '<div id="root"'
    } catch { return $false }
}

function Wait-NyraUi {
    param([int]$TimeoutSeconds, $OwnerProcess)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-NyraUi) { return }
        $OwnerProcess.Refresh()
        if ($OwnerProcess.HasExited) {
            throw ('Vite encerrou antes da readiness. ' + (Read-TextTail -Path (Join-Path $logDir 'frontend.stderr.log')))
        }
        Start-Sleep -Milliseconds 350
    }
    throw "UI nao ficou pronta na porta 5173 dentro de $TimeoutSeconds segundos."
}

function Find-ListeningProcess {
    param([int]$Port)
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) { return $null }
    return Get-CimInstance Win32_Process -Filter ('ProcessId=' + $listener.OwningProcess) -ErrorAction SilentlyContinue
}

function Assert-PortAvailable {
    param([int]$Port, [string]$Purpose)
    $owner = Find-ListeningProcess -Port $Port
    if (-not $owner) { return }
    $owned = Test-NyraOwnedProcess -Process $owner -RepoRoot $repoRoot
    throw ("Porta $Port ocupada por processo nao reutilizavel para $Purpose (pid=" + $owner.ProcessId + ", nyra_owned=" + $owned.ToString().ToLowerInvariant() + ').')
}

function Ensure-OllamaAvailable {
    try {
        Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 | Out-Null
        Write-BootstrapLog -Level INFO -Message 'Ollama reutilizado'
        return
    } catch {}
    $ollamaPath = $null
    $ollama = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($ollama) { $ollamaPath = $ollama.Source }
    if (-not $ollamaPath) {
        $candidate = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
        if (Test-Path -LiteralPath $candidate) { $ollamaPath = $candidate }
    }
    if (-not $ollamaPath) { throw 'Ollama nao esta disponivel e o executavel local nao foi encontrado.' }
    Start-Process -FilePath $ollamaPath -ArgumentList @('serve') -WorkingDirectory (Split-Path $ollamaPath) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'ollama.stdout.log') -RedirectStandardError (Join-Path $logDir 'ollama.stderr.log') | Out-Null
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 | Out-Null
            Write-BootstrapLog -Level INFO -Message 'Ollama iniciado'
            return
        } catch { Start-Sleep -Milliseconds 500 }
    }
    throw 'Ollama nao respondeu dentro de 45 segundos.'
}

function Get-NyraOfficialModel {
    $settingsPath = Join-Path $runtimePaths.Data 'brain-settings.json'
    if (Test-Path -LiteralPath $settingsPath) {
        try {
            $saved = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
            if ($saved.official_model) { return [string]$saved.official_model }
        } catch { Write-BootstrapLog -Level WARN -Message 'brain-settings.json invalido; usando configuracao local' }
    }
    if ($env:NYRA_LLM_MODEL) { return [string]$env:NYRA_LLM_MODEL }
    $envPath = Join-Path $repoRoot '.env'
    if (Test-Path -LiteralPath $envPath) {
        $line = Get-Content -LiteralPath $envPath | Where-Object { $_ -match '^\s*NYRA_LLM_MODEL\s*=' } | Select-Object -Last 1
        if ($line) { return (($line -split '=', 2)[1].Trim().Trim('"').Trim("'")) }
    }
    return 'qwen3:8b'
}

function Ensure-NyraOllamaModel {
    $model = Get-NyraOfficialModel
    $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5
    $installed = @($tags.models | ForEach-Object { [string]$_.name })
    if ($model -in $installed) {
        Write-BootstrapLog -Level INFO -Message ("modelo Ollama validado model=$model")
        return
    }
    $ollama = Get-Command ollama.exe -ErrorAction SilentlyContinue
    $ollamaPath = if ($ollama) { $ollama.Source } else { Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe' }
    if (-not (Test-Path -LiteralPath $ollamaPath -PathType Leaf)) { throw "Modelo $model ausente e ollama.exe nao foi encontrado." }
    Write-BootstrapLog -Level WARN -Message ("modelo Ollama ausente; pull automatico model=$model")
    $pull = Invoke-BoundedProcess -FilePath $ollamaPath -ArgumentList @('pull', $model) -WorkingDirectory (Split-Path $ollamaPath) -TimeoutSeconds $BuildTimeoutSeconds -Label 'ollama-pull'
    if ($pull.ExitCode -ne 0) { throw ("ollama pull falhou para $model. " + $pull.Stderr) }
    $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5
    if ($model -notin @($tags.models | ForEach-Object { [string]$_.name })) { throw "Ollama nao confirmou o modelo $model apos o pull." }
    Write-BootstrapLog -Level INFO -Message ("modelo Ollama instalado model=$model")
}

function Save-NyraState {
    param(
        [string]$StateMode,
        $BackendProcess = $null,
        $FrontendProcess = $null,
        $TauriProcess = $null,
        $DesktopProcess = $null
    )
    $backendId = if ($BackendProcess -and $BackendProcess.Id) { $BackendProcess.Id } elseif ($BackendProcess) { $BackendProcess.ProcessId } else { $null }
    $frontendId = if ($FrontendProcess -and $FrontendProcess.Id) { $FrontendProcess.Id } elseif ($FrontendProcess) { $FrontendProcess.ProcessId } else { $null }
    $tauriId = if ($TauriProcess -and $TauriProcess.Id) { $TauriProcess.Id } elseif ($TauriProcess) { $TauriProcess.ProcessId } else { $null }
    $desktopId = if ($DesktopProcess -and $DesktopProcess.Id) { $DesktopProcess.Id } elseif ($DesktopProcess) { $DesktopProcess.ProcessId } else { $null }
    [ordered]@{
        schema = 2
        mode = $StateMode
        root = $repoRoot
        launcher = $PID
        backend = $backendId
        frontend = $frontendId
        tauri = $tauriId
        desktop = $desktopId
        started = (Get-Date).ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
}

function Get-ValidatedStateProcess {
    param($State, [string]$Property)
    if (-not $State -or -not $State.$Property) { return $null }
    $candidate = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$State.$Property) -ErrorAction SilentlyContinue
    if (-not $candidate -or -not (Test-NyraOwnedProcess -Process $candidate -RepoRoot $repoRoot)) { return $null }
    return Get-Process -Id $candidate.ProcessId -ErrorAction SilentlyContinue
}

function Get-SavedState {
    if (-not (Test-Path -LiteralPath $statePath)) { return $null }
    try { return Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json }
    catch { return $null }
}

function Find-NyraDesktopProcess {
    param([string]$ExecutablePath)
    $expected = [IO.Path]::GetFullPath($ExecutablePath)
    return Get-Process -Name 'nyra-desktop' -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.Equals($expected, [StringComparison]::OrdinalIgnoreCase) } catch { $false }
    } | Select-Object -First 1
}

function Wait-NyraDesktop {
    param([string]$ExecutablePath, [int]$TimeoutSeconds, $OwnerProcess = $null)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $desktop = Find-NyraDesktopProcess -ExecutablePath $ExecutablePath
        if ($desktop) {
            $handleDeadline = (Get-Date).AddSeconds(20)
            while ((Get-Date) -lt $handleDeadline) {
                $desktop.Refresh()
                if ($desktop.MainWindowHandle -ne 0) { return $desktop }
                Start-Sleep -Milliseconds 300
            }
            return $desktop
        }
        if ($OwnerProcess) {
            $OwnerProcess.Refresh()
            if ($OwnerProcess.HasExited) {
                throw ('Tauri encerrou antes de abrir a UI. ' + (Read-TextTail -Path (Join-Path $logDir 'tauri.stderr.log')))
            }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Desktop Tauri nao abriu dentro de $TimeoutSeconds segundos."
}

function Build-NyraRelease {
    Write-BootstrapLog -Level INFO -Message 'build release: encerrando somente processos owned pela raiz canonica'
    $null = Stop-NyraOwnedProcesses -RepoRoot $repoRoot -ExcludeProcessId $PID
    $result = Invoke-BoundedProcess -FilePath $node -ArgumentList @($npmCliArgument, 'run', 'build') -WorkingDirectory (Join-Path $repoRoot 'desktop') -TimeoutSeconds $BuildTimeoutSeconds -Label 'tauri-build'
    if ($result.ExitCode -ne 0) { throw ('Tauri build falhou. ' + $result.Stderr) }
    $release = Join-Path $repoRoot 'desktop\src-tauri\target\release\nyra-desktop.exe'
    if (-not (Test-Path -LiteralPath $release -PathType Leaf)) { throw 'Tauri terminou sem produzir nyra-desktop.exe.' }
    $fingerprint = Get-NyraReleaseSourceFingerprint -RepoRoot $repoRoot
    $hash = (Get-FileHash -LiteralPath $release -Algorithm SHA256).Hash.ToLowerInvariant()
    [ordered]@{ schema = 1; source_fingerprint = $fingerprint; executable_sha256 = $hash; built_at = (Get-Date).ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $repoRoot 'desktop\src-tauri\target\release\.nyra-release-build.json') -Encoding UTF8
    Write-BootstrapLog -Level INFO -Message ("build release PASS path=$release")
    return $release
}

function Test-ReleaseCurrent {
    $release = Join-Path $repoRoot 'desktop\src-tauri\target\release\nyra-desktop.exe'
    $markerPath = Join-Path $repoRoot 'desktop\src-tauri\target\release\.nyra-release-build.json'
    if (-not (Test-Path -LiteralPath $release -PathType Leaf) -or -not (Test-Path -LiteralPath $markerPath -PathType Leaf)) { return $false }
    try {
        $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
        return $marker.source_fingerprint -eq (Get-NyraReleaseSourceFingerprint -RepoRoot $repoRoot) -and
            $marker.executable_sha256 -eq (Get-FileHash -LiteralPath $release -Algorithm SHA256).Hash.ToLowerInvariant()
    } catch { return $false }
}

function Start-Development {
    $saved = Get-SavedState
    $savedDesktop = Get-ValidatedStateProcess -State $saved -Property 'desktop'
    $savedHealth = Get-NyraHealth
    if ($saved -and $saved.mode -eq 'development' -and $savedDesktop -and $savedHealth -and $savedHealth.status -eq 'online' -and (Test-NyraUi)) {
        Write-BootstrapLog -Level INFO -Message 'NYRA development ja esta pronta; processos existentes reutilizados'
        return
    }
    $null = Stop-NyraOwnedProcesses -RepoRoot $repoRoot -ExcludeProcessId $PID
    if (Test-Path -LiteralPath $statePath) { Remove-Item -LiteralPath $statePath -Force }
    Assert-PortAvailable -Port 8000 -Purpose 'backend'
    Assert-PortAvailable -Port 5173 -Purpose 'frontend'
    Ensure-OllamaAvailable
    Ensure-NyraOllamaModel

    $python = Find-NyraPythonExecutable -RepoRoot $repoRoot
    if (-not $python) { throw 'Venv Python ausente em .venv e backend\.venv.' }
    $backend = Start-NyraManagedProcess -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000') -WorkingDirectory (Join-Path $repoRoot 'backend') -LogName 'backend'
    $health = Wait-NyraHealth -TimeoutSeconds $BackendTimeoutSeconds -OwnerProcess $backend
    Write-BootstrapLog -Level INFO -Message ("backend HEALTHY pid=" + $backend.Id + ' port=8000 status=' + $health.status)

    $vite = Join-Path $repoRoot 'frontend\node_modules\vite\bin\vite.js'
    $frontend = Start-NyraManagedProcess -FilePath $node -ArgumentList @($vite, '--host', '127.0.0.1', '--port', '5173', '--strictPort') -WorkingDirectory (Join-Path $repoRoot 'frontend') -LogName 'frontend'
    Wait-NyraUi -TimeoutSeconds $FrontendTimeoutSeconds -OwnerProcess $frontend
    Write-BootstrapLog -Level INFO -Message ("frontend READY pid=" + $frontend.Id + ' port=5173')

    $tauriCli = Join-Path $repoRoot 'desktop\node_modules\@tauri-apps\cli\tauri.js'
    $tauri = Start-NyraManagedProcess -FilePath $node -ArgumentList @($tauriCli, 'dev') -WorkingDirectory (Join-Path $repoRoot 'desktop') -LogName 'tauri'
    $debugExecutable = Join-Path $repoRoot 'desktop\src-tauri\target\debug\nyra-desktop.exe'
    $desktop = Wait-NyraDesktop -ExecutablePath $debugExecutable -TimeoutSeconds $DesktopTimeoutSeconds -OwnerProcess $tauri
    Save-NyraState -StateMode 'development' -BackendProcess $backend -FrontendProcess $frontend -TauriProcess $tauri -DesktopProcess $desktop
    Write-BootstrapLog -Level INFO -Message ("startup_result mode=development dependency_installs=$script:DependencyInstalls backend_pid=" + $backend.Id + ' backend_port=8000 frontend_pid=' + $frontend.Id + ' frontend_port=5173 tauri_pid=' + $tauri.Id + ' desktop_pid=' + $desktop.Id + ' health=online ui=ready')
}

function Start-Release {
    $release = Join-Path $repoRoot 'desktop\src-tauri\target\release\nyra-desktop.exe'
    $saved = Get-SavedState
    $savedDesktop = Get-ValidatedStateProcess -State $saved -Property 'desktop'
    $savedHealth = Get-NyraHealth
    if ($saved -and $saved.mode -eq 'release' -and $savedDesktop -and $savedHealth -and $savedHealth.status -eq 'online') {
        $backendOwner = Find-ListeningProcess -Port 8000
        Save-NyraState -StateMode 'release' -BackendProcess $backendOwner -DesktopProcess $savedDesktop
        Write-BootstrapLog -Level INFO -Message 'NYRA release ja esta pronta; processos existentes reutilizados'
        return
    }
    $null = Stop-NyraOwnedProcesses -RepoRoot $repoRoot -ExcludeProcessId $PID
    if (Test-Path -LiteralPath $statePath) { Remove-Item -LiteralPath $statePath -Force }
    Assert-PortAvailable -Port 8000 -Purpose 'backend release'
    Ensure-OllamaAvailable
    Ensure-NyraOllamaModel
    $desktop = Start-Process -FilePath $release -WorkingDirectory $repoRoot -PassThru
    $desktop = Wait-NyraDesktop -ExecutablePath $release -TimeoutSeconds 60 -OwnerProcess $desktop
    $health = Wait-NyraHealth -TimeoutSeconds $BackendTimeoutSeconds -OwnerProcess $desktop
    $backendOwner = Find-ListeningProcess -Port 8000
    Save-NyraState -StateMode 'release' -BackendProcess $backendOwner -DesktopProcess $desktop
    Write-BootstrapLog -Level INFO -Message ("startup_result mode=release dependency_installs=$script:DependencyInstalls backend_pid=" + $backendOwner.ProcessId + ' backend_port=8000 desktop_pid=' + $desktop.Id + ' health=' + $health.status + ' ui=ready')
}

try {
    $createdNew = $false
    $mutex = New-Object Threading.Mutex($false, 'Local\NYRA.Canonical.Bootstrap', [ref]$createdNew)
    try { $mutexOwned = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $mutexOwned = $true }
    if (-not $mutexOwned) { throw 'Outro bootstrap da NYRA esta em andamento.' }

    Write-BootstrapLog -Level INFO -Message ("bootstrap_begin root=$repoRoot")
    Ensure-NpmDependencies -Component 'frontend'
    Ensure-NpmDependencies -Component 'desktop'
    Ensure-NyraTtsModels
    Ensure-BackendPackage

    if ($Mode -eq 'Build') {
        if ($ForceBuild -or -not (Test-ReleaseCurrent)) { $release = Build-NyraRelease }
        else { $release = Join-Path $repoRoot 'desktop\src-tauri\target\release\nyra-desktop.exe' }
        Write-BootstrapLog -Level INFO -Message ("bootstrap_complete mode=build dependency_installs=$script:DependencyInstalls release=$release")
    } elseif ($Mode -eq 'Release') {
        if (-not (Test-ReleaseCurrent)) { $null = Build-NyraRelease }
        Start-Release
        Write-BootstrapLog -Level INFO -Message ("bootstrap_complete mode=release dependency_installs=$script:DependencyInstalls")
    } else {
        Start-Development
        Write-BootstrapLog -Level INFO -Message ("bootstrap_complete mode=development dependency_installs=$script:DependencyInstalls")
    }
} catch {
    Write-BootstrapLog -Level ERROR -Message ('bootstrap_failed: ' + ($_.Exception.Message -replace '[\r\n]+', ' '))
    if ($Mode -ne 'Build') {
        $null = Stop-NyraOwnedProcesses -RepoRoot $repoRoot -ExcludeProcessId $PID
        if (Test-Path -LiteralPath $statePath) { Remove-Item -LiteralPath $statePath -Force }
    }
    exit 1
} finally {
    if ($mutexOwned -and $mutex) { try { $mutex.ReleaseMutex() } catch {} }
    if ($mutex) { $mutex.Dispose() }
}
