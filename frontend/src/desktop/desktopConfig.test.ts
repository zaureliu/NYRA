import { describe, expect, it } from 'vitest'
import config from '../../../desktop/src-tauri/tauri.conf.json'
import capabilities from '../../../desktop/src-tauri/capabilities/default.json'

describe('desktop transparency', () => {
  it('uses a real transparent frameless shadowless window', () => {
    const window = config.app.windows[0]
    expect(window.transparent).toBe(true)
    expect(window.decorations).toBe(false)
    expect(window.shadow).toBe(false)
    expect(window.width).toBe(480)
    expect(window.height).toBe(560)
    expect(capabilities.permissions).toContain('core:window:allow-set-size')
  })

  it('embeds both release pages and keeps Vite limited to development', () => {
    expect(config.build.frontendDist).toBe('../../frontend/dist')
    expect(config.build.beforeBuildCommand).toContain('frontend run build')
    expect(config.build.devUrl).toBe('http://127.0.0.1:5173/desktop.html')
    expect(capabilities.windows).toContain('dashboard')
  })
})
