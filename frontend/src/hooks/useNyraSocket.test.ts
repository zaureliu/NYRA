import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { reconnectDelay } from './useNyraSocket'

describe('reconnectDelay', () => {
  it('uses bounded exponential backoff', () => {
    expect([0, 1, 2, 3, 4, 5, 20].map(reconnectDelay)).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000])
  })
})

describe('persona presence bridge', () => {
  it('applies the provider-neutral NYRA emotion event to desktop presence', () => {
    const source = readFileSync(new URL('./useNyraSocket.ts', import.meta.url), 'utf-8')
    expect(source).toContain("event.type === 'NYRA_EMOTION_CHANGED'")
    expect(source).toContain('event.payload.emotion as EmotionalState')
  })
})
