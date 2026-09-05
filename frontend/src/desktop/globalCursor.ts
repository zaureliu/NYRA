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
  virtualDesktopBounds: ScreenBounds
  monitorCount: number
}

export interface Live2DCursorPayload {
  x: number
  y: number
}

export function globalCursorPointer(sample: GlobalCursorSample): Live2DCursorPayload & { available: boolean } {
  if (!sample.available) return { x: 0, y: 0, available: false }
  return {
    x: Math.max(-1, Math.min(1, sample.normalizedX)),
    y: Math.max(-1, Math.min(1, sample.normalizedY)),
    available: true,
  }
}

export function live2dCursorPayload(sample: GlobalCursorSample): Live2DCursorPayload {
  const pointer = globalCursorPointer(sample)
  return { x: pointer.available ? pointer.x : 0, y: pointer.available ? pointer.y : 0 }
}
