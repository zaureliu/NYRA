import { useRef, useState } from 'react'
import type { InputMetrics } from '../types'

const EMPTY_METRICS: InputMetrics = { rms: 0, peak: 0, clipping: false, speechDetected: false, durationMs: 0 }

export function usePushToTalk(
  onAudio: (blob: Blob) => Promise<void>, deviceId = 'default', silenceThreshold = 0.018,
  silenceDurationMs = 1400, onError?: (message: string) => void,
) {
  const [recording, setRecording] = useState(false)
  const [metrics, setMetrics] = useState<InputMetrics>(EMPTY_METRICS)
  const [capabilities, setCapabilities] = useState({ echoCancellation: false, noiseSuppression: false, autoGainControl: false })
  const recorderRef = useRef<MediaRecorder | null>(null)
  const rafRef = useRef<number | null>(null)
  const stop = () => { if (recorderRef.current?.state === 'recording') recorderRef.current.stop() }
  const start = async () => {
    if (recording) return
    try {
      const audio: MediaTrackConstraints = { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1, sampleRate: 48000, ...(deviceId === 'default' ? {} : { deviceId: { exact: deviceId } }) }
      const stream = await navigator.mediaDevices.getUserMedia({ audio })
      const settings = stream.getAudioTracks()[0].getSettings() as MediaTrackSettings & Record<string, unknown>
      setCapabilities({ echoCancellation: settings.echoCancellation === true, noiseSuppression: settings.noiseSuppression === true, autoGainControl: settings.autoGainControl === true })
      const chunks: Blob[] = []; const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : 'audio/webm'
      const recorder = new MediaRecorder(stream, { mimeType, audioBitsPerSecond: 96000 }); recorderRef.current = recorder
      const context = new AudioContext({ sampleRate: 48000 }); const startedAt = performance.now()
      recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data) }
      recorder.onstop = async () => {
        setRecording(false); stream.getTracks().forEach((item) => item.stop()); if (rafRef.current) cancelAnimationFrame(rafRef.current); await context.close()
        const blob = new Blob(chunks, { type: mimeType }); if (blob.size > 100) try { await onAudio(blob) } catch (error) { onError?.(error instanceof Error ? error.message : 'Falha no áudio') }
      }
      recorder.start(100); setRecording(true); setMetrics(EMPTY_METRICS)
      const source = context.createMediaStreamSource(stream); const analyser = context.createAnalyser(); analyser.fftSize = 1024; source.connect(analyser)
      const samples = new Float32Array(analyser.fftSize); let heardVoice = false; let quietSince = performance.now(); let clippedFrames = 0
      const watchSilence = () => {
        analyser.getFloatTimeDomainData(samples); let power = 0; let peak = 0
        for (const sample of samples) { power += sample * sample; peak = Math.max(peak, Math.abs(sample)) }
        const rms = Math.sqrt(power / samples.length); clippedFrames = peak > .985 ? clippedFrames + 1 : Math.max(0, clippedFrames - 1)
        if (rms > silenceThreshold) { heardVoice = true; quietSince = performance.now() }
        setMetrics({ rms, peak, clipping: clippedFrames >= 3, speechDetected: heardVoice && rms > silenceThreshold, durationMs: performance.now() - startedAt })
        if (heardVoice && performance.now() - quietSince > silenceDurationMs) stop(); else rafRef.current = requestAnimationFrame(watchSilence)
      }
      watchSilence()
    } catch (error) { setRecording(false); onError?.(error instanceof Error ? error.message : 'Microfone indisponível') }
  }
  return { recording, start, stop, metrics, capabilities }
}
