import { useMemo, useState, type CSSProperties } from 'react'
import { apiGet, apiSend } from '../../runtime/api'
import { usePolling } from '../hooks'
import { ActionButton, Card, Empty, ErrorAlert, StatusBadge, formatMs, formatRelative } from '../ui'
import { authLabel } from './integrationsHelpers'
import { HAEntityBrowser } from './HAEntityBrowser'
import type {
  HADiagnostics,
  HAProfile,
  HAProfilesResponse,
  HATestDetail,
} from '../types'

interface EditorState {
  profile_id: string
  name: string
  url: string
  tls: boolean
  enabled: boolean
  priority: number
  token: string
}

function editorFrom(profile: HAProfile): EditorState {
  return {
    profile_id: profile.profile_id,
    name: profile.name,
    url: profile.url,
    tls: profile.tls,
    enabled: profile.enabled,
    priority: profile.priority,
    token: '', // write-only: nunca recebemos nem reexibimos o valor (§19)
  }
}

/** Editor completo de perfis Home Assistant (prompt11_1 §17-§28). */
export function HAProfilesCard({ onNotify }: { onNotify: (message: string) => void }) {
  const { data, error, loading, refresh } = usePolling<HAProfilesResponse>('/api/home-assistant/profiles', 15000)
  const [busyId, setBusyId] = useState('')
  const [actionError, setActionError] = useState('')
  const [lastTest, setLastTest] = useState<HATestDetail | null>(null)
  const [editing, setEditing] = useState<EditorState | null>(null)
  const [newName, setNewName] = useState('')
  const [newUrl, setNewUrl] = useState('')
  const [diagnostics, setDiagnostics] = useState<HADiagnostics | null>(null)
  const [pendingDelete, setPendingDelete] = useState<{ profile: HAProfile; approvalId: string } | null>(null)

  const activeProfile = useMemo(
    () => (data?.profiles ?? []).find((p) => p.profile_id === data?.active_profile) ?? null,
    [data],
  )

  const run = async (profile: HAProfile, action: 'activate' | 'test' | 'enable' | 'disable' | 'delete', approvalId?: string) => {
    setBusyId(`${profile.profile_id}:${action}`)
    setActionError('')
    try {
      if (action === 'activate') {
        await apiSend(`/api/home-assistant/profiles/${profile.profile_id}/activate`, 'POST')
        onNotify(`Perfil ativo: ${profile.name}`)
      } else if (action === 'test') {
        const result = await apiSend<HATestDetail>(`/api/home-assistant/profiles/${profile.profile_id}/test`, 'POST')
        setLastTest(result)
        onNotify(result.ok ? `Teste OK (${result.latency_ms}ms)` : `Teste falhou: ${result.error_code}`)
      } else if (action === 'delete') {
        const suffix = approvalId ? `?approval_id=${encodeURIComponent(approvalId)}` : ''
        const result = await apiSend<{ approval_required?: boolean; approval_id?: string }>(
          `/api/home-assistant/profiles/${profile.profile_id}${suffix}`, 'DELETE',
        )
        if (result.approval_required && result.approval_id && !approvalId) {
          setPendingDelete({ profile, approvalId: result.approval_id })
          onNotify('A exclusão do perfil exige approval destrutivo de uso único.')
          return
        }
        setPendingDelete(null)
        onNotify(`Perfil removido: ${profile.name}`)
      } else {
        await apiSend('/api/home-assistant/profiles', 'PUT', {
          profile_id: profile.profile_id,
          name: profile.name,
          url: profile.url,
          tls: profile.tls,
          priority: profile.priority,
          enabled: action === 'enable',
        })
        onNotify(`${profile.name} ${action === 'enable' ? 'habilitado' : 'desabilitado'}.`)
      }
      refresh()
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusyId('')
    }
  }

  const confirmDelete = async (approved: boolean) => {
    if (!pendingDelete) return
    const pending = pendingDelete
    if (!approved) setPendingDelete(null)
    try {
      await apiSend(`/api/shell/approvals/${encodeURIComponent(pending.approvalId)}`, 'POST', { approved })
      if (approved) await run(pending.profile, 'delete', pending.approvalId)
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    }
  }

  const saveEditor = async () => {
    if (!editing) return
    setBusyId(`${editing.profile_id}:save`)
    setActionError('')
    try {
      const saved = await apiSend<{ profile: HAProfile & { credentials_reset?: boolean } }>('/api/home-assistant/profiles', 'PUT', {
        profile_id: editing.profile_id,
        name: editing.name,
        url: editing.url,
        tls: editing.tls,
        priority: editing.priority,
        enabled: editing.enabled,
      })
      if (editing.token.trim()) {
        // Token vai exclusivamente para o Credential Broker via backend;
        // após salvar o campo é limpo e só "auth configured" é exibido (§19).
        await apiSend(`/api/home-assistant/profiles/${editing.profile_id}/token`, 'POST', {
          token: editing.token.trim(),
        })
      }
      if (saved.profile.credentials_reset && !editing.token.trim()) {
        onNotify(`Perfil salvo: ${editing.name}. O endpoint mudou; forneça um novo token.`)
      } else if (saved.profile.credentials_reset) {
        onNotify(`Perfil salvo e credencial vinculada ao novo endpoint: ${editing.name}`)
      } else {
        onNotify(`Perfil salvo: ${editing.name}`)
      }
      setEditing(null)
      refresh()
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusyId('')
    }
  }

  const openDiagnostics = async () => {
    setBusyId('diagnostics')
    setActionError('')
    try {
      setDiagnostics(await apiGet<HADiagnostics>('/api/integrations/home_assistant/diagnostics'))
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusyId('')
    }
  }

  const createPhysicalProfile = async () => {
    setBusyId('create')
    setActionError('')
    try {
      await apiSend('/api/home-assistant/profiles', 'PUT', {
        profile_id: newName.trim().toLowerCase().replace(/[^a-z0-9-]+/g, '-') || 'ha-fisico',
        name: newName.trim() || 'Home Assistant Físico',
        url: newUrl.trim(),
        enabled: false,
        priority: 5,
      })
      setNewName('')
      setNewUrl('')
      onNotify('Perfil criado desabilitado — nenhum contato até ser habilitado.')
      refresh()
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusyId('')
    }
  }

  return (
    <Card title="Perfis Home Assistant" sub="Credenciais no Credential Broker · troca de perfil altera runtime sem restart">
      <ErrorAlert message={error} />
      <ErrorAlert message={actionError} />

      {activeProfile && activeProfile.status === 'READY' && (
        <div className="ops-card" style={{ margin: '10px 0' }}>
          <h4 style={{ margin: '0 0 6px' }}>Home Assistant — ativo</h4>
          <dl className="ops-kv">
            <dt>Core Version</dt><dd>{activeProfile.last_test?.core_version ?? '—'}</dd>
            <dt>API State</dt><dd>{activeProfile.last_test?.state ?? '—'}</dd>
            <dt>Entities</dt><dd>{activeProfile.last_test?.entity_count ?? 0}</dd>
            <dt>Latency</dt><dd>{formatMs(activeProfile.last_test?.latency_ms)}</dd>
            <dt>Active Profile</dt><dd>{activeProfile.profile_id}</dd>
            <dt>Authentication</dt><dd>{authLabel(activeProfile.auth_configured)}</dd>
            <dt>Last Success</dt><dd>{formatRelative(ago(activeProfile.last_test?.tested_at))}</dd>
            <dt>Last Sync</dt><dd>{formatRelative(ago(activeProfile.last_test?.tested_at))}</dd>
          </dl>
          <div className="ops-hint" style={{ marginTop: 6 }}>Realtime Events: NOT AVAILABLE</div>
        </div>
      )}

      <div className="table-scroll">
        <table className="ops-table ha-profile-table">
          <thead>
            <tr>
              <th>Perfil</th><th>URL</th><th>Auth</th><th>Estado</th><th>Último teste</th><th>Ações</th>
            </tr>
          </thead>
          <tbody>
            {(data?.profiles ?? []).map((profile) => (
              <tr key={profile.profile_id}>
                <td>
                  <strong>{profile.name}</strong>
                  {data?.active_profile === profile.profile_id && (
                    <span className="ops-badge" data-state="READY" style={{ marginLeft: 8 }}>ATIVO</span>
                  )}
                  {!profile.enabled && <span className="ops-hint" style={{ marginLeft: 6 }}>desabilitado</span>}
                </td>
                <td>{profile.url || <span className="ops-hint">não configurada</span>}</td>
                <td>{authLabel(profile.auth_configured)}</td>
                <td><StatusBadge state={profile.status} /></td>
                <td>{profile.last_test ? formatRelative(ago(profile.last_test.tested_at)) : '—'}</td>
                <td>
                  <div className="ha-profile-actions">
                    <ActionButton small busy={busyId === `${profile.profile_id}:test`} disabled={!profile.enabled || !profile.url}
                      onClick={() => void run(profile, 'test')}>
                      Testar
                    </ActionButton>
                    <ActionButton small title="Editar nome/URL/token/TLS"
                      onClick={() => setEditing(editorFrom(profile))}>
                      Editar
                    </ActionButton>
                    {profile.enabled ? (
                      <>
                        <ActionButton small busy={busyId === `${profile.profile_id}:activate`}
                          disabled={!profile.url} title="Define este perfil como ativo no runtime"
                          onClick={() => void run(profile, 'activate')}>
                          Ativar
                        </ActionButton>
                        <ActionButton small busy={busyId === `${profile.profile_id}:disable`}
                          onClick={() => void run(profile, 'disable')}>
                          Desabilitar
                        </ActionButton>
                      </>
                    ) : (
                      <ActionButton small variant="primary" busy={busyId === `${profile.profile_id}:enable`}
                        onClick={() => void run(profile, 'enable')}>
                        Habilitar
                      </ActionButton>
                    )}
                    {profile.profile_id !== 'ha-vm' && (
                      <ActionButton small variant="danger" busy={busyId === `${profile.profile_id}:delete`}
                        onClick={() => void run(profile, 'delete')}>
                        Excluir
                      </ActionButton>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {editing && (
        <div className="ops-card" style={{ marginTop: 12 }}>
          <h4 style={{ margin: '0 0 8px' }}>Editar perfil: {editing.profile_id}</h4>
          <div style={{ display: 'grid', gap: 8 }}>
            <label style={labelStyle}>
              Profile Name
              <input type="text" value={editing.name} style={inputStyle}
                onChange={(event) => setEditing({ ...editing, name: event.target.value })} />
            </label>
            <label style={labelStyle}>
              Base URL
              <input type="text" value={editing.url} placeholder="https://home-assistant.example.invalid" style={inputStyle}
                onChange={(event) => setEditing({ ...editing, url: event.target.value })} />
            </label>
            <label style={labelStyle}>
              Long-Lived Access Token
              <input type="password" value={editing.token} autoComplete="new-password"
                placeholder={tokenPlaceholder(editing.profile_id, data)}
                style={inputStyle}
                onChange={(event) => setEditing({ ...editing, token: event.target.value })} />
            </label>
            <span className="ops-hint">
              Authentication configured:{' '}
              {tokenPlaceholder(editing.profile_id, data) === '' ? 'YES' : 'NO'}
            </span>
            <label style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <input type="checkbox" checked={editing.tls}
                onChange={(event) => setEditing({ ...editing, tls: event.target.checked })} />
              TLS Verification
            </label>
            <label style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <input type="checkbox" checked={editing.enabled}
                onChange={(event) => setEditing({ ...editing, enabled: event.target.checked })} />
              Enabled
            </label>
            <label style={labelStyle}>
              Prioridade
              <input type="number" min={1} max={999} value={editing.priority} style={inputStyle}
                onChange={(event) => setEditing({ ...editing, priority: Number(event.target.value) || 99 })} />
            </label>
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
            <ActionButton small variant="primary" busy={busyId === `${editing.profile_id}:save`}
              onClick={() => void saveEditor()}>
              Save
            </ActionButton>
            <ActionButton small onClick={() => setEditing(null)}>Cancelar</ActionButton>
          </div>
        </div>
      )}

      {lastTest && (
        <div className={`ops-alert ${lastTest.ok ? 'info' : 'warn'}`} style={{ marginTop: 10 }}>
          {lastTest.ok
            ? `Conexão OK · Core ${lastTest.core_version ?? '?'} · estado ${lastTest.state ?? '?'} · ${lastTest.entity_count ?? 0} entidades · ${formatMs(lastTest.latency_ms)}`
            : `Falha no teste: ${lastTest.error_code ?? 'desconhecida'}`}
        </div>
      )}

      {pendingDelete && (
        <div className="ops-alert warn" style={{ marginTop: 10 }}>
          Excluir o perfil {pendingDelete.profile.name} e sua credencial local?
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <ActionButton small variant="danger" onClick={() => void confirmDelete(true)}>Aprovar exclusão</ActionButton>
            <ActionButton small onClick={() => void confirmDelete(false)}>Recusar</ActionButton>
          </div>
        </div>
      )}

      {diagnostics && (
        <details style={{ marginTop: 10 }}>
          <summary className="ops-hint" style={{ cursor: 'pointer' }}>Diagnóstico bruto</summary>
          <pre className="ops-code" style={{ maxHeight: 220 }}>{JSON.stringify(diagnostics, null, 2)}</pre>
        </details>
      )}

      <div className="ha-profile-create-row">
        <input
          type="text"
          placeholder="Nome do novo perfil físico"
          value={newName}
          onChange={(event) => setNewName(event.target.value)}
          style={inputStyle}
        />
        <input
          type="text"
          placeholder="http://192.168.1.xxx (opcional)"
          value={newUrl}
          onChange={(event) => setNewUrl(event.target.value)}
          style={inputStyle}
        />
        <ActionButton small busy={busyId === 'create'} onClick={() => void createPhysicalProfile()}>
          Criar perfil físico-ready
        </ActionButton>
        <ActionButton small busy={busyId === 'diagnostics'} onClick={() => void openDiagnostics()}>
          Diagnóstico
        </ActionButton>
      </div>
      {!loading && !data && <Empty text="Perfis indisponíveis." />}

      <h4 style={{ margin: '18px 0 8px' }}>Entidades</h4>
      <HAEntityBrowser />
    </Card>
  )
}

const labelStyle: CSSProperties = { display: 'grid', gap: 4, fontSize: 13 }
const inputStyle: CSSProperties = {
  background: 'var(--ops-bg-2)', border: '1px solid var(--ops-line-strong)',
  color: 'var(--ops-text)', borderRadius: 6, height: 30, padding: '0 10px', fontSize: 13,
}

function tokenPlaceholder(profileId: string, data: HAProfilesResponse | null): string {
  const profile = (data?.profiles ?? []).find((p) => p.profile_id === profileId)
  return profile?.auth_configured ? '•••••••• (configured)' : ''
}

function ago(testedAt: number | undefined): number | null {
  if (!testedAt) return null
  return Math.max(0, Date.now() / 1000 - testedAt)
}
