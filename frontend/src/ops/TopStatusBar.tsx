import { usePolling } from './hooks'
import type { Health } from '../types'

interface Readiness {
  state?: string
  ready?: boolean
}

interface WatchdogLite {
  running?: boolean
  stale?: boolean
  heartbeat_age_seconds?: number
}

interface TasksLite {
  tasks?: Array<{ state?: string }>
}

export function TopStatusBar({ connected, health }: { connected: boolean; health: Health | null }) {
  const readiness = usePolling<Readiness>('/api/ollama/readiness', 20000)
  const watchdog = usePolling<WatchdogLite>('/api/watchdog/status', 15000)
  const tasks = usePolling<TasksLite>('/api/tasks?limit=10', 10000)

  const nyraState = !connected ? 'OFFLINE' : (health ? (health.llm_ready ? 'READY' : 'STARTING') : 'UNKNOWN')
  const ollamaOk = Boolean(health?.llm_ready) || readiness.data?.ready === true
  const voiceOk = Boolean(health?.stt && health?.tts)
  const activeTasks = (tasks.data?.tasks ?? []).filter((task) => ['RUNNING', 'PENDING'].includes(String(task.state).toUpperCase())).length

  return (
    <header className="ops-topbar">
      <div className="ops-brand">
        <strong>NYRA</strong>
        <span>OPS V3</span>
      </div>
      <div className="ops-topbar-chips" aria-label="Estado do sistema">
        <Chip tone={toneOf(nyraState)} label="NYRA" value={nyraState} />
        <Chip tone={ollamaOk ? 'ok' : connected ? 'warn' : 'off'} label="Ollama" value={ollamaOk ? 'ready' : 'warmup'} />
        <Chip tone={voiceOk ? 'ok' : 'warn'} label="Voz" value={voiceOk ? 'pronta' : 'parcial'} />
        <Chip tone={connected ? 'ok' : 'err'} label="Backend" value={connected ? 'online' : 'offline'} />
        <Chip
          tone={watchdog.data == null ? 'off' : watchdog.data.stale ? 'warn' : watchdog.data.running || watchdog.data.heartbeat_age_seconds != null ? 'ok' : 'off'}
          label="Watchdog"
          value={watchdog.data?.stale ? 'stale' : watchdog.data?.heartbeat_age_seconds != null ? 'vivo' : 'sem hb'}
        />
        <Chip tone={activeTasks > 0 ? 'ok' : 'off'} label="Tarefa" value={activeTasks > 0 ? `${activeTasks} ativa(s)` : 'nenhuma'} />
      </div>
    </header>
  )
}

function Chip({ tone, label, value }: { tone: 'ok' | 'warn' | 'err' | 'off'; label: string; value: string }) {
  return (
    <span className="ops-chip" data-tone={tone} title={`${label}: ${value}`}>
      <span className="chip-dot" />
      <span>{label}</span>
      <span style={{ color: 'var(--ops-faint)' }}>{value}</span>
    </span>
  )
}

function toneOf(state: string): 'ok' | 'warn' | 'err' | 'off' {
  const value = String(state).toUpperCase()
  if (['READY', 'IDLE', 'ONLINE', 'LISTENING'].includes(value)) return 'ok'
  if (value === 'OFFLINE') return 'err'
  if (['UNKNOWN'].includes(value)) return 'off'
  return 'warn'
}
