import { useEffect, useState } from 'react'
import { apiGet, apiSend } from '../../runtime/api'
import { usePolling } from '../hooks'
import { ActionButton, Card, ErrorAlert, StatusBadge, Toggle, formatMs } from '../ui'
import { MicrophoneTest } from '../../components/MicrophoneTest'
import { TtsProviderSettings } from '../../components/TtsProviderSettings'
import { SttProviderSettings } from '../../components/SttProviderSettings'
import type { VoiceBridgeStatus } from '../types'
import './VoicePage.css'

interface AudioSettingsShape {
  settings: {
    voice: string
    speed: number
    volume: number
    mode?: string
    microphone?: string
  }
  voices: Array<{ id: string; name: string; language: string; provider: string }>
}

interface ConversationStatus {
  natural_conversation?: { enabled: boolean; conversation_id: string; states: string[]; speech_queue_state: number; echo_guard: string; metrics: Record<string, {count: number; average_ms: number | null; p50_ms: number | null; p95_ms: number | null}> }
  state?: string
  stt_ready?: boolean
  tts_provider?: string
}

interface RealtimeDebugInfo {
  last_turn?: Record<string, unknown>
  recent?: Array<Record<string, unknown>>
  snapshot?: Record<string, unknown>
}

export function VoicePage() {
  const audio = usePolling<AudioSettingsShape>('/api/audio/settings', 10000)
  const conversation = usePolling<ConversationStatus>('/api/conversation/status', 5000)
  const bridge = usePolling<VoiceBridgeStatus>('/api/voice-bridge/status', 8000)
  const realtime = usePolling<RealtimeDebugInfo>('/api/realtime/debug', 4000)
  const profiles = usePolling<{ profiles: Array<{ profile_id: string; name: string; description: string; active: boolean }> }>('/api/voice/profiles', 15000)

  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState('')
  const [volume, setVolume] = useState<number | null>(null)
  const [bargeIn, setBargeIn] = useState(true)

  useEffect(() => {
    void apiGet<{ settings: Array<{ key: string; current: unknown }> }>('/api/settings/v3')
      .then((payload) => {
        const entry = payload.settings.find((item) => item.key === 'voice_barge_in')
        if (entry && typeof entry.current === 'boolean') setBargeIn(entry.current)
      })
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    if (volume === null && audio.data?.settings) setVolume(audio.data.settings.volume)
  }, [audio.data, volume])

  const patchAudio = async (updates: Record<string, unknown>, label: string) => {
    setBusy(label)
    setError('')
    try {
      await apiSend('/api/audio/settings', 'PUT', updates)
      audio.refresh()
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  const testVoice = async () => {
    setBusy('test-voice')
    setError('')
    try {
      const result = await apiSend<{ synthesis_ms: number; provider: string }>('/api/audio/test-voice', 'POST')
      setNotice(`Voz de teste reproduzida (${result.provider}, ${result.synthesis_ms}ms).`)
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  const toggleListening = async (enabled: boolean) => {
    setBusy('listening')
    try {
      await apiSend(`/api/listening/${enabled ? 'start' : 'stop'}`, 'POST')
      conversation.refresh()
      setNotice(enabled ? 'Escuta contínua iniciada.' : 'Escuta contínua parada.')
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  const putSetting = async (key: string, value: unknown) => {
    try {
      await apiSend('/api/settings/v3', 'PUT', { key, value })
      conversation.refresh()
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : String(issue))
    }
  }

  const activateProfile = async (profileId: string) => {
    setBusy('profile')
    setError('')
    try {
      await apiSend(`/api/voice/profiles/${profileId}/activate`, 'POST')
      profiles.refresh()
      setNotice('Perfil de voz ativado e persistido.')
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  const testBridge = async () => {
    setBusy('bridge-test')
    setError('')
    try {
      const result = await apiSend<{ ok?: boolean; latency_ms?: number; capabilities?: Record<string, boolean> }>('/api/voice-bridge/test', 'POST')
      if (result.ok === false) {
        setError('Processor externo não respondeu ao health check.')
      } else {
        setNotice(`Bridge OK (${result.latency_ms}ms). Capabilities: ${Object.entries(result.capabilities ?? {}).filter(([, v]) => v).map(([k]) => k).join(', ') || '—'}`)
      }
      bridge.refresh()
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  const lastTurn = realtime.data?.last_turn as Record<string, number | string> | undefined

  return (
    <div className="voice-page">
      <header className="ops-page-header">
        <div>
          <h1 className="ops-page-title">Voz</h1>
          <p className="ops-page-subtitle">
            Pipeline V3: mic → VAD → STT → LLM → segmenter → TTS → playback.
            Chat textual nunca depende daqui.
          </p>
        </div>
        <div className="ops-header-spacer" />
        <StatusBadge state={conversation.data?.stt_ready === false ? 'DEGRADED' : 'READY'} label={`STT ${conversation.data?.state ?? ''}`} />
      </header>

      <ErrorAlert message={error} />
      {notice && <div className="ops-alert info">{notice}</div>}

      <Card title="Provider de voz" sub="Local por padrão; serviços online são opt-in">
        <TtsProviderSettings />
      </Card>

      <h2 className="ops-section-title">Perfis de conversação</h2>
      <Card sub="Cada perfil muda o runtime de verdade (persistido no backend)">
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {(profiles.data?.profiles ?? []).map((profile) => (
            <ActionButton
              key={profile.profile_id}
              small
              variant={profile.active ? 'primary' : undefined}
              busy={busy === 'profile' && profile.active}
              title={profile.description}
              onClick={() => void activateProfile(profile.profile_id)}
            >
              {profile.name}{profile.active ? ' ✓' : ''}
            </ActionButton>
          ))}
        </div>
      </Card>

      <div className="ops-grid-2" style={{ marginTop: 14 }}>
        <Card title="Entrada" sub="Microfone ativo para STT">
          <MicrophoneTest microphone={audio.data?.settings.microphone ?? 'default'} />
        </Card>

        <Card title="Voz sintetizada" sub={`Provider ativo: ${conversation.data?.tts_provider ?? audio.data?.settings.voice ?? '—'}`}>
          <div className="ops-field">
            <label htmlFor="voice-select">Voz</label>
            <select
              id="voice-select"
              value={audio.data?.settings.voice ?? ''}
              disabled={(audio.data?.voices.length ?? 0) === 0}
              onChange={(event) => void patchAudio({ voice: event.target.value }, 'voice')}
            >
              {(audio.data?.voices.length ?? 0) === 0 && <option value="">catálogo indisponível</option>}
              {(audio.data?.voices ?? []).map((voice) => (
                <option key={voice.id} value={voice.id}>
                  {voice.name} · {voice.language}
                </option>
              ))}
            </select>
            <span className="ops-hint">Catálogo vem do backend (fonte única).</span>
          </div>
          <div className="ops-field">
            <label htmlFor="volume-range">Volume</label>
            <input
              id="volume-range"
              type="number" min={0} max={1} step={0.05}
              value={volume ?? 0.9}
              onChange={(event) => setVolume(Number(event.target.value))}
              onBlur={() => volume !== null && void patchAudio({ volume }, 'volume')}
            />
          </div>
          <ActionButton busy={busy === 'test-voice'} onClick={() => void testVoice()}>Testar voz</ActionButton>
        </Card>
      </div>

      <SttProviderSettings />
      {conversation.data?.natural_conversation && <details className="ops-card" style={{fontSize: 14, marginTop: 14}}>
        <summary style={{fontSize: 15}}>Diagnostics — Natural Conversation</summary>
        <dl className="ops-kv">
          <dt>Session</dt><dd style={{overflowWrap: 'anywhere'}}>{conversation.data.natural_conversation.conversation_id}</dd>
          <dt>State</dt><dd>{conversation.data.natural_conversation.states.join(' + ')}</dd>
          <dt>Speech queue</dt><dd>{conversation.data.natural_conversation.speech_queue_state}</dd>
          <dt>Echo guard</dt><dd>AEC + playback reference</dd>
        </dl>
        {Object.entries(conversation.data.natural_conversation.metrics).map(([name, metric]) => <p key={name} style={{overflowWrap: 'anywhere'}}>
          {name}: n={metric.count}; avg {formatMs(metric.average_ms)}; p50 {formatMs(metric.p50_ms)}; p95 {formatMs(metric.p95_ms)}
        </p>)}
      </details>}
      <h2 className="ops-section-title">Conversação</h2>
      <div className="ops-grid-2">
        <Card>
          <dl className="ops-kv">
            <dt>Always Listening</dt>
            <dd>
              <Toggle
                checked={Boolean(conversation.data?.state && conversation.data.state !== 'OFF')}
                disabled={busy === 'listening'}
                label=""
                onChange={(value) => void toggleListening(value)}
              />
            </dd>
            <dt>Barge-in</dt>
            <dd>
              <Toggle checked={bargeIn} label=""
                onChange={(value) => { setBargeIn(value); void putSetting('voice_barge_in', value) }} />
            </dd>
            <dt>Estado do pipeline</dt><dd>{conversation.data?.state ?? '—'}</dd>
            <dt>Natural Conversation</dt>
            <dd><Toggle checked={Boolean(conversation.data?.natural_conversation?.enabled)} label=""
              onChange={(value) => void putSetting('natural_conversation_enabled', value)} /></dd>
          </dl>
        </Card>
        <Card title="Processor externo" sub="VoiceProcessorBridge — apenas localhost">
          <dl className="ops-kv">
            <dt>Estado</dt><dd><StatusBadge state={bridge.data?.health ?? 'DISABLED'} /></dd>
            <dt>Endpoint</dt><dd>{bridge.data?.endpoint ?? 'http://127.0.0.1:8977'}</dd>
            <dt>Capabilities</dt>
            <dd>{Object.entries(bridge.data?.capabilities ?? {}).filter(([, v]) => v).map(([k]) => k).join(', ') || '—'}</dd>
            <dt>Fallback interno</dt>
            <dd>{bridge.data?.fallback_internal_active ? 'ATIVO (processor offline)' : 'em espera'}</dd>
          </dl>
          <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
            <ActionButton small busy={busy === 'bridge-test'} onClick={() => void testBridge()}>Testar bridge</ActionButton>
            <ToggleButtonBridge enabled={bridge.data?.enabled ?? false} onDone={() => bridge.refresh()} onError={(m) => setError(m)} />
          </div>
        </Card>
      </div>

      <h2 className="ops-section-title">Diagnóstico</h2>
      <div className="ops-grid-2">
        <Card sub="Métricas do último turno (Realtime Telemetry)">
          {lastTurn ? (
            <dl className="ops-kv">
              <dt>STT latency</dt><dd>{formatMs(num(lastTurn.stt_latency_ms ?? lastTurn.stt_total_ms))}</dd>
              <dt>LLM TTFT</dt><dd>{formatMs(num(lastTurn.llm_first_token_ms ?? lastTurn.ollama_first_token_ms))}</dd>
              <dt>TTS TTFA</dt><dd>{formatMs(num(lastTurn.tts_first_audio_ms))}</dd>
              <dt>Fala → primeiro áudio</dt><dd>{formatMs(num(lastTurn.end_to_first_audio_ms ?? lastTurn.speech_to_playback_ms))}</dd>
              <dt>Total do turno</dt><dd>{formatMs(num(lastTurn.request_total_ms))}</dd>
            </dl>
          ) : (
            <div className="ops-empty">Nenhum turno medido ainda — fale com a NYRA ou envie uma mensagem.</div>
          )}
        </Card>
        <Card title="Último erro de pipeline" sub="Envelope seguro do backend">
          <pre className="ops-code" style={{ maxHeight: 180 }}>
{JSON.stringify(realtime.data?.snapshot ?? {}, null, 2).slice(0, 900)}
          </pre>
        </Card>
      </div>
    </div>
  )
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function ToggleButtonBridge({ enabled, onDone, onError }: {
  enabled: boolean
  onDone: () => void
  onError: (message: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const toggle = async () => {
    setBusy(true)
    try {
      await apiSend('/api/voice-bridge/settings', 'PUT', { enabled: !enabled })
      onDone()
    } catch (issue) {
      onError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy(false)
    }
  }
  return (
    <ActionButton small variant={enabled ? undefined : 'primary'} busy={busy} onClick={() => void toggle()}>
      {enabled ? 'Desabilitar bridge' : 'Habilitar bridge'}
    </ActionButton>
  )
}
