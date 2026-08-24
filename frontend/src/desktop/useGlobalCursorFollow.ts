import { useEffect } from 'react'
import { listen } from '@tauri-apps/api/event'
import { DEFAULT_POINTER_FOLLOW, DESKTOP_GLOBAL_POINTER_EVENT, smoothPointer } from '../avatar/usePointerFollow'
import { globalCursorPointer, shouldSendPointer, type GlobalCursorSample } from './globalCursor'

const NATIVE_CURSOR_EVENT = 'nyra-global-cursor'
const LIVE2D_INTERVAL_MS = 1000 / 30

export function useGlobalCursorFollow(live2dEnabled: boolean, onAvailability?: (available: boolean) => void) {
  useEffect(() => {
    let disposed = false
    let unlisten = () => {}
    let frame = 0
    let lastLive2DUpdate = -Infinity
    let lastMove = -Infinity
    let previous = performance.now()
    let target = { x: 0, y: 0 }
    let current = { x: 0, y: 0 }
    let lastSent = { x: Number.NaN, y: Number.NaN }

    void listen<GlobalCursorSample>(NATIVE_CURSOR_EVENT, (event) => {
      if (disposed) return
      const sample = event.payload
      const pointer = globalCursorPointer(sample)
      onAvailability?.(pointer.available)
      window.dispatchEvent(new CustomEvent(DESKTOP_GLOBAL_POINTER_EVENT, { detail: pointer }))
      target = pointer.available ? { x: pointer.x, y: pointer.y } : { x: 0, y: 0 }
      lastMove = pointer.available ? performance.now() : -Infinity
    }).then((dispose) => {
      if (disposed) dispose()
      else unlisten = dispose
    }).catch(() => onAvailability?.(false))

    const tick = (now: number) => {
      if (disposed) return
      const elapsed = Math.min(64, Math.max(0, now - previous)); previous = now
      const desired = now - lastMove <= DEFAULT_POINTER_FOLLOW.returnDelayMs ? target : { x: 0, y: 0 }
      current = smoothPointer(current, desired, DEFAULT_POINTER_FOLLOW.smoothing, elapsed)
      if (Math.abs(current.x) < .001) current.x = 0
      if (Math.abs(current.y) < .001) current.y = 0
      const changed = shouldSendPointer(current, lastSent)
      if (live2dEnabled && changed && now - lastLive2DUpdate >= LIVE2D_INTERVAL_MS) {
        lastLive2DUpdate = now
        lastSent = { ...current }
        void fetch('http://127.0.0.1:8000/api/live2d/cursor', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(current),
        }).catch(() => undefined)
      }
      frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)

    return () => { disposed = true; cancelAnimationFrame(frame); unlisten() }
  }, [live2dEnabled, onAvailability])
}
