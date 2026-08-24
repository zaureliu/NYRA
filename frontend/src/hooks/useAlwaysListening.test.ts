import { describe, expect, it } from 'vitest'
import { encodeWav } from './useAlwaysListening'

describe('Always Listening PCM ring output', () => {
  it('creates a valid mono PCM WAV for local STT', async () => {
    const blob = encodeWav([new Float32Array([0, .5, -.5, 1, -1])], 48000)
    const buffer = await blob.arrayBuffer()
    const bytes = new Uint8Array(buffer)
    const text = (offset: number, length: number) => String.fromCharCode(...bytes.slice(offset, offset + length))
    const view = new DataView(buffer)
    expect(text(0, 4)).toBe('RIFF')
    expect(text(8, 4)).toBe('WAVE')
    expect(view.getUint16(22, true)).toBe(1)
    expect(view.getUint32(24, true)).toBe(48000)
    expect(view.getUint32(40, true)).toBe(10)
  })
})
