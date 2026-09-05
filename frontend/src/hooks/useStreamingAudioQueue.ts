import { useCallback, useEffect, useMemo, useRef } from 'react'
import { BARGE_IN_EVENT, setOutputPlaying } from '../runtime/speechOutput'

export interface Chunk { url: string; responseId: string; index: number; sentenceIndex?: number; final?: boolean; queuedAt?: number }
export interface PlaybackAck { phase: 'started' | 'completed' | 'interrupted' | 'failed'; chunk_index: number; spoken_fraction?: number; barge_in_latency_ms?: number; audio_buffer_delay_ms?: number }
type Play = (url: string, onEnd: (completed?: boolean) => void, onStart?: () => void, onProgress?: (fraction: number) => void) => Promise<void>
type Guard = (playing: boolean, responseId?: string, ack?: PlaybackAck) => Promise<void>

export function useStreamingAudioQueue(play: Play, guard: Guard, onState: (speaking: boolean) => void, stop: () => void = () => undefined) {
  const queue = useRef<Chunk[]>([])
  const active = useRef<Chunk | null>(null)
  const generation = useRef(0)
  const fraction = useRef(0)
  const seen = useRef(new Set<string>())
  const cancelled = useRef(new Set<string>())
  const callbacks = useRef({ play, guard, onState, stop }); callbacks.current = { play, guard, onState, stop }

  const pump = useCallback(() => {
    if (active.current) return
    const item = queue.current.shift()
    const cb = callbacks.current
    if (!item) { cb.onState(false); setOutputPlaying(false); void cb.guard(false); return }
    active.current = item; fraction.current = 0
    const currentGeneration = generation.current
    cb.onState(true)
    let settled = false
    const finish = (completed = true) => {
      if (settled || generation.current !== currentGeneration) return
      settled = true
      if (!completed) {
        // A failed PCM packet invalidates the sentence even if an end marker
        // is already queued. Never record missing audio as fully heard.
        cancelled.current.add(item.responseId)
        if (cancelled.current.size > 100) cancelled.current.delete(cancelled.current.values().next().value!)
        queue.current = queue.current.filter(value => value.responseId !== item.responseId)
        void cb.guard(false, item.responseId, { phase: 'failed', chunk_index: item.sentenceIndex ?? item.index })
      } else if (item.final !== false) {
        void cb.guard(false, item.responseId, { phase: 'completed', chunk_index: item.sentenceIndex ?? item.index })
      }
      active.current = null
      pump()
    }
    void cb.play(item.url, finish, () => {
      if (settled) return
      if (generation.current !== currentGeneration) { cb.stop(); return }
      setOutputPlaying(true)
      void cb.guard(true, item.responseId, { phase: 'started', chunk_index: item.sentenceIndex ?? item.index,
        audio_buffer_delay_ms: Math.max(0, performance.now() - (item.queuedAt ?? performance.now())) })
    }, (value) => { if (!settled && generation.current === currentGeneration) fraction.current = value }).catch(() => finish(false))
  }, [])

  const enqueue = useCallback((item: Chunk) => {
    const key = `${item.responseId}:${item.index}`
    if (seen.current.has(key) || cancelled.current.has(item.responseId)) return
    if (queue.current.length >= 48) {
      // Missing a packet invalidates the whole sentence; never acknowledge its
      // end marker as fully heard after silently dropping earlier audio.
      cancelled.current.add(item.responseId)
      queue.current = queue.current.filter((value) => value.responseId !== item.responseId)
      if (active.current?.responseId === item.responseId) {
        generation.current += 1; callbacks.current.stop(); active.current = null
        setOutputPlaying(false); callbacks.current.onState(false)
      }
      void callbacks.current.guard(false, item.responseId, {phase: 'failed', chunk_index: item.sentenceIndex ?? item.index})
      return
    }
    seen.current.add(key)
    if (seen.current.size > 512) seen.current.delete(seen.current.values().next().value!)
    queue.current.push({ ...item, queuedAt: performance.now() })
    queue.current.sort((a, b) => a.responseId === b.responseId ? a.index - b.index : 0)
    pump()
  }, [pump])

  const clear = useCallback((responseId?: string, detectedAt?: number) => {
    const cb = callbacks.current
    const item = active.current
    if (responseId) {
      cancelled.current.add(responseId)
      if (cancelled.current.size > 100) cancelled.current.delete(cancelled.current.values().next().value!)
    }
    queue.current = responseId ? queue.current.filter((value) => value.responseId !== responseId) : []
    if (item && (!responseId || item.responseId === responseId)) {
      generation.current += 1
      cb.stop()
      active.current = null; setOutputPlaying(false)
      void cb.guard(false, item.responseId, { phase: 'interrupted', chunk_index: item.sentenceIndex ?? item.index,
        spoken_fraction: fraction.current, barge_in_latency_ms: detectedAt === undefined ? undefined : performance.now() - detectedAt })
      cb.onState(false)
    }
  }, [])
  useEffect(() => {
    const interrupt = (event: Event) => {
      if (active.current) clear(active.current.responseId, (event as CustomEvent).detail.detectedAt)
    }
    window.addEventListener(BARGE_IN_EVENT, interrupt)
    return () => { window.removeEventListener(BARGE_IN_EVENT, interrupt); clear() }
  }, [clear])

  const pending = useCallback(() => queue.current.length + Number(Boolean(active.current)), [])
  return useMemo(() => ({ enqueue, clear, pending }), [clear, enqueue, pending])
}
