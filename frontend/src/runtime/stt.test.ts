import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiSend } from './api'
import { STTStream, pcm16, sttSocketUrl } from './stt'

vi.mock('./api', () => ({ apiSend: vi.fn() }))
vi.mock('./backend', () => ({ BACKEND_ORIGIN: 'http://127.0.0.1:8000', isTauriRuntime: () => false }))

class Socket {
  static last: Socket
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  sent: unknown[] = []
  bufferedAmount = 0
  closed = false
  constructor(public url: string) { Socket.last = this }
  send(value: unknown) { this.sent.push(value) }
  close() { this.closed = true; this.onclose?.() }
  message(value: unknown) { this.onmessage?.({ data: JSON.stringify(value) }) }
}

function setup() {
  vi.stubGlobal('window', { location: { origin: 'http://127.0.0.1:5173' } })
  vi.stubGlobal('WebSocket', Socket)
  vi.mocked(apiSend).mockResolvedValue({ ticket: 'one-use-local-ticket' })
}
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks() })

describe('STT audio transport', () => {
  it('encodes little-endian PCM without resampling or a WAV container', () => {
    const audio = pcm16(new Float32Array([0, 1, -1, .5]))
    expect(audio.byteLength).toBe(8)
    const view = new DataView(audio)
    expect(view.getInt16(2, true)).toBe(32767)
    expect(view.getInt16(4, true)).toBe(-32768)
    expect(view.getInt16(6, true)).toBe(16383)
  })

  it('sends queued audio while capture is active; ticket never appears in URL', async () => {
    setup()
    const onEvent = vi.fn()
    const stream = new STTStream({ mode: 'direct', sampleRate: 48000, onEvent })
    stream.sendSamples(new Float32Array([.1, .2]))
    await Promise.resolve(); await Promise.resolve()
    const socket = Socket.last
    expect(sttSocketUrl()).toBe('ws://127.0.0.1:5173/api/stt/stream')
    socket.onopen?.()
    expect(socket.url).not.toContain('ticket')
    expect(socket.sent[0]).toBe('{"ticket":"one-use-local-ticket"}')
    socket.message({ type: 'ready' })
    expect(socket.sent[1]).toBeInstanceOf(ArrayBuffer)
    socket.message({ type: 'interim', transcript: { text: 'conectei', is_final: false } })
    expect(onEvent).toHaveBeenCalledOnce()
    stream.sendSamples(new Float32Array([.3]))
    expect(socket.sent[2]).toBeInstanceOf(ArrayBuffer)
    const result = stream.finish()
    expect(socket.sent[3]).toBe('{"type":"end"}')
    socket.message({ type: 'result', result: { accepted: true, transcription: { text: 'Conectei um ESP32.', is_final: true } } })
    expect((await result).transcription?.text).toBe('Conectei um ESP32.')
    expect(socket.closed).toBe(true)
  })

  it('bounds the pending microphone queue when the bridge never connects', async () => {
    setup()
    vi.mocked(apiSend).mockImplementation(() => new Promise(() => undefined))
    const stream = new STTStream({ mode: 'diagnostic', sampleRate: 48000 })
    for (let index = 0; index < 34; index++) stream.sendSamples(new Float32Array(4096))
    await expect(stream.finish()).rejects.toThrow('não respondeu a tempo')
  })

  it('does not retry or duplicate a turn after an ambiguous local disconnect', async () => {
    setup()
    const stream = new STTStream({ mode: 'direct', sampleRate: 48000 })
    await Promise.resolve(); await Promise.resolve()
    Socket.last.message({ type: 'ready' })
    stream.sendSamples(new Float32Array(4096))
    const result = stream.finish()
    Socket.last.close()
    await expect(result).rejects.toThrow('antes do resultado')
    expect(apiSend).toHaveBeenCalledOnce()
  })

  it('cancels capture before a ticket response without opening a stale socket', async () => {
    setup()
    const stream = new STTStream({ mode: 'diagnostic', sampleRate: 48000 })
    stream.cancel()
    await expect(stream.finish()).rejects.toThrow('cancelado')
  })
})
