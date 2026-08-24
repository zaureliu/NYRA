import type { ActivityStatus, EmotionalState, MouthState } from '../types'

export type BlinkFrame = 'open' | 'seventy_five' | 'half' | 'twenty_five' | 'closed'
export type GazeDirection =
  | 'front'
  | 'left_light' | 'left'
  | 'right_light' | 'right'
  | 'up_light' | 'up'
  | 'down_light' | 'down'
  | 'up_left' | 'up_right'
  | 'down_left' | 'down_right'

export type AvatarExpression =
  | 'neutral' | 'idle' | 'slight_smile' | 'smile'
  | 'listening' | 'focused' | 'thinking' | 'speaking'
  | 'surprised_soft' | 'concerned_soft'
  | 'blink' | 'eyes_half_closed' | 'eyes_closed'

export const AVATAR_STATE_PRIORITY = ['speaking', 'listening', 'thinking', 'interaction', 'mouse_follow', 'idle', 'blink'] as const

export interface AvatarPresentation {
  operationalStatus: 'idle' | 'listening' | 'thinking' | 'speaking' | 'offline'
  expression: AvatarExpression
  gaze: GazeDirection
  mouth: MouthState
  pointerAllowed: boolean
  priority: typeof AVATAR_STATE_PRIORITY[number]
}

function idleExpression(state: EmotionalState): Pick<AvatarPresentation, 'expression' | 'mouth'> {
  if (state === 'happy') return { expression: 'smile', mouth: 'mouth_smile' }
  if (state === 'amused') return { expression: 'slight_smile', mouth: 'mouth_smile' }
  if (state === 'focused' || state === 'curious') return { expression: 'focused', mouth: 'mouth_closed' }
  if (state === 'concerned') return { expression: 'concerned_soft', mouth: 'mouth_closed' }
  if (state === 'surprised') return { expression: 'surprised_soft', mouth: 'mouth_wide' }
  if (state === 'tired') return { expression: 'eyes_half_closed', mouth: 'mouth_closed' }
  return { expression: 'idle', mouth: 'mouth_closed' }
}

export function resolveAvatarPresentation(status: ActivityStatus, state: EmotionalState): AvatarPresentation {
  if (status === 'SPEAKING') return {
    operationalStatus: 'speaking', expression: state === 'happy' || state === 'amused' ? 'slight_smile' : 'speaking',
    gaze: 'front', mouth: state === 'happy' || state === 'amused' ? 'mouth_speaking_smile' : 'mouth_closed',
    pointerAllowed: true, priority: 'speaking',
  }
  if (status === 'LISTENING' || status === 'USER_SPEAKING' || status === 'INTERRUPTED') return {
    operationalStatus: 'listening', expression: 'listening', gaze: 'front', mouth: 'mouth_closed',
    pointerAllowed: true, priority: 'listening',
  }
  if (status === 'TRANSCRIBING' || status === 'THINKING' || status === 'TOOL_EXECUTION') return {
    operationalStatus: 'thinking', expression: status === 'TRANSCRIBING' ? 'focused' : 'thinking',
    gaze: 'up_right', mouth: 'mouth_closed', pointerAllowed: true, priority: 'thinking',
  }
  if (status === 'OFFLINE') return {
    operationalStatus: 'offline', expression: 'eyes_half_closed', gaze: 'down_light', mouth: 'mouth_closed',
    pointerAllowed: false, priority: 'idle',
  }
  if (status === 'ERROR') return {
    operationalStatus: 'idle', expression: 'concerned_soft', gaze: 'front', mouth: 'mouth_closed',
    pointerAllowed: true, priority: 'interaction',
  }
  const visual = idleExpression(state)
  return {
    operationalStatus: 'idle', expression: visual.expression, gaze: 'front', mouth: visual.mouth,
    pointerAllowed: state !== 'tired', priority: state === 'neutral' ? 'mouse_follow' : 'interaction',
  }
}
