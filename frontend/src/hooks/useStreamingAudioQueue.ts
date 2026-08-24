import { useCallback, useMemo, useRef } from 'react'

interface Chunk { url: string; responseId: string; index: number }
type Play = (url: string, onEnd: () => void, onStart?: () => void) => Promise<void>
type Guard = (playing: boolean, responseId?: string) => Promise<void>

export function useStreamingAudioQueue(play: Play, guard: Guard, onState: (speaking: boolean) => void) {
  const queue = useRef<Chunk[]>([])
  const active = useRef(false)
  const generation = useRef(0)

  const pump = useCallback(() => {
    if (active.current) return
    const item = queue.current.shift()
    if (!item) { onState(false); void guard(false); return }
    active.current = true
    const currentGeneration = generation.current
    onState(true)
    void guard(true).then(() => play(item.url, () => {
      if (generation.current !== currentGeneration) return
      active.current = false
      pump()
    }, () => { void guard(true, item.responseId) })).catch(() => { active.current = false; pump() })
  }, [guard, onState, play])

  const enqueue = useCallback((item: Chunk) => {
    if (queue.current.some((value) => value.responseId === item.responseId && value.index === item.index)) return
    queue.current.push(item)
    queue.current.sort((a, b) => a.responseId === b.responseId ? a.index - b.index : 0)
    pump()
  }, [pump])

  const clear = useCallback(() => {
    generation.current += 1
    queue.current = []
    active.current = false
    onState(false)
    void guard(false)
  }, [guard, onState])

  const pending = useCallback(() => queue.current.length + Number(active.current), [])
  return useMemo(() => ({ enqueue, clear, pending }), [clear, enqueue, pending])
}
