import { describe, expect, it } from 'vitest'
import { globalCursorPointer, live2dCursorPayload, type GlobalCursorSample } from './globalCursor'

const sample = (values: Partial<GlobalCursorSample> = {}): GlobalCursorSample => ({
  available: true,
  cursorX: 3840,
  cursorY: 400,
  normalizedX: 1.4,
  normalizedY: -.45,
  virtualDesktopBounds: { x: -1920, y: 0, width: 6400, height: 1440 },
  monitorCount: 3,
  ...values,
})

describe('Desktop Presence global cursor mapping', () => {
  it('clamps coordinates while preserving cross-monitor direction', () => {
    expect(globalCursorPointer(sample())).toEqual({ x: 1, y: -.45, available: true })
    expect(live2dCursorPayload(sample())).toEqual({ x: 1, y: -.45 })
  })

  it('returns a safe neutral fallback when native capture is unavailable', () => {
    expect(globalCursorPointer(sample({ available: false }))).toEqual({ x: 0, y: 0, available: false })
    expect(live2dCursorPayload(sample({ available: false }))).toEqual({ x: 0, y: 0 })
  })
})
