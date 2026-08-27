import { useState } from 'react'
import { apiGet, apiSend } from '../../runtime/api'
import { usePolling } from '../hooks'
import type { SelfDevIssue, SelfDevNotification, SelfDevStatus } from '../types'
import { ActionButton, Card, ErrorAlert, StatusBadge } from '../ui'

interface IssuesResponse { issues: SelfDevIssue[]; count: number }
interface NotificationsResponse { notifications: SelfDevNotification[]; unread: number }
interface IssueDetails { issue: SelfDevIssue; promotions: Array<Record<string, unknown>> }

export function SelfDevPanel() {
  const status = usePolling<SelfDevStatus>('/api/selfdev/status', 8000)
  const issues = usePolling<IssuesResponse>('/api/selfdev/issues', 12000)
  const notifications = usePolling<NotificationsResponse>('/api/selfdev/notifications', 10000)
  const [selected, setSelected] = useState('')
  const details = usePolling<IssueDetails>(selected ? `/api/selfdev/issues/${selected}` : null, 12000)
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [diff, setDiff] = useState('')
  const [confirmRevert, setConfirmRevert] = useState(false)

  const act = async (key: string, action: () => Promise<unknown>, message: string) => {
    setBusy(key)
    setError('')
    setNotice('')
    try {
      const result = await action() as { approval_required?: boolean; approval_id?: string; error_code?: string }
      if (result?.approval_required) {
        setNotice(`Aprovação de uso único necessária no System Shell: ${result.approval_id ?? 'pendente'}.`)
      } else {
        setNotice(message)
      }
      status.refresh()
      issues.refresh()
      notifications.refresh()
      details.refresh()
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  const showDiff = async () => {
    if (!selected) return
    setBusy('diff')
    setError('')
    try {
      const result = await apiGet<{ diff: string; error_code?: string }>(`/api/selfdev/issues/${selected}/diff`)
      setDiff(result.diff || result.error_code || 'Nenhuma alteração disponível.')
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  const current = status.data
  return (
    <div style={{ marginBottom: 16 }}>
      <ErrorAlert message={status.error || issues.error || notifications.error || error} />
      {notice && <div className="ops-alert info">{notice}</div>}
      <div className="ops-grid-2">
        <Card title="Self-Development Engine" sub="Evidência → candidato isolado → validação → promoção reversível">
          <div className="ops-kv">
            <dt>Estado</dt><dd><StatusBadge state={current?.state ?? 'UNKNOWN'} /></dd>
            <dt>Modo</dt><dd>{current?.mode ?? '—'}</dd>
            <dt>Fila</dt><dd>{current?.queue_size ?? 0}</dd>
            <dt>Índice</dt><dd>{current?.repository_files ?? 0} arquivos</dd>
            <dt>GitHub</dt><dd>{current?.github_status ?? 'OFF'}</dd>
            <dt>Workspace</dt><dd>{current?.workspace_ready ? 'pronto' : 'indisponível'}</dd>
          </div>
          {current?.last_error_code && <div className="ops-alert warn">{current.last_error_code}</div>}
          <div style={{ marginTop: 10 }}>
            <ActionButton
              small
              variant="primary"
              busy={busy === 'run'}
              disabled={current?.state === 'BUSY'}
              onClick={() => act('run', () => apiSend('/api/selfdev/run-once', 'POST', { issue_id: selected || null }, 30000), 'Ciclo SelfDev concluído.')}
            >Executar próximo ciclo</ActionButton>
          </div>
        </Card>

        <Card title="Notificações" sub={`${notifications.data?.unread ?? 0} não lida(s)`}>
          {(notifications.data?.notifications ?? []).slice(0, 6).map((item) => (
            <div key={item.notification_id} style={{ borderBottom: '1px solid var(--ops-line)', padding: '7px 0', opacity: item.read ? 0.65 : 1 }}>
              <strong>{item.title}</strong>
              <div className="ops-card-sub">{item.message}</div>
              {!item.read && (
                <ActionButton small busy={busy === item.notification_id}
                  onClick={() => act(item.notification_id, () => apiSend(`/api/selfdev/notifications/${item.notification_id}/read`, 'POST'), 'Notificação marcada como lida.')}>
                  Marcar como lida
                </ActionButton>
              )}
            </div>
          ))}
          {(notifications.data?.notifications ?? []).length === 0 && <div className="ops-empty">Nenhuma notificação.</div>}
        </Card>
      </div>

      <Card title="Fila e histórico" sub="Selecione uma melhoria para ver evidência, promoção e diff">
        <div className="table-scroll">
          <table className="ops-table">
            <thead><tr><th>Issue</th><th>Tipo</th><th>Título</th><th>Risco</th><th>Status</th></tr></thead>
            <tbody>
              {(issues.data?.issues ?? []).map((item) => (
                <tr key={item.issue_id} onClick={() => { setSelected(item.issue_id); setDiff(''); setConfirmRevert(false) }} style={{ cursor: 'pointer' }}>
                  <td><code>{item.issue_id}</code></td><td>{item.type}</td><td>{item.title}</td><td>{item.risk}</td><td><StatusBadge state={item.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {(issues.data?.issues ?? []).length === 0 && <div className="ops-empty">Nenhuma melhoria detectada.</div>}
      </Card>

      {selected && details.data && (
        <Card title={details.data.issue.title} sub={selected} actions={<ActionButton small busy={busy === 'diff'} onClick={showDiff}>Ver diff</ActionButton>}>
          <p>{details.data.issue.description}</p>
          <div className="ops-hint">Ocorrências: {details.data.issue.occurrences} · Promoções: {details.data.promotions.length}</div>
          {details.data.promotions.length > 0 && !confirmRevert && (
            <ActionButton small variant="danger" onClick={() => setConfirmRevert(true)}>Reverter melhoria</ActionButton>
          )}
          {confirmRevert && (
            <div className="ops-alert warn" style={{ marginTop: 8 }}>
              Confirmar rollback reversível desta melhoria? O System Shell poderá exigir approval de uso único.
              <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                <ActionButton small variant="danger" busy={busy === 'revert'} onClick={() => act('revert', () => apiSend(`/api/selfdev/issues/${selected}/revert`, 'POST', {}), 'Rollback aplicado.')}>Confirmar</ActionButton>
                <ActionButton small onClick={() => setConfirmRevert(false)}>Cancelar</ActionButton>
              </div>
            </div>
          )}
          {diff && <pre style={{ maxHeight: 420, overflow: 'auto', whiteSpace: 'pre-wrap', fontSize: 11 }}>{diff}</pre>}
        </Card>
      )}
    </div>
  )
}
