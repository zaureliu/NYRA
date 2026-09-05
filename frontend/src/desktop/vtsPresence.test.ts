import { describe, expect, it } from 'vitest'
import { backendPresenceReport, hasActiveVtsCharacter, nativePresenceConfig, type NativePresenceStatus } from './vtsPresence'

const active: NativePresenceStatus = {
  state: 'VTS_ACTIVE', alpha: 'VALID', vtsActive: true, sender: 'VTubeStudioSpout',
  width: 1103, height: 909, format: 'DXGI_FORMAT_R8G8B8A8_UNORM', senderFps: 30,
  receiverFps: 30, frameCount: 20, droppedFrames: 0, lastFrameAgeMs: 0,
  adapterMatch: true, memoryBytes: 12_000_000,
}

describe('VTube Studio-only Presence safety gate', () => {
  it('shows a character only while native transparent VTS frames are valid', () => {
    expect(hasActiveVtsCharacter(active)).toBe(true)
    expect(hasActiveVtsCharacter({ ...active, alpha: 'OPAQUE' })).toBe(false)
    expect(hasActiveVtsCharacter({ ...active, state: 'VTS_WAITING_FRAMES' })).toBe(false)
    expect(hasActiveVtsCharacter({ ...active, vtsActive: false })).toBe(false)
  })

  it('keeps Spout discovery active independently of API authorization', () => {
    const config = { enabled: true, renderer: 'VTUBE_STUDIO' as const, mouse_tracking: 'HEAD_EYES' as const, spout_sender: 'AUTO', presence_scale: 1, presence_offset_x: 0, presence_offset_y: 0, frame_watchdog_seconds: 12 }
    expect(nativePresenceConfig({ connected: true, authenticated: true, model_loaded: true, config })).not.toHaveProperty('mode')
    expect(nativePresenceConfig({ connected: false, authenticated: false, model_loaded: false, config }).sender).toBe('AUTO')
  })

  it('maps the native camelCase status to the backend health report', () => {
    expect(backendPresenceReport(active)).toMatchObject({
      state: 'VTS_ACTIVE', alpha: 'VALID', vts_active: true, sender_fps: 30, adapter_match: true,
    })
  })
})
