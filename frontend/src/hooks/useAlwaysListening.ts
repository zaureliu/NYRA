import { useCallback, useEffect, useRef, useState } from 'react'
import { audioInputs, microphoneErrorState, selectMicrophoneDevice, type MicrophoneAvailability, type MicrophonePermission } from './audioDevices'

export interface ListeningConfig {
  enabled: boolean
  mode: 'push_to_talk' | 'wake_word' | 'hands_free'
  wake_word: string
  hands_free_timeout_seconds: number
  vad_threshold: number
  energy_threshold: number
  preroll_ms: number
  postroll_ms: number
  speech_start_ms: number
  max_utterance_seconds: number
  guard_ms: number
  microphone: string
  barge_in: boolean
  audio_debug: boolean
  privacy_indicator: boolean
}

export interface ListeningStatus {
  enabled: boolean
  muted: boolean
  microphone: boolean
  processing: boolean
  speaking_guard: boolean
  mode: string
  wake_word: string
  hands_free_active: boolean
  hands_free_remaining_seconds: number
  privacy_indicator: boolean
  lease_active: boolean
}

export interface AlwaysListeningResult {
  accepted: boolean
  reason: string
  decision?: { text: string; hands_free_active: boolean; wake_word_detected: boolean }
  chat?: { response: string; state: string; audio_url: string | null; audio_urls?: string[]; response_id?: string | null }
}

interface Options {
  baseUrl?: string
  deviceId?: string
  suspended?: boolean
  outputPlaying?: boolean
  onResult?: (result: AlwaysListeningResult) => void | Promise<void>
  onError?: (message: string) => void
  onDeviceSelected?: (deviceId: string) => void
}

interface AudioChunk { samples: Float32Array; capturedAt: number }

const createClientId = () => {
  const key = 'nyra-listening-client-id'
  const existing = sessionStorage.getItem(key)
  if (existing) return existing
  const value = `client_${crypto.randomUUID().replaceAll('-', '')}`
  sessionStorage.setItem(key, value)
  return value
}

export function encodeWav(chunks: Float32Array[], sampleRate: number): Blob {
  const length = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
  const buffer = new ArrayBuffer(44 + length * 2)
  const view = new DataView(buffer)
  const write = (offset: number, value: string) => [...value].forEach((character, index) => view.setUint8(offset + index, character.charCodeAt(0)))
  write(0, 'RIFF'); view.setUint32(4, 36 + length * 2, true); write(8, 'WAVE'); write(12, 'fmt ')
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); write(36, 'data'); view.setUint32(40, length * 2, true)
  let offset = 44
  for (const chunk of chunks) for (const sample of chunk) { const clamped = Math.max(-1, Math.min(1, sample)); view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true); offset += 2 }
  return new Blob([buffer], { type: 'audio/wav' })
}

export function useAlwaysListening({ baseUrl = '', deviceId = 'default', suspended = false, outputPlaying = false, onResult, onError, onDeviceSelected }: Options = {}) {
  const [config, setConfig] = useState<ListeningConfig | null>(null)
  const [status, setStatus] = useState<ListeningStatus | null>(null)
  const [listening, setListening] = useState(false)
  const [processing, setProcessing] = useState(false)
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([])
  const [micActive, setMicActive] = useState(false)
  const [microphoneAvailability, setMicrophoneAvailability] = useState<MicrophoneAvailability>('checking')
  const [microphonePermission, setMicrophonePermission] = useState<MicrophonePermission>('unknown')
  const [deviceRevision, setDeviceRevision] = useState(0)
  const permissionRef = useRef<MicrophonePermission>(microphonePermission); permissionRef.current = microphonePermission
  const clientId = useRef(createClientId())
  const configRef = useRef(config); configRef.current = config
  const statusRef = useRef(status); statusRef.current = status
  const suspendedRef = useRef(suspended); suspendedRef.current = suspended
  const outputPlayingRef = useRef(outputPlaying); outputPlayingRef.current = outputPlaying
  const resultRef = useRef(onResult); resultRef.current = onResult
  const errorRef = useRef(onError); errorRef.current = onError
  const deviceSelectedRef = useRef(onDeviceSelected); deviceSelectedRef.current = onDeviceSelected
  const streamRef = useRef<MediaStream | null>(null)
  const contextRef = useRef<AudioContext | null>(null)
  const processorRef = useRef<ScriptProcessorNode | null>(null)
  const ringRef = useRef<AudioChunk[]>([])
  const utteranceRef = useRef<Float32Array[]>([])
  const speakingRef = useRef(false)
  const aboveSinceRef = useRef(0)
  const quietSinceRef = useRef(0)
  const speechStartedAtRef = useRef(0)
  const postingRef = useRef(false)
  const captureBlockedRef = useRef(false)
  const lastCaptureErrorRef = useRef('')

  const refreshDevices = useCallback(async () => {
    if (!navigator.mediaDevices?.enumerateDevices) {
      setDevices([]); setMicrophoneAvailability('unsupported')
      return [] as MediaDeviceInfo[]
    }
    try {
      const listed = await navigator.mediaDevices.enumerateDevices()
      setDevices(listed)
      const selected = selectMicrophoneDevice(listed, deviceId)
      if (!selected) setMicrophoneAvailability('unavailable')
      else setMicrophoneAvailability((current) => current === 'denied' ? current : 'available')
      if (selected && selected !== deviceId) deviceSelectedRef.current?.(selected)
      return audioInputs(listed)
    } catch {
      setDevices([]); setMicrophoneAvailability('error')
      return [] as MediaDeviceInfo[]
    }
  }, [deviceId])

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${baseUrl}/api/listening/settings`)
      if (!response.ok) return
      const value = await response.json()
      configRef.current = value.settings; statusRef.current = value.status
      setConfig(value.settings); setStatus(value.status)
      return value as { settings: ListeningConfig; status: ListeningStatus }
    } catch { /* backend may still be starting */ }
  }, [baseUrl])

  const submit = useCallback(async (chunks: Float32Array[], sampleRate: number) => {
    if (!chunks.length || postingRef.current) return
    postingRef.current = true; setProcessing(true)
    try {
      const audio = encodeWav(chunks, sampleRate)
      const body = new FormData(); body.append('client_id', clientId.current); body.append('audio', audio, 'always-listening.wav')
      const response = await fetch(`${baseUrl}/api/listening/utterance`, { method: 'POST', body })
      const value: AlwaysListeningResult = await response.json()
      if (!response.ok) throw new Error((value as unknown as { detail?: string }).detail ?? 'Falha na escuta local')
      await resultRef.current?.(value)
    } catch (error) {
      errorRef.current?.(error instanceof Error ? error.message : 'Microfone indisponível')
    } finally {
      postingRef.current = false; setProcessing(false); setListening(false)
    }
  }, [baseUrl])

  const stopCapture = useCallback(() => {
    processorRef.current?.disconnect(); processorRef.current = null
    streamRef.current?.getTracks().forEach((track) => track.stop()); streamRef.current = null
    if (contextRef.current) void contextRef.current.close(); contextRef.current = null
    ringRef.current = []; utteranceRef.current = []; speakingRef.current = false; speechStartedAtRef.current = 0
    setListening(false); setMicActive(false)
  }, [])

  const startCapture = useCallback(async () => {
    if (streamRef.current || !configRef.current || captureBlockedRef.current) return
    if (!navigator.mediaDevices?.getUserMedia) {
      setMicrophoneAvailability('unsupported'); return
    }
    try {
      const selected = deviceId || configRef.current.microphone || 'default'
      const baseConstraints: MediaTrackConstraints = {
        echoCancellation: true, noiseSuppression: true, autoGainControl: true,
        channelCount: 1, sampleRate: 48000,
      }
      const request = (requested: string) => navigator.mediaDevices.getUserMedia({
        audio: { ...baseConstraints, ...(requested === 'default' ? {} : { deviceId: { exact: requested } }) },
      })
      let stream: MediaStream
      try { stream = await request(selected) }
      catch (error) {
        const failure = microphoneErrorState(error)
        if (selected === 'default' || !failure.retryOnDeviceChange) throw error
        stream = await request('default')
        deviceSelectedRef.current?.('default')
      }
      const context = new AudioContext({ sampleRate: 48000 })
      const source = context.createMediaStreamSource(stream)
      const processor = context.createScriptProcessor(4096, 1, 1)
      const silent = context.createGain(); silent.gain.value = 0
      source.connect(processor); processor.connect(silent); silent.connect(context.destination)
      streamRef.current = stream; contextRef.current = context; processorRef.current = processor
      captureBlockedRef.current = false; lastCaptureErrorRef.current = ''
      setMicActive(true); setMicrophoneAvailability('available'); setMicrophonePermission('granted')
      for (const track of stream.getAudioTracks()) track.addEventListener('ended', () => {
        stopCapture(); captureBlockedRef.current = false; setDeviceRevision((value) => value + 1)
      }, { once: true })
      void refreshDevices()
      processor.onaudioprocess = (event) => {
        const currentConfig = configRef.current
        if (!currentConfig) return
        const samples = new Float32Array(event.inputBuffer.getChannelData(0))
        const now = performance.now()
        const playbackActive = outputPlayingRef.current || suspendedRef.current || Boolean(statusRef.current?.speaking_guard)
        const selfVoiceBlocked = playbackActive && !currentConfig.barge_in
        if (selfVoiceBlocked) {
          ringRef.current = []; utteranceRef.current = []; speakingRef.current = false; setListening(false); return
        }
        let power = 0
        for (const sample of samples) power += sample * sample
        const rms = Math.sqrt(power / samples.length)
        const duringPlayback = playbackActive
        const speechThreshold = duringPlayback && currentConfig.barge_in
          ? Math.max(.04, currentConfig.energy_threshold * 2.5)
          : currentConfig.energy_threshold
        const speechStartMs = duringPlayback && currentConfig.barge_in
          ? Math.max(180, currentConfig.speech_start_ms)
          : currentConfig.speech_start_ms
        const chunk = { samples, capturedAt: now }
        ringRef.current.push(chunk)
        ringRef.current = ringRef.current.filter((item) => now - item.capturedAt <= currentConfig.preroll_ms)
        if (speakingRef.current) utteranceRef.current.push(samples)
        if (rms >= speechThreshold) {
          quietSinceRef.current = 0
          if (!aboveSinceRef.current) aboveSinceRef.current = now
          if (!speakingRef.current && now - aboveSinceRef.current >= speechStartMs) {
            speakingRef.current = true; utteranceRef.current = ringRef.current.map((item) => item.samples); setListening(true)
            speechStartedAtRef.current = now
            void fetch(`${baseUrl}/api/listening/speech-start`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({client_id:clientId.current}) })
          }
        } else {
          aboveSinceRef.current = 0
          if (speakingRef.current && !quietSinceRef.current) quietSinceRef.current = now
          if (speakingRef.current && quietSinceRef.current && now - quietSinceRef.current >= currentConfig.postroll_ms) {
            const completed = utteranceRef.current
            utteranceRef.current = []; ringRef.current = []; speakingRef.current = false; quietSinceRef.current = 0; speechStartedAtRef.current = 0
            void submit(completed, context.sampleRate)
          }
        }
        if (speakingRef.current && speechStartedAtRef.current && now - speechStartedAtRef.current >= currentConfig.max_utterance_seconds * 1000) {
          const completed = utteranceRef.current
          utteranceRef.current = []; ringRef.current = []; speakingRef.current = false; quietSinceRef.current = 0; speechStartedAtRef.current = 0
          void submit(completed, context.sampleRate)
        }
      }
    } catch (error) {
      stopCapture()
      const failure = microphoneErrorState(error)
      setMicrophoneAvailability(failure.availability); setMicrophonePermission(failure.permission)
      captureBlockedRef.current = true
      const message = error instanceof Error ? error.message : 'Microfone indisponível'
      if (lastCaptureErrorRef.current !== message) { lastCaptureErrorRef.current = message; errorRef.current?.(message) }
    }
  }, [deviceId, refreshDevices, stopCapture, submit])

  useEffect(() => {
    let disposed = false
    let permission: PermissionStatus | undefined
    let updatePermission: (() => void) | undefined
    const inspectPermission = async () => {
      if (!navigator.permissions?.query) return
      try {
        permission = await navigator.permissions.query({ name: 'microphone' } as PermissionDescriptor)
        if (disposed) return
        updatePermission = () => {
          const next = permission?.state ?? 'prompt'
          setMicrophonePermission(next)
          if (next !== 'denied') { captureBlockedRef.current = false; setDeviceRevision((value) => value + 1) }
        }
        updatePermission(); permission.addEventListener('change', updatePermission)
      } catch { /* Permissions API may not expose microphone in every WebView. */ }
    }
    const onDeviceChange = () => {
      stopCapture()
      if (permissionRef.current !== 'denied') captureBlockedRef.current = false
      void refreshDevices().finally(() => setDeviceRevision((value) => value + 1))
    }
    void refreshDevices(); void inspectPermission()
    navigator.mediaDevices?.addEventListener?.('devicechange', onDeviceChange)
    return () => {
      disposed = true
      if (updatePermission) permission?.removeEventListener('change', updatePermission)
      navigator.mediaDevices?.removeEventListener?.('devicechange', onDeviceChange)
    }
  }, [refreshDevices, stopCapture])

  useEffect(() => {
    if (microphonePermission !== 'denied') captureBlockedRef.current = false
    setDeviceRevision((value) => value + 1)
  }, [deviceId, microphonePermission])

  useEffect(() => {
    let disposed = false
    const sync = async () => {
      const snapshot = await refresh()
      const current = snapshot?.settings ?? configRef.current
      if (!current?.enabled || statusRef.current?.muted) { stopCapture(); return }
      try {
        const response = await fetch(`${baseUrl}/api/listening/lease`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: clientId.current }) })
        const value = await response.json(); if (disposed) return
        statusRef.current = value.status; setStatus(value.status)
        if (value.acquired) await startCapture(); else stopCapture()
      } catch { stopCapture() }
    }
    void sync(); const timer = window.setInterval(() => void sync(), 5000)
    return () => { disposed = true; clearInterval(timer); stopCapture() }
  }, [baseUrl, deviceRevision, refresh, startCapture, stopCapture])

  return { config, status, listening, processing, micActive, devices, microphoneAvailability, microphonePermission, refresh, refreshDevices }
}
