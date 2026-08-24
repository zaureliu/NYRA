import { useCallback, useEffect, useState } from 'react'
import { backendUrl } from '../runtime/backend'

// Operator Activity (prompt9 Parte P §266-§273): seção COMPACTA de observabilidade.
// Mostra tarefas, jobs, watches, workflows e watchdog — sem dashboard gigante,
// sem chain-of-thought (§273). Poll leve a cada 5s como os demais painéis.

interface OperatorStatus {
  flags?: Record<string, boolean>
  contexts?: { counts?: Record<string, number> }
  watches?: { running?: boolean; count?: number }
  workflows_count?: number
  elevated_sessions?: unknown[]
}

interface TaskSummary {
  task_id: string
  goal: string
  state: string
  progress: { label: string }
}

interface JobSummary {
  job_id: string
  name: string
  state: string
  progress: number | null
}

interface WatchdogSummary {
  running: boolean
  stale?: boolean
  components?: Record<string, boolean>
}

const stateClass = (state: string): string => {
  const clean = state.toUpperCase()
  if (['SUCCEEDED', 'COMPLETED'].includes(clean)) return 'active'
  if (['FAILED', 'CANCELLED', 'CRASH_LOOP_PROTECTED'].includes(clean)) return 'danger'
  if (['WAITING_FOR_USER', 'WAITING_FOR_JOB', 'PAUSED'].includes(clean)) return 'warning'
  return 'active'
}

export function OperatorActivityPanel() {
  const [status, setStatus] = useState<OperatorStatus | null>(null)
  const [tasks, setTasks] = useState<TaskSummary[]>([])
  const [jobs, setJobs] = useState<JobSummary[]>([])
  const [watchdog, setWatchdog] = useState<WatchdogSummary | null>(null)

  const load = useCallback(async () => {
    try {
      const [statusRes, tasksRes, jobsRes, watchdogRes] = await Promise.all([
        fetch(backendUrl('/api/operator/v2/status')),
        fetch(backendUrl('/api/tasks')),
        fetch(backendUrl('/api/jobs')),
        fetch(backendUrl('/api/watchdog/status')),
      ])
      if (statusRes.ok) setStatus(await statusRes.json())
      if (tasksRes.ok) setTasks(((await tasksRes.json())?.tasks ?? []) as TaskSummary[])
      if (jobsRes.ok) setJobs(((await jobsRes.json())?.jobs ?? []).filter((job: JobSummary) => !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(job.state)))
      if (watchdogRes.ok) setWatchdog((await watchdogRes.json()) as WatchdogSummary)
    } catch {
      /* painel nunca derruba o app */
    }
  }, [])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), 5000)
    return () => window.clearInterval(timer)
  }, [load])

  const counts = status?.contexts?.counts ?? {}
  const activeTasks = tasks.filter(task => !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(task.state))
  const hasContent = activeTasks.length > 0 || jobs.length > 0 || (counts.WATCH ?? 0) > 0

  return <div className="workspace-card operator-activity">
    <header className="panel-heading">
      <h2>Operator Activity</h2>
      <span className={`runtime-chip ${status ? 'active' : ''}`}><i/>{status ? 'OPERADOR V2' : 'INDISPONÍVEL'}</span>
    </header>

    {!hasContent && <p className="muted">Nenhuma tarefa/job/watch ativo agora.</p>}

    {activeTasks.length > 0 && <section>
      <h3>Tarefas</h3>
      <ul className="operator-list">
        {activeTasks.slice(0, 4).map(task => (
          <li key={task.task_id}>
            <span className={`runtime-chip ${stateClass(task.state)}`}><i/>{task.state}</span>
            <strong>{task.goal.slice(0, 60)}</strong>
            <em>{task.progress.label}</em>
          </li>
        ))}
      </ul>
    </section>}

    {jobs.length > 0 && <section>
      <h3>Jobs</h3>
      <ul className="operator-list">
        {jobs.slice(0, 4).map(job => (
          <li key={job.job_id}>
            <span className={`runtime-chip ${stateClass(job.state)}`}><i/>{job.state}</span>
            <strong>{job.name}</strong>
            <em>{job.progress != null ? `${job.progress}%` : '—'}</em>
          </li>
        ))}
      </ul>
    </section>}

    <footer className="operator-footline">
      <span>Watches: {counts.WATCH ?? 0}{status?.watches?.running === false ? ' (off)' : ''}</span>
      <span>Workflows: {status?.workflows_count ?? 0}</span>
      <span className={watchdog?.running && !watchdog?.stale ? 'ok' : 'warn'}>
        Watchdog: {watchdog?.running ? (watchdog.stale ? 'SEM HEARTBEAT' : 'ATIVO') : 'INATIVO'}
      </span>
    </footer>
  </div>
}
