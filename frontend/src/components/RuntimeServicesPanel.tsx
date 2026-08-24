import { useCallback, useEffect, useState } from 'react'

export type RuntimeStateLabel =
  | 'UNKNOWN' | 'STOPPED' | 'STARTING' | 'RUNNING' | 'READY' | 'DEGRADED'
  | 'STOPPING' | 'RESTARTING' | 'FAILED' | 'CRASH_LOOP' | 'DISABLED' | 'INVALID_CONFIGURATION'

export interface RuntimeService {
  id: string
  display_name: string
  state: RuntimeStateLabel
  ownership: string
  type: string
  pid: number | null
  uptime_seconds: number | null
  restart_count: number
  last_error: string | null
  health?: { healthy?: boolean; latency_ms?: number; detail?: string } | null
  capabilities: { status: boolean; health: boolean; start: boolean; stop: boolean; restart: boolean; logs: boolean }
  startup_policy: string
}

interface MutationResponse {
  success: boolean
  error_code?: string | null
  message?: string
  approval_id?: string
  state?: string
}

const STATE_CLASS: Record<RuntimeStateLabel, string> = {
  READY: 'ok',
  RUNNING: 'ok',
  STOPPED: 'idle',
  DISABLED: 'idle',
  UNKNOWN: 'idle',
  STARTING: 'busy',
  STOPPING: 'busy',
  RESTARTING: 'busy',
  DEGRADED: 'warn',
  FAILED: 'fail',
  CRASH_LOOP: 'fail',
  INVALID_CONFIGURATION: 'fail',
}

export const describeServiceState = (state: RuntimeStateLabel): string =>
  ({
    READY: 'Ready',
    RUNNING: 'Running (não confirmado)',
    STOPPED: 'Stopped',
    STARTING: 'Starting…',
    STOPPING: 'Stopping…',
    RESTARTING: 'Restarting…',
    DEGRADED: 'Degraded',
    FAILED: 'Failed',
    CRASH_LOOP: 'Crash loop',
    DISABLED: 'Disabled',
    UNKNOWN: 'Unknown',
    INVALID_CONFIGURATION: 'Config inválida',
  })[state] ?? state

export const capabilityAllows = (
  service: Pick<RuntimeService, 'capabilities'>,
  action: 'start' | 'stop' | 'restart' | 'logs',
): boolean => Boolean(service.capabilities?.[action])

export const busyState = (state: RuntimeStateLabel): boolean =>
  state === 'STARTING' || state === 'STOPPING' || state === 'RESTARTING'

async function decideApproval(approvalId: string, approved: boolean): Promise<boolean> {
  try {
    const response = await fetch(`/api/shell/approvals/${approvalId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved }),
    })
    return response.ok
  } catch {
    return false
  }
}

interface PendingApproval {
  approvalId: string
  service: string
  action: 'start' | 'stop' | 'restart'
}

export function RuntimeServicesPanel() {
  const [services, setServices] = useState<RuntimeService[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<string | null>(null)
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null)
  const [openLogs, setOpenLogs] = useState<string | null>(null)
  const [logLines, setLogLines] = useState<string[]>([])
  const [logsTruncated, setLogsTruncated] = useState(false)

  const load = useCallback(async () => {
    try {
      const response = await fetch('/api/runtime/services')
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const payload = (await response.json()) as { services: RuntimeService[] }
      setServices(payload.services)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [])

  useEffect(() => {
    void load()
    const timer = setInterval(() => void load(), 5000)
    return () => clearInterval(timer)
  }, [load])

  const issueAction = useCallback(
    async (service: string, action: 'start' | 'stop' | 'restart', approvalId?: string) => {
      setPendingAction(`${action}:${service}`)
      setNotice(null)
      try {
        const response = await fetch(`/api/runtime/services/${service}/${action}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(approvalId ? { approval_id: approvalId } : {}),
        })
        const payload = (await response.json()) as MutationResponse
        if (payload.error_code === 'APPROVAL_REQUIRED' && payload.approval_id) {
          // Approval é de uso único e pertence ao OPERADOR — nunca auto-aprovado.
          setPendingApproval({ approvalId: payload.approval_id, service, action })
          setNotice(`Ação "${action}" em ${service} exige sua autorização explícita.`)
          return
        }
        setNotice(
          payload.success
            ? `${action} ${service}: ${payload.state ?? 'ok'}`
            : `${action} ${service} falhou: ${payload.message ?? payload.error_code ?? 'erro'}`,
        )
      } finally {
        setPendingAction(null)
        void load()
      }
    },
    [],
  )

  const mutate = useCallback(
    async (service: string, action: 'start' | 'stop' | 'restart') => {
      await issueAction(service, action)
    },
    [issueAction],
  )

  const resolveApproval = useCallback(async (approved: boolean) => {
    if (!pendingApproval) return
    const { approvalId, service, action } = pendingApproval
    setPendingApproval(null)
    const granted = await decideApproval(approvalId, approved)
    if (!approved) {
      setNotice(`Ação cancelada pelo operador (${service}).`)
      return
    }
    if (!granted) {
      setNotice('Não foi possível registrar a autorização no backend.')
      return
    }
    await issueAction(service, action, approvalId)
  }, [pendingApproval, issueAction])

  const showLogs = useCallback(async (service: string) => {
    if (openLogs === service) {
      setOpenLogs(null)
      return
    }
    try {
      const response = await fetch(`/api/runtime/services/${service}/logs?lines=100`)
      const payload = (await response.json()) as { lines?: string[]; truncated?: boolean }
      setOpenLogs(service)
      setLogLines(payload.lines ?? [])
      setLogsTruncated(Boolean(payload.truncated))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [openLogs])

  return <section className="panel workspace-card" aria-label="Runtime Services">
    <header className="panel-header"><span>RUNTIME SERVICES</span><small>{services ? `${services.length} serviços` : 'carregando…'}</small></header>
    {error && <p className="runtime-error" role="alert">Falha ao consultar runtime: {error}</p>}
    {pendingApproval && (
      <div className="runtime-notice" style={{ border: '1px solid #f0bd63', borderRadius: 8, padding: '8px 10px' }} role="alert">
        <strong>Autorização necessária:</strong> {pendingApproval.action} em {pendingApproval.service}.
        <div style={{ display: 'flex', gap: 8, marginTop: 6 }}>
          <button onClick={() => void resolveApproval(true)}>Aprovar</button>
          <button onClick={() => void resolveApproval(false)}>Negar</button>
        </div>
      </div>
    )}
    {notice && <p className="runtime-notice">{notice}</p>}
    {!services && !error && <p className="runtime-loading">Carregando serviços…</p>}
    <ul className="runtime-services">
      {(services ?? []).map((service) => <li key={service.id} className={`runtime-service state-${STATE_CLASS[service.state]}`}>
        <div className="runtime-service-head">
          <strong>{service.display_name}</strong>
          <span className={`badge badge-${STATE_CLASS[service.state]}`}>{describeServiceState(service.state)}</span>
        </div>
        <div className="runtime-service-meta">
          <span>{service.ownership.toLowerCase()}</span>
          {service.pid != null && <span>pid {service.pid}</span>}
          {service.uptime_seconds != null && <span>up {Math.round(service.uptime_seconds)}s</span>}
          {service.health?.healthy != null && <span>health {service.health.healthy ? 'ok' : 'fail'}{service.health.latency_ms != null ? ` ${service.health.latency_ms.toFixed(1)}ms` : ''}</span>}
        </div>
        {service.last_error && <p className="runtime-service-error">{service.last_error}</p>}
        <div className="runtime-service-actions">
          {capabilityAllows(service, 'start') && (
            <button disabled={busyState(service.state) || pendingAction !== null} onClick={() => void mutate(service.id, 'start')}>Start</button>
          )}
          {capabilityAllows(service, 'stop') && (
            <button disabled={busyState(service.state) || pendingAction !== null} onClick={() => void mutate(service.id, 'stop')}>Stop</button>
          )}
          {capabilityAllows(service, 'restart') && (
            <button disabled={busyState(service.state) || pendingAction !== null} onClick={() => void mutate(service.id, 'restart')}>Restart</button>
          )}
          {capabilityAllows(service, 'logs') && (
            <button onClick={() => void showLogs(service.id)}>{openLogs === service.id ? 'Fechar logs' : 'Logs'}</button>
          )}
        </div>
        {openLogs === service.id && <pre className="runtime-logs">{logLines.join('\n')}{logsTruncated ? '\n…(truncado)' : ''}</pre>}
      </li>)}
    </ul>
  </section>
}
