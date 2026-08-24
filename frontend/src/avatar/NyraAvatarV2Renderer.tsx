import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import type { EyeState, MouthState } from '../types'
import type { AvatarRendererProps } from './AvatarRenderer'
import { resolveAvatarPresentation, type BlinkFrame } from './avatarState'
import type { NyraAvatarV2Manifest } from './avatarV2Manifest'
import { mouthFromAmplitude } from './lipSync'
import { usePointerFollow } from './usePointerFollow'

const FALLBACK_MOUTH_SEQUENCE: MouthState[] = [
  'mouth_small', 'mouth_medium', 'mouth_small', 'mouth_open',
  'mouth_medium', 'mouth_small', 'mouth_closed',
]
const FALLBACK_MOUTH_DELAYS = [88, 112, 76, 138, 98, 82, 126]

export function randomBlinkDelay(minimum: number, maximum: number, random = Math.random) {
  return Math.round(minimum + (maximum - minimum) * random())
}

export interface BlinkScheduler {
  set(callback: () => void, delay: number): number
  clear(timer: number): void
}

export function createNaturalBlinkController(
  manifest: NyraAvatarV2Manifest,
  onFrame: (frame: BlinkFrame) => void,
  scheduler: BlinkScheduler,
  random = Math.random,
) {
  let cancelled = false
  let timer: number | undefined
  const sequence = manifest.blink.sequence
  const runBlink = (index = 1) => {
    if (cancelled) return
    onFrame(sequence[index])
    if (index < sequence.length - 1) {
      timer = scheduler.set(() => runBlink(index + 1), manifest.blink.frameDurationsMs[index] ?? 24)
    } else {
      timer = scheduler.set(() => runBlink(1), randomBlinkDelay(manifest.blink.intervalMs.minimum, manifest.blink.intervalMs.maximum, random))
    }
  }
  timer = scheduler.set(() => runBlink(1), randomBlinkDelay(manifest.blink.intervalMs.minimum, manifest.blink.intervalMs.maximum, random))
  return () => { cancelled = true; if (timer !== undefined) scheduler.clear(timer) }
}

export function normalizeEyeState(eye?: EyeState): BlinkFrame | undefined {
  if (eye === 'blink') return 'closed'
  return eye === 'open' || eye === 'half' || eye === 'closed' ? eye : undefined
}

function useReducedMotion() {
  const [reduced, setReduced] = useState(() => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches)
  useEffect(() => {
    if (typeof matchMedia !== 'function') return
    const query = matchMedia('(prefers-reduced-motion: reduce)')
    const update = () => setReduced(query.matches)
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])
  return reduced
}

function useNaturalBlink(manifest: NyraAvatarV2Manifest, enabled: boolean, forced?: BlinkFrame) {
  const [frame, setFrame] = useState<BlinkFrame>('open')
  const reducedMotion = useReducedMotion()

  useEffect(() => {
    if (forced) { setFrame(forced); return }
    setFrame('open')
    if (!enabled || reducedMotion) return
    return createNaturalBlinkController(manifest, setFrame, {
      set: (callback, delay) => window.setTimeout(callback, delay),
      clear: (timer) => window.clearTimeout(timer),
    })
  }, [enabled, forced, manifest, reducedMotion])

  return frame
}

function useSpeakingFallback(status: AvatarRendererProps['status'], supplied: MouthState) {
  const [fallback, setFallback] = useState<MouthState>('mouth_closed')
  useEffect(() => {
    if (status !== 'SPEAKING' || supplied !== 'mouth_closed') { setFallback('mouth_closed'); return }
    let cancelled = false
    let timer: number | undefined
    let index = 0
    const advance = () => {
      if (cancelled) return
      setFallback(FALLBACK_MOUTH_SEQUENCE[index])
      const delay = FALLBACK_MOUTH_DELAYS[index]
      index = (index + 1) % FALLBACK_MOUTH_SEQUENCE.length
      timer = window.setTimeout(advance, delay)
    }
    timer = window.setTimeout(advance, 180)
    return () => { cancelled = true; if (timer !== undefined) window.clearTimeout(timer) }
  }, [status, supplied])
  return fallback
}

export function resolveNyraV2Mouth(status: AvatarRendererProps['status'], supplied: MouthState, fallback: MouthState, controlMouth?: number, emotionalState?: AvatarRendererProps['state']) {
  if (status === 'SPEAKING') {
    if (controlMouth !== undefined && controlMouth > 0) return mouthFromAmplitude(controlMouth)
    if (emotionalState === 'happy' || emotionalState === 'amused') return supplied === 'mouth_closed' ? 'mouth_speaking_smile' : supplied
    return supplied === 'mouth_closed' ? fallback : supplied
  }
  if (emotionalState === 'happy' || emotionalState === 'amused') return 'mouth_smile'
  if (emotionalState === 'surprised') return 'mouth_wide'
  return 'mouth_closed'
}

export function NyraAvatarV2Renderer({
  manifest, state, status, mouth, eye, variant = 'dashboard', className,
  idleAnimations = true, eyeMovement = true, blink = true, debug = false, control, pointerSource = 'web',
}: AvatarRendererProps & { manifest: NyraAvatarV2Manifest }) {
  const rootRef = useRef<HTMLSpanElement>(null)
  const presentation = useMemo(() => resolveAvatarPresentation(status, state), [state, status])
  const forcedEye = normalizeEyeState(eye)
  const blinkEye = useNaturalBlink(manifest, blink && status !== 'OFFLINE', forcedEye)
  const fallbackMouth = useSpeakingFallback(status, mouth)
  const activeEye = forcedEye ?? (status === 'OFFLINE' || state === 'tired' ? 'half' : blinkEye)
  const activeMouth = resolveNyraV2Mouth(status, mouth, fallbackMouth, control?.mouth_open, state)
  const operationalStatus = presentation.operationalStatus
  const pointerScale = operationalStatus === 'idle' ? 1 : operationalStatus === 'offline' ? 0 : .55
  const pointerHeadScale = operationalStatus === 'idle' ? 1 : operationalStatus === 'offline' ? 0 : .35
  const pointerConfig = useMemo(() => ({
    deadZone: manifest.gaze.deadZone, returnDelayMs: manifest.gaze.returnDelayMs, smoothing: manifest.gaze.smoothing,
    maxEyeX: manifest.gaze.limits.eyeX * pointerScale, maxEyeY: manifest.gaze.limits.eyeY * pointerScale,
    maxHeadX: manifest.gaze.limits.headX * pointerHeadScale, maxHeadY: manifest.gaze.limits.headY * pointerHeadScale,
    maxHeadTilt: manifest.gaze.limits.headTilt * pointerHeadScale,
  }), [manifest, pointerHeadScale, pointerScale])
  usePointerFollow(rootRef, eyeMovement && presentation.pointerAllowed, eyeMovement ? presentation.gaze : 'front', pointerConfig, pointerSource)
  const style = useMemo(() => ({
    '--nyra-root-x': `${((control?.head_x ?? 0) * 7) + ((control?.body_x ?? 0) * 4.5)}px`,
    '--nyra-root-y': `${(control?.head_y ?? 0) * 5}px`,
    '--nyra-root-tilt': `${(control?.head_tilt ?? 0) * 1.4}deg`,
    '--nyra-eye-control-x': `${(control?.eye_x ?? 0) * 4}px`,
    '--nyra-eye-control-y': `${(control?.eye_y ?? 0) * 3}px`,
    '--nyra-head-origin-x': `${manifest.head.transformOrigin.x}px`,
    '--nyra-head-origin-y': `${manifest.head.transformOrigin.y}px`,
  } as CSSProperties), [control, manifest])

  return <span
    ref={rootRef}
    className={`nyra-avatar nyra-avatar-v2 nyra-avatar-${variant} ${className ?? ''}`}
    data-pack="nyra_v2"
    data-renderer="unified-svg-layers"
    data-state={state}
    data-status={operationalStatus}
    data-expression={presentation.expression}
    data-gaze={presentation.gaze}
    data-eye={activeEye}
    data-mouth={activeMouth}
    data-idle-animation={idleAnimations}
    data-eye-movement={eyeMovement}
    style={style}
  >
    <svg
      className="nyra-v2-canvas"
      viewBox={manifest.canvas.viewBox}
      preserveAspectRatio={manifest.canvas.preserveAspectRatio}
      role="img"
      aria-label={`NYRA Avatar V2, ${state}, ${status.toLowerCase()}`}
    >
      <defs>
        <radialGradient id="nyra-v2-breath" cx="50%" cy="45%" r="55%">
          <stop offset="0" stopColor="#fff7df" stopOpacity=".2"/><stop offset="1" stopColor="#fff7df" stopOpacity="0"/>
        </radialGradient>
        <linearGradient id="nyra-v2-headphone-light" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#76e9ff"/><stop offset=".52" stopColor="#718cff"/><stop offset="1" stopColor="#b067ff"/>
        </linearGradient>
        <linearGradient id="nyra-v2-iris" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#173b55"/><stop offset=".48" stopColor="#287a9a"/><stop offset="1" stopColor="#77c9d9"/>
        </linearGradient>
        <filter id="nyra-v2-glow" x="-80%" y="-40%" width="260%" height="180%">
          <feGaussianBlur stdDeviation="5" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
      </defs>
      <g className="nyra-v2-character-root" data-layer="character-root">
        <g className="nyra-v2-presence" data-layer="presence">
          <image className="nyra-v2-master" data-layer="base" href={manifest.assets.master} width={manifest.canvas.width} height={manifest.canvas.height}/>
          <g className="nyra-v2-body" data-layer="body">
            <ellipse className="nyra-v2-breath-highlight" cx={manifest.body.breathingOrigin.x} cy="1115" rx="265" ry="250" fill="url(#nyra-v2-breath)"/>
          </g>
          <g className="nyra-v2-head" data-layer="head">
            <g className="nyra-v2-face" data-layer="face">
              <g className="nyra-v2-eyes" data-layer="eyes">
                <g className="nyra-v2-gaze-layer">
                  <image className="nyra-v2-gaze-base" href={manifest.assets.eyes.gaze_base} width={manifest.canvas.width} height={manifest.canvas.height}/>
                  <g className="nyra-v2-gaze-pupils">
                    <g><ellipse cx="390" cy="500" rx="29" ry="39" fill="url(#nyra-v2-iris)"/><ellipse cx="390" cy="500" rx="9" ry="25" fill="#18324a"/><circle cx="378" cy="483" r="6.5" fill="#fff8ea"/><circle cx="401" cy="519" r="3" fill="#a6e5e8"/></g>
                    <g><ellipse cx="653" cy="494" rx="29" ry="38" fill="url(#nyra-v2-iris)"/><ellipse cx="653" cy="494" rx="9" ry="24" fill="#18324a"/><circle cx="641" cy="477" r="6.5" fill="#fff8ea"/><circle cx="664" cy="513" r="3" fill="#a6e5e8"/></g>
                  </g>
                </g>
                <image className="nyra-v2-eye-layer nyra-v2-eye-seventy-five" href={manifest.assets.eyes.seventy_five} width={manifest.canvas.width} height={manifest.canvas.height}/>
                <image className="nyra-v2-eye-layer nyra-v2-eye-half" href={manifest.assets.eyes.half} width={manifest.canvas.width} height={manifest.canvas.height}/>
                <image className="nyra-v2-eye-layer nyra-v2-eye-twenty-five" href={manifest.assets.eyes.twenty_five} width={manifest.canvas.width} height={manifest.canvas.height}/>
                <image className="nyra-v2-eye-layer nyra-v2-eye-closed" href={manifest.assets.eyes.closed} width={manifest.canvas.width} height={manifest.canvas.height}/>
              </g>
              <g className="nyra-v2-mouth" data-layer="mouth">
                <image className="nyra-v2-mouth-layer nyra-v2-mouth-small" href={manifest.assets.mouth.small} width={manifest.canvas.width} height={manifest.canvas.height}/>
                <image className="nyra-v2-mouth-layer nyra-v2-mouth-medium" href={manifest.assets.mouth.medium} width={manifest.canvas.width} height={manifest.canvas.height}/>
                <image className="nyra-v2-mouth-layer nyra-v2-mouth-open" href={manifest.assets.mouth.open} width={manifest.canvas.width} height={manifest.canvas.height}/>
                <image className="nyra-v2-mouth-layer nyra-v2-mouth-wide" href={manifest.assets.mouth.wide} width={manifest.canvas.width} height={manifest.canvas.height}/>
                <image className="nyra-v2-mouth-layer nyra-v2-mouth-smile" href={manifest.assets.mouth.smile} width={manifest.canvas.width} height={manifest.canvas.height}/>
                <image className="nyra-v2-mouth-layer nyra-v2-mouth-speaking-smile" href={manifest.assets.mouth.speaking_smile} width={manifest.canvas.width} height={manifest.canvas.height}/>
              </g>
            </g>
            <g className="nyra-v2-headphones" data-layer="headphones" fill="none" stroke="url(#nyra-v2-headphone-light)" filter="url(#nyra-v2-glow)">
              <path d="M220 427C202 459 202 528 221 558"/>
              <path d="M868 414C888 447 888 518 868 551"/>
            </g>
          </g>
        </g>
      </g>
    </svg>
    {debug && <output className="avatar-debug">pack={manifest.pack} · canvas={manifest.canvas.width}×{manifest.canvas.height} · state={state} · status={operationalStatus} · eye={activeEye} · mouth={activeMouth} · root=unified</output>}
  </span>
}
