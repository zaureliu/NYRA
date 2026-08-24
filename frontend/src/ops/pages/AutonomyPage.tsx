import { useMemo } from 'react'
import { usePolling } from '../hooks'
import { Card, Empty, ErrorAlert, StatusBadge, Toggle } from '../ui'

interface OperatorStatus {
  flags?: Record<string, boolean | null>
  watches?: { running?: boolean; active?: number }
  workflows_count?: number
  elevated_sessions?: { active_sessions?: number }
  proactive?: { enabled?: boolean; budget_remaining?: number; quiet_mode?: boolean }
}

interface TaskRow {
  task_id: string
  goal: string
  state: string
  progress: number
  current_step?: string
}

interface WatchRow {
  watch_id: string
  event_types?: string[]
  ttl_seconds?: number
  created_at?: string
}

interface WorkflowRow {
  workflow_id: string
  name: string
  version?: string
  steps?: number
  last_run_status?: string
}

export function AutonomyPage() {
  const operator = usePolling<OperatorStatus>('/api/operator/v2/status', 6000)
  const tasks = usePolling<{ tasks: TaskRow[] }>('/api/tasks?limit=8', 8000)
  const watches = usePolling<{ watches: WatchRow[] }>('/api/watches', 10000)
  const workflows = usePolling<{ workflows: WorkflowRow[] }>('/api/workflows', 15000)
  const watchdog = usePolling<{ heartbeat_age_seconds?: number; running?: boolean; stale?: boolean }>('/api/watchdog/status', 12000)

  const proactiveEnabled = Boolean(operator.data?.proactive?.enabled
    ?? operator.data?.flags?.proactive_operator)

  const schedulerRows = useMemo(() => {
    const rows: Array<{ id: string; kind: string; detail: string; state: string }> = []
    for (const watch of watches.data?.watches ?? []) {
      rows.push({
        id: watch.watch_id,
        kind: 'watch',
        detail: (watch.event_types ?? []).join(', ') || 'desktop events',
        state: 'READY',
      })
    }
    return rows.slice(0, 8)
  }, [watches.data])

  return (
    <div>
      <header className="ops-page-header">
        <div>
          <h1 className="ops-page-title">Autonomia</h1>
          <p className="ops-page-subtitle">
            O que a NYRA pode iniciar sozinha, sob quais políticas e com qual verificação.
            Nada aqui executa sem passar por grounding e approval quando mutável.
          </p>
        </div>
      </header>

      <ErrorAlert message={operator.error} />

      <div className="ops-grid-2">
        <Card title="Proactive Mode" sub="Iniciativas autônomas da NYRA">
          <div style={{ display: 'flex', alignItems: 'center', gap: 18 }}>
            <span style={{
              fontSize: 34,
              fontWeight: 800,
              letterSpacing: '0.08em',
              color: proactiveEnabled ? 'var(--ops-warn)' : 'var(--ops-idle)',
            }}>
              {proactiveEnabled ? 'ON' : 'OFF'}
            </span>
            <div className="ops-card-sub" style={{ flex: 1 }}>
              {proactiveEnabled
                ? 'A NYRA pode propor ações por regras registradas (sempre dentro das policies).'
                : 'A NYRA só age em resposta direta ao operador. Recomendado manter OFF para uso diário.'}
            </div>
          </div>
        </Card>

        <Card title="Componentes de autonomia" sub="Estado real por subsistema">
          <dl className="ops-kv">
            <dt>Autonomy Core</dt><dd><StatusBadge state={operator.data ? 'READY' : 'DISABLED'} /></dd>
            <dt>Proactive Rules</dt><dd><StatusBadge state={operator.data?.flags?.proactive_operator ? 'READY' : 'DISABLED'} /></dd>
            <dt>Task Planner</dt><dd><StatusBadge state={operator.data ? 'READY' : 'DISABLED'} /></dd>
            <dt>Workflow Engine</dt><dd><StatusBadge state={operator.data?.flags?.workflow_engine ? 'READY' : 'DISABLED'} /></dd>
            <dt>Verifier</dt><dd><StatusBadge state={operator.data ? 'READY' : 'DISABLED'} /></dd>
            <dt>Recovery</dt><dd><StatusBadge state={operator.data ? 'READY' : 'DISABLED'} /></dd>
            <dt>Watchdog</dt><dd><StatusBadge state={watchdog.data?.stale ? 'STALE' : (watchdog.data?.running || watchdog.data?.heartbeat_age_seconds != null ? 'READY' : 'OFFLINE')} /></dd>
          </dl>
        </Card>
      </div>

      <h2 className="ops-section-title">Goals ativos (tasks declarativas)</h2>
      <Card>
        {(tasks.data?.tasks ?? []).length === 0 ? (
          <Empty text="Nenhuma task ativa. Goals são criados pela conversa ou pela página Tasks." />
        ) : (
          <div className="table-scroll">
            <table className="ops-table">
              <thead>
                <tr><th>Goal</th><th>Estado</th><th>Progresso</th><th>Passo atual</th></tr>
              </thead>
              <tbody>
                {(tasks.data?.tasks ?? []).slice(0, 8).map((task) => (
                  <tr key={task.task_id}>
                    <td><strong>{task.goal}</strong></td>
                    <td><StatusBadge state={task.state} /></td>
                    <td>{Math.round((task.progress ?? 0) * 100)}%</td>
                    <td>{task.current_step ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <h2 className="ops-section-title">Scheduler (watches registrados)</h2>
      <Card>
        {schedulerRows.length === 0 ? (
          <Empty text="Nenhum watch agendado." />
        ) : (
          <div className="table-scroll">
            <table className="ops-table">
              <thead>
                <tr><th>Watch</th><th>Eventos</th><th>Estado</th></tr>
              </thead>
              <tbody>
                {schedulerRows.map((row) => (
                  <tr key={row.id}>
                    <td><strong>{row.id.slice(0, 12)}…</strong></td>
                    <td>{row.detail}</td>
                    <td><StatusBadge state={row.state} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <h2 className="ops-section-title">Policies</h2>
      <Card sub="Políticas estruturais do runtime — edição fina via Settings › Automação">
        <div className="ops-hint" style={{ marginBottom: 10 }}>
          Toda ação mutável passa por classificação de risco, approval de uso único e verificação.
          Policies de workflow editáveis na página Tasks › Workflows.
        </div>
        <dl className="ops-kv">
          <dt>Workflow Engine</dt>
          <dd><Toggle checked={Boolean(operator.data?.flags?.workflow_engine)} disabled label="" onChange={() => undefined} /></dd>
          <dt>Persistent Jobs</dt>
          <dd><Toggle checked={Boolean(operator.data?.flags?.persistent_jobs)} disabled label="" onChange={() => undefined} /></dd>
          <dt>Credential Broker</dt>
          <dd><Toggle checked={Boolean(operator.data?.flags?.credentials)} disabled label="" onChange={() => undefined} /></dd>
          <dt>Sessões elevadas ativas</dt>
          <dd>{operator.data?.elevated_sessions?.active_sessions ?? 0}</dd>
        </dl>
        <div className="ops-hint" style={{ marginTop: 8 }}>
          Toggles read-only aqui refletem o runtime; alteração permanente em Capabilities.
        </div>
      </Card>

      <h2 className="ops-section-title">Verifier</h2>
      <Card sub="Resumo de verificação — sem chain-of-thought (§102)">
        {workflows.data ? (
          <dl className="ops-kv">
            <dt>Workflows registrados</dt><dd>{workflows.data.workflows.length}</dd>
            <dt>Watches ativos</dt><dd>{operator.data?.watches?.active ?? 0}</dd>
            <dt>Elevated sessions</dt><dd>{operator.data?.elevated_sessions?.active_sessions ?? 0}</dd>
          </dl>
        ) : <Empty text="Sem dados de verificação no momento." />}
      </Card>
    </div>
  )
}
