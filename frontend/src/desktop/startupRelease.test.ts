import { describe, expect, it } from 'vitest'
import launcher from '../../../scripts/start-nyra.ps1?raw'
import runtimePaths from '../../../scripts/runtime-paths.ps1?raw'
import hiddenEntry from '../../../scripts/launch-nyra.vbs?raw'
import shortcutInstaller from '../../../scripts/install-shortcut.ps1?raw'
import desktopNative from '../../../desktop/src-tauri/src/lib.rs?raw'

describe('release startup', () => {
  it('orchestrates the compiled app without a Vite runtime', () => {
    expect(launcher).toContain('target\\release\\nyra-desktop.exe')
    expect(launcher).toContain('/api/tags')
    expect(launcher).toContain('ollama_preload_owner backend=true')
    expect(launcher).not.toContain("keep_alive = '30m'")
    expect(launcher).toContain('/health')
    expect(launcher).toContain('Find-NyraPythonExecutable')
    expect(runtimePaths).toContain("'.venv\\Scripts\\python.exe'")
    expect(runtimePaths).toContain("'backend\\.venv\\Scripts\\python.exe'")
    expect(launcher).not.toContain('127.0.0.1:5173')
    expect(launcher).not.toContain('npm.cmd')
  })

  it('uses a hidden entrypoint and the official executable icon', () => {
    expect(hiddenEntry).toContain('shell.Run command, 0, False')
    expect(shortcutInstaller).toContain('wscript.exe')
    expect(shortcutInstaller).toContain("'NYRA.lnk'")
    expect(shortcutInstaller).toContain("$icon = if (Test-Path -LiteralPath $release)")
    expect(shortcutInstaller).toContain("$shortcut.IconLocation = $icon + ',0'")
  })

  it('opens the packaged dashboard instead of a development URL', () => {
    expect(desktopNative).toContain('WebviewUrl::App("index.html".into())')
    expect(desktopNative).not.toContain('127.0.0.1:5173')
  })
})
