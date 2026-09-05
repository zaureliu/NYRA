import { useCallback, useEffect, useRef } from 'react'
import { backendObjectUrl, releaseBackendObjectUrl } from '../runtime/backend'
import { outputReference } from '../runtime/speechOutput'

export function useAudioLipSync(outputDevice = 'default', onAmplitude?: (value: number) => void, volume = 1) {
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const generationRef = useRef(0)
  const contextRef = useRef<AudioContext | null>(null)
  const frameRef = useRef<number | null>(null)
  const endTimerRef = useRef<number | null>(null)
  const objectUrlRef = useRef<string | null>(null)
  const smoothedRef = useRef(0)
  const amplitudeCallback = useRef(onAmplitude)
  amplitudeCallback.current = onAmplitude

  const stop = useCallback(() => {
    generationRef.current += 1
    audioRef.current?.pause()
    audioRef.current = null
    if (objectUrlRef.current) releaseBackendObjectUrl(objectUrlRef.current)
    objectUrlRef.current = null
    if (frameRef.current !== null) cancelAnimationFrame(frameRef.current)
    frameRef.current = null
    if (endTimerRef.current !== null) window.clearTimeout(endTimerRef.current)
    endTimerRef.current = null
    if (contextRef.current && contextRef.current.state !== 'closed') void contextRef.current.close()
    contextRef.current = null
    smoothedRef.current = 0
    amplitudeCallback.current?.(0)
  }, [])

  useEffect(() => () => {
    audioRef.current?.pause()
    if (objectUrlRef.current) releaseBackendObjectUrl(objectUrlRef.current)
    if (frameRef.current !== null) cancelAnimationFrame(frameRef.current)
    if (endTimerRef.current !== null) window.clearTimeout(endTimerRef.current)
    if (contextRef.current && contextRef.current.state !== 'closed') void contextRef.current.close()
    amplitudeCallback.current?.(0)
  }, [])

  const play = useCallback(async (url: string, onEnd: (completed?: boolean) => void, onStart?: () => void, onProgress?: (fraction: number) => void) => {
    const generation = ++generationRef.current
    audioRef.current?.pause()
    if (frameRef.current !== null) cancelAnimationFrame(frameRef.current)
    if (contextRef.current && contextRef.current.state !== 'closed') void contextRef.current.close()
    if (objectUrlRef.current) releaseBackendObjectUrl(objectUrlRef.current)
    let sourceUrl: string | null = null
    let context: AudioContext | null = null
    try {
      if (url.startsWith('pcm16:')) {
        const [, rate, encoded] = url.split(':')
        const bytes = atob(encoded)
        if (!bytes.length) { onEnd(true); return }
        if (![16000, 24000, 48000].includes(Number(rate)) || bytes.length % 2 || bytes.length > 96000) throw new Error('Invalid PCM packet')
        context = new AudioContext({ sampleRate: 48000 }); contextRef.current = context
        if (outputDevice !== 'default' && 'setSinkId' in context) {
          await (context as AudioContext & { setSinkId(id: string): Promise<void> }).setSinkId(outputDevice)
        }
        if (generationRef.current !== generation) { await context.close(); return }
        const buffer = context.createBuffer(1, bytes.length / 2, Number(rate))
        const samples = buffer.getChannelData(0)
        for (let i = 0; i < samples.length; i++) {
          const raw = bytes.charCodeAt(i * 2) | (bytes.charCodeAt(i * 2 + 1) << 8)
          samples[i] = (raw >= 32768 ? raw - 65536 : raw) / 32768
        }
        const source = context.createBufferSource(); source.buffer = buffer
        const gain = context.createGain(); gain.gain.value = Math.max(0, Math.min(1, volume))
        const analyser = context.createAnalyser(); analyser.fftSize = 1024
        source.connect(gain); gain.connect(analyser); analyser.connect(context.destination)
        const bins = new Uint8Array(analyser.frequencyBinCount)
        await context.resume()
        if (generationRef.current !== generation) { await context.close(); return }
        const started = context.currentTime
        const animate = () => {
          if (generationRef.current !== generation || !context) return
          analyser.getByteFrequencyData(bins); outputReference(bins)
          amplitudeCallback.current?.(Math.min(1, bins.reduce((sum, value) => sum + value, 0) / bins.length / 60))
          onProgress?.(Math.min(1, (context.currentTime - started) / buffer.duration))
          frameRef.current = requestAnimationFrame(animate)
        }
        source.onended = () => {
          if (generationRef.current !== generation) return
          if (frameRef.current !== null) cancelAnimationFrame(frameRef.current)
          amplitudeCallback.current?.(0)
          if (context?.state !== 'closed') void context?.close()
          contextRef.current = null
          onEnd(true)
        }
        source.start(); onStart?.(); animate()
        return
      }
      sourceUrl = await backendObjectUrl(url)
      if (generationRef.current !== generation) { releaseBackendObjectUrl(sourceUrl); return }
      objectUrlRef.current = sourceUrl
      const audio = new Audio(sourceUrl)
      audio.volume = Math.max(0, Math.min(1, volume))
      audio.crossOrigin = 'anonymous'
      if (outputDevice !== 'default' && 'setSinkId' in audio) {
        await (audio as HTMLAudioElement & { setSinkId(id: string): Promise<void> }).setSinkId(outputDevice)
      }
      audioRef.current = audio
      context = new AudioContext({ sampleRate: 48000 })
      contextRef.current = context
      const source = context.createMediaElementSource(audio)
      const analyser = context.createAnalyser()
      analyser.fftSize = 1024
      source.connect(analyser)
      analyser.connect(context.destination)
      const bins = new Uint8Array(analyser.frequencyBinCount)
      const animate = () => {
        if (generationRef.current !== generation) return
        analyser.getByteFrequencyData(bins)
        outputReference(bins)
        if (Number.isFinite(audio.duration) && audio.duration > 0) onProgress?.(Math.min(1, audio.currentTime / audio.duration))
        const raw = Math.min(1, (bins.reduce((sum, value) => sum + value, 0) / bins.length / 255) * 4.2)
        const coefficient = raw > smoothedRef.current ? 0.58 : 0.2
        const amplitude = smoothedRef.current + (raw - smoothedRef.current) * coefficient
        smoothedRef.current = amplitude
        amplitudeCallback.current?.(amplitude)
        frameRef.current = requestAnimationFrame(animate)
      }
      let finished = false
      const finish = (completed = false) => {
        if (generationRef.current !== generation) return
        if (finished) return
        finished = true
        if (frameRef.current !== null) cancelAnimationFrame(frameRef.current)
        frameRef.current = null
        if (endTimerRef.current !== null) window.clearTimeout(endTimerRef.current)
        endTimerRef.current = null
        audioRef.current = null
        smoothedRef.current = 0
        amplitudeCallback.current?.(0)
        if (context && context.state !== 'closed') void context.close()
        if (contextRef.current === context) contextRef.current = null
        if (sourceUrl) releaseBackendObjectUrl(sourceUrl)
        if (objectUrlRef.current === sourceUrl) objectUrlRef.current = null
        onEnd(completed)
      }
      audio.onended = () => finish(true)
      audio.onerror = () => finish(false)
      // WebView media events can be lost when a window is restored/minimized.
      // Never leave Hands On suspended forever because `ended` was missed.
      endTimerRef.current = window.setTimeout(finish, 60_000)
      audio.onloadedmetadata = () => {
        if (!Number.isFinite(audio.duration) || audio.duration <= 0) return
        if (endTimerRef.current !== null) window.clearTimeout(endTimerRef.current)
        endTimerRef.current = window.setTimeout(finish, Math.min(60_000, Math.ceil(audio.duration * 1000) + 5_000))
      }
      if (generationRef.current !== generation) return
      await audio.play()
      if (generationRef.current !== generation) { audio.pause(); return }
      onStart?.()
      animate()
    } catch (error) {
      audioRef.current?.pause()
      audioRef.current = null
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current)
      frameRef.current = null
      if (endTimerRef.current !== null) window.clearTimeout(endTimerRef.current)
      endTimerRef.current = null
      if (context && context.state !== 'closed') await context.close().catch(() => undefined)
      if (contextRef.current === context) contextRef.current = null
      if (sourceUrl) releaseBackendObjectUrl(sourceUrl)
      if (objectUrlRef.current === sourceUrl) objectUrlRef.current = null
      smoothedRef.current = 0
      amplitudeCallback.current?.(0)
      throw error
    }
  }, [outputDevice, volume])

  return { play, stop }
}
