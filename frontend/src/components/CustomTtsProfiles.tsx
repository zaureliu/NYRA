import { useEffect, useState } from 'react'
import { apiGet, apiSend } from '../runtime/api'

export interface GradiumSettings {
  endpoint: string
  voice_id: string
  model: string
  sample_rate: number
  pronunciation_id: string
  json_config: { temp: number; cfg_coef: number; padding_bonus: number; rewrite_rules: string | null }
}
export interface CustomProfile {
  id: string; name: string; endpoint: string; transport: 'rest' | 'websocket'
  allow_loopback: boolean; auth_type: 'bearer' | 'api_key_header' | 'custom_header' | 'none'
  header_name: string; voice_id: string; model: string; language: string
  sample_rate: number; output_format: string; streaming: boolean; fallback: string
  request_template: Record<string, unknown>; setup_template: Record<string, unknown> | null
  text_template: Record<string, unknown>; end_template: Record<string, unknown> | null
  cancel_template: Record<string, unknown> | null; response_mode: string
  audio_field: string; event_type_field: string; audio_event_value: string
  ready_event_value: string; end_event_value: string
}
export interface UniversalSettings {
  gradium: GradiumSettings
  custom_profiles: CustomProfile[]
  active_custom_profile: string | null
}

export function emptyProfile(): CustomProfile {
  return { id: `provider-${Date.now().toString(36)}`, name: 'Novo provider', endpoint: '', transport: 'rest',
    allow_loopback: false, auth_type: 'bearer', header_name: 'x-api-key', voice_id: '', model: '', language: 'pt-BR',
    sample_rate: 48000, output_format: 'pcm_s16le', streaming: true, fallback: 'local',
    request_template: { text: '{{text}}', voice: '{{voice_id}}' }, setup_template: null,
    text_template: { type: 'text', text: '{{text}}' }, end_template: null, cancel_template: null,
    response_mode: 'RAW_AUDIO_BYTES', audio_field: 'audio', event_type_field: 'type',
    audio_event_value: 'audio', ready_event_value: '', end_event_value: 'end_of_stream' }
}

export function CustomTtsProfiles({ settings, busy, save }: {
  settings: UniversalSettings; busy: boolean; save: (settings: UniversalSettings) => Promise<void>
}) {
  const active = settings.custom_profiles.find(p => p.id === settings.active_custom_profile)
  const [draft, setDraft] = useState<CustomProfile | null>(active ?? null)
  const [advanced, setAdvanced] = useState('')
  const [error, setError] = useState('')
  const setProfile = (value: CustomProfile | null) => {
    setDraft(value); setError('')
    setAdvanced(value ? JSON.stringify(Object.fromEntries(Object.entries(value).filter(([k]) => k.endsWith('_template'))), null, 2) : '')
  }
  useEffect(() => { setProfile(active ?? null) }, [active])
  const change = (key: keyof CustomProfile, value: unknown) => setDraft(current => current ? { ...current, [key]: value } : current)
  const submit = async () => {
    if (!draft) return
    try {
      const templates = JSON.parse(advanced)
      if (Object.keys(templates).some(k => !['request_template', 'setup_template', 'text_template', 'end_template', 'cancel_template'].includes(k))) throw Error('Advanced aceita somente os cinco templates JSON.')
      const profile = { ...draft, ...templates }
      await save({ ...settings, active_custom_profile: profile.id,
        custom_profiles: [...settings.custom_profiles.filter(p => p.id !== profile.id), profile] })
    } catch { setError('Perfil inválido. Use apenas JSON declarativo, sem credenciais.') }
  }
  const remove = async () => {
    if (!active) return
    try {
      if (active.auth_type !== 'none') await apiSend(`/api/audio/providers/custom:${active.id}/credential`, 'DELETE')
      const remaining = settings.custom_profiles.filter(p => p.id !== active.id)
      await save({ ...settings, custom_profiles: remaining, active_custom_profile: remaining[0]?.id ?? null })
    } catch { setError('Não foi possível excluir o perfil com segurança.') }
  }
  const exportProfile = async () => {
    if (!active) return
    try {
      const data = await apiGet<CustomProfile>(`/api/audio/providers/custom/profiles/${active.id}/export`)
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a'); link.href = url; link.download = `${active.id}.tts.json`; link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch { setError('Exportação não disponível.') }
  }
  const importProfile = async (file?: File) => {
    if (!file) return
    try {
      if (file.size > 100000) throw Error()
      const value = JSON.parse(await file.text())
      const allowed = Object.keys(emptyProfile())
      if (!value || Array.isArray(value) || Object.keys(value).some(k => !allowed.includes(k))) throw Error()
      // Import as a NEW profile: never binds an imported file to an existing secret.
      setProfile({ ...emptyProfile(), ...value, id: emptyProfile().id })
    } catch { setError('Importação inválida. Credenciais e campos desconhecidos são proibidos.') }
  }
  return <div className="custom-tts-profiles">
    <p className="ops-hint">Configuração avançada para contratos REST/WebSocket declarativos. Protocolos proprietários podem exigir adapter dedicado. Prefira Gradium nativo quando disponível.</p>
    <label>Profile<select value={settings.active_custom_profile ?? ''} disabled={busy}
      onChange={e => void save({ ...settings, active_custom_profile: e.target.value || null })}>
      <option value="">Selecione um perfil</option>
      {settings.custom_profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
    </select></label>
    <div className="settings-actions">
      <button disabled={busy} onClick={() => setProfile(emptyProfile())}>Novo perfil</button>
      <button disabled={busy || !active} onClick={() => setProfile(active ?? null)}>Editar</button>
      <button disabled={busy || !active} onClick={() => void remove()}>Excluir perfil</button>
      <button disabled={busy || !active} onClick={() => void exportProfile()}>Exportar sem segredo</button>
      <label>Importar<input aria-label="Importar perfil TTS" type="file" accept=".json,application/json" disabled={busy}
        onChange={e => { void importProfile(e.target.files?.[0]); e.target.value = '' }} /></label>
    </div>
    {draft && <fieldset disabled={busy}>
      <legend>Perfil Custom — sem segredos</legend>
      <div className="settings-grid">
        {(['name', 'endpoint', 'voice_id', 'model', 'language'] as const).map(key => <label key={key}>
          {{ name: 'Provider Name', endpoint: 'Endpoint URL', voice_id: 'Voice ID', model: 'Model', language: 'Language' }[key]}
          <input value={draft[key]} maxLength={key === 'endpoint' ? 2048 : 128} onChange={e => change(key, e.target.value)} />
        </label>)}
        <label>Transport<select value={draft.transport} onChange={e => setDraft({ ...draft,
          transport: e.target.value as CustomProfile['transport'], response_mode: e.target.value === 'rest' ? 'RAW_AUDIO_BYTES' : 'WEBSOCKET_JSON_BASE64' })}>
          <option value="rest">REST POST</option><option value="websocket">WebSocket</option>
        </select></label>
        <label>Authentication Type<select value={draft.auth_type} onChange={e => change('auth_type', e.target.value)}>
          <option value="bearer">Bearer Token</option><option value="api_key_header">API Key Header</option>
          <option value="custom_header">Custom Header</option><option value="none">No Auth</option>
        </select></label>
        {['api_key_header', 'custom_header'].includes(draft.auth_type) && <label>Header name<input value={draft.header_name} onChange={e => change('header_name', e.target.value)} /></label>}
        <label>Sample Rate<select value={draft.sample_rate} onChange={e => change('sample_rate', Number(e.target.value))}>
          {[16000, 24000, 48000].map(rate => <option key={rate} value={rate}>{rate / 1000} kHz</option>)}
        </select></label>
        <label>Output Format<select value={draft.output_format} onChange={e => setDraft({ ...draft, output_format: e.target.value, streaming: false })}>
          {['pcm_s16le', 'wav', 'mp3', 'ogg'].map(f => <option key={f}>{f}</option>)}
        </select></label>
        <label>Response Mode<select value={draft.response_mode} onChange={e => setDraft({ ...draft, response_mode: e.target.value, streaming: e.target.value === 'JSON_BASE64_AUDIO' ? false : draft.streaming })}>
          {(draft.transport === 'rest' ? ['RAW_AUDIO_BYTES', 'JSON_BASE64_AUDIO'] : ['WEBSOCKET_BINARY_FRAMES', 'WEBSOCKET_JSON_BASE64']).map(mode => <option key={mode}>{mode}</option>)}
        </select></label>
        <label>Fallback<select value={draft.fallback} onChange={e => change('fallback', e.target.value)}><option value="local">Local / Kokoro</option><option value="none">Sem fallback</option></select></label>
        <label><input type="checkbox" checked={draft.streaming} disabled={draft.output_format !== 'pcm_s16le' || draft.response_mode === 'JSON_BASE64_AUDIO'} onChange={e => change('streaming', e.target.checked)} />Streaming PCM</label>
        <label><input type="checkbox" checked={draft.allow_loopback} onChange={e => change('allow_loopback', e.target.checked)} />Permitir provider local (loopback)</label>
      </div>
      <details><summary>Advanced — templates e resposta</summary>
        <p className="ops-hint">Placeholders: text, voice_id, model, language, sample_rate, output_format, emotion, style, speed. Somente {'{{placeholder}}'}, sem scripts. Nunca cole tokens aqui.</p>
        <label>JSON templates<textarea rows={14} value={advanced} spellCheck={false} onChange={e => setAdvanced(e.target.value)} /></label>
        <div className="settings-grid">{(['audio_field', 'event_type_field', 'audio_event_value', 'ready_event_value', 'end_event_value'] as const).map(key =>
          <label key={key}>{key}<input value={draft[key]} onChange={e => change(key, e.target.value)} /></label>)}</div>
        <p className="ops-hint">WAV/MP3/OGG e JSON REST são recebidos por completo antes de decodificar. Testar conexão faz síntese mínima, sem playback, e pode consumir créditos.</p>
      </details>
      <button onClick={() => void submit()}>Salvar perfil</button>
    </fieldset>}
    {error && <p role="alert">{error}</p>}
  </div>
}
