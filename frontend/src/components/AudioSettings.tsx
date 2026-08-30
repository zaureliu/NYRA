import { useEffect, useState } from 'react'
import type { AudioSettingsValue } from '../hooks/useAudioSettings'
import type { MicrophoneAvailability, MicrophonePermission } from '../hooks/audioDevices'
import { MicrophoneTest } from './MicrophoneTest'

interface ConversationStatus {
  state: string
  stt?: { provider?: string; model?: string; loaded?: boolean }
  tts?: { primary?: string; fallback?: string; emotion_engine_supported?: boolean }
  ollama?: { state?: string; model?: string; keep_alive?: string; metrics?: Record<string, number> }
  performance?: { stt_latency_ms?: number; llm_first_token_ms?: number; end_to_first_audio_ms?: number; playback_start_ms?: number }
}

interface Props {
  value: AudioSettingsValue
  devices: MediaDeviceInfo[]
  microphoneAvailability: MicrophoneAvailability
  microphonePermission: MicrophonePermission
  notice?: string
  onSave: (value: AudioSettingsValue) => Promise<unknown>
}

export function AudioSettings({ value, devices, microphoneAvailability, microphonePermission, notice, onSave }: Props) {
  const [draft, setDraft] = useState(value)
  const [status, setStatus] = useState<ConversationStatus | null>(null)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => setDraft(value), [value])
  useEffect(() => {
    const load = () => void fetch('/api/conversation/status').then((response) => response.json()).then(setStatus).catch(() => undefined)
    load(); const timer = window.setInterval(load, 5000); return () => clearInterval(timer)
  }, [])
  const change = <K extends keyof AudioSettingsValue>(key: K, next: AudioSettingsValue[K]) => setDraft((current) => ({ ...current, [key]: next }))
  const save = async () => {
    setBusy(true)
    try { await onSave(draft); setMessage('Áudio aplicado e persistido no backend.') }
    catch (error) { setMessage(error instanceof Error ? error.message : 'Falha ao aplicar áudio') }
    finally { setBusy(false) }
  }
  const testVoice = async () => {
    setBusy(true)
    try {
      const response = await fetch('/api/audio/test-voice', { method: 'POST' })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail ?? 'Falha no teste de voz')
      setMessage(`${result.provider} · playback confirmado em ${result.playback_start_ms} ms`)
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Falha no teste de voz') }
    finally { setBusy(false) }
  }

  return <div className="audio-settings-v2">
    <section className="settings-group">
      <h3>VOICE</h3>
      <div className="settings-grid">
        <label>Microfone<select value={draft.microphone} onChange={(event) => change('microphone', event.target.value)}><option value="default">Padrão do sistema</option>{devices.filter((item) => item.kind === 'audioinput').map((item) => <option value={item.deviceId} key={item.deviceId}>{item.label || `Entrada ${item.deviceId.slice(0, 6)}`}</option>)}</select></label>
        <label>Saída de áudio<select value={draft.speaker} onChange={(event) => change('speaker', event.target.value)}><option value="default">Padrão do sistema</option>{devices.filter((item) => item.kind === 'audiooutput').map((item) => <option value={item.deviceId} key={item.deviceId}>{item.label || `Saída ${item.deviceId.slice(0, 6)}`}</option>)}</select></label>
        <div className="setting-readonly"><span>Voz da NYRA</span><strong>{status?.tts?.primary === 'kokoro' ? 'NYRA · Feminina V2 local pt-BR' : (status?.tts?.primary ?? 'Carregando…')}</strong></div>
        <label>Emotion Mode<select value={draft.emotion_mode} onChange={(event) => change('emotion_mode', event.target.value as AudioSettingsValue['emotion_mode'])}><option value="automatic">Automatic</option><option value="neutral_only">Neutral Only</option></select></label>
        <label>Expressiveness<select value={draft.expressiveness} onChange={(event) => change('expressiveness', event.target.value as AudioSettingsValue['expressiveness'])}><option value="low">Low</option><option value="normal">Normal</option><option value="high">High</option></select></label>
        <label>Velocidade <output>{draft.speech_speed.toFixed(2)}×</output><input type="range" min=".7" max="1.3" step=".01" value={draft.speech_speed} onChange={(event) => change('speech_speed', Number(event.target.value))}/></label>
        <label>Volume <output>{Math.round(draft.volume * 100)}%</output><input type="range" min="0" max="1" step=".05" value={draft.volume} onChange={(event) => change('volume', Number(event.target.value))}/></label>
      </div>
    </section>
    <section className="settings-group">
      <h3>CONVERSATION</h3>
      <div className="settings-grid">
        <label>Modo de conversa<select value={draft.conversation_mode} onChange={(event) => change('conversation_mode', event.target.value as AudioSettingsValue['conversation_mode'])}><option value="push_to_talk">Push-to-talk</option><option value="wake_word">Wake word “Nyra”</option><option value="hands_free">Hands-free</option></select></label>
        <label><input type="checkbox" checked={draft.always_listening} onChange={(event) => change('always_listening', event.target.checked)}/> Always Listening</label>
        <label><input type="checkbox" checked={draft.allow_interruption} onChange={(event) => change('allow_interruption', event.target.checked)}/> Permitir interrupção</label>
      </div>
      <div className="settings-actions"><button disabled={busy} onClick={() => void save()}>APLICAR ÁUDIO</button><button disabled={busy} onClick={() => void testVoice()}>TESTAR VOZ</button></div>
      {(message || notice) && <p className="lab-notice">{message || notice}</p>}
    </section>
    <details className="settings-accordion audio-diagnostics">
      <summary><span><strong>Audio Diagnostics</strong><small>Estado e latência do pipeline, sem parâmetros decorativos.</small></span><i>+</i></summary>
      <div className="diagnostic-grid">
        <span>Microfone <b>{microphonePermission === 'denied' ? 'Permissão negada' : microphoneAvailability}</b></span>
        <span>STT <b>{status?.stt?.provider ?? '—'} · {status?.stt?.loaded ? 'Ready' : 'Loading'}</b></span>
        <span>TTS <b>{status?.tts?.primary ?? '—'} / fallback {status?.tts?.fallback ?? '—'}</b></span>
        <span>Emotion Engine <b>{status?.tts?.emotion_engine_supported ? 'Native' : 'Planner / neutral acoustic fallback'}</b></span>
        <span>Ollama <b>{status?.ollama?.state ?? '—'} · {status?.ollama?.model ?? '—'}</b></span>
        <span>Keep alive <b>{status?.ollama?.keep_alive ?? '—'}</b></span>
        <span>Conversation <b>{status?.state ?? '—'}</b></span>
        <span>Last STT <b>{status?.performance?.stt_latency_ms == null ? '—' : `${status.performance.stt_latency_ms} ms`}</b></span>
        <span>Last LLM TTFT <b>{status?.performance?.llm_first_token_ms == null ? '—' : `${status.performance.llm_first_token_ms} ms`}</b></span>
        <span>First TTS file <b>{status?.performance?.end_to_first_audio_ms == null ? '—' : `${status.performance.end_to_first_audio_ms} ms`}</b></span>
        <span>Playback TTFA <b>{status?.performance?.playback_start_ms == null ? '—' : `${status.performance.playback_start_ms} ms`}</b></span>
      </div>
      <MicrophoneTest microphone={draft.microphone}/>
    </details>
  </div>
}
