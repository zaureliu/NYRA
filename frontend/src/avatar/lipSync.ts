import type { MouthState } from '../types'

export const LIP_SYNC_THRESHOLDS = {
  closed: 0.025,
  small: 0.08,
  medium: 0.16,
} as const

export function mouthFromAmplitude(amplitude: number): MouthState {
  if (amplitude < LIP_SYNC_THRESHOLDS.closed) return 'mouth_closed'
  if (amplitude < LIP_SYNC_THRESHOLDS.small) return 'mouth_small'
  if (amplitude < LIP_SYNC_THRESHOLDS.medium) return 'mouth_medium'
  return 'mouth_open'
}
