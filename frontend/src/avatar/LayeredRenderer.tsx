import { useMemo, useState, type CSSProperties } from 'react'
import type { EyeState } from '../types'
import type { AvatarManifest, AvatarRendererProps } from './AvatarRenderer'
import { resolveAvatarFraming } from './framing'

const STATUS_EYE: Record<string, EyeState> = { IDLE: 'open', LISTENING: 'open', THINKING: 'half', SPEAKING: 'open', OFFLINE: 'half' }

export function resolveVisualState(status: AvatarRendererProps['status'], state: AvatarRendererProps['state'], eye?: EyeState, blink = true) {
  const resolvedEye = eye ?? (state === 'tired' ? 'half' : STATUS_EYE[status])
  const expressionMouth = state === 'happy' || state === 'amused' ? 'mouth_smile' : state === 'surprised' ? 'mouth_open' : undefined
  return { eye: resolvedEye, mouth: expressionMouth, blink: blink && status !== 'OFFLINE' }
}

export function LayeredRenderer({ manifest, state, status, mouth, eye, variant = 'dashboard', characterView = 'bust', className, idleAnimations = true, eyeMovement = true, blink = true, debug = false, control }: AvatarRendererProps & { manifest: AvatarManifest }) {
  const [assetFailed, setAssetFailed] = useState(false)
  if (assetFailed) throw new Error(`Asset essencial V3 ausente: ${variant}`)
  const framing = resolveAvatarFraming(manifest, variant, characterView)
  const { eyeY, mouthY, linkY } = framing.config.face
  const visual = useMemo(() => resolveVisualState(status, state, eye, blink), [status, state, eye, blink])
  const activeMouth = status === 'SPEAKING' ? mouth : visual.mouth ?? 'mouth_closed'
  return <span
    className={`nyra-avatar nyra-avatar-${variant} nyra-avatar-view-${framing.id} ${className ?? ''}`}
    data-state={state}
    data-status={status.toLowerCase()}
    data-eye={visual.eye}
    data-mouth={activeMouth}
    data-blink={visual.blink}
    data-idle-animation={idleAnimations}
    data-eye-movement={eyeMovement}
    data-renderer="layered"
    style={{
      '--eye-x': `${control?.eye_x ?? 0}px`, '--eye-y': `${control?.eye_y ?? 0}px`,
      '--head-x': `${(control?.head_x ?? 0) * .7}%`, '--head-y': `${(control?.head_y ?? 0) * .5}%`,
      '--head-tilt': `${(control?.head_tilt ?? 0) * 1.4}deg`, '--body-x': `${(control?.body_x ?? 0) * .45}%`,
      '--expression-weight': control?.expression_weight ?? 1,
    } as CSSProperties}
  >
    <img className="nyra-avatar-base" src={framing.source} alt={`NYRA ${state}`} draggable={false} onError={() => setAssetFailed(true)} />
    <span className="nyra-hair-motion" aria-hidden="true" />
    <svg className="nyra-face-layer" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
      <g className="nyra-eye-overlay nyra-eye-continuous">
        <path className="eye eye-left" d={`M41 ${eyeY} Q44 ${eyeY + 1.2} 47 ${eyeY}`} />
        <path className="eye eye-right" d={`M54 ${eyeY} Q57 ${eyeY + 1.2} 60 ${eyeY}`} />
      </g>
      <g className="nyra-mouth-overlay">
        <ellipse className="mouth-cover" cx="50" cy={mouthY} rx="2.2" ry="1.45" />
        <path className="mouth-line" d={activeMouth === 'mouth_smile'
          ? `M47.8 ${mouthY - .3} Q50 ${mouthY + 1.5} 52.2 ${mouthY - .3}`
          : `M48.2 ${mouthY - .3} Q50 ${mouthY + .5} 51.8 ${mouthY - .3}`} />
        <ellipse className="mouth-open" cx="50" cy={mouthY} rx="1.45" ry="1.15" />
      </g>
      <g className="nyra-neural-link">
        <circle className="link-left" cx="37.5" cy={linkY} r=".72" />
        <circle className="link-right" cx="62.5" cy={linkY} r=".72" />
      </g>
      <g className="nyra-eye-network nyra-eye-continuous">
        <circle cx="44" cy={eyeY} r=".18"/><circle cx="44.7" cy={eyeY - .3} r=".12"/><path d={`M 44 ${eyeY} l .7 -.3`}/>
        <circle cx="57" cy={eyeY} r=".18"/><circle cx="57.7" cy={eyeY - .3} r=".12"/><path d={`M 57 ${eyeY} l .7 -.3`}/>
      </g>
    </svg>
    {debug && <output className="avatar-debug">state={state} · status={status} · eye={visual.eye} · mouth={activeMouth} · framing={framing.id} · renderer=layered · pack={manifest.pack} · fallback=ready</output>}
  </span>
}
