import { usePolling } from './hooks'
import type { Health } from '../types'

interface Readiness {
  state?: string
  ready?: boolean
  model?: string
  last_error?: string | null
}

interface WatchdogLite {
  success?: boolean
  running?: boolean
  stale?: boolean
  heartbeat_age_seconds?: number
  error_code?: string
}

interface TasksLite {
  tasks?: Array<{ state?: string }>
}

interface SelfDevLite {
  state?: string
  unread_notifications?: number
}

const LIVE_STATUS_POLLING = { clearOnError: true, headerStatus: true, retryIntervalMs: 1000, noStore: true } as const

export function TopStatusBar() {
  const healthStatus = usePolling<Health>('/api/health', 5000, LIVE_STATUS_POLLING)
  const readiness = usePolling<Readiness>('/api/ollama/readiness', 5000, LIVE_STATUS_POLLING)
  const watchdog = usePolling<WatchdogLite>('/api/watchdog/status', 5000, LIVE_STATUS_POLLING)
  const tasks = usePolling<TasksLite>('/api/tasks?limit=10', 10000)
  const selfdev = usePolling<SelfDevLite>('/api/selfdev/status', 5000, LIVE_STATUS_POLLING)

  const health = healthStatus.data
  const backendState = health ? 'ONLINE' : healthStatus.loading && !healthStatus.error ? 'STARTING' : 'OFFLINE'
  const kazumiState = health ? normalizeKnownState(health.status) : backendState
  const ollama = readiness.data ?? health?.ollama
  const ollamaState = resolveOllamaState(ollama, readiness.loading, readiness.error, Boolean(health))
  const activeModel = ollamaState === 'READY' ? String(ollama?.model ?? '').trim() : ''
  const configuredModel = String(health?.model ?? '').trim()
  const ollamaValue = activeModel ? `${ollamaState} · ${activeModel}` : ollamaState
  const ollamaDetail = [
    `Ollama: ${ollamaState}`,
    activeModel ? `modelo ativo/residente: ${activeModel}` : '',
    configuredModel ? `modelo configurado/default: ${configuredModel}` : '',
  ].filter(Boolean).join(' · ')
  const voiceState = resolveVoiceState(health, healthStatus.loading, healthStatus.error)
  const watchdogState = resolveWatchdogState(watchdog.data, watchdog.loading, watchdog.error)
  const selfdevState = resolveSelfDevState(selfdev.data, selfdev.loading, selfdev.error)
  const activeTasks = (tasks.data?.tasks ?? []).filter((task) => ['RUNNING', 'PENDING'].includes(String(task.state).toUpperCase())).length

  return (
    <header className="ops-topbar">
      <div className="ops-brand">
        <strong>KAZUMI</strong>
        <span>OPS V3</span>
      </div>
      <div className="ops-topbar-chips" aria-label="Estado do sistema">
        <Chip tone={toneOf(kazumiState)} label="KAZUMI" value={kazumiState} />
        <Chip tone={toneOf(ollamaState)} label="Ollama" value={ollamaValue} title={ollamaDetail} />
        <Chip tone={toneOf(voiceState)} label="Voz" value={voiceState} />
        <Chip tone={toneOf(backendState)} label="Backend" value={backendState} />
        <Chip tone={toneOf(watchdogState)} label="Watchdog" value={watchdogState} />
        <Chip tone={activeTasks > 0 ? 'ok' : 'off'} label="Tarefa" value={activeTasks > 0 ? `${activeTasks} ativa(s)` : 'nenhuma'} />
        <Chip
          tone={toneOf(selfdevState)}
          label="Self-Dev"
          value={(selfdev.data?.unread_notifications ?? 0) > 0 ? `${selfdevState} · ${selfdev.data?.unread_notifications} nova(s)` : selfdevState}
        />
      </div>
    </header>
  )
}

function Chip({ tone, label, value, title }: { tone: 'ok' | 'warn' | 'err' | 'off'; label: string; value: string; title?: string }) {
  return (
    <span className="ops-chip" data-tone={tone} title={title ?? `${label}: ${value}`}>
      <span className="chip-dot" />
      <span>{label}</span>
      <span style={{ color: 'var(--ops-faint)' }}>{value}</span>
    </span>
  )
}

function toneOf(state: string): 'ok' | 'warn' | 'err' | 'off' {
  const value = String(state).toUpperCase()
  if (['READY', 'IDLE', 'ONLINE', 'LISTENING', 'ATIVO', 'PRONTA'].includes(value)) return 'ok'
  if (['OFFLINE', 'ERROR', 'BLOCKED'].includes(value)) return 'err'
  if (['UNKNOWN', 'OFF', 'INATIVO', 'VERIFICANDO'].includes(value)) return 'off'
  return 'warn'
}

function normalizeKnownState(state: string | undefined): string {
  const value = String(state ?? '').trim().toUpperCase()
  return value || 'UNKNOWN'
}

function resolveOllamaState(status: Readiness | Health['ollama'] | null | undefined, loading: boolean, error: string, healthKnown: boolean): string {
  if (status) {
    const value = normalizeKnownState(status.state)
    if (status.ready === true || ['OLLAMA_READY', 'READY'].includes(value)) return 'READY'
    if (['OLLAMA_LOADING', 'LOADING', 'STARTING'].includes(value)) return 'LOADING'
    if (['OLLAMA_OFFLINE', 'OFFLINE'].includes(value)) return 'OFFLINE'
    if (['OLLAMA_ERROR', 'ERROR'].includes(value)) return 'ERROR'
    return value
  }
  if (loading && !error) return 'VERIFICANDO'
  return healthKnown ? 'UNKNOWN' : 'OFFLINE'
}

function resolveVoiceState(health: Health | null, loading: boolean, error: string): string {
  if (!health) return loading && !error ? 'VERIFICANDO' : 'OFFLINE'
  if (health.stt && health.tts) return 'PRONTA'
  if (health.stt || health.tts) return 'PARCIAL'
  return 'OFFLINE'
}

function resolveWatchdogState(status: WatchdogLite | null, loading: boolean, error: string): string {
  if (error || status?.success === false) return 'ERROR'
  if (!status) return loading ? 'VERIFICANDO' : 'UNKNOWN'
  if (status.stale) return 'STALE'
  if (status.running === true) return 'ATIVO'
  if (status.running === false) return 'INATIVO'
  return 'UNKNOWN'
}

function resolveSelfDevState(status: SelfDevLite | null, loading: boolean, error: string): string {
  if (error) return 'ERROR'
  if (!status) return loading ? 'VERIFICANDO' : 'UNKNOWN'
  return normalizeKnownState(status.state)
}
