import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { useStreamingAudioQueue } from './useStreamingAudioQueue'

describe('single scoped playback coordinator', () => {
  it.each(['callback', 'rejection'])('cannot acknowledge an end marker after PCM failure via %s', async (failure) => {
    let failPacket!: (completed?: boolean) => void
    let rejectPacket!: (reason: Error) => void
    const play = vi.fn((_url: string, end: (completed?: boolean) => void) => {
      failPacket = end
      return new Promise<void>((_resolve, reject) => { rejectPacket = reject })
    })
    const guard = vi.fn(async (_playing: boolean, _responseId?: string, _ack?: { phase?: string }) => undefined)
    let queue!: ReturnType<typeof useStreamingAudioQueue>
    function Capture() { queue = useStreamingAudioQueue(play, guard, () => undefined); return null }
    renderToString(createElement(Capture))
    queue.enqueue({url: 'pcm16:24000:packet', responseId: 'remote', index: 0, sentenceIndex: 0, final: false})
    queue.enqueue({url: 'pcm16:24000:', responseId: 'remote', index: 1, sentenceIndex: 0, final: true})
    if (failure === 'callback') failPacket(false)
    else { rejectPacket(new Error('controlled player failure')); await Promise.resolve(); await Promise.resolve() }
    failPacket(true) // A late completion cannot turn the failure into success.
    queue.enqueue({url: 'pcm16:24000:', responseId: 'remote', index: 2, sentenceIndex: 0, final: true})
    expect(queue.pending()).toBe(0)
    expect(play).toHaveBeenCalledTimes(1)
    expect(guard).toHaveBeenCalledWith(false, 'remote', {phase: 'failed', chunk_index: 0})
    expect(guard.mock.calls.some(call => (call[2] as { phase?: string } | undefined)?.phase === 'completed')).toBe(false)
    queue.clear()
  })

  it('stops only the interrupted response, rejects duplicates and late callbacks', async () => {
    const endings: Array<(completed?: boolean) => void> = []
    const play = vi.fn(async (_url: string, end: (completed?: boolean) => void, start?: () => void) => { endings.push(end); start?.() })
    const guard = vi.fn(async () => undefined)
    const stop = vi.fn()
    let queue!: ReturnType<typeof useStreamingAudioQueue>
    function Capture() { queue = useStreamingAudioQueue(play, guard, () => undefined, stop); return null }
    renderToString(createElement(Capture))
    queue.enqueue({url: 'first', responseId: 'a', index: 0})
    queue.enqueue({url: 'first', responseId: 'a', index: 0})
    queue.enqueue({url: 'obsolete', responseId: 'a', index: 1})
    queue.enqueue({url: 'notification', responseId: 'independent', index: 0})
    expect(play).toHaveBeenCalledTimes(1)
    queue.clear('a')
    expect(stop).toHaveBeenCalledTimes(1)
    expect(queue.pending()).toBe(1)
    endings[0](true)
    expect(play).toHaveBeenCalledTimes(1)
    queue.enqueue({url: 'late', responseId: 'a', index: 2})
    expect(queue.pending()).toBe(1)
    queue.enqueue({url: 'new', responseId: 'b', index: 0})
    expect(play).toHaveBeenCalledTimes(2)
    expect(play.mock.calls[1][0]).toBe('notification')
    queue.clear('a')
    expect(stop).toHaveBeenCalledTimes(1)
    endings[1](true)
    expect(play.mock.calls[2][0]).toBe('new')
    queue.clear()
  })
})
