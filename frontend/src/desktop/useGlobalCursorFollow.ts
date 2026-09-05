import { useEffect } from 'react'
import { listen } from '@tauri-apps/api/event'
import { globalCursorPointer, type GlobalCursorSample } from './globalCursor'
import type { MouseTrackingMode } from './vtsPresence'

const NATIVE_CURSOR_EVENT = 'kazumi-global-cursor'
const LIVE2D_INTERVAL_MS = 1000 / 30

export function useGlobalCursorFollow(vtsActive: boolean, mode: MouseTrackingMode, onAvailability?: (available: boolean) => void) {
  useEffect(() => {
    let disposed = false
    let unlisten = () => {}
    let lastLive2DUpdate = -Infinity

    const send = (value: { x: number; y: number }) => {
      void fetch('http://127.0.0.1:8000/api/live2d/cursor', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(value),
      }).catch(() => undefined)
    }

    if (mode === 'OFF') send({ x: 0, y: 0 })

    void listen<GlobalCursorSample>(NATIVE_CURSOR_EVENT, (event) => {
      if (disposed) return
      const pointer = globalCursorPointer(event.payload)
      onAvailability?.(pointer.available)
      const now = performance.now()
      if (!vtsActive || mode === 'OFF' || now - lastLive2DUpdate < LIVE2D_INTERVAL_MS * .9) return
      lastLive2DUpdate = now
      send({ x: pointer.available ? pointer.x : 0, y: pointer.available ? pointer.y : 0 })
    }).then((dispose) => {
      if (disposed) dispose()
      else unlisten = dispose
    }).catch(() => onAvailability?.(false))

    return () => { disposed = true; unlisten() }
  }, [mode, onAvailability, vtsActive])
}
