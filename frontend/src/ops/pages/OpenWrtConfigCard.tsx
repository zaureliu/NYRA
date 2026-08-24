import { useState } from 'react'
import { apiSend } from '../../runtime/api'
import { usePolling } from '../hooks'
import { ActionButton, Card, Empty, ErrorAlert, StatusBadge, formatMs, formatRelative } from '../ui'
import type { OpenWrtConfigStatus } from '../types'

interface FormState {
  url: string
  username: string
  password: string
}

function formFrom(status: OpenWrtConfigStatus | null): FormState {
  return {
    url: status?.url ?? '',
    username: status?.username ?? '',
    password: '',
  }
}

function summarizeOpenWrtTest(result: Record<string, unknown>): string {
  const parts: string[] = []
  if (result.state) parts.push(String(result.state))
  if (result.latency_ms != null) parts.push(`${result.latency_ms}ms`)
  if (result.version) parts.push(String(result.version))
  if (result.uptime_s != null) parts.push(`uptime ${Math.round(Number(result.uptime_s))}s`)
  if (result.error_code) parts.push(String(result.error_code))
  return parts.length ? parts.join(' · ') : (result.ok === true ? 'OK' : 'sem detalhes')
}

/** Configuração OpenWrt pela UI (hotfix openwrt_config_hotfix.md).
 * Senha SSH vai EXCLUSIVAMENTE ao Credential Broker; o frontend nunca a
 * recebe de volta — após salvar mostra apenas "Authentication configured:
 * YES". Testar conexão executa o teste REAL no backend (OpenWrtAdapter). */
export function OpenWrtConfigCard({ onNotify }: { onNotify: (message: string) => void }) {
  const { data: status, error, loading, refresh } = usePolling<OpenWrtConfigStatus>('/api/openwrt/config', 15000)
  const [form, setForm] = useState<FormState | null>(null)
  const [busy, setBusy] = useState('')
  const [actionError, setActionError] = useState('')
  const [testResult, setTestResult] = useState<string>('')

  const editing = form ?? formFrom(status)

  /** Persiste o formulário: UI → backend → Credential Broker → refresh.
   * Campo de senha vazio NUNCA sobrescreve a credencial existente. */
  const persistForm = async (): Promise<boolean> => {
    setActionError('')
    try {
      await apiSend('/api/openwrt/config', 'PUT', {
        url: editing.url.trim(),
        username: editing.username.trim(),
        ...(editing.password.trim() ? { password: editing.password.trim() } : {}),
      })
      setForm(null)
      await refresh()
      return true
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
      return false
    }
  }

  const save = async () => {
    setBusy('save')
    try {
      if (await persistForm()) onNotify('Configuração OpenWrt salva.')
    } finally {
      setBusy('')
    }
  }

  /** Testar conexão usa o adapter OpenWrt existente via backend real;
   * com edições pendentes a config não secreta é salva antes. */
  const runTest = async () => {
    setBusy('test')
    setActionError('')
    try {
      if (form && !(await persistForm())) return
      const result = await apiSend<Record<string, unknown>>('/api/openwrt/test', 'POST', undefined, 45000)
      setTestResult(summarizeOpenWrtTest(result))
      await refresh()
    } catch (issue) {
      setTestResult('')
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  /** Cancelar descarta as edições e volta aos valores salvos. */
  const cancel = () => {
    setForm(null)
    setActionError('')
    setTestResult('')
  }

  return (
    <Card title="OpenWrt" sub="SSH via Credential Broker · teste real pelo adapter OpenWrt"
      actions={<StatusBadge state={status?.state ?? 'UNKNOWN'} />}>
      <ErrorAlert message={error} />
      <ErrorAlert message={actionError} />

      <dl className="ops-kv">
        <dt>Host/URL</dt><dd>{status?.url || '—'}</dd>
        <dt>Usuário SSH</dt><dd>{status?.username || '—'}</dd>
        <dt>Authentication</dt>
        <dd>{status ? `Authentication configured: ${status.auth_configured ? 'YES' : 'NO'}` : '—'}</dd>
        <dt>Estado</dt><dd><StatusBadge state={status?.state ?? 'UNKNOWN'} /></dd>
        <dt>Latência</dt><dd>{formatMs(status?.latency_ms)}</dd>
        <dt>Versão</dt><dd>{status?.version || '—'}</dd>
        <dt>Último teste</dt><dd>{formatRelative(secondsAgo(status?.last_test))}</dd>
        <dt>Último sucesso</dt><dd>{formatRelative(secondsAgo(status?.last_success))}</dd>
        {status?.last_error && (<><dt>Último erro</dt><dd>{status.last_error}</dd></>)}
      </dl>

      {/* Estados coerentes (hotfix §7): avisos honestos por estado real. */}
      {status && status.state === 'UNCONFIGURED' && (
        <div className="ops-alert info" style={{ marginTop: 10 }}>
          OpenWrt ainda não configurado. Informe Host/URL e senha SSH para habilitar o teste real.
        </div>
      )}
      {status?.state === 'AUTH_FAILED' && (
        <div className="ops-alert warn" style={{ marginTop: 10 }}>
          Credencial recusada pelo host (REMOTE_AUTH_FAILED). Atualize a senha SSH.
        </div>
      )}
      {status?.state === 'OFFLINE' && (
        <div className="ops-alert warn" style={{ marginTop: 10 }}>
          Host inalcançável no último teste (OFFLINE).
        </div>
      )}

      {testResult && (
        <div className="ops-alert info" style={{ marginTop: 10 }}>Teste: {testResult}</div>
      )}

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 12 }}>
        <ActionButton small variant="primary" busy={busy === 'save'} onClick={() => void save()}>
          Salvar
        </ActionButton>
        <ActionButton small busy={busy === 'test'} onClick={() => void runTest()}>
          Testar conexão
        </ActionButton>
        <ActionButton small onClick={cancel}>Cancelar</ActionButton>
      </div>

      <div style={{ display: 'grid', gap: 8, marginTop: 12 }}>
        <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
          Host/URL
          <input type="text" placeholder="http://192.168.1.1" value={editing.url}
            style={inputStyle}
            onChange={(event) => setForm({ ...editing, url: event.target.value })} />
        </label>
        <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
          Usuário SSH
          <input type="text" autoComplete="off" placeholder="root" value={editing.username}
            style={inputStyle}
            onChange={(event) => setForm({ ...editing, username: event.target.value })} />
        </label>
        <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
          Senha SSH {status?.password_configured ? '(configured)' : ''}
          <input type="password" autoComplete="new-password"
            placeholder={status?.password_configured ? '•••••••• (configured)' : ''}
            value={editing.password} style={inputStyle}
            onChange={(event) => setForm({ ...editing, password: event.target.value })} />
        </label>
        <span className="ops-hint">A senha vai direto ao Credential Broker e nunca é exibida novamente após salvar.</span>
      </div>

      {!loading && !status && <Empty text="Configuração OpenWrt indisponível." />}
    </Card>
  )
}

const inputStyle = {
  background: 'var(--ops-bg-2)', border: '1px solid var(--ops-line-strong)',
  color: 'var(--ops-text)', borderRadius: 6, height: 30, padding: '0 10px',
  fontSize: 13,
}

function secondsAgo(value: number | null | undefined): number | null {
  if (!value) return null
  return Math.max(0, Date.now() / 1000 - value)
}
