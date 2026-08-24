import { describe, expect, it } from 'vitest'
import { dashboardOwnsRealtimeAudio } from './audioOwnership'

describe('TTS playback ownership', () => {
  it('assigns packaged playback only to Desktop Presence', () => {
    expect(dashboardOwnsRealtimeAudio(true)).toBe(false)
  })

  it('keeps standalone Web UI playback enabled', () => {
    expect(dashboardOwnsRealtimeAudio(false)).toBe(true)
  })
})
