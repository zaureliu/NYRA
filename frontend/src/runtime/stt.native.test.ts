import { afterEach, describe, expect, it, vi } from 'vitest'
import { invoke } from '@tauri-apps/api/core'
import { STTStream } from './stt'

const channels = vi.hoisted(() => ({ current: null as null | { onmessage: (value: unknown) => void } }))
vi.mock('./api', () => ({ apiSend: vi.fn().mockResolvedValue({ ticket: 'local-ticket' }) }))
vi.mock('./backend', () => ({ BACKEND_ORIGIN: 'http://127.0.0.1:8000', isTauriRuntime: () => true }))
vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn().mockResolvedValue(undefined),
  Channel: class { onmessage = (_value: unknown) => {}; constructor() { channels.current = this } },
}))
afterEach(() => vi.clearAllMocks())

describe('packaged STT native transport', () => {
  it('uses the fixed native audio bridge with ordered frames and closes after final', async () => {
    const stream = new STTStream({ mode: 'diagnostic', sampleRate: 48000 })
    stream.sendSamples(new Float32Array([0, 1]))
    await Promise.resolve(); await Promise.resolve()
    expect(invoke).toHaveBeenCalledWith('stt_stream_open', expect.objectContaining({ ticket: 'local-ticket' }))
    channels.current?.onmessage({ type: 'ready', utterance_end_ms: 1000 })
    expect(stream.utteranceEndMs).toBe(1000)
    stream.sendSamples(new Float32Array([-1, 0]))
    const result = stream.finish()
    for (let i = 0; i < 12; i++) await Promise.resolve()
    const frames = vi.mocked(invoke).mock.calls.filter(([command]) => command === 'stt_stream_audio').map(([, args]) => args)
    expect(frames).toHaveLength(3)
    expect(frames[0]).toEqual(expect.objectContaining({ audio: [0, 0, 255, 127], end: false }))
    expect(frames[1]).toEqual(expect.objectContaining({ audio: [0, 128, 0, 0], end: false }))
    expect(frames[2]).toEqual(expect.objectContaining({ audio: [], end: true }))
    channels.current?.onmessage({ type: 'result', result: { accepted: true } })
    expect((await result).accepted).toBe(true)
    expect(invoke).toHaveBeenCalledWith('stt_stream_close', expect.anything())
  })
})
