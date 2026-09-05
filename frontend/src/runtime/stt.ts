import { apiSend } from './api'
import { BACKEND_ORIGIN, isTauriRuntime } from './backend'
import { Channel, invoke } from '@tauri-apps/api/core'

export interface CanonicalTranscript {
  text: string; is_final: boolean; speech_final: boolean; provider: string; language: string
  utterance_id: string; sequence: number; started_at: number; ended_at: number
}

export interface STTResult {
  accepted: boolean; reason?: string
  transcription?: CanonicalTranscript
  decision?: { text: string; hands_free_active: boolean; wake_word_detected: boolean }
  chat?: { response: string; state: string; audio_url: string | null; audio_urls?: string[]; response_id?: string | null }
  diagnostics?: Record<string, unknown>
  comparison?: Record<string, unknown> | null
}

export interface STTEvent { type: string; transcript?: CanonicalTranscript; state?: string }
export interface STTStreamOptions {
  mode: 'listening' | 'direct' | 'diagnostic'
  clientId?: string
  sampleRate: number
  micStartedAt?: number
  benchmark?: boolean
  reference?: string
  onEvent?: (event: STTEvent) => void
}

export function pcm16(samples: Float32Array): ArrayBuffer {
  const result = new ArrayBuffer(samples.length * 2)
  const view = new DataView(result)
  samples.forEach((sample, index) => {
    const clamped = Math.max(-1, Math.min(1, sample))
    view.setInt16(index * 2, clamped < 0 ? clamped * 32768 : clamped * 32767, true)
  })
  return result
}

export function sttSocketUrl(): string {
  const base = isTauriRuntime() ? BACKEND_ORIGIN : window.location.origin
  const url = new URL('/api/stt/stream', base)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}

export class STTStream {
  utteranceEndMs = 0
  utteranceEnded = false
  private socket: WebSocket | null = null
  private nativeId: string | null = null
  private nativeQueue: Promise<void> = Promise.resolve()
  private nativeBytes = 0
  private pending: ArrayBuffer[] = []
  private pendingBytes = 0
  private ready = false
  private ending = false
  private closed = false
  private resolve!: (result: STTResult) => void
  private reject!: (error: Error) => void
  private result: Promise<STTResult>
  private timer: ReturnType<typeof setTimeout>

  constructor(private options: STTStreamOptions) {
    this.result = new Promise((resolve, reject) => { this.resolve = resolve; this.reject = reject })
    void this.result.catch(() => undefined)
    this.timer = setTimeout(() => this.fail('STT stream timed out'), 75000)
    void this.connect().catch(() => this.fail('Falha no bridge local de reconhecimento'))
  }

  private async connect() {
    const { ticket } = await apiSend<{ ticket: string }>('/api/stt/ticket', 'POST', {
      mode: this.options.mode, client_id: this.options.clientId ?? '',
      audio_format: { encoding: 'linear16', sample_rate: this.options.sampleRate, channels: 1 },
      mic_started_at: this.options.micStartedAt ?? Date.now() / 1000,
      benchmark: this.options.benchmark ?? false, reference: this.options.reference ?? '',
    })
    if (this.closed) return
    if (isTauriRuntime()) {
      const channel = new Channel<Record<string, unknown>>()
      channel.onmessage = (value) => this.handleMessage(value)
      this.nativeId = crypto.randomUUID()
      await invoke('stt_stream_open', { streamId: this.nativeId, ticket, channel })
      if (this.closed) this.closeTransport()
      return
    }
    const socket = new WebSocket(sttSocketUrl())
    this.socket = socket
    socket.onopen = () => socket.send(JSON.stringify({ ticket }))
    socket.onmessage = (event) => {
      let value
      try { value = JSON.parse(String(event.data)) } catch { this.fail('Resposta STT inválida'); return }
      this.handleMessage(value)
    }
    socket.onerror = () => this.fail('Conexão STT local indisponível')
    socket.onclose = () => { if (!this.closed) this.fail('Conexão STT encerrada antes do resultado') }
  }

  private handleMessage(value: Record<string, unknown>) {
      if (this.closed) return
      if (value.type === 'ready') {
        this.ready = true
        this.utteranceEndMs = Number(value.utterance_end_ms) || 0
        for (const audio of this.pending) this.send(audio)
        this.pending = []; this.pendingBytes = 0
        if (this.ending && !this.closed) this.sendEnd()
      } else if (value.type === 'result') {
        this.closed = true; clearTimeout(this.timer)
        this.resolve(value.result as STTResult); this.closeTransport()
      } else if (value.type === 'error') this.fail(String(value.message ?? 'Falha no reconhecimento'))
      else {
        if (value.type === 'utterance_end') this.utteranceEnded = true
        if (value.type === 'speech_started') this.utteranceEnded = false
        if (value.type === 'state' && value.state === 'FALLBACK') this.utteranceEndMs = 0
        if (typeof value.type === 'string') this.options.onEvent?.({
          type: value.type, transcript: value.transcript as CanonicalTranscript | undefined,
          state: typeof value.state === 'string' ? value.state : undefined,
        })
      }
  }

  private closeTransport() {
    this.socket?.close()
    if (this.nativeId) void invoke('stt_stream_close', { streamId: this.nativeId }).catch(() => undefined)
  }

  private sendNative(audio: ArrayBuffer | null) {
    const streamId = this.nativeId
    this.nativeBytes += audio?.byteLength ?? 0
    if (this.nativeBytes > 262144) { this.fail('Fila de áudio local cheia; tente novamente'); return }
    this.nativeQueue = this.nativeQueue.then(async () => {
      if (this.closed) return
      await invoke('stt_stream_audio', { streamId, audio: audio ? Array.from(new Uint8Array(audio)) : [], end: audio === null })
      this.nativeBytes -= audio?.byteLength ?? 0
    }).catch(() => this.fail('Falha no streaming de áudio local'))
  }

  private sendEnd() {
    if (this.nativeId) this.sendNative(null)
    else this.socket?.send('{"type":"end"}')
  }

  private send(audio: ArrayBuffer) {
    if (this.closed) return
    if (this.nativeId) { this.sendNative(audio); return }
    if (!this.socket || this.socket.bufferedAmount > 262144) {
      this.fail('Fila de áudio local cheia; tente novamente'); return
    }
    this.socket.send(audio)
  }

  sendSamples(samples: Float32Array) {
    if (this.closed || this.ending) return
    const audio = pcm16(samples)
    if (this.ready) this.send(audio)
    else {
      this.pendingBytes += audio.byteLength
      if (this.pendingBytes > 262144) { this.fail('Bridge STT não respondeu a tempo'); return }
      this.pending.push(audio)
    }
  }

  finish(): Promise<STTResult> {
    if (!this.ending && !this.closed) {
      this.ending = true
      clearTimeout(this.timer)
      this.timer = setTimeout(() => this.fail('Reconhecimento excedeu o tempo limite'), 180000)
      if (this.ready) this.sendEnd()
    }
    return this.result
  }

  cancel() { this.fail('Reconhecimento cancelado') }

  private fail(message: string) {
    if (this.closed) return
    this.closed = true; clearTimeout(this.timer)
    this.pending = []; this.pendingBytes = 0
    this.closeTransport(); this.reject(new Error(message))
  }
}
