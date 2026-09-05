import { useEffect, useRef, useState } from 'react'
import type { InputMetrics } from '../types'
import { STTStream, type STTEvent, type STTResult } from '../runtime/stt'
import { acquireMicrophone, setManualCapture } from '../runtime/microphoneOwnership'

const EMPTY_METRICS: InputMetrics = { rms: 0, peak: 0, clipping: false, speechDetected: false, durationMs: 0 }
interface Options { mode?: 'direct' | 'diagnostic'; benchmark?: boolean; reference?: string; onEvent?: (event: STTEvent) => void }

export function usePushToTalk(
  onResult: (result: STTResult) => Promise<void>, deviceId = 'default', silenceThreshold = 0.018,
  silenceDurationMs = 1400, onError?: (message: string) => void, options: Options = {},
) {
  const [recording, setRecording] = useState(false)
  const [metrics, setMetrics] = useState<InputMetrics>(EMPTY_METRICS)
  const [capabilities, setCapabilities] = useState({ echoCancellation: false, noiseSuppression: false, autoGainControl: false })
  const capture = useRef<{ stream: MediaStream; context: AudioContext; processor: ScriptProcessorNode; stt: STTStream; release: () => void } | null>(null)
  const starting = useRef(false)
  const captureOwner = useRef(crypto.randomUUID())
  const released = useRef(false)
  const disposed = useRef(false)
  const pending = useRef<STTStream | null>(null)
  const callbacks = useRef({ onResult, onError, options }); callbacks.current = { onResult, onError, options }

  const releaseDevice = () => {
    const current = capture.current
    capture.current = null
    if (current) {
      current.processor.onaudioprocess = null; current.processor.disconnect()
      current.stream.getTracks().forEach((track) => track.stop())
      void current.context.close(); current.release()
    }
    setManualCapture(false, captureOwner.current)
    if (!disposed.current) setRecording(false)
    return current
  }

  const stop = () => {
    released.current = true
    const current = releaseDevice()
    if (!current) return
    pending.current = current.stt
    void current.stt.finish().then((value) => callbacks.current.onResult(value))
      .catch((error) => { if (!disposed.current) callbacks.current.onError?.(error instanceof Error ? error.message : 'Falha no áudio') })
      .finally(() => { pending.current = null })
  }

  const start = async () => {
    if (starting.current || capture.current || pending.current) return
    starting.current = true; released.current = false; setManualCapture(true, captureOwner.current)
    let release: (() => void) | null = null
    let stream: MediaStream | null = null
    let context: AudioContext | null = null
    try {
      await new Promise((resolve) => setTimeout(resolve, 100))
      release = await acquireMicrophone()
      if (!release) throw new Error('Microfone em uso por outra janela NYRA')
      if (released.current || disposed.current) { release(); setManualCapture(false, captureOwner.current); return }
      const audio: MediaTrackConstraints = { echoCancellation: true, noiseSuppression: true, autoGainControl: true,
        channelCount: 1, sampleRate: 48000, ...(deviceId === 'default' ? {} : { deviceId: { exact: deviceId } }) }
      stream = await navigator.mediaDevices.getUserMedia({ audio })
      if (released.current || disposed.current) { stream.getTracks().forEach((track) => track.stop()); release(); setManualCapture(false, captureOwner.current); return }
      const settings = stream.getAudioTracks()[0].getSettings()
      setCapabilities({ echoCancellation: settings.echoCancellation === true, noiseSuppression: settings.noiseSuppression === true, autoGainControl: settings.autoGainControl === true })
      context = new AudioContext({ sampleRate: 48000 })
      const source = context.createMediaStreamSource(stream)
      const processor = context.createScriptProcessor(4096, 1, 1)
      const silent = context.createGain(); silent.gain.value = 0
      source.connect(processor); processor.connect(silent); silent.connect(context.destination)
      const stt = new STTStream({ mode: callbacks.current.options.mode ?? 'direct', sampleRate: context.sampleRate,
        benchmark: callbacks.current.options.benchmark, reference: callbacks.current.options.reference,
        onEvent: (event) => callbacks.current.options.onEvent?.(event) })
      capture.current = { stream, context, processor, stt, release }
      const startedAt = performance.now()
      let heardVoice = false; let quietSince = startedAt
      setRecording(true); setMetrics(EMPTY_METRICS)
      stream.getTracks().forEach((track) => track.addEventListener('ended', stop, { once: true }))
      processor.onaudioprocess = (event) => {
        const samples = event.inputBuffer.getChannelData(0)
        stt.sendSamples(samples)
        let power = 0; let peak = 0
        for (const sample of samples) { power += sample * sample; peak = Math.max(peak, Math.abs(sample)) }
        const now = performance.now(); const rms = Math.sqrt(power / samples.length)
        if (rms > silenceThreshold) { heardVoice = true; quietSince = now }
        setMetrics({ rms, peak, clipping: peak > .985, speechDetected: rms > silenceThreshold, durationMs: now - startedAt })
        if ((heardVoice && now - quietSince > silenceDurationMs) || now - startedAt >= 59000) stop()
      }
    } catch (error) {
      stream?.getTracks().forEach((track) => track.stop())
      if (context) void context.close()
      release?.(); setManualCapture(false, captureOwner.current)
      if (!disposed.current) callbacks.current.onError?.(error instanceof Error ? error.message : 'Microfone indisponível')
    } finally { starting.current = false }
  }

  useEffect(() => {
    disposed.current = false
    return () => {
      disposed.current = true; released.current = true
      const current = releaseDevice(); current?.stt.cancel(); pending.current?.cancel()
    }
  }, [])

  return { recording, start, stop, metrics, capabilities }
}
