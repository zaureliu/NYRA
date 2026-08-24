import { describe, expect, it } from 'vitest'
import { resolveVisualState } from './LayeredRenderer'
import { resolveAvatarFraming } from './framing'
import { validateAvatarManifest } from './AvatarRenderer'
import manifestJson from '../../public/avatar/nyra_v3/manifest.json'

describe('visual state mapping', () => {
  it('maps operational state to eyes and blink', () => {
    expect(resolveVisualState('THINKING', 'neutral')).toMatchObject({ eye: 'half', blink: true })
    expect(resolveVisualState('OFFLINE', 'neutral')).toMatchObject({ eye: 'half', blink: false })
  })

  it('maps amused and surprised expressions to mouth layers', () => {
    expect(resolveVisualState('IDLE', 'amused').mouth).toBe('mouth_smile')
    expect(resolveVisualState('IDLE', 'surprised').mouth).toBe('mouth_open')
  })

  it('defaults desktop to bust and preserves full body and dashboard portrait', () => {
    const manifest = validateAvatarManifest(manifestJson)
    expect(resolveAvatarFraming(manifest, 'desktop').id).toBe('bust')
    expect(resolveAvatarFraming(manifest, 'desktop', 'full_body').source).toBe(manifest.assets.full_body)
    expect(resolveAvatarFraming(manifest, 'dashboard', 'full_body').id).toBe('portrait')
  })
})
