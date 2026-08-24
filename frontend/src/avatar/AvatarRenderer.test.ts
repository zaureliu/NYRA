import { describe, expect, it } from 'vitest'
import manifest from '../../public/avatar/nyra_v3/manifest.json'
import { validateAvatarManifest } from './AvatarRenderer'

describe('NYRA V3 avatar manifest', () => {
  it('loads essential variants and fallback', () => {
    const value = validateAvatarManifest(manifest)
    expect(value.renderer).toBe('layered')
    expect(value.framing.default).toBe('bust')
    expect(value.assets.bust.endsWith('nyra-bust-violet.png')).toBe(true)
    expect(value.assets.portrait.endsWith('.png')).toBe(true)
    expect(value.assets.full_body.endsWith('.png')).toBe(true)
    expect(value.fallback.renderer).toBe('svg')
    expect(Object.keys(value.expressions)).toEqual(['neutral', 'happy', 'curious', 'focused', 'concerned', 'amused', 'tired', 'surprised'])
    expect(Object.keys(value.mouths)).toEqual(['closed', 'small', 'medium', 'open', 'smile'])
    expect(value.states).toMatchObject({ idle: {}, listening: {}, thinking: {}, speaking: {} })
  })

  it('rejects missing essential assets', () => {
    expect(() => validateAvatarManifest({ ...manifest, assets: { ...manifest.assets, bust: '' } })).toThrow(/essencial/)
  })
})
