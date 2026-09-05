import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { SttProviderSettings, type RecognitionStatus } from './SttProviderSettings'

const state: RecognitionStatus = {
  settings: { provider: 'deepgram', model: 'nova-3', language: 'pt-BR', smart_format: true, interim_results: true,
    utterance_end_ms: 1000, endpointing: 300, vad_events: true, punctuate: true, numerals: true, profanity_filter: false,
    diarize: false, redact: false, dictation: false, fallback: 'faster_whisper', keyterms_enabled: false, keyterms: [] },
  credential_configured: false, deepgram_state: 'NOT_CONFIGURED', active_provider: null, connection_state: 'NOT_CONFIGURED',
  fallback_available: true, fallback_loaded: false, fallback_active: false, last_error: null, diagnostics: {},
}

describe('Speech Recognition settings', () => {
  it('keeps readability styling scoped to Voice without changing the controls', () => {
    const html = renderToStaticMarkup(<SttProviderSettings initialState={state} />)
    const loading = renderToStaticMarkup(<SttProviderSettings />)
    expect(html).toContain('settings-group stt-provider-settings')
    expect(loading).toContain('class="stt-provider-settings"')
    expect((html.match(/type="checkbox"/g) ?? [])).toHaveLength(8)
    expect((html.match(/class="diagnostic-grid"/g) ?? [])).toHaveLength(2)
    expect((html.match(/<details>/g) ?? [])).toHaveLength(2)
    const css = readFileSync(new URL('../ops/pages/VoicePage.css', import.meta.url), 'utf8')
    // Every style rule requires the local page root, including media overrides.
    const rules = css.replace(/\/\*[\s\S]*?\*\//g, '').split(/[{}]/)
      .map((rule) => rule.trim()).filter((rule) => rule && !rule.includes(';') && !rule.startsWith('@'))
    expect(rules.length).toBeGreaterThan(10)
    expect(rules.every((selector) => selector.startsWith('.voice-page ')
      || selector.startsWith('.ops-shell:has(.voice-page) '))).toBe(true)
    expect(css).not.toMatch(/\bzoom\s*:|transform\s*:|:root/)
    const sizes = [...css.matchAll(/font-size:\s*(\d+)px/g)].map((match) => Number(match[1]))
    expect(Math.min(...sizes)).toBeGreaterThanOrEqual(13)
    expect(css).toContain('font-size: 20px')
    expect(css).toContain('font-size: 15px')
    expect(css).toContain('width: 18px')
    expect(css).toContain('min-height: 36px')
  })
  it('shows cloud/local privacy, requested defaults and no credential value', () => {
    const html = renderToStaticMarkup(<SttProviderSettings initialState={state} />)
    for (const text of ['Speech Recognition', 'Cloud STT', 'Local STT', 'nova-3', 'pt-BR', 'Smart Format', 'Interim Results', 'VAD Events', 'Punctuation', 'Numerals', 'Profanity Filter', 'Advanced', 'Diagnostics', '1000', '300']) expect(html).toContain(text)
    expect(html).toContain('Configure credential')
    expect(html).not.toContain('type="password"')
    expect(html).not.toContain('localStorage')
    expect(html).toContain('não medido')
  })
  it('reports authentication error and fallback without claiming connected', () => {
    const html = renderToStaticMarkup(<SttProviderSettings initialState={{ ...state, credential_configured: true,
      deepgram_state: 'AUTH_ERROR', connection_state: 'FALLBACK', fallback_active: true,
      last_error: 'Deepgram authentication failed' }} />)
    expect(html).toContain('AUTH_ERROR')
    expect(html).toContain('Deepgram authentication failed')
    expect(html).toContain('Update credential')
    expect(html).not.toContain('CONNECTED')
  })
})
