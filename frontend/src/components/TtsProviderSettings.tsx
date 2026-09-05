import { useCallback, useEffect, useMemo, useState } from 'react'
import { apiGet, apiSend } from '../runtime/api'
import { CustomTtsProfiles, UniversalSettings } from './CustomTtsProfiles'
import './TtsProviderSettings.css'

interface ProviderOption {
  id: string
  name: string
  language?: string
}

export interface ProviderMetadata {
  id: 'local' | 'openai' | 'elevenlabs' | 'gradium' | 'custom'
  display_name: string
  configured: boolean
  selected: boolean
  status: string
  model?: string | null
  voice?: string | null
  models: Array<{ id: string; name: string }>
  voices: ProviderOption[]
  capabilities: Record<string, boolean>
  last_latency_ms?: number | null
}

export interface ProviderState {
  configured_provider: ProviderMetadata['id']
  active_provider: ProviderMetadata['id']
  fallback_provider: 'local'
  fallback_active: boolean
  fallback_reason?: string | null
  online_enabled: boolean
  last_latency_ms?: number | null
  providers: ProviderMetadata[]
  universal?: UniversalSettings
  audio_buffer_delay_ms?: number | null
}

export function TtsProviderSettings({ initialState }: { initialState?: ProviderState } = {}) {
  const [state, setState] = useState<ProviderState | null>(initialState ?? null)
  const [apiKey, setApiKey] = useState('')
  const [voiceDraft, setVoiceDraft] = useState('')
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    const value = await apiGet<ProviderState>('/api/audio/providers', 12000, 'no-store')
    setState(value)
    const selected = value.providers.find((item) => item.id === value.configured_provider)
    setVoiceDraft(String(selected?.voice ?? ''))
  }, [])

  useEffect(() => {
    if (!initialState) void refresh().catch((issue) => setError(messageOf(issue)))
  }, [initialState, refresh])

  const selected = useMemo(
    () => state?.providers.find((item) => item.id === state.configured_provider) ?? null,
    [state],
  )
  const profile = state?.universal?.custom_profiles.find(p => p.id === state.universal?.active_custom_profile)
  const credentialTarget = selected?.id === 'custom' ? `custom:${profile?.id ?? ''}` : selected?.id

  const update = async (body: Record<string, unknown>, label: string) => {
    setBusy(label); setError(''); setNotice('')
    try {
      const value = await apiSend<ProviderState>('/api/audio/providers/settings', 'PUT', body)
      setState(value)
      const current = value.providers.find((item) => item.id === value.configured_provider)
      setVoiceDraft(String(current?.voice ?? ''))
      setNotice('Provider de voz aplicado e persistido.')
    } catch (issue) {
      setError(messageOf(issue))
      if (label === 'profile') throw issue
    } finally {
      setBusy('')
    }
  }

  const saveKey = async () => {
    if (!selected || selected.id === 'local' || !apiKey.trim()) return
    setBusy('credential'); setError(''); setNotice('')
    try {
      await apiSend(`/api/audio/providers/${credentialTarget}/credential`, 'PUT', { api_key: apiKey })
      setApiKey('')
      await refresh()
      setNotice('API key salva com segurança. O valor não será exibido novamente.')
    } catch (issue) {
      setError(messageOf(issue))
    } finally {
      setApiKey('')
      setBusy('')
    }
  }

  const removeKey = async () => {
    if (!selected || selected.id === 'local') return
    setBusy('credential'); setError(''); setNotice('')
    try {
      await apiSend(`/api/audio/providers/${credentialTarget}/credential`, 'DELETE')
      await refresh()
      setNotice('Credencial removida do broker seguro.')
    } catch (issue) {
      setError(messageOf(issue))
    } finally {
      setApiKey(''); setBusy('')
    }
  }

  const refreshCatalog = async () => {
    if (!selected || selected.id === 'local') return
    setBusy('catalog'); setError(''); setNotice('')
    try {
      const value = await apiSend<ProviderState>(`/api/audio/providers/${selected.id}/catalog/refresh`, 'POST', undefined, 45000)
      setState(value)
      const current = value.providers.find((item) => item.id === selected.id)
      setVoiceDraft(String(current?.voice ?? ''))
      setNotice('Catálogo atualizado por ação explícita.')
    } catch (issue) {
      setError(messageOf(issue))
    } finally {
      setBusy('')
    }
  }

  const testProvider = async () => {
    if (!selected) return
    setBusy('test'); setError(''); setNotice('')
    try {
      const result = await apiSend<{ provider: string; synthesis_ms: number }>(
        `/api/audio/providers/${selected.id}/test`, 'POST', undefined, 90000,
      )
      setNotice(`Teste reproduzido por ${result.provider} (${Math.round(result.synthesis_ms)} ms).`)
      await refresh()
    } catch (issue) {
      setError(messageOf(issue))
    } finally {
      setBusy('')
    }
  }

  if (!state || !selected) return <section className="tts-provider-settings" aria-label="Speech Synthesis">
    <h3>Speech Synthesis</h3>
    {error ? <><p role="alert">Não foi possível carregar os providers de voz: {error}</p>
      <button onClick={() => { setError(''); void refresh().catch(issue => setError(messageOf(issue))) }}>Tentar novamente</button></>
      : <div className="ops-empty">Carregando providers de voz…</div>}
  </section>

  const online = selected.id !== 'local'
  return (
    <section className="tts-provider-settings" aria-label="Speech Synthesis">
      <h3>Speech Synthesis</h3>
      <div className="settings-grid">
        <label>
          Provider
          <select
            value={state.configured_provider}
            disabled={Boolean(busy)}
            onChange={(event) => void update({ provider: event.target.value }, 'provider')}
          >
            {state.providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.display_name}</option>)}
          </select>
        </label>
        <label className="tts-online-toggle">
          <input
            type="checkbox"
            checked={state.online_enabled}
            disabled={Boolean(busy)}
            onChange={(event) => void update({ online_enabled: event.target.checked }, 'online')}
          /> Enable Online Voice Providers
        </label>
      </div>

      {!online && (
        <dl className="diagnostic-grid">
          <span>Provider <b>Local</b></span>
          <span>Status <b>{selected.status}</b></span>
          <span>Engine <b>{selected.model ?? 'engine local existente'}</b></span>
          <span>Voice <b>{selected.voice || 'padrão local'}</b></span>
        </dl>
      )}

      {online && (
        <div className="tts-online-config">
          <p className="ops-hint">Quando ativo, somente o texto final destinado à fala é enviado ao provider selecionado.</p>
          {selected.id === 'custom' && state.universal && <CustomTtsProfiles settings={state.universal} busy={Boolean(busy)} save={async universal => { await update({ universal }, 'profile') }} />}
          <div className="settings-grid">
            {selected.id !== 'custom' && <>
            <label>
              Modelo
              <select
                value={selected.model ?? ''}
                disabled={Boolean(busy)}
                onChange={(event) => void update({ provider: selected.id, model: event.target.value }, 'model')}
              >
                {selected.models.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}
              </select>
            </label>
            {selected.id === 'openai' ? (
              <label>
                Voz
                <select
                  value={selected.voice ?? ''}
                  disabled={Boolean(busy)}
                  onChange={(event) => void update({ provider: selected.id, voice: event.target.value }, 'voice')}
                >
                  {selected.voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}</option>)}
                </select>
              </label>
            ) : (
              <label>
                Voice ID
                <input
                  value={voiceDraft}
                  maxLength={128}
                  placeholder="Selecione no catálogo ou informe o Voice ID"
                  onChange={(event) => setVoiceDraft(event.target.value)}
                  onBlur={() => voiceDraft !== (selected.voice ?? '') && void update({ provider: selected.id, voice: voiceDraft }, 'voice')}
                />
              </label>
            )}
            {['elevenlabs', 'gradium'].includes(selected.id) && selected.voices.length > 0 && (
              <label>
                Vozes disponíveis
                <select value={selected.voice ?? ''} onChange={(event) => void update({ provider: selected.id, voice: event.target.value }, 'voice')}>
                  <option value="">Selecione…</option>
                  {selected.voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}</option>)}
                </select>
              </label>
            )}
            </>}
            {(selected.id !== 'custom' || (profile && profile.auth_type !== 'none')) &&
            <label>
              API Key
              <input
                type="password"
                value={apiKey}
                autoComplete="off"
                placeholder={selected.configured ? 'Configured · informe apenas para substituir' : 'Not configured'}
                onChange={(event) => setApiKey(event.target.value)}
              />
            </label>}
          </div>
          {selected.id === 'gradium' && state.universal && <>
            <label>Sample Rate<select value={state.universal.gradium.sample_rate} disabled={Boolean(busy)}
              onChange={e => void update({ universal: { ...state.universal, gradium: { ...state.universal!.gradium, sample_rate: Number(e.target.value) } } }, 'gradium')}>
              {[16000, 24000, 48000].map(rate => <option key={rate} value={rate}>{rate / 1000} kHz</option>)}
            </select></label>
            <details><summary>Advanced — Gradium</summary>
              <div className="settings-grid">
                <label>Endpoint<select value={state.universal.gradium.endpoint} disabled={Boolean(busy)} onChange={e => void update({ universal: { ...state.universal, gradium: { ...state.universal!.gradium, endpoint: e.target.value } } }, 'gradium')}>
                  {['api', 'eu.api', 'us.api'].map(region => <option key={region}>{`wss://${region}.gradium.ai/api/speech/tts`}</option>)}
                </select></label>
                <label>Pronunciation dictionary<input defaultValue={state.universal.gradium.pronunciation_id} onBlur={e => void update({ universal: { ...state.universal, gradium: { ...state.universal!.gradium, pronunciation_id: e.target.value } } }, 'gradium')} /></label>
                {([{ key: 'temp', label: 'Temperature', min: 0, max: 1.4 }, { key: 'cfg_coef', label: 'Voice similarity', min: 1, max: 4 }, { key: 'padding_bonus', label: 'Speed (padding bonus)', min: -4, max: 4 }] as const).map(field =>
                  <label key={field.key}>{field.label}<input type="number" min={field.min} max={field.max} step="0.1" defaultValue={state.universal!.gradium.json_config[field.key]}
                    onBlur={e => void update({ universal: { ...state.universal, gradium: { ...state.universal!.gradium, json_config: { ...state.universal!.gradium.json_config, [field.key]: Number(e.target.value) } } } }, 'gradium')} /></label>)}
              </div>
              <p className="ops-hint">PCM mono incremental. Speed negativo acelera; positivo desacelera. Emoção acústica e nonverbals não são presumidos.</p>
            </details>
          </>}
          <div className="settings-actions tts-provider-actions">
            <button disabled={Boolean(busy) || !apiKey.trim()} onClick={() => void saveKey()}>Save securely</button>
            <button disabled={Boolean(busy) || !selected.configured} onClick={() => void removeKey()}>Remove</button>
            {selected.id !== 'custom' && <button disabled={Boolean(busy) || !state.online_enabled || !selected.configured} onClick={() => void refreshCatalog()}>Atualizar catálogo</button>}
            {['gradium', 'custom'].includes(selected.id) && <button disabled={Boolean(busy) || (!state.online_enabled && !profile?.allow_loopback) || !selected.configured} onClick={async () => {
              setBusy('connection'); setError('')
              try { await apiSend(`/api/audio/providers/${selected.id}/connection`, 'POST', undefined, 90000); setNotice('Conexão e resposta do provider validadas.'); await refresh() }
              catch (issue) { setError(messageOf(issue)) } finally { setBusy('') }
            }}>Testar conexão</button>}
            <button disabled={Boolean(busy) || (!state.online_enabled && !profile?.allow_loopback) || !selected.configured} onClick={() => void testProvider()}>Testar voz</button>
          </div>
          <p className="ops-hint">Testes de provedores online podem consumir créditos do serviço.</p>
        </div>
      )}

      <dl className="diagnostic-grid">
        <span>Configured <b>{selected.configured ? 'yes' : 'no'}</b></span>
        <span>Status <b>{selected.status}</b></span>
        <span>Streaming <b>{selected.capabilities.streaming ? 'ON · PCM incremental' : 'Buffered'}</b></span>
        <span>Ativo real <b>{state.active_provider}</b></span>
        <span>Fallback <b>{state.fallback_active ? `local · ${state.fallback_reason ?? 'ativo'}` : state.fallback_provider}</b></span>
        <span>Última latência <b>{state.last_latency_ms == null ? '—' : `${Math.round(state.last_latency_ms)} ms`}</b></span>
      </dl>
      <details><summary>Diagnostics — Speech Synthesis</summary>
        <p className="ops-hint">Buffer até playback: {state.audio_buffer_delay_ms == null ? 'sem amostra' : `${Math.round(state.audio_buffer_delay_ms)} ms`}. Configuração e READY não substituem um teste de áudio real. A troca vale para o próximo speech_id.</p>
      </details>
      {notice && <p className="lab-notice">{notice}</p>}
      {error && <p className="lab-notice error">{error}</p>}
    </section>
  )
}

function messageOf(value: unknown): string {
  return value instanceof Error ? value.message : String(value)
}
