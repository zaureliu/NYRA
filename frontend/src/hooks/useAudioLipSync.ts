import { useCallback, useEffect, useRef, useState } from 'react'
import { mouthFromAmplitude } from '../avatar/lipSync'
import type { MouthState } from '../types'
import { backendObjectUrl, releaseBackendObjectUrl } from '../runtime/backend'

export function useAudioLipSync(outputDevice = 'default', onAmplitude?: (value: number) => void, volume = 1) {
  const [mouth, setMouth] = useState<MouthState>('mouth_closed')
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const contextRef = useRef<AudioContext | null>(null)
  const frameRef = useRef<number | null>(null)
  const endTimerRef = useRef<number | null>(null)
  const objectUrlRef = useRef<string | null>(null)
  const smoothedRef = useRef(0)
  const amplitudeCallback = useRef(onAmplitude)
  amplitudeCallback.current = onAmplitude

  const stop = useCallback(() => {
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
    setMouth('mouth_closed')
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

  const play = useCallback(async (url: string, onEnd: () => void, onStart?: () => void) => {
    audioRef.current?.pause()
    if (frameRef.current !== null) cancelAnimationFrame(frameRef.current)
    if (contextRef.current && contextRef.current.state !== 'closed') void contextRef.current.close()
    if (objectUrlRef.current) releaseBackendObjectUrl(objectUrlRef.current)
    let sourceUrl: string | null = null
    let context: AudioContext | null = null
    try {
      sourceUrl = await backendObjectUrl(url)
      objectUrlRef.current = sourceUrl
      const audio = new Audio(sourceUrl)
      audio.volume = Math.max(0, Math.min(1, volume))
      audio.crossOrigin = 'anonymous'
      if (outputDevice !== 'default' && 'setSinkId' in audio) {
        await (audio as HTMLAudioElement & { setSinkId(id: string): Promise<void> }).setSinkId(outputDevice)
      }
      audioRef.current = audio
      context = new AudioContext()
      contextRef.current = context
      const source = context.createMediaElementSource(audio)
      const analyser = context.createAnalyser()
      analyser.fftSize = 256
      source.connect(analyser)
      analyser.connect(context.destination)
      const bins = new Uint8Array(analyser.frequencyBinCount)
      const animate = () => {
        analyser.getByteFrequencyData(bins)
        const raw = Math.min(1, (bins.reduce((sum, value) => sum + value, 0) / bins.length / 255) * 4.2)
        const coefficient = raw > smoothedRef.current ? 0.58 : 0.2
        const amplitude = smoothedRef.current + (raw - smoothedRef.current) * coefficient
        smoothedRef.current = amplitude
        amplitudeCallback.current?.(amplitude)
        setMouth(mouthFromAmplitude(amplitude))
        frameRef.current = requestAnimationFrame(animate)
      }
      let finished = false
      const finish = () => {
        if (finished) return
        finished = true
        if (frameRef.current !== null) cancelAnimationFrame(frameRef.current)
        frameRef.current = null
        if (endTimerRef.current !== null) window.clearTimeout(endTimerRef.current)
        endTimerRef.current = null
        audioRef.current = null
        setMouth('mouth_closed')
        smoothedRef.current = 0
        amplitudeCallback.current?.(0)
        if (context && context.state !== 'closed') void context.close()
        if (contextRef.current === context) contextRef.current = null
        if (sourceUrl) releaseBackendObjectUrl(sourceUrl)
        if (objectUrlRef.current === sourceUrl) objectUrlRef.current = null
        onEnd()
      }
      audio.onended = finish
      audio.onerror = finish
      // WebView media events can be lost when a window is restored/minimized.
      // Never leave Hands On suspended forever because `ended` was missed.
      endTimerRef.current = window.setTimeout(finish, 60_000)
      audio.onloadedmetadata = () => {
        if (!Number.isFinite(audio.duration) || audio.duration <= 0) return
        if (endTimerRef.current !== null) window.clearTimeout(endTimerRef.current)
        endTimerRef.current = window.setTimeout(finish, Math.min(60_000, Math.ceil(audio.duration * 1000) + 5_000))
      }
      await audio.play()
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
      setMouth('mouth_closed')
      smoothedRef.current = 0
      amplitudeCallback.current?.(0)
      throw error
    }
  }, [outputDevice, volume])

  return { mouth, play, stop }
}
