import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import manifestJson from '../../public/avatar/nyra_v2/avatar-manifest.json'
import type { ActivityStatus } from '../types'
import { createNaturalBlinkController, NyraAvatarV2Renderer, normalizeEyeState, randomBlinkDelay, resolveNyraV2Mouth } from './NyraAvatarV2Renderer'
import { validateNyraAvatarV2Manifest } from './avatarV2Manifest'
import { mouthFromAmplitude } from './lipSync'

const manifest = validateNyraAvatarV2Manifest(manifestJson)

describe('NYRA Avatar V2 geometry', () => {
  it('uses one immutable canvas and stable facial anchors', () => {
    expect(manifest.canvas).toMatchObject({ width: 1086, height: 1448, viewBox: '0 0 1086 1448' })
    expect(manifest.leftEye.center).toEqual(manifest.leftEye.anchor)
    expect(manifest.rightEye.center).toEqual(manifest.rightEye.anchor)
    expect(manifest.mouth.center).toEqual(manifest.mouth.anchor)
    expect(manifest.headphones).toMatchObject({ group: 'head', maxIndicatorScale: 1.02 })
  })

  it('keeps every face state on the full master canvas', () => {
    expect(Object.values(manifest.assets.eyes)).toHaveLength(6)
    expect(Object.values(manifest.assets.mouth)).toHaveLength(7)
    expect([...Object.values(manifest.assets.eyes), ...Object.values(manifest.assets.mouth)].every((asset) => asset.endsWith('.svg'))).toBe(true)
  })

  it('renders root, body, head, eyes, mouth and headphones in one SVG', () => {
    const html = renderToStaticMarkup(<NyraAvatarV2Renderer
      manifest={manifest} state="neutral" status="IDLE" mouth="mouth_closed"
      idleAnimations blink
    />)
    expect(html).toContain('data-pack="nyra_v2"')
    expect(html).toContain('data-renderer="unified-svg-layers"')
    expect(html).toContain('viewBox="0 0 1086 1448"')
    for (const layer of ['character-root', 'base', 'body', 'head', 'face', 'eyes', 'mouth', 'headphones']) {
      expect(html).toContain(`data-layer="${layer}"`)
    }
    expect(html.match(/width="1086" height="1448"/g)?.length).toBe(12)
  })
})

describe('NYRA Avatar V2 state mapping', () => {
  it('maps operational states without changing the coordinate system', () => {
    const cases: Array<[ActivityStatus, string]> = [
      ['IDLE', 'idle'], ['LISTENING', 'listening'], ['TRANSCRIBING', 'thinking'],
      ['THINKING', 'thinking'], ['SPEAKING', 'speaking'], ['INTERRUPTED', 'listening'], ['OFFLINE', 'offline'],
    ]
    for (const [status, expected] of cases) {
      const html = renderToStaticMarkup(<NyraAvatarV2Renderer manifest={manifest} state="neutral" status={status} mouth="mouth_closed" blink={false}/>)
      expect(html).toContain(`data-status="${expected}"`)
      expect(html).toContain('viewBox="0 0 1086 1448"')
    }
  })

  it('keeps explicit blink states and a natural 120–220ms sequence', () => {
    expect(normalizeEyeState('open')).toBe('open')
    expect(normalizeEyeState('half')).toBe('half')
    expect(normalizeEyeState('closed')).toBe('closed')
    expect(normalizeEyeState('look_left')).toBeUndefined()
    expect(manifest.blink.sequence).toEqual(['open', 'seventy_five', 'half', 'twenty_five', 'closed', 'twenty_five', 'half', 'seventy_five', 'open'])
    expect(manifest.blink.frameDurationsMs.reduce((total, value) => total + value, 0)).toBeGreaterThanOrEqual(120)
    expect(manifest.blink.frameDurationsMs.reduce((total, value) => total + value, 0)).toBeLessThanOrEqual(220)
    expect(randomBlinkDelay(3600, 7200, () => 0)).toBe(3600)
    expect(randomBlinkDelay(3600, 7200, () => 1)).toBe(7200)
  })

  it('maps WebAudio amplitude and speaking fallback to four mouth states', () => {
    expect([0, 0.04, 0.1, 0.3].map(mouthFromAmplitude)).toEqual([
      'mouth_closed', 'mouth_small', 'mouth_medium', 'mouth_open',
    ])
    expect(resolveNyraV2Mouth('SPEAKING', 'mouth_closed', 'mouth_medium')).toBe('mouth_medium')
    expect(resolveNyraV2Mouth('SPEAKING', 'mouth_closed', 'mouth_small', 0.3)).toBe('mouth_open')
    expect(resolveNyraV2Mouth('IDLE', 'mouth_open', 'mouth_open')).toBe('mouth_closed')
  })

  it('keeps one blink timeout active and cancels it during cleanup', () => {
    let nextTimer = 0
    const pending = new Map<number, () => void>()
    const cleared: number[] = []
    const frames: string[] = []
    const stop = createNaturalBlinkController(manifest, (frame) => frames.push(frame), {
      set(callback) { const id = ++nextTimer; pending.set(id, callback); return id },
      clear(timer) { cleared.push(timer); pending.delete(timer) },
    }, () => 0)
    expect(pending.size).toBe(1)
    const [firstId, firstFrame] = [...pending.entries()][0]
    pending.delete(firstId)
    firstFrame()
    expect(frames).toEqual(['seventy_five'])
    expect(pending.size).toBe(1)
    stop()
    expect(cleared).toHaveLength(1)
    expect(pending.size).toBe(0)
  })
})
