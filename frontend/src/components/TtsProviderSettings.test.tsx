import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ProviderState, TtsProviderSettings } from './TtsProviderSettings'
import { CustomTtsProfiles, emptyProfile, UniversalSettings } from './CustomTtsProfiles'

const providers: ProviderState['providers'] = [
  {
    id: 'local', display_name: 'Local', configured: true, selected: true,
    status: 'LOCAL_READY', model: 'kokoro', voice: 'kazumi-local',
    models: [{ id: 'kokoro', name: 'Kokoro' }],
    voices: [{ id: 'kazumi-local', name: 'Kazumi local' }],
    capabilities: { offline: true },
  },
  {
    id: 'openai', display_name: 'OpenAI', configured: false, selected: false,
    status: 'NOT_CONFIGURED', model: 'gpt-4o-mini-tts', voice: 'coral',
    models: [{ id: 'gpt-4o-mini-tts', name: 'GPT-4o Mini TTS' }],
    voices: [{ id: 'coral', name: 'Coral' }], capabilities: { offline: false },
  },
  {
    id: 'elevenlabs', display_name: 'ElevenLabs', configured: false, selected: false,
    status: 'NOT_CONFIGURED', model: 'eleven_multilingual_v2', voice: '',
    models: [{ id: 'eleven_multilingual_v2', name: 'Eleven Multilingual v2' }],
    voices: [], capabilities: { offline: false },
  },
]

function state(provider: ProviderState['configured_provider'], configured = false): ProviderState {
  return {
    configured_provider: provider,
    active_provider: provider === 'local' ? 'local' : configured ? provider : 'local',
    fallback_provider: 'local',
    fallback_active: provider !== 'local' && !configured,
    fallback_reason: provider !== 'local' && !configured ? 'NOT_CONFIGURED' : null,
    online_enabled: provider !== 'local',
    providers: providers.map((item) => ({
      ...item,
      selected: item.id === provider,
      configured: item.id === provider && provider !== 'local' ? configured : item.configured,
      status: item.id === provider && configured ? 'READY' : item.status,
    })),
  }
}

describe('TtsProviderSettings', () => {
  const universal: UniversalSettings = {
    gradium: { endpoint: 'wss://api.gradium.ai/api/speech/tts', voice_id: '', model: 'default', sample_rate: 48000,
      pronunciation_id: '', json_config: { temp: .7, cfg_coef: 2, padding_bonus: 0, rewrite_rules: null } },
    custom_profiles: [], active_custom_profile: null,
  }
  it('declara Gradium nativo com PCM 48 kHz e testes explícitos, sem fingir configuração', () => {
    const value = state('local')
    value.configured_provider = 'gradium'; value.universal = universal
    value.providers.push({ id: 'gradium', display_name: 'Gradium', configured: false, selected: true,
      status: 'NOT_CONFIGURED', model: 'default', voice: '', models: [{ id: 'default', name: 'default' }], voices: [], capabilities: { streaming: true } })
    const html = renderToStaticMarkup(<TtsProviderSettings initialState={value} />)
    for (const text of ['Speech Synthesis', '48 kHz', 'Testar conexão', 'Testar voz', 'Voice ID', 'NOT_CONFIGURED', 'PCM incremental']) expect(html).toContain(text)
    expect(html).not.toContain('fixture-secret')
  })
  it.each(['rest', 'websocket'] as const)('expõe contratos Custom %s sem scripts nem campo de segredo no perfil', transport => {
    const profile = { ...emptyProfile(), transport, response_mode: transport === 'rest' ? 'RAW_AUDIO_BYTES' : 'WEBSOCKET_JSON_BASE64' }
    const html = renderToStaticMarkup(<CustomTtsProfiles settings={{ ...universal, custom_profiles: [profile], active_custom_profile: profile.id }} busy={false} save={async () => undefined} />)
    for (const text of ['Novo perfil', 'Exportar sem segredo', 'Importar', 'Endpoint URL', 'Authentication Type', 'No Auth', 'JSON templates']) expect(html).toContain(text)
    expect(html).not.toContain('type="password"')
    expect(html).not.toContain('fixture-secret')
  })
  it('mantém Local como tela padrão sem expor controles de credencial', () => {
    const html = renderToStaticMarkup(<TtsProviderSettings initialState={state('local')} />)

    expect(html).toContain('Enable Online Voice Providers')
    expect(html).toContain('LOCAL_READY')
    expect(html).toContain('kazumi-local')
    expect(html).not.toContain('API Key')
    expect(html).not.toContain('TEST PROVIDER')
  })

  it('exibe configuração opcional OpenAI sem jamais renderizar o secret', () => {
    const html = renderToStaticMarkup(<TtsProviderSettings initialState={state('openai', true)} />)

    expect(html).toContain('GPT-4o Mini TTS')
    expect(html).toContain('Coral')
    expect(html).toContain('API Key')
    expect(html).toContain('Save securely')
    expect(html).toContain('Testar voz')
    expect(html).toContain('podem consumir créditos')
    expect(html).toContain('somente o texto final destinado à fala')
    expect(html).not.toContain('fixture-secret')
  })

  it('exibe somente os controles relevantes do ElevenLabs', () => {
    const html = renderToStaticMarkup(<TtsProviderSettings initialState={state('elevenlabs')} />)

    expect(html).toContain('Eleven Multilingual v2')
    expect(html).toContain('Voice ID')
    expect(html).toContain('Not configured')
    expect(html).not.toContain('Coral')
  })
})
