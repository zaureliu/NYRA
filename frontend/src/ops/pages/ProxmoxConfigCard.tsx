import { useState } from 'react'
import { apiGet, apiSend } from '../../runtime/api'
import { usePolling } from '../hooks'
import { ActionButton, Card, Empty, ErrorAlert, StatusBadge, Toggle, formatMs, formatRelative } from '../ui'
import {
  formatBytes,
  formatUptime,
  guestRiskLabel,
  summarizeProxmoxTest,
  type GuestAction,
} from './integrationsHelpers'
import type {
  HAActionResponse,
  ProxmoxConfigStatus,
  ProxmoxInventory,
} from '../types'

interface FormState {
  enabled: boolean
  url: string
  verify_ssl: boolean
  preferred_node: string
  timeout_seconds: number
  token_id: string
  token_secret: string
}

function formFrom(status: ProxmoxConfigStatus | null): FormState {
  return {
    enabled: status?.enabled ?? false,
    url: status?.url ?? '',
    verify_ssl: true,
    preferred_node: status?.preferred_node ?? '',
    timeout_seconds: status?.timeout_seconds ?? 8,
    token_id: '',
    token_secret: '',
  }
}

/** Configuração completa do Proxmox pela UI (prompt11_1 §29-§39).
 * Token ID/Secret vão só para o Credential Broker; a UI nunca os recebe. */
export function ProxmoxConfigCard({ onNotify }: { onNotify: (message: string) => void }) {
  const { data: status, error, loading, refresh } = usePolling<ProxmoxConfigStatus>('/api/proxmox/config', 15000)
  const [form, setForm] = useState<FormState | null>(null)
  const [busy, setBusy] = useState('')
  const [actionError, setActionError] = useState('')
  const [testResult, setTestResult] = useState<string>('')
  const [inventory, setInventory] = useState<ProxmoxInventory | null>(null)
  const [pendingApproval, setPendingApproval] = useState<{ approvalId: string; action: GuestAction; reference: string } | null>(null)
  const [pendingDisconnect, setPendingDisconnect] = useState<string | null>(null)
  const [diagnosticsBody, setDiagnosticsBody] = useState<Record<string, unknown> | null>(null)

  const editing = form ?? formFrom(status)

  /** Persiste o formulário (prompt11_2 §11): UI → validação → Credential
   * Broker → persistência → reload do connector → refresh. Campos vazios
   * NUNCA sobrescrevem credencial existente no broker. */
  const persistForm = async (): Promise<'ok' | 'reset' | false> => {
    setActionError('')
    try {
      const saved = await apiSend<{ credentials?: { credentials_reset?: boolean } }>('/api/proxmox/config', 'PUT', {
        enabled: editing.enabled,
        url: editing.url.trim(),
        verify_ssl: editing.verify_ssl,
        preferred_node: editing.preferred_node.trim(),
        timeout_seconds: Number(editing.timeout_seconds) || 8,
        ...(editing.token_id.trim() && editing.token_secret.trim()
          ? { token_id: editing.token_id.trim(), token_secret: editing.token_secret.trim() }
          : {}),
      })
      setForm(null)
      await refresh()
      return saved.credentials?.credentials_reset ? 'reset' : 'ok'
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
      return false
    }
  }

  const save = async () => {
    setBusy('save')
    try {
      const persisted = await persistForm()
      if (persisted === 'reset') {
        onNotify('Configuração salva. O endpoint mudou; forneça um novo par de API Token.')
      } else if (persisted) {
        onNotify('Configuração Proxmox salva.')
      }
    } finally {
      setBusy('')
    }
  }

  /** §12: Testar conexão executa o backend real; com formulário aberto a
   * config não secreta é salva antes (URL confirmada), token só se preenchido. */
  const runTest = async () => {
    setBusy('test')
    setActionError('')
    try {
      if (form && !(await persistForm())) return
      const result = await apiSend<Record<string, unknown>>('/api/proxmox/test', 'POST')
      setTestResult(summarizeProxmoxTest(result))
      await refresh()
    } catch (issue) {
      setTestResult('')
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  const toggleEnabled = async (enable: boolean) => {
    setBusy(enable ? 'enable' : 'disable')
    setActionError('')
    try {
      // Envia SOMENTE enabled — a URL/timeout salvos são preservados (§11).
      await apiSend('/api/proxmox/config', 'PUT', { enabled: enable })
      onNotify(`Proxmox ${enable ? 'habilitado' : 'desabilitado'}.`)
      await refresh()
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  const disconnect = async (approvalId?: string) => {
    setBusy('disconnect')
    setActionError('')
    try {
      const response = await apiSend<HAActionResponse>(
        '/api/proxmox/disconnect', 'POST', approvalId ? { approval_id: approvalId } : {},
      )
      if (response.approval_required && response.approval_id && !approvalId) {
        setPendingDisconnect(response.approval_id)
        onNotify('A desconexão exige aprovação destrutiva de uso único.')
        return
      }
      setPendingDisconnect(null)
      onNotify('Credenciais removidas e fallback legado bloqueado.')
      refresh()
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  const confirmDisconnect = async (approved: boolean) => {
    if (!pendingDisconnect) return
    const approvalId = pendingDisconnect
    if (!approved) setPendingDisconnect(null)
    try {
      await apiSend(`/api/shell/approvals/${encodeURIComponent(approvalId)}`, 'POST', { approved })
      if (approved) await disconnect(approvalId)
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    }
  }

  const diagnostics = async () => {
    setBusy('diagnostics')
    setActionError('')
    try {
      setDiagnosticsBody(await apiGet<Record<string, unknown>>('/api/integrations/proxmox/diagnostics'))
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  const loadInventory = async () => {
    setBusy('inventory')
    setActionError('')
    try {
      setInventory(await apiGet<ProxmoxInventory>('/api/proxmox/inventory'))
    } catch (issue) {
      setInventory(null)
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  const power = async (action: GuestAction, guest: { vmid: number; name: string }, approvalId?: string) => {
    const reference = String(guest.vmid)
    setBusy(`power:${reference}:${action}`)
    setActionError('')
    try {
      const response = await apiSend<HAActionResponse>(
        `/api/homelab/proxmox/guests/${encodeURIComponent(reference)}/action`,
        'POST',
        approvalId
          ? { action, approval_id: approvalId, reason: `UI ${action} ${guest.name}` }
          : { action, reason: `UI ${action} ${guest.name}` },
      )
      if (response.approval_required && response.approval_id && !approvalId) {
        setPendingApproval({ approvalId: response.approval_id, action, reference })
        onNotify(`Ação '${action}' requer sua aprovação explícita.`)
      } else {
        const verified = response.verification_status === 'VERIFIED' || response.effect_verified === true
        onNotify(`${action} ${guest.name}: ${verified ? 'VERIFICADO' : (response.error_code ?? response.verification_status ?? 'executado')}`)
        void loadInventory()
      }
      refresh()
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  const confirmApproval = async (approved: boolean) => {
    if (!pendingApproval) return
    const { approvalId, action, reference } = pendingApproval
    setPendingApproval(null)
    setBusy(`approval:${approvalId}`)
    try {
      await apiSend(`/api/shell/approvals/${encodeURIComponent(approvalId)}`, 'POST', { approved })
      if (approved) {
        const guest = inventoryGuest(inventory, reference)
        await power(action, guest ?? { vmid: Number(reference), name: reference }, approvalId)
      }
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusy('')
    }
  }

  return (
    <Card title="Proxmox VE" sub="API Token via Credential Broker · inventário real quando autenticado"
      actions={<StatusBadge state={status?.state ?? 'UNKNOWN'} />}>
      <ErrorAlert message={error} />
      <ErrorAlert message={actionError} />

      <dl className="ops-kv">
        <dt>Habilitada</dt><dd>{status?.enabled ? 'Sim' : 'Não'}</dd>
        <dt>Configurada</dt><dd>{status?.configured ? 'Sim' : 'Não'}</dd>
        <dt>Authentication</dt>
        <dd>{status ? `API Token configured: ${status.auth_configured ? 'YES' : 'NO'}` : '—'}</dd>
        <dt>Estado</dt><dd><StatusBadge state={status?.state ?? 'UNKNOWN'} /></dd>
        <dt>Latência</dt><dd>{formatMs(status?.latency_ms)}</dd>
        <dt>Último teste</dt><dd>{formatRelative(secondsAgo(status?.last_test))}</dd>
        <dt>Último sucesso</dt><dd>{formatRelative(secondsAgo(status?.last_success))}</dd>
        {status?.last_error && (<><dt>Último erro</dt><dd>{status.last_error}</dd></>)}
      </dl>

      {status?.open_url && (
        <div style={{ marginTop: 8 }}>
          <a className="ops-hint" href={status.open_url} target="_blank" rel="noreferrer">
            Open — {status.open_url}
          </a>
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 12 }}>
        <ActionButton small busy={busy === 'save'} onClick={() => void save()}>Salvar</ActionButton>
        <ActionButton small busy={busy === 'test'} onClick={() => void runTest()}>Testar conexão</ActionButton>
        {status?.enabled ? (
          <ActionButton small busy={busy === 'disable'} onClick={() => void toggleEnabled(false)}>Desabilitar</ActionButton>
        ) : (
          <ActionButton small variant="primary" busy={busy === 'enable'} onClick={() => void toggleEnabled(true)}>Habilitar</ActionButton>
        )}
        <ActionButton small variant="danger" busy={busy === 'disconnect'}
          title="Remove o API Token do Credential Broker"
          onClick={() => void disconnect()}>
          Desconectar
        </ActionButton>
        <ActionButton small busy={busy === 'diagnostics'} onClick={() => void diagnostics()}>Diagnóstico</ActionButton>
        <ActionButton small onClick={() => setForm(form ? null : formFrom(status))}>
          {form ? 'Fechar formulário' : 'Configurar'}
        </ActionButton>
      </div>

      {/* prompt11_2 §13: UNCONFIGURED nunca mostra cards falsos. */}
      {status && status.state === 'UNCONFIGURED' && !status.auth_configured && (
        <div className="ops-alert info" style={{ marginTop: 10 }}>
          Proxmox ainda não configurado. Configure um API Token para habilitar
          inventário e operações.
        </div>
      )}

      {/* prompt11_2 §14: resumo real quando autenticado (nada fabricado). */}
      {status?.authenticated && (
        <dl className="ops-kv" style={{ marginTop: 10 }}>
          <dt>Version</dt><dd>{status.version ?? '—'}</dd>
          <dt>Nodes</dt><dd>{status.node_count ?? '—'}</dd>
          <dt>QEMU VMs</dt><dd>{status.qemu_count ?? '—'}</dd>
          <dt>LXC Containers</dt><dd>{status.lxc_count ?? '—'}</dd>
          <dt>Storage</dt><dd>{status.storage_count ?? '—'}</dd>
        </dl>
      )}

      {testResult && (
        <div className="ops-alert info" style={{ marginTop: 10 }}>Teste: {testResult}</div>
      )}

      {diagnosticsBody && (
        <details style={{ marginTop: 10 }}>
          <summary className="ops-hint" style={{ cursor: 'pointer' }}>Diagnóstico bruto</summary>
          <pre className="ops-code" style={{ maxHeight: 220 }}>{JSON.stringify(diagnosticsBody, null, 2)}</pre>
        </details>
      )}

      {(form || !status) && (
        <div style={{ display: 'grid', gap: 8, marginTop: 12 }}>
          <Toggle checked={editing.enabled} label="Enabled"
            onChange={(value) => setForm({ ...editing, enabled: value })} />
          <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
            Base URL
            <input type="text" placeholder="https://HOST:8006" value={editing.url}
              style={inputStyle}
              onChange={(event) => setForm({ ...editing, url: event.target.value })} />
          </label>
          <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
            API Token ID {status?.token_id_configured ? '(configured)' : ''}
            <input type="text" autoComplete="off" placeholder="user@pve!tokenid"
              value={editing.token_id} style={inputStyle}
              onChange={(event) => setForm({ ...editing, token_id: event.target.value })} />
          </label>
          <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
            API Token Secret {status?.token_secret_configured ? '(configured)' : ''}
            <input type="password" autoComplete="new-password"
              placeholder={status?.token_secret_configured ? '•••••••• (configured)' : ''}
              value={editing.token_secret} style={inputStyle}
              onChange={(event) => setForm({ ...editing, token_secret: event.target.value })} />
          </label>
          <Toggle checked label="TLS Verification (obrigatória)" disabled
            onChange={() => undefined} />
          <span className="ops-hint">
            Para certificados internos, instale a CA do Proxmox no repositório de confiança do host NYRA.
          </span>
          <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
            Preferred Node
            <input type="text" value={editing.preferred_node} style={inputStyle}
              onChange={(event) => setForm({ ...editing, preferred_node: event.target.value })} />
          </label>
          <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
            Request Timeout (segundos)
            <input type="number" min={4} max={60} value={editing.timeout_seconds}
              style={inputStyle}
              onChange={(event) => setForm({ ...editing, timeout_seconds: Number(event.target.value) })} />
          </label>
          <span className="ops-hint">O secret nunca é exibido novamente após salvar.</span>
          {form && (
            <div style={{ display: 'flex', gap: 8, marginTop: 2 }}>
              <ActionButton small onClick={() => setForm(null)}>Cancelar</ActionButton>
            </div>
          )}
        </div>
      )}

      <div style={{ marginTop: 14 }}>
        <ActionButton small busy={busy === 'inventory'} disabled={!status?.auth_configured}
          title={!status?.auth_configured ? 'Configure o API Token primeiro' : undefined}
          onClick={() => void loadInventory()}>
          Carregar inventário
        </ActionButton>
      </div>

      {inventory && (
        <>
          <h4 style={{ margin: '14px 0 6px' }}>Nodes</h4>
          <div className="table-scroll">
            <table className="ops-table">
              <thead><tr><th>Node</th><th>State</th><th>CPU</th><th>RAM</th><th>Uptime</th></tr></thead>
              <tbody>
                {inventory.nodes.map((node) => (
                  <tr key={node.node}>
                    <td>{node.node}</td>
                    <td><StatusBadge state={node.state.toUpperCase()} /></td>
                    <td>{node.cpu_percent}%</td>
                    <td>{formatBytes(node.memory_used_bytes)} / {formatBytes(node.memory_total_bytes)}</td>
                    <td>{formatUptime(node.uptime_s)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <h4 style={{ margin: '14px 0 6px' }}>QEMU</h4>
          <GuestTable guests={inventory.qemu} busy={busy} onPower={(action, guest) => void power(action, guest)} />

          <h4 style={{ margin: '14px 0 6px' }}>LXC</h4>
          <GuestTable guests={inventory.lxc} busy={busy} onPower={(action, guest) => void power(action, guest)} />

          <h4 style={{ margin: '14px 0 6px' }}>Storage</h4>
          <div className="table-scroll">
            <table className="ops-table">
              <thead><tr><th>Name</th><th>Type</th><th>Usage</th><th>Capacity</th></tr></thead>
              <tbody>
                {inventory.storage.map((item) => (
                  <tr key={`${item.node}-${item.storage}`}>
                    <td>{item.storage}</td>
                    <td>{item.type}</td>
                    <td>{item.usage_percent != null ? `${item.usage_percent}%` : '—'}</td>
                    <td>{formatBytes(item.used_bytes)} / {formatBytes(item.total_bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="ops-hint" style={{ marginTop: 8 }}>
            Associações (ex.: Home Assistant em uma VM) vêm da API — nada é fixo no código.
          </p>
        </>
      )}

      {pendingApproval && (
        <div className="ops-alert warn" style={{ marginTop: 12 }}>
          <strong>Aprovação necessária:</strong> {pendingApproval.action} da guest {pendingApproval.reference}
          {' '}(risco {guestRiskLabel(pendingApproval.action)}).
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <ActionButton small variant="primary" busy={busy === `approval:${pendingApproval.approvalId}`}
              onClick={() => void confirmApproval(true)}>
              Aprovar e executar
            </ActionButton>
            <ActionButton small variant="danger" onClick={() => void confirmApproval(false)}>
              Recusar
            </ActionButton>
          </div>
        </div>
      )}

      {pendingDisconnect && (
        <div className="ops-alert warn" style={{ marginTop: 12 }}>
          Remover as credenciais Proxmox e bloquear a reimportação de valores legados?
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <ActionButton small variant="danger" onClick={() => void confirmDisconnect(true)}>
              Aprovar desconexão
            </ActionButton>
            <ActionButton small onClick={() => void confirmDisconnect(false)}>Recusar</ActionButton>
          </div>
        </div>
      )}

      {!loading && !status && <Empty text="Configuração Proxmox indisponível." />}
    </Card>
  )
}

const inputStyle = {
  background: 'var(--ops-bg-2)', border: '1px solid var(--ops-line-strong)',
  color: 'var(--ops-text)', borderRadius: 6, height: 30, padding: '0 10px',
  fontSize: 13,
}

function GuestTable({ guests, busy, onPower }: {
  guests: Array<{ vmid: number | null; name: string; node?: string; status: string; cpu_percent?: number; memory_used_bytes?: number | null; memory_total_bytes?: number | null; uptime_s?: number | null }>
  busy: string
  onPower: (action: GuestAction, guest: { vmid: number; name: string }) => void
}) {
  if (!guests.length) return <Empty text="Nenhum guest deste tipo." />
  return (
    <div className="table-scroll">
      <table className="ops-table">
        <thead>
          <tr><th>ID</th><th>Name</th><th>Node</th><th>State</th><th>CPU</th><th>RAM</th><th>Uptime</th><th>Power</th></tr>
        </thead>
        <tbody>
          {guests.map((guest) => (
            <tr key={`${guest.vmid}-${guest.name}`}>
              <td>{guest.vmid ?? '—'}</td>
              <td>{guest.name}</td>
              <td>{guest.node ?? '—'}</td>
              <td><StatusBadge state={guest.status === 'running' ? 'READY' : 'OFFLINE'} label={guest.status} /></td>
              <td>{guest.cpu_percent != null ? `${guest.cpu_percent}%` : '—'}</td>
              <td>{formatBytes(guest.memory_used_bytes)} / {formatBytes(guest.memory_total_bytes)}</td>
              <td>{formatUptime(guest.uptime_s)}</td>
              <td>
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                  <ActionButton small disabled={guest.status === 'running' || guest.vmid == null}
                    busy={busy === `power:${guest.vmid}:start`}
                    title={`Start (${guestRiskLabel('start')})`}
                    onClick={() => onPower('start', { vmid: guest.vmid as number, name: guest.name })}>
                    Start
                  </ActionButton>
                  <ActionButton small disabled={guest.status !== 'running' || guest.vmid == null}
                    busy={busy === `power:${guest.vmid}:shutdown`}
                    title={`Shutdown (${guestRiskLabel('shutdown')}) — exige aprovação`}
                    onClick={() => onPower('shutdown', { vmid: guest.vmid as number, name: guest.name })}>
                    Shutdown
                  </ActionButton>
                  <ActionButton small disabled={guest.status !== 'running' || guest.vmid == null}
                    busy={busy === `power:${guest.vmid}:reboot`}
                    title={`Reboot (${guestRiskLabel('reboot')}) — exige aprovação`}
                    onClick={() => onPower('reboot', { vmid: guest.vmid as number, name: guest.name })}>
                    Reboot
                  </ActionButton>
                  <ActionButton small variant="danger"
                    disabled={guest.status !== 'running' || guest.vmid == null}
                    busy={busy === `power:${guest.vmid}:stop`}
                    title={`Stop (${guestRiskLabel('stop')}) — exige aprovação`}
                    onClick={() => onPower('stop', { vmid: guest.vmid as number, name: guest.name })}>
                    Stop
                  </ActionButton>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function inventoryGuest(inventory: ProxmoxInventory | null, reference: string): { vmid: number; name: string } | null {
  if (!inventory) return null
  const all = [...inventory.qemu, ...inventory.lxc]
  const found = all.find((guest) => String(guest.vmid) === reference)
  return found ? { vmid: found.vmid as number, name: found.name } : null
}

function secondsAgo(value: number | null | undefined): number | null {
  if (!value) return null
  return Math.max(0, Date.now() / 1000 - value)
}
