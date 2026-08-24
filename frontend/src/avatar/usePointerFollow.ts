import { useEffect, type RefObject } from 'react'
import type { GazeDirection } from './avatarState'

export interface NormalizedPointer { x: number; y: number }
export type PointerFollowSource = 'web' | 'desktop-global'
export const DESKTOP_GLOBAL_POINTER_EVENT = 'nyra-desktop-global-pointer'
export interface PointerFollowConfig {
  deadZone: number
  returnDelayMs: number
  smoothing: number
  maxEyeX: number
  maxEyeY: number
  maxHeadX: number
  maxHeadY: number
  maxHeadTilt: number
}

export const DEFAULT_POINTER_FOLLOW: PointerFollowConfig = {
  deadZone: .12, returnDelayMs: 1700, smoothing: 10,
  maxEyeX: 13, maxEyeY: 9, maxHeadX: 7, maxHeadY: 4, maxHeadTilt: .75,
}

export function clampUnit(value: number) {
  return Math.max(-1, Math.min(1, Number.isFinite(value) ? value : 0))
}

export function smoothPointer(current: NormalizedPointer, target: NormalizedPointer, smoothing: number, elapsedMs: number): NormalizedPointer {
  const alpha = 1 - Math.exp(-Math.max(0, smoothing) * Math.max(0, elapsedMs) / 1000)
  return {
    x: current.x + (target.x - current.x) * alpha,
    y: current.y + (target.y - current.y) * alpha,
  }
}

export function normalizePointerPosition(clientX: number, clientY: number, width: number, height: number, deadZone = .12): NormalizedPointer {
  if (width <= 0 || height <= 0) return { x: 0, y: 0 }
  return normalizePointerVector((clientX / width) * 2 - 1, (clientY / height) * 2 - 1, deadZone)
}

export function normalizePointerVector(rawX: number, rawY: number, deadZone = .12): NormalizedPointer {
  let x = clampUnit(rawX)
  let y = clampUnit(rawY)
  if (Math.hypot(x, y) <= deadZone) return { x: 0, y: 0 }
  const scale = 1 / Math.max(1, Math.hypot(x, y))
  x = clampUnit(x * scale); y = clampUnit(y * scale)
  return { x, y }
}

export function classifyGaze({ x, y }: NormalizedPointer): GazeDirection {
  const ax = Math.abs(x); const ay = Math.abs(y)
  if (Math.hypot(x, y) < .14) return 'front'
  if (ax >= .22 && ay >= .22) return `${y < 0 ? 'up' : 'down'}_${x < 0 ? 'left' : 'right'}` as GazeDirection
  if (ax >= ay) return `${x < 0 ? 'left' : 'right'}${ax < .58 ? '_light' : ''}` as GazeDirection
  return `${y < 0 ? 'up' : 'down'}${ay < .58 ? '_light' : ''}` as GazeDirection
}

export function gazeVector(direction: GazeDirection): NormalizedPointer {
  const vectors: Record<GazeDirection, NormalizedPointer> = {
    front: { x: 0, y: 0 },
    left_light: { x: -.38, y: 0 }, left: { x: -.82, y: 0 },
    right_light: { x: .38, y: 0 }, right: { x: .82, y: 0 },
    up_light: { x: 0, y: -.34 }, up: { x: 0, y: -.74 },
    down_light: { x: 0, y: .34 }, down: { x: 0, y: .74 },
    up_left: { x: -.62, y: -.52 }, up_right: { x: .62, y: -.52 },
    down_left: { x: -.62, y: .52 }, down_right: { x: .62, y: .52 },
  }
  return vectors[direction]
}

export function usePointerFollow(
  reference: RefObject<HTMLElement | null>,
  enabled: boolean,
  restingGaze: GazeDirection,
  overrides?: Partial<PointerFollowConfig>,
  source: PointerFollowSource = 'web',
) {
  useEffect(() => {
    const element = reference.current
    if (!element || typeof window === 'undefined') return
    const config = { ...DEFAULT_POINTER_FOLLOW, ...overrides }
    const reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
    const rest = gazeVector(restingGaze)
    let current = { ...rest }; let target = { ...rest }
    let lastMove = -Infinity; let pointerSeen = false; let frame = 0; let previous = performance.now()

    const write = (active: boolean) => {
      element.style.setProperty('--nyra-eye-follow-x', `${current.x * config.maxEyeX}px`)
      element.style.setProperty('--nyra-eye-follow-y', `${current.y * config.maxEyeY}px`)
      element.style.setProperty('--nyra-pointer-head-x', `${current.x * config.maxHeadX}px`)
      element.style.setProperty('--nyra-pointer-head-y', `${current.y * config.maxHeadY}px`)
      element.style.setProperty('--nyra-pointer-head-tilt', `${current.x * config.maxHeadTilt}deg`)
      element.dataset.gaze = classifyGaze(current)
      element.dataset.pointerActive = String(active)
    }

    if (!enabled || reduced) { write(false); return }

    const onPointerMove = (event: PointerEvent) => {
      if (event.pointerType === 'touch') return
      target = normalizePointerPosition(event.clientX, event.clientY, window.innerWidth, window.innerHeight, config.deadZone)
      element.dataset.pointerTarget = classifyGaze(target)
      lastMove = performance.now(); pointerSeen = true
    }
    const onDesktopPointer = (event: Event) => {
      const detail = (event as CustomEvent<NormalizedPointer & { available?: boolean }>).detail
      if (!detail?.available && detail?.available !== undefined) { releasePointer(); return }
      target = normalizePointerVector(detail?.x ?? 0, detail?.y ?? 0, config.deadZone)
      element.dataset.pointerTarget = classifyGaze(target)
      lastMove = performance.now(); pointerSeen = true
    }
    const releasePointer = () => { if (pointerSeen) lastMove = performance.now() }
    const tick = (now: number) => {
      const elapsed = Math.min(64, Math.max(0, now - previous)); previous = now
      const active = pointerSeen && now - lastMove <= config.returnDelayMs
      if (!active) { target = rest; element.dataset.pointerTarget = classifyGaze(rest) }
      current = smoothPointer(current, target, config.smoothing, elapsed)
      if (Math.abs(current.x) < .001) current.x = 0
      if (Math.abs(current.y) < .001) current.y = 0
      write(active)
      frame = requestAnimationFrame(tick)
    }

    if (source === 'desktop-global') window.addEventListener(DESKTOP_GLOBAL_POINTER_EVENT, onDesktopPointer)
    else {
      window.addEventListener('pointermove', onPointerMove, { passive: true })
      document.documentElement.addEventListener('pointerleave', releasePointer)
      window.addEventListener('blur', releasePointer)
    }
    frame = requestAnimationFrame(tick)
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener(DESKTOP_GLOBAL_POINTER_EVENT, onDesktopPointer)
      window.removeEventListener('pointermove', onPointerMove)
      document.documentElement.removeEventListener('pointerleave', releasePointer)
      window.removeEventListener('blur', releasePointer)
    }
  }, [enabled, overrides, reference, restingGaze, source])
}
