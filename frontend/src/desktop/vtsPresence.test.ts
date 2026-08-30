import { describe, expect, it } from 'vitest'
import { backendPresenceReport, canReplaceInternalAvatar, nativePresenceConfig, type NativePresenceStatus } from './vtsPresence'

const active: NativePresenceStatus = {
  state: 'VTS_ACTIVE', alpha: 'VALID', fallbackActive: false, sender: 'VTubeStudioSpout',
  width: 1103, height: 909, format: 'DXGI_FORMAT_R8G8B8A8_UNORM', senderFps: 30,
  receiverFps: 30, frameCount: 20, droppedFrames: 0, lastFrameAgeMs: 0,
  adapterMatch: true, memoryBytes: 12_000_000,
}

describe('VTube Studio Presence safety gate', () => {
  it('hides the internal avatar only after valid transparent frames', () => {
    expect(canReplaceInternalAvatar(active)).toBe(true)
    expect(canReplaceInternalAvatar({ ...active, alpha: 'OPAQUE' })).toBe(false)
    expect(canReplaceInternalAvatar({ ...active, state: 'VTS_WAITING_FRAMES' })).toBe(false)
    expect(canReplaceInternalAvatar({ ...active, fallbackActive: true })).toBe(false)
  })

  it('keeps INTERNAL until the existing VTS API is authenticated with a model', () => {
    const config = { enabled: true, renderer: 'AUTO' as const, spout_sender: 'AUTO', presence_scale: 1, presence_offset_x: 0, presence_offset_y: 0, frame_watchdog_seconds: 12 }
    expect(nativePresenceConfig({ connected: true, authenticated: true, model_loaded: true, config }).mode).toBe('AUTO')
    expect(nativePresenceConfig({ connected: true, authenticated: false, model_loaded: true, config }).mode).toBe('INTERNAL')
  })

  it('maps the native camelCase status to the backend health report', () => {
    expect(backendPresenceReport(active)).toMatchObject({
      state: 'VTS_ACTIVE', alpha: 'VALID', sender_fps: 30, adapter_match: true,
    })
  })
})
