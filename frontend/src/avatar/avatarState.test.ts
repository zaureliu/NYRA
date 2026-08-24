import { describe, expect, it } from 'vitest'
import { AVATAR_STATE_PRIORITY, resolveAvatarPresentation } from './avatarState'

describe('avatar state manager', () => {
  it('keeps operational state priority deterministic', () => {
    expect(AVATAR_STATE_PRIORITY).toEqual(['speaking', 'listening', 'thinking', 'interaction', 'mouse_follow', 'idle', 'blink'])
    expect(resolveAvatarPresentation('SPEAKING', 'happy')).toMatchObject({ priority: 'speaking', operationalStatus: 'speaking', mouth: 'mouth_speaking_smile', pointerAllowed: true })
    expect(resolveAvatarPresentation('LISTENING', 'surprised')).toMatchObject({ priority: 'listening', expression: 'listening', pointerAllowed: true })
    expect(resolveAvatarPresentation('THINKING', 'happy')).toMatchObject({ priority: 'thinking', gaze: 'up_right', pointerAllowed: true })
    expect(resolveAvatarPresentation('IDLE', 'neutral')).toMatchObject({ priority: 'mouse_follow', pointerAllowed: true })
  })

  it('uses subtle, non-caricature expressions for idle emotions', () => {
    expect(resolveAvatarPresentation('IDLE', 'concerned').expression).toBe('concerned_soft')
    expect(resolveAvatarPresentation('IDLE', 'surprised').expression).toBe('surprised_soft')
    expect(resolveAvatarPresentation('OFFLINE', 'neutral').pointerAllowed).toBe(false)
  })
})
