import { useState } from 'react'
import { apiGet, apiSend } from '../../runtime/api'
import { usePolling } from '../hooks'
import { ActionButton, Card, Empty, ErrorAlert, StatusBadge } from '../ui'

interface TaskRow {
  task_id: string
  goal: string
  state: string
  progress?: number
  current_step?: string
}

interface JobRow {
  job_id: string
  name?: string
  command?: string
  state: string
  progress?: number | null
  started_at?: string
  duration_seconds?: number
}

interface WorkflowRow {
  workflow_id: string
  name: string
  version?: string | number
  steps?: unknown[]
}

interface WatchRow {
  watch_id: string
  event_types?: string[]
  ttl_seconds?: number
}

export function TasksPage() {
  const tasks = usePolling<{ tasks: TaskRow[] }>('/api/tasks?limit=20', 6000)
  const jobs = usePolling<{ jobs: JobRow[] }>('/api/jobs', 5000)
  const workflows = usePolling<{ workflows: WorkflowRow[] }>('/api/workflows', 20000)
  const watches = usePolling<{ watches: WatchRow[] }>('/api/watches', 12000)

  const [busyKey, setBusyKey] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const mutate = async (key: string, path: string, method: 'POST' | 'DELETE', body?: unknown) => {
    setBusyKey(key)
    setError('')
    setNotice('')
    try {
      await apiSend(path, method, body)
      setNotice(`Ação concluída (${key.split(':')[1]}).`)
      tasks.refresh()
      jobs.refresh()
      workflows.refresh()
      watches.refresh()
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusyKey('')
    }
  }

  const viewHistory = async (runId: string) => {
    try {
      const detail = await apiGet(`/api/workflows/runs/${runId}`)
      setNotice(JSON.stringify(detail).slice(0, 240))
    } catch {
      setError('Histórico indisponível para este run.')
    }
  }

  return (
    <div>
      <header className="ops-page-header">
        <div>
          <h1 className="ops-page-title">Tarefas & Operações</h1>
          <p className="ops-page-subtitle">
            Tasks declarativas, jobs persistentes, workflows e watches — visão consolidada com ações reais.
          </p>
        </div>
      </header>

      <ErrorAlert message={error} />
      {notice && <div className="ops-alert info">{notice}</div>}

      <h2 className="ops-section-title">Tasks</h2>
      <Card>
        {(tasks.data?.tasks ?? []).length === 0 ? (
          <Empty text="Nenhuma task registrada." />
        ) : (
          <div className="table-scroll">
            <table className="ops-table">
              <thead><tr><th>Goal</th><th>Estado</th><th>Progresso</th><th>Passo atual</th><th></th></tr></thead>
              <tbody>
                {(tasks.data?.tasks ?? []).map((task) => (
                  <tr key={task.task_id}>
                    <td><strong>{task.goal}</strong></td>
                    <td><StatusBadge state={task.state} /></td>
                    <td>{Math.round((task.progress ?? 0) * 100)}%</td>
                    <td>{task.current_step ?? '—'}</td>
                    <td>
                      {!['COMPLETED', 'CANCELLED', 'FAILED'].includes(task.state.toUpperCase()) && (
                        <ActionButton small variant="danger" busy={busyKey === `task:${task.task_id}`}
                          onClick={() => void mutate(`task:${task.task_id}:cancel`, `/api/tasks/${task.task_id}/cancel`, 'POST')}>
                          Cancelar
                        </ActionButton>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <h2 className="ops-section-title">Jobs persistentes</h2>
      <Card>
        {(jobs.data?.jobs ?? []).length === 0 ? (
          <Empty text="Nenhum job em execução ou agendado." />
        ) : (
          <div className="table-scroll">
            <table className="ops-table">
              <thead><tr><th>Job</th><th>Estado</th><th>Progresso</th><th>Duração</th><th></th></tr></thead>
              <tbody>
                {(jobs.data?.jobs ?? []).map((job) => (
                  <tr key={job.job_id}>
                    <td><strong>{job.name ?? job.command ?? job.job_id}</strong></td>
                    <td><StatusBadge state={job.state} /></td>
                    <td>{job.progress != null ? `${Math.round(job.progress * 100)}%` : '—'}</td>
                    <td>{job.duration_seconds != null ? `${job.duration_seconds}s` : '—'}</td>
                    <td>
                      {!['COMPLETED', 'FAILED', 'CANCELLED'].includes(job.state.toUpperCase()) && (
                        <ActionButton small variant="danger" busy={busyKey === `job:${job.job_id}`}
                          onClick={() => void mutate(`job:${job.job_id}:cancel`, `/api/jobs/${job.job_id}/cancel`, 'POST')}>
                          Cancelar
                        </ActionButton>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <h2 className="ops-section-title">Workflows</h2>
      <Card>
        {(workflows.data?.workflows ?? []).length === 0 ? (
          <Empty text="Nenhum workflow registrado. Templates podem ser criados via config/workflow_templates.json." />
        ) : (
          <div className="table-scroll">
            <table className="ops-table">
              <thead><tr><th>Nome</th><th>Versão</th><th>Passos</th><th>Ações</th></tr></thead>
              <tbody>
                {(workflows.data?.workflows ?? []).map((workflow) => (
                  <tr key={workflow.workflow_id}>
                    <td><strong>{workflow.name}</strong></td>
                    <td>v{workflow.version ?? 1}</td>
                    <td>{Array.isArray(workflow.steps) ? workflow.steps.length : '—'}</td>
                    <td>
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                        <ActionButton small busy={busyKey === `wf-run:${workflow.workflow_id}`}
                          onClick={() => void mutate(`wf-run:${workflow.workflow_id}`, `/api/workflows/${workflow.workflow_id}/run`, 'POST')}>
                          Run
                        </ActionButton>
                        <ActionButton small busy={busyKey === `wf-dry:${workflow.workflow_id}`}
                          onClick={() => void mutate(`wf-dry:${workflow.workflow_id}`, `/api/workflows/${workflow.workflow_id}/dry-run`, 'POST')}>
                          Dry Run
                        </ActionButton>
                        <ActionButton small onClick={() => void viewHistory(workflow.workflow_id)}>
                          Histórico
                        </ActionButton>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <h2 className="ops-section-title">Watches</h2>
      <Card>
        {(watches.data?.watches ?? []).length === 0 ? (
          <Empty text="Nenhum watch ativo." />
        ) : (
          <div className="table-scroll">
            <table className="ops-table">
              <thead><tr><th>Watch</th><th>Eventos observados</th><th>TTL</th><th></th></tr></thead>
              <tbody>
                {(watches.data?.watches ?? []).map((watch) => (
                  <tr key={watch.watch_id}>
                    <td><strong>{watch.watch_id.slice(0, 14)}…</strong></td>
                    <td>{(watch.event_types ?? []).join(', ') || 'desktop'}</td>
                    <td>{watch.ttl_seconds ? `${watch.ttl_seconds}s` : '—'}</td>
                    <td>
                      <ActionButton small variant="danger" busy={busyKey === `watch:${watch.watch_id}`}
                        onClick={() => void mutate(`watch:${watch.watch_id}`, `/api/watches/${watch.watch_id}`, 'DELETE')}>
                        Remover
                      </ActionButton>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
