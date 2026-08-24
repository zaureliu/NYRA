import { clampUnit, type NormalizedPointer } from '../avatar/usePointerFollow'

export interface ScreenBounds {
  x: number
  y: number
  width: number
  height: number
}

export interface GlobalCursorSample {
  available: boolean
  cursorX: number
  cursorY: number
  normalizedX: number
  normalizedY: number
  windowBounds: ScreenBounds
  windowMonitorBounds: ScreenBounds
  cursorMonitorBounds: ScreenBounds
  monitorChanged: boolean
}

export interface Live2DCursorPayload {
  x: number
  y: number
}

export function shouldSendPointer(current: Live2DCursorPayload, previous: Live2DCursorPayload, threshold = .002): boolean {
  return !Number.isFinite(previous.x) || !Number.isFinite(previous.y)
    || Math.abs(current.x - previous.x) > threshold
    || Math.abs(current.y - previous.y) > threshold
}

export function globalCursorPointer(sample: GlobalCursorSample): NormalizedPointer & { available: boolean } {
  if (!sample.available) return { x: 0, y: 0, available: false }
  return { x: clampUnit(sample.normalizedX), y: clampUnit(sample.normalizedY), available: true }
}

export function live2dCursorPayload(sample: GlobalCursorSample): Live2DCursorPayload {
  const pointer = globalCursorPointer(sample)
  return { x: pointer.available ? pointer.x : 0, y: pointer.available ? pointer.y : 0 }
}
