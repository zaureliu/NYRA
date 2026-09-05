// Playback reference in the WebView that owns the existing microphone.
// Browser AEC remains primary; spectral matches reject residual self-voice.
export const BARGE_IN_EVENT = 'nyra:local-barge-in'
let playing = false
const references: Array<{ at: number; bands: number[] }> = []
export const outputIsPlaying = () => playing
export function setOutputPlaying(value: boolean) { playing = value; if (!value) references.length = 0 }
export function spectralBands(bins: Uint8Array): number[] {
  return Array.from({ length: 16 }, (_, band) => {
    const start = 2 + band * 5
    let sum = 0
    for (let i = start; i < Math.min(bins.length, start + 5); i++) sum += bins[i] ** 2
    return Math.sqrt(sum)
  })
}
export function outputReference(bins: Uint8Array, at = performance.now()) {
  references.push({ at, bands: spectralBands(bins) })
  while (references.length > 24) references.shift()
}
export function isResidualEcho(bins: Uint8Array, at = performance.now()): boolean {
  if (!playing) return false
  const input = spectralBands(bins)
  const norm = Math.sqrt(input.reduce((sum, value) => sum + value * value, 0))
  if (norm < 80) return false
  return references.some(({ at: captured, bands }) => {
    if (at - captured > 260) return false
    const refNorm = Math.sqrt(bands.reduce((sum, value) => sum + value * value, 0))
    if (refNorm < 80 || norm > refNorm * 1.3) return false
    return input.reduce((sum, value, i) => sum + value * bands[i], 0) / (norm * refNorm) > .992
  })
}
export function bargeInLocally() {
  window.dispatchEvent(new CustomEvent(BARGE_IN_EVENT, { detail: { detectedAt: performance.now() } }))
}
