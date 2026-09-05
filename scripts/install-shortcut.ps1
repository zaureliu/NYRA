[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$launcher = Join-Path $PSScriptRoot 'launch-kazumi.vbs'
$release = Join-Path $repoRoot 'desktop\src-tauri\target\release\kazumi-desktop.exe'
$fallbackIcon = Join-Path $repoRoot 'desktop\src-tauri\icons\icon.ico'
$desktop = Join-Path $env:USERPROFILE 'Desktop'
$shortcutPath = Join-Path $desktop 'Kazumi.lnk'
$startMenuDirectory = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$startMenuShortcutPath = Join-Path $startMenuDirectory 'Kazumi.lnk'
$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'

if (-not (Test-Path -LiteralPath $launcher)) { throw 'Launcher VBS ausente.' }
$icon = if (Test-Path -LiteralPath $release) { $release } else { $fallbackIcon }
if (-not (Test-Path -LiteralPath $icon)) { throw 'Icone oficial da KAZUMI ausente.' }

New-Item -ItemType Directory -Path $desktop,$startMenuDirectory -Force | Out-Null
$shell = New-Object -ComObject WScript.Shell
foreach ($target in @($shortcutPath, $startMenuShortcutPath)) {
    $shortcut = $shell.CreateShortcut($target)
    $shortcut.TargetPath = $wscript
    $shortcut.Arguments = '"' + $launcher + '"'
    $shortcut.WorkingDirectory = $repoRoot
    $shortcut.IconLocation = $icon + ',0'
    $shortcut.Description = 'KAZUMI - inicializacao automatica e idempotente'
    $shortcut.Save()
}

Write-Output $shortcutPath
Write-Output $startMenuShortcutPath
