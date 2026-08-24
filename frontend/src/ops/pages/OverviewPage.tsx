import { usePolling } from '../hooks'
import { Card, Empty, ErrorAlert, StatusBadge, formatRelative } from '../ui'
import { OperatorActivityPanel } from '../../components/OperatorActivityPanel'
import type { HealthReport } from '../types'

interface SentinelStatusLite {
  enabled: boolean
  state: string
  last_event?: { title?: string; timestamp?: string } | null
}

interface HAStatusLite {
  enabled?: boolean
  state?: string
  configured?: boolean
}

export function OverviewPage({ activityFeed }: {
  activityFeed: Array<{ id: string; label: string; at: number }>
}) {
  const health = usePolling<HealthReport>('/api/health_report', 15000)
  const sentinel = usePolling<SentinelStatusLite>('/api/sentinel-watch/status', 6000)
  const ha = usePolling<HAStatusLite>('/api/home-assistant/status', 20000)

  const subsystemState = (...names: string[]): string => {
    const entries = names
      .map((name) => health.data?.subsystems?.[name])
      .filter(Boolean)
    if (entries.length === 0) return 'UNKNOWN'
    const worst = entries.find((entry) => ['FAILED', 'OFFLINE'].includes(entry!.state))
    if (worst) return worst.state
    const degraded = entries.find((entry) => !['READY'].includes(entry!.state))
    if (degraded && degraded.state !== 'READY') return degraded.state
    return 'READY'
  }

  const overall = health.data?.overall ?? 'UNKNOWN'
  const alertsCount = countAlerts(activityFeed)

  return (
    <div>
      <header className="ops-page-header">
        <div>
          <h1 className="ops-page-title">Visão geral</h1>
          <p className="ops-page-subtitle">Estado operacional consolidado — todos os dados vêm do backend.</p>
        </div>
        <div className="ops-header-spacer" />
        <StatusBadge state={overall} />
      </header>

      <ErrorAlert message={health.error} />

      <div className="ops-card-grid">
        <Card title="NYRA Core" sub="API + memória + banco"><StatusBadge state={subsystemState('api', 'memory', 'database')} /></Card>
        <Card title="LLM" sub="Modelo local via Ollama"><StatusBadge state={subsystemState('llm', 'ollama')} /></Card>
        <Card title="Voz" sub="STT · TTS · escuta"><StatusBadge state={subsystemState('voice', 'conversation', 'always_listening')} /></Card>
        <Card title="Watchdog" sub="Supervisão externa"><StatusBadge state={subsystemState('watchdog')} /></Card>
        <Card title="Tasks & Jobs" sub="Operações autônomas"><StatusBadge state={mergeStates(subsystemState('jobs'), subsystemState('workflows'))} /></Card>
        <Card title="Homelab" sub="Registry + probes"><StatusBadge state={subsystemState('homelab')} /></Card>
        <Card title="UTAMO Sentinel" sub={sentinel.data?.last_event?.title || 'Percepção de rede'}>
          <StatusBadge state={!sentinel.data?.enabled ? 'DISABLED' : mapSentinel(sentinel.data?.state)} />
        </Card>
        <Card title="Home Assistant" sub={ha.data?.configured ? 'Configurado' : 'Sem URL/token'}>
          <StatusBadge state={mapHA(ha.data)} />
        </Card>
      </div>

      <div className="ops-grid-2" style={{ marginTop: 16 }}>
        <div>
          <h2 className="ops-section-title" style={{ marginTop: 0 }}>Atividade recente</h2>
          <Card>
            {activityFeed.length === 0 ? (
              <Empty text="Nenhum evento do runtime nesta sessão ainda." />
            ) : (
              <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
                {activityFeed.slice(-9).reverse().map((item) => (
                  <li key={item.id} style={{ display: 'flex', justifyContent: 'space-between', gap: 10, fontSize: 13, color: 'var(--ops-text-soft)' }}>
                    <span>{item.label}</span>
                    <span className="ops-hint">{formatRelative(Math.max(0, Date.now() / 1000 - item.at))}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
        <div>
          <h2 className="ops-section-title" style={{ marginTop: 0 }}>Alertas</h2>
          <Card sub={`${alertsCount} alertas nesta sessão`}>
            {alertsCount === 0 ? (
              <Empty text="Nenhum alerta crítico nesta sessão." />
            ) : (
              <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
                {activityFeed.filter((i) => i.label.includes('ALERT') || i.label.includes('SENTINEL')).slice(-6).reverse().map((item) => (
                  <li key={item.id} className="ops-alert warn" style={{ fontSize: 13 }}>{item.label}</li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>

      <h2 className="ops-section-title">Operator V2</h2>
      <OperatorActivityPanel />
    </div>
  )
}

function mergeStates(a: string, b: string): string {
  for (const candidate of [a, b]) {
    if (['FAILED', 'OFFLINE', 'DEGRADED', 'DISABLED'].includes(candidate)) return candidate
  }
  return 'READY'
}

function mapSentinel(state: string | undefined): string {
  switch (state) {
    case 'CONNECTED': return 'READY'
    case 'DISCOVERING':
    case 'CONNECTING': return 'STARTING'
    case 'RECONNECTING': return 'RECOVERING'
    case 'AUTH_REQUIRED': return 'UNCONFIGURED'
    default: return state ?? 'OFFLINE'
  }
}

function mapHA(status: HAStatusLite | null): string {
  if (!status) return 'UNKNOWN'
  if (status.enabled === false) return 'DISABLED'
  if (!status.configured) return 'UNCONFIGURED'
  return 'READY'
}

function countAlerts(feed: Array<{ label: string }>): number {
  return feed.filter((item) => item.label.includes('ALERT') || item.label.includes('SENTINEL') || item.label.includes('NETWORK')).length
}
