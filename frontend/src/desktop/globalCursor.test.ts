import { describe, expect, it } from 'vitest'
import { globalCursorPointer, live2dCursorPayload, shouldSendPointer, type GlobalCursorSample } from './globalCursor'

const sample = (values: Partial<GlobalCursorSample> = {}): GlobalCursorSample => ({
  available: true,
  cursorX: 3840,
  cursorY: 400,
  normalizedX: 1.4,
  normalizedY: -.45,
  windowBounds: { x: 1500, y: 500, width: 480, height: 560 },
  windowMonitorBounds: { x: 0, y: 0, width: 1920, height: 1080 },
  cursorMonitorBounds: { x: 1920, y: 0, width: 2560, height: 1440 },
  monitorChanged: true,
  ...values,
})

describe('Desktop Presence global cursor mapping', () => {
  it('sends the first Live2D sample instead of getting stuck on NaN', () => {
    expect(shouldSendPointer({ x: .2, y: -.1 }, { x: Number.NaN, y: Number.NaN })).toBe(true)
    expect(shouldSendPointer({ x: .2, y: -.1 }, { x: .2, y: -.1 })).toBe(false)
  })

  it('clamps coordinates while preserving cross-monitor direction', () => {
    expect(globalCursorPointer(sample())).toEqual({ x: 1, y: -.45, available: true })
    expect(live2dCursorPayload(sample())).toEqual({ x: 1, y: -.45 })
  })

  it('returns a safe neutral fallback when native capture is unavailable', () => {
    expect(globalCursorPointer(sample({ available: false }))).toEqual({ x: 0, y: 0, available: false })
    expect(live2dCursorPayload(sample({ available: false }))).toEqual({ x: 0, y: 0 })
  })
})
