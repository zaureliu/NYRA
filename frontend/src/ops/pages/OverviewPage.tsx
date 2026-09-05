import { usePolling } from '../hooks'
import { Card, Empty, ErrorAlert, KeyValue, StatusBadge, formatRelative } from '../ui'
import { OperatorActivityPanel } from '../../components/OperatorActivityPanel'
import type { HealthReport, IntelligenceStatus } from '../types'

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
  const intelligence = usePolling<IntelligenceStatus>('/api/intelligence/status', 10000, { clearOnError: true, noStore: true })
  const selfdev = usePolling<{ state: string }>('/api/selfdev/status', 10000, { clearOnError: true, noStore: true })

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
  const intelligenceState = (...ids: string[]): string => {
    if (!intelligence.data) return 'UNKNOWN'
    const values = ids
      .map((id) => intelligence.data?.capabilities.capabilities.find((item) => item.id === id)?.state)
      .filter((value): value is string => Boolean(value))
    if (values.length === 0) return 'UNKNOWN'
    return mergeStates(...values)
  }
  const selectedModel = intelligence.data?.model_router.last_route?.selected_model || 'Aguardando primeira rota'
  const world = intelligence.data?.world_state
  const worldApp = worldAppName(world?.snapshot.current_app?.value)
  const worldFocus = worldFocusName(world?.snapshot.current_focus?.value)
  const activeWorldTasks = worldListCount(world?.snapshot.active_tasks?.value)
  const activeWorldMonitors = worldListCount(world?.snapshot.active_monitors?.value)
  const loops = intelligence.data?.open_loops
  const persona = intelligence.data?.persona_runtime
  const emotionalPresence = intelligence.data?.emotional_presence

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
      <ErrorAlert message={intelligence.error} />

      <div className="ops-card-grid">
        <Card title="KAZUMI Core" sub="API + memória + banco"><StatusBadge state={subsystemState('api', 'memory', 'database')} /></Card>
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

      <h2 className="ops-section-title">Intelligence Platform</h2>
      <div className="ops-card-grid">
        <Card title="Brain / Model Router" sub={selectedModel}><StatusBadge state={intelligenceState('model_router_v2')} /></Card>
        <Card title="Memory V2" sub={intelligence.data ? `${intelligence.data.counts.memory} registros persistentes` : 'Telemetria indisponível'}><StatusBadge state={intelligenceState('memory_v2')} /></Card>
        <Card title="RAG local" sub={intelligence.data ? `${intelligence.data.counts.documents} documentos · ${intelligence.data.counts.chunks} chunks` : 'Telemetria indisponível'}><StatusBadge state={intelligenceState('rag_local')} /></Card>
        <Card title="Context Engine" sub={intelligence.data ? `Budget ${intelligence.data.context.budget_characters} caracteres` : 'Telemetria indisponível'}><StatusBadge state={intelligenceState('context_engine')} /></Card>
        <Card
          title="Persona Runtime"
          sub={persona ? `${persona.emotion.primary} · ${(persona.emotion.intensity * 100).toFixed(0)}% · ${persona.dialogue_policy.mode}` : 'Emoção e policy indisponíveis'}
        ><StatusBadge state={mergeStates(intelligenceState('persona_emotional_runtime'), intelligenceState('emotional_presence_sync'))} /></Card>
        <Card title="Task Engine" sub={intelligence.data ? `${intelligence.data.tasks.active_or_queued} ativas ou em fila` : 'Telemetria indisponível'}><StatusBadge state={intelligenceState('autonomous_tasks_v2')} /></Card>
        <Card title="Event Intelligence" sub={intelligence.data ? `${intelligence.data.counts.events} eventos correlacionáveis` : 'Telemetria indisponível'}><StatusBadge state={intelligenceState('event_intelligence')} /></Card>
        <Card title="Trace / Replay" sub={intelligence.data ? `${intelligence.data.counts.traces} entradas · ${intelligence.data.trace.dropped_events} descartadas` : 'Telemetria indisponível'}><StatusBadge state={intelligenceState('trace_replay')} /></Card>
        <Card title="Skills / Capabilities" sub={intelligence.data ? `${intelligence.data.capabilities.capabilities.length} capabilities observadas` : 'Telemetria indisponível'}><StatusBadge state={intelligenceState('skill_catalog')} /></Card>
        <Card title="Browser" sub="CDP · DOM · conteúdo não confiável"><StatusBadge state={intelligenceState('browser_control')} /></Card>
        <Card title="Desktop / Vision" sub={intelligence.data?.vision.details?.models?.join(', ') || 'Visão estrutural local'}><StatusBadge state={mergeStates(intelligenceState('desktop_control'), intelligenceState('vision'))} /></Card>
        <Card title="Diagnostics" sub={intelligence.data ? `${intelligence.data.diagnostic_domains.length} domínios` : 'Telemetria indisponível'}><StatusBadge state={intelligenceState('diagnostics_engine')} /></Card>
        <Card title="SelfDev" sub="Lifecycle isolado + rollback"><StatusBadge state={selfdev.data?.state ?? 'UNKNOWN'} /></Card>
      </div>

      <h2 className="ops-section-title">World State</h2>
      <Card
        title="World State Engine"
        sub="Estado local grounded, compartilhado e com TTL"
        actions={<StatusBadge state={world?.health.state ?? 'UNKNOWN'} />}
      >
        <KeyValue rows={[
          ['Current Focus', worldFocus],
          ['Current App', worldApp],
          ['Active Tasks', activeWorldTasks],
          ['Active Monitors', activeWorldMonitors],
          ['Recent Events', world?.snapshot.recent_events?.length ?? 0],
          ['Freshness', world?.snapshot.current_focus?.freshness ?? world?.snapshot.current_app?.freshness ?? 'UNKNOWN'],
          ['Current emotion', persona?.emotion.primary ?? 'UNKNOWN'],
          ['Intensity', persona ? `${(persona.emotion.intensity * 100).toFixed(0)}%` : 'UNKNOWN'],
          ['Dialogue policy', persona?.dialogue_policy.mode ?? 'UNKNOWN'],
          ['Voice style', emotionalPresence ? `${emotionalPresence.voice.delivery} · ${emotionalPresence.voice.emotion_support}` : 'UNKNOWN'],
          ['VTS expression', emotionalPresence?.avatar?.vts_target ?? emotionalPresence?.avatar?.fallback ?? 'neutral'],
          ['Emotion sync', emotionalPresence?.state ?? 'UNKNOWN'],
        ]} />
      </Card>

      <Card
        title="Open Loops & Goals"
        sub="Pendências persistentes; lembrar não autoriza executar"
        actions={<StatusBadge state={mergeStates(loops?.state ?? 'UNKNOWN', intelligenceState('open_loops_engine'))} />}
      >
        <KeyValue rows={[
          ['Open', loops?.counts.open ?? 0],
          ['Waiting', loops?.counts.waiting ?? 0],
          ['Blocked', loops?.counts.blocked ?? 0],
          ['Recent resolved', loops?.counts.recent_resolved ?? 0],
        ]} />
        {loops && Object.values(loops.counts).some((count) => count > 0) ? (
          <div style={{ marginTop: 12, display: 'grid', gap: 8 }}>
            {([
              ['open', 'Open'], ['waiting', 'Waiting'], ['blocked', 'Blocked'],
              ['recent_resolved', 'Recent resolved'],
            ] as const).map(([key, label]) => (
              <details key={key}>
                <summary>{label} ({loops.counts[key]})</summary>
                <ul style={{ margin: '8px 0 0', paddingLeft: 20 }}>
                  {loops.sections[key].length === 0 ? (
                    <li className="ops-hint">Nenhum item.</li>
                  ) : loops.sections[key].map((item) => (
                    <li key={item.id} style={{ marginBottom: 6 }}>
                      <span>{item.title}</span>
                      {item.next_possible_action ? (
                        <div className="ops-hint">Próximo passo: {item.next_possible_action}</div>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </details>
            ))}
          </div>
        ) : <Empty text="Nenhum Open Loop registrado." />}
      </Card>

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

function mergeStates(...states: string[]): string {
  for (const candidate of states) {
    if (['FAILED', 'OFFLINE', 'DEGRADED', 'DISABLED'].includes(candidate)) return candidate
  }
  if (states.some((state) => ['UNCONFIGURED', 'BLOCKED', 'UNKNOWN'].includes(state))) {
    return states.find((state) => ['UNCONFIGURED', 'BLOCKED', 'UNKNOWN'].includes(state)) || 'UNKNOWN'
  }
  return states.length > 0 ? 'READY' : 'UNKNOWN'
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

function worldAppName(value: unknown): string {
  if (!value || typeof value !== 'object') return 'Não observado'
  const app = value as Record<string, unknown>
  return String(app.display_name || app.canonical_id || 'Não observado')
}

function worldFocusName(value: unknown): string {
  if (!value || typeof value !== 'object') return 'Não observado'
  const focus = value as Record<string, unknown>
  return String(focus.title || worldAppName(focus.app) || 'Não observado')
}

function worldListCount(value: unknown): number {
  return Array.isArray(value) ? value.length : 0
}
