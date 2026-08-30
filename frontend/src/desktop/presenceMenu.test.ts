import { describe, expect, it } from 'vitest'
import { computePresenceMenuLayout, PRESENCE_MENU_LIMITS, type PresenceRect } from './presenceMenu'

const workArea: PresenceRect = { x: 0, y: 0, width: 1920, height: 1040 }

function layoutAt(windowRect: PresenceRect, desiredHeight = 252) {
  return computePresenceMenuLayout({
    windowRect,
    workArea,
    viewport: { width: windowRect.width, height: windowRect.height },
    desiredMenu: { width: 328, height: desiredHeight },
    scaleFactor: 1,
  })
}

function expectInside(layout: ReturnType<typeof layoutAt>, viewport: { width: number; height: number }) {
  expect(layout.x).toBeGreaterThanOrEqual(PRESENCE_MENU_LIMITS.margin)
  expect(layout.y).toBeGreaterThanOrEqual(PRESENCE_MENU_LIMITS.margin)
  expect(layout.x + layout.width).toBeLessThanOrEqual(viewport.width - PRESENCE_MENU_LIMITS.margin)
  expect(layout.y + Math.min(252, layout.maxHeight)).toBeLessThanOrEqual(viewport.height - PRESENCE_MENU_LIMITS.margin)
}

describe('Desktop Presence options menu geometry', () => {
  it.each([
    ['100% bottom-right', { x: 1440, y: 480, width: 480, height: 560 }],
    ['75% bottom-left', { x: 0, y: 620, width: 360, height: 420 }],
    ['50% bottom-right', { x: 1680, y: 760, width: 240, height: 280 }],
    ['minimum near top', { x: 20, y: 0, width: 240, height: 280 }],
  ] as const)('keeps the full card visible at %s', (_name, windowRect) => {
    const layout = layoutAt(windowRect)
    expectInside(layout, windowRect)
    expect(layout.width).toBeLessThanOrEqual(PRESENCE_MENU_LIMITS.maxWidth)
    expect(layout.maxHeight).toBeGreaterThanOrEqual(PRESENCE_MENU_LIMITS.minHeight)
  })

  it('opens inward from each monitor edge', () => {
    expect(layoutAt({ x: 1440, y: 480, width: 480, height: 560 })).toMatchObject({ horizontal: 'end', vertical: 'end' })
    expect(layoutAt({ x: 0, y: 620, width: 360, height: 420 })).toMatchObject({ horizontal: 'start', vertical: 'end' })
    expect(layoutAt({ x: 720, y: 0, width: 480, height: 560 })).toMatchObject({ vertical: 'start' })
  })

  it('compensates for a restored window crossing the work-area boundary', () => {
    const windowRect = { x: -24, y: -16, width: 480, height: 560 }
    const layout = layoutAt(windowRect)
    expect(layout.x).toBeGreaterThanOrEqual(32)
    expect(layout.y).toBeGreaterThanOrEqual(24)
    expectInside(layout, windowRect)
  })
})
