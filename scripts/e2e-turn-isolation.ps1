#requires -version 5
<#
NYRA E2E Turn Isolation — executa a sequência obrigatória contra o backend REAL
pela mesma interface usada pelo operador (POST /api/chat + WebSocket /api/ws),
incluindo abertura física de aplicativos e verificação Win32 de janela.
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [switch]$SkipPhysicalLaunch,
    [string]$DynamicApp = "Wireshark",
    [switch]$LaunchDynamic,
    [switch]$CloseNotepad,
    [int]$TimeoutSeconds = 180
)
$ErrorActionPreference = "Stop"
$python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
$script = Join-Path (Get-Location) "scripts\e2e_turn_isolation.py"
$e2eArgs = @($script, "--base-url", $BaseUrl, "--timeout", $TimeoutSeconds, "--dynamic-app", $DynamicApp)
if ($SkipPhysicalLaunch) { $e2eArgs += "--skip-physical-launch" }
if ($LaunchDynamic) { $e2eArgs += "--launch-dynamic" }
if ($CloseNotepad) { $e2eArgs += "--close-notepad" }
& $python @e2eArgs
exit $LASTEXITCODE
