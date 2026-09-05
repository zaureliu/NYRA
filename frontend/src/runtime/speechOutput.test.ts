import { describe, expect, it } from 'vitest'
import { isResidualEcho, outputIsPlaying, outputReference, setOutputPlaying } from './speechOutput'

describe('real output reference, not a second microphone', () => {
  it('rejects correlated residual echo, but allows distinct simultaneous input', () => {
    const output = new Uint8Array(512)
    for (let i = 5; i < 80; i += 9) output[i] = 240
    setOutputPlaying(true); outputReference(output, 1000)
    expect(isResidualEcho(output, 1090)).toBe(true)
    const user = new Uint8Array(512)
    for (let i = 2; i < 80; i += 7) user[i] = 230
    expect(isResidualEcho(user, 1090)).toBe(false)
    expect(isResidualEcho(output, 1500)).toBe(false)
    setOutputPlaying(false)
    expect(outputIsPlaying()).toBe(false)
    expect(isResidualEcho(output, 1090)).toBe(false)
  })
})
