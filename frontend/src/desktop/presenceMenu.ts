export interface PresenceRect {
  x: number
  y: number
  width: number
  height: number
}

export interface PresenceSize {
  width: number
  height: number
}

export interface PresenceMenuLayout {
  x: number
  y: number
  width: number
  maxHeight: number
  horizontal: 'start' | 'end'
  vertical: 'start' | 'end'
}

export const PRESENCE_MENU_LIMITS = {
  margin: 8,
  minWidth: 216,
  maxWidth: 340,
  minHeight: 244,
  maxHeight: 420,
} as const

const clamp = (value: number, minimum: number, maximum: number) =>
  Math.min(maximum, Math.max(minimum, value))

/**
 * Positions the options card in the visible intersection between the Presence
 * window and the current monitor work area. Monitor/window values are physical
 * pixels; viewport/menu values are CSS logical pixels.
 */
export function computePresenceMenuLayout(input: {
  windowRect: PresenceRect
  workArea: PresenceRect
  viewport: PresenceSize
  desiredMenu: PresenceSize
  scaleFactor: number
}): PresenceMenuLayout {
  const scale = Number.isFinite(input.scaleFactor) && input.scaleFactor > 0
    ? input.scaleFactor
    : 1
  const margin = PRESENCE_MENU_LIMITS.margin
  const windowRight = input.windowRect.x + input.windowRect.width
  const windowBottom = input.windowRect.y + input.windowRect.height
  const workRight = input.workArea.x + input.workArea.width
  const workBottom = input.workArea.y + input.workArea.height

  const safeLeft = margin + Math.max(0, input.workArea.x - input.windowRect.x) / scale
  const safeTop = margin + Math.max(0, input.workArea.y - input.windowRect.y) / scale
  const safeRight = margin + Math.max(0, windowRight - workRight) / scale
  const safeBottom = margin + Math.max(0, windowBottom - workBottom) / scale
  const availableWidth = Math.max(1, input.viewport.width - safeLeft - safeRight)
  const availableHeight = Math.max(1, input.viewport.height - safeTop - safeBottom)
  const width = Math.min(
    availableWidth,
    clamp(input.desiredMenu.width, PRESENCE_MENU_LIMITS.minWidth, PRESENCE_MENU_LIMITS.maxWidth),
  )
  const layoutHeight = Math.min(
    availableHeight,
    clamp(input.desiredMenu.height, PRESENCE_MENU_LIMITS.minHeight, PRESENCE_MENU_LIMITS.maxHeight),
  )

  const windowCenterX = input.windowRect.x + input.windowRect.width / 2
  const windowCenterY = input.windowRect.y + input.windowRect.height / 2
  const workCenterX = input.workArea.x + input.workArea.width / 2
  const workCenterY = input.workArea.y + input.workArea.height / 2
  const horizontal = windowCenterX > workCenterX ? 'end' : 'start'
  const vertical = windowCenterY > workCenterY ? 'end' : 'start'
  const x = horizontal === 'start'
    ? safeLeft
    : input.viewport.width - safeRight - width
  const y = vertical === 'start'
    ? safeTop
    : input.viewport.height - safeBottom - layoutHeight

  return {
    x: Math.max(safeLeft, x),
    y: Math.max(safeTop, y),
    width,
    maxHeight: availableHeight,
    horizontal,
    vertical,
  }
}
