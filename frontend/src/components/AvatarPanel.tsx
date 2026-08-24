import { AvatarRenderer } from '../avatar/AvatarRenderer'
import { useVisualSettings } from '../avatar/visualSettings'
import type { ActivityStatus, AvatarControl, EmotionalState, MouthState } from '../types'

interface Props { state: EmotionalState; status: ActivityStatus; mouth: MouthState; control?: Partial<AvatarControl> }

export function AvatarPanel({ state, status, mouth, control }: Props) {
  const [visual] = useVisualSettings()
  return (
    <section className={`panel avatar-panel state-${state}`} aria-label="Avatar de NYRA">
      <div className="scanline" />
      <AvatarRenderer
        state={state} status={status} mouth={mouth} variant="dashboard"
        avatarVersion={visual.avatarVersion} renderer={visual.renderer}
        idleAnimations={visual.idleAnimations} eyeMovement={visual.eyeMovement}
        blink={visual.blink} debug={visual.debug}
        control={control}
      />
      <div className="avatar-readout">
        <span className="coordinate">NYRA AVATAR V2 // LOCAL</span>
        <span className={`activity-dot status-${status.toLowerCase()}`} />
        <strong>{status}</strong>
      </div>
    </section>
  )
}
