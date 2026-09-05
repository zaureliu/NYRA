import { useEffect, useRef, useState } from 'react'
import { apiGet, apiSend } from '../runtime/api'

export interface RecognitionSettings {
  provider: 'deepgram' | 'faster_whisper'; model: string; language: string
  smart_format: boolean; interim_results: boolean; utterance_end_ms: number; endpointing: number
  vad_events: boolean; punctuate: boolean; numerals: boolean; profanity_filter: boolean
  diarize: boolean; redact: false; dictation: boolean; fallback: 'faster_whisper'
  keyterms_enabled: boolean; keyterms: string[]
}
export interface RecognitionStatus {
  settings: RecognitionSettings; credential_configured: boolean; deepgram_state: string
  active_provider: string | null; connection_state: string; fallback_available: boolean
  fallback_loaded: boolean; fallback_active: boolean; last_error: string | null
  diagnostics: { audio_format?: { encoding: string; sample_rate: number; channels: number };
    mic_to_first_interim_ms?: number | null; mic_to_final_ms?: number | null;
    audio_duration_seconds?: number; audio_sent_bytes?: number; queue_overflows?: number;
    duplicates_suppressed?: number; timestamps?: Record<string, number>; fallback_reason?: string | null }
}

export function SttProviderSettings({ initialState }: { initialState?: RecognitionStatus } = {}) {
  const [state, setState] = useState<RecognitionStatus | null>(initialState ?? null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [credentialOpen, setCredentialOpen] = useState(false)
  const credentialInput = useRef<HTMLInputElement | null>(null)
  useEffect(() => {
    if (initialState) return
    let disposed = false
    const refresh = () => void apiGet<RecognitionStatus>('/api/stt/settings', 10000, 'no-store')
      .then((value) => { if (!disposed) setState(value) }).catch(() => { if (!disposed) setError('Reconhecimento de fala indisponível') })
    refresh(); const timer = setInterval(refresh, 4000)
    return () => { disposed = true; clearInterval(timer) }
  }, [initialState])

  const update = async (patch: Partial<RecognitionSettings>) => {
    if (!state) return
    setBusy(true); setError(''); setNotice('')
    try {
      setState(await apiSend<RecognitionStatus>('/api/stt/settings', 'PUT', { ...state.settings, ...patch }))
      setNotice('Reconhecimento atualizado. A próxima frase usará esta configuração.')
    } catch { setError('Não foi possível aplicar a configuração de reconhecimento') }
    finally { setBusy(false) }
  }
  const saveCredential = async () => {
    const input = credentialInput.current
    if (!input?.value.trim()) return
    const body = { api_key: input.value }; input.value = ''
    setBusy(true); setError('')
    try {
      setState(await apiSend<RecognitionStatus>('/api/stt/credential', 'PUT', body))
      setCredentialOpen(false); setNotice('Credencial salva no Broker. Deepgram selecionado.')
    } catch { setError('Não foi possível salvar no Credential Broker') }
    finally { body.api_key = ''; setBusy(false) }
  }
  const removeCredential = async () => {
    setBusy(true); setError('')
    try {
      setState(await apiSend<RecognitionStatus>('/api/stt/credential', 'DELETE'))
      setNotice('Credencial removida do Broker. Fallback local disponível.')
    } catch { setError('Não foi possível remover a credencial') }
    finally { setBusy(false) }
  }
  const testConnection = async () => {
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await apiSend<{ auth: string; websocket: string; message?: string }>('/api/stt/probe', 'POST')
      setNotice(result.message ?? `AUTH: ${result.auth} · WEBSOCKET: ${result.websocket}`)
      setState(await apiGet<RecognitionStatus>('/api/stt/settings'))
    } catch { setError('Não foi possível testar a conexão Deepgram') }
    finally { setBusy(false) }
  }
  if (!state) return <section className="stt-provider-settings" aria-label="Speech Recognition"><h3>Speech Recognition</h3><p>{error || 'Carregando…'}</p></section>
  const config = state.settings
  const toggles: Array<[keyof RecognitionSettings, string]> = [
    ['smart_format', 'Smart Format'], ['interim_results', 'Interim Results'], ['vad_events', 'Speech Started / VAD Events'],
    ['punctuate', 'Punctuation'], ['numerals', 'Numerals'], ['profanity_filter', 'Profanity Filter'],
  ]
  const latency = (value?: number | null) => value == null ? 'não medido' : `${Math.round(value)} ms`
  return <section className="settings-group stt-provider-settings" aria-label="Speech Recognition">
    <h3>Speech Recognition</h3>
    <div className="settings-grid">
      <label>Provider<select value={config.provider} disabled={busy} onChange={(event) => void update({ provider: event.target.value as RecognitionSettings['provider'] })}>
        <option value="deepgram">Deepgram · Cloud STT</option><option value="faster_whisper">Faster-Whisper Local · Local STT</option>
      </select></label>
      <label>Model<select value={config.model} disabled><option value="nova-3">nova-3 (Deepgram)</option></select></label>
      <label>Language<input defaultValue={config.language} key={config.language} maxLength={12} disabled={busy}
        onBlur={(event) => { if (event.target.value !== config.language) void update({ language: event.target.value }) }} /></label>
    </div>
    <p className="ops-hint">Deepgram envia áudio ao serviço de transcrição quando selecionado e configurado. Faster-Whisper processa o áudio neste computador.</p>
    <dl className="diagnostic-grid">
      <span>Deepgram Status <b>{state.deepgram_state}</b></span>
      <span>Credential <b>{state.credential_configured ? 'Configured' : 'Not Configured'}</b></span>
      <span>Streaming <b>{config.provider === 'deepgram' ? 'ON' : 'Não disponível no provider local'}</b></span>
      <span>Fallback <b>Faster-Whisper Local · {state.fallback_available ? 'disponível' : 'indisponível'}</b></span>
    </dl>
    <div className="settings-grid">{toggles.map(([key, label]) => <label key={key}>
      <input type="checkbox" checked={Boolean(config[key]) || (key === 'punctuate' && config.smart_format)} disabled={busy || (key === 'punctuate' && config.smart_format)} onChange={(event) => void update({ [key]: event.target.checked })} /> {label}
    </label>)}</div>
    <div className="settings-actions">
      <button disabled={busy} onClick={() => setCredentialOpen(!credentialOpen)}>{state.credential_configured ? 'Update credential' : 'Configure credential'}</button>
      <button disabled={busy || !state.credential_configured} onClick={() => void removeCredential()}>Remove credential</button>
      <button disabled={busy || !state.credential_configured || config.provider !== 'deepgram'} onClick={() => void testConnection()}>Testar conexão</button>
    </div>
    {credentialOpen && <div className="settings-grid"><label>Deepgram API key<input ref={credentialInput} type="password" autoComplete="off" spellCheck={false} maxLength={4096} /></label>
      <button disabled={busy} onClick={() => void saveCredential()}>Salvar no Broker e selecionar Deepgram</button></div>}
    <details><summary>Advanced</summary><div className="settings-grid">
      <label>Endpointing (ms)<input type="number" min={100} max={2000} step={100} key={`ep-${config.endpointing}`} defaultValue={config.endpointing} disabled={busy}
        onBlur={(event) => { if (+event.target.value !== config.endpointing) void update({ endpointing: +event.target.value }) }} /></label>
      <label>Utterance End (ms)<input type="number" min={1000} max={5000} step={100} key={`ue-${config.utterance_end_ms}`} defaultValue={config.utterance_end_ms} disabled={busy || !config.interim_results}
        onBlur={(event) => { if (+event.target.value !== config.utterance_end_ms) void update({ utterance_end_ms: +event.target.value }) }} /></label>
      <label><input type="checkbox" checked={config.diarize} disabled={busy} onChange={(event) => void update({ diarize: event.target.checked })} /> Diarization</label>
      <label><input type="checkbox" checked={config.keyterms_enabled} disabled={busy} onChange={(event) => void update({ keyterms_enabled: event.target.checked })} /> Keyterms</label>
      <label>Termos específicos (um por linha, até 20)<textarea defaultValue={config.keyterms.join('\n')} key={config.keyterms.join('|')} disabled={busy || !config.keyterms_enabled}
        onBlur={(event) => void update({ keyterms: event.target.value.split('\n').map((term) => term.trim()).filter(Boolean) })} /></label>
    </div><p className="ops-hint">Smart Format também aplica pontuação. Utterance End requer Interim Results. O formato de áudio acompanha a captura existente: PCM 16-bit, mono, sem conversão desnecessária.</p></details>
    <details><summary>Diagnostics · Speech Recognition</summary><dl className="diagnostic-grid">
      <span>Conexão <b>{state.connection_state}</b></span><span>Provider ativo <b>{state.active_provider ?? 'sem stream'}</b></span>
      <span>Áudio <b>{state.diagnostics.audio_format ? `${state.diagnostics.audio_format.encoding} · ${state.diagnostics.audio_format.sample_rate} Hz · mono` : 'não capturado'}</b></span>
      <span>MIC → first interim <b>{latency(state.diagnostics.mic_to_first_interim_ms)}</b></span>
      <span>MIC → final <b>{latency(state.diagnostics.mic_to_final_ms)}</b></span>
      <span>Áudio enviado <b>{state.diagnostics.audio_sent_bytes ?? 0} bytes</b></span>
      <span>Fila excedida <b>{state.diagnostics.queue_overflows ?? 0}</b></span>
      <span>Duplicatas suprimidas <b>{state.diagnostics.duplicates_suppressed ?? 0}</b></span>
      <span>Fallback <b>{state.diagnostics.fallback_reason ?? 'inativo'}</b></span>
      <span>Último final <b>{state.diagnostics.timestamps?.last_final_transcript_time ? new Date(state.diagnostics.timestamps.last_final_transcript_time * 1000).toLocaleTimeString() : 'não medido'}</b></span>
    </dl></details>
    {(error || state.last_error) && <p className="lab-notice error" role="alert">{error || state.last_error}</p>}
    {notice && <p className="lab-notice">{notice}</p>}
  </section>
}
