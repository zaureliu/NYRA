import { describe, expect, it } from 'vitest'
import { classifyGaze, clampUnit, normalizePointerPosition, normalizePointerVector, smoothPointer } from './usePointerFollow'

describe('mouse follow normalization', () => {
  it('normalizes, clamps and applies a center dead zone', () => {
    expect(normalizePointerPosition(500, 500, 1000, 1000)).toEqual({ x: 0, y: 0 })
    const corner = normalizePointerPosition(1000, 0, 1000, 1000)
    expect(corner.x).toBeCloseTo(Math.SQRT1_2)
    expect(corner.y).toBeCloseTo(-Math.SQRT1_2)
    const clamped = normalizePointerVector(8, -4)
    expect(clamped.x).toBeCloseTo(Math.SQRT1_2)
    expect(clamped.y).toBeCloseTo(-Math.SQRT1_2)
    expect(clampUnit(Number.NaN)).toBe(0)
  })

  it('classifies all cardinal and diagonal gaze regions', () => {
    expect(classifyGaze({ x: 0, y: 0 })).toBe('front')
    expect(classifyGaze({ x: -.4, y: 0 })).toBe('left_light')
    expect(classifyGaze({ x: .9, y: 0 })).toBe('right')
    expect(classifyGaze({ x: 0, y: -.8 })).toBe('up')
    expect(classifyGaze({ x: 0, y: .4 })).toBe('down_light')
    expect(classifyGaze({ x: -.6, y: -.5 })).toBe('up_left')
    expect(classifyGaze({ x: .6, y: .5 })).toBe('down_right')
  })

  it('approaches the target without an instantaneous jump', () => {
    const first = smoothPointer({ x: 0, y: 0 }, { x: 1, y: -1 }, 10, 16)
    expect(first.x).toBeGreaterThan(0)
    expect(first.x).toBeLessThan(.2)
    expect(first.y).toBeCloseTo(-first.x)
    const next = smoothPointer(first, { x: 1, y: -1 }, 10, 16)
    expect(next.x).toBeGreaterThan(first.x)
    expect(smoothPointer(next, { x: 0, y: 0 }, 10, 100).x).toBeLessThan(next.x)
  })
})
