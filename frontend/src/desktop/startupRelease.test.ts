import { describe, expect, it } from 'vitest'
import launcher from '../../../scripts/start-kazumi.ps1?raw'
import rootLauncher from '../../../start-kazumi.ps1?raw'
import runtimePaths from '../../../scripts/runtime-paths.ps1?raw'
import hiddenEntry from '../../../scripts/launch-kazumi.vbs?raw'
import shortcutInstaller from '../../../scripts/install-shortcut.ps1?raw'
import desktopNative from '../../../desktop/src-tauri/src/lib.rs?raw'

describe('release startup', () => {
  it('orchestrates the compiled app without a Vite runtime', () => {
    expect(launcher).toContain('target\\release\\kazumi-desktop.exe')
    expect(launcher).toContain('/api/tags')
    expect(launcher).toContain('ollama_preload_owner backend=true')
    expect(launcher).not.toContain("keep_alive = '30m'")
    expect(launcher).toContain('/health')
    expect(launcher).toContain('Find-KazumiPythonExecutable')
    expect(launcher).toContain("backend_start owner=desktop sidecar=frozen")
    expect(launcher).toContain('backend_online reused=false owner=desktop')
    expect(runtimePaths).toContain("'.venv\\Scripts\\python.exe'")
    expect(runtimePaths).toContain("'backend\\.venv\\Scripts\\python.exe'")
    expect(launcher).not.toContain('127.0.0.1:5173')
    expect(launcher).not.toContain('npm.cmd')
  })

  it('uses a hidden entrypoint and the official executable icon', () => {
    expect(hiddenEntry).toContain('shell.Run command, 0, False')
    expect(rootLauncher).toContain("scripts\\start-kazumi.ps1")
    expect(rootLauncher).not.toContain('Set-MpPreference')
    expect(shortcutInstaller).toContain('wscript.exe')
    expect(shortcutInstaller).toContain("'Kazumi.lnk'")
    expect(rootLauncher).toContain("-Mode Release")
    expect(rootLauncher).toContain("[switch]$SourceBackend")
    expect(shortcutInstaller).toContain("$icon = if (Test-Path -LiteralPath $release)")
    expect(shortcutInstaller).toContain("$shortcut.IconLocation = $icon + ',0'")
  })

  it('opens the packaged dashboard instead of a development URL', () => {
    expect(desktopNative).toContain('WebviewUrl::App("index.html".into())')
    expect(desktopNative).not.toContain('127.0.0.1:5173')
  })
})
