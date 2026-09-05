import { useMemo, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { apiSend } from '../../runtime/api'
import { ModelSelectorCard } from '../components/ModelSelectorCard'
import { SelfDevPanel } from '../components/SelfDevPanel'
import { usePolling } from '../hooks'
import { ActionButton, Card, ErrorAlert, Toggle } from '../ui'
import type { SettingEntry, SettingsV3Response } from '../types'

const CATEGORY_META: Record<string, { label: string; icon: string }> = {
  general: { label: 'Geral', icon: 'GE' },
  ai: { label: 'IA', icon: 'IA' },
  voice: { label: 'Voz', icon: 'VZ' },
  desktop: { label: 'Desktop', icon: 'DT' },
  automation: { label: 'Automação', icon: 'AU' },
  homelab: { label: 'Homelab', icon: 'HL' },
  integrations: { label: 'Integrações', icon: 'IN' },
  privacy: { label: 'Privacidade', icon: 'PV' },
  developer: { label: 'Developer', icon: 'DV' },
  selfdev: { label: 'Self-Dev', icon: 'SD' },
}

export function SettingsPageV3() {
  const { data, error, loading, refresh } = usePolling<SettingsV3Response>('/api/settings/v3', 20000)
  const [activeCategory, setActiveCategory] = useState('general')
  const [busyKey, setBusyKey] = useState('')
  const [actionError, setActionError] = useState('')
  const [notice, setNotice] = useState('')
  const [powerConfirm, setPowerConfirm] = useState<'shutdown' | 'restart' | null>(null)
  const [powerApproval, setPowerApproval] = useState<string | null>(null)

  const requestPower = async (action: 'shutdown' | 'restart', approvalId?: string) => {
    setBusyKey(`__power_${action}`)
    setActionError('')
    try {
      const response = await apiSend<{ approval_required?: boolean; approval_id?: string }>(
        `/api/runtime/power/${action}`, 'POST',
        { reason: 'settings_ui', ...(approvalId ? { approval_id: approvalId } : {}) },
      )
      if (response.approval_required && response.approval_id && !approvalId) {
        setPowerApproval(response.approval_id)
        setNotice('Approval crítico criado. Revise a ação e confirme uma segunda vez.')
        return
      }
      setNotice(
        action === 'shutdown'
          ? 'Encerramento completo solicitado. A KAZUMI vai desligar todos os componentes dela.'
          : 'Reinício completo solicitado. Uma nova sessão vai iniciar após o encerramento validado.',
      )
      setPowerConfirm(null)
      setPowerApproval(null)
      if (action === 'shutdown' && '__TAURI_INTERNALS__' in window) {
        await invoke('quit_kazumi')
      }
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
      setPowerConfirm(null)
    } finally {
      setBusyKey('')
    }
  }

  const decidePower = async (approved: boolean) => {
    if (!powerConfirm || !powerApproval) return
    const action = powerConfirm
    const approvalId = powerApproval
    if (!approved) {
      await apiSend(`/api/shell/approvals/${encodeURIComponent(approvalId)}`, 'POST', { approved: false })
      setPowerApproval(null)
      setPowerConfirm(null)
      return
    }
    await apiSend(`/api/shell/approvals/${encodeURIComponent(approvalId)}`, 'POST', { approved: true })
    await requestPower(action, approvalId)
  }

  const entriesByCategory = useMemo(() => {
    const map = new Map<string, SettingEntry[]>()
    for (const entry of data?.settings ?? []) {
      const list = map.get(entry.category) ?? []
      list.push(entry)
      map.set(entry.category, list)
    }
    return map
  }, [data])

  const update = async (entry: SettingEntry, value: unknown) => {
    setBusyKey(entry.key)
    setActionError('')
    setNotice('')
    try {
      await apiSend('/api/settings/v3', 'PUT', { key: entry.key, value })
      setNotice(`${entry.key} salvo no backend.`)
      refresh()
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusyKey('')
    }
  }

  const exportConfig = async () => {
    setBusyKey('__export')
    try {
      const blob = await fetch('/api/config/export').then((response) => response.blob())
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = 'kazumi-config-export.json'
      anchor.click()
      URL.revokeObjectURL(url)
      setNotice('Export gerado — segredos aparecem apenas como configured true/false.')
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusyKey('')
    }
  }

  const categories = data?.categories ?? Object.keys(CATEGORY_META)

  return (
    <div>
      <header className="ops-page-header">
        <div>
          <h1 className="ops-page-title">Configurações</h1>
          <p className="ops-page-subtitle">
            Fonte única: backend. Nada é salvo no navegador. Segredos nunca aparecem aqui —
            continuam no Credential Broker / secret stores.
          </p>
        </div>
        <div className="ops-header-spacer" />
        <ActionButton onClick={() => void exportConfig()} busy={busyKey === '__export'}>
          Exportar config (sem segredos)
        </ActionButton>
      </header>

      <ErrorAlert message={error} />
      <ErrorAlert message={actionError} />
      {notice && <div className="ops-alert info">{notice}</div>}

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 16 }}>
        {categories.map((category) => (
          <button
            key={category}
            type="button"
            className={`ops-btn small${activeCategory === category ? ' primary' : ''}`}
            onClick={() => setActiveCategory(category)}
          >
            {CATEGORY_META[category]?.label ?? category}
          </button>
        ))}
      </div>

      {!data && loading && <div className="ops-empty"><span className="ops-loading" /> Carregando schema de settings…</div>}

      {activeCategory === 'ai' && <ModelSelectorCard />}
      {activeCategory === 'selfdev' && <SelfDevPanel />}

      {(entriesByCategory.get(activeCategory) ?? []).length > 0 && (
        <Card title={CATEGORY_META[activeCategory]?.label ?? activeCategory}
          sub={`${entriesByCategory.get(activeCategory)?.length ?? 0} settings`}>
          <div className="table-scroll">
            <table className="ops-table">
              <thead>
                <tr><th>Chave</th><th>Descrição</th><th style={{ width: 220 }}>Valor</th></tr>
              </thead>
              <tbody>
                {(entriesByCategory.get(activeCategory) ?? []).map((entry) => (
                  <SettingRow
                    key={entry.key}
                    entry={entry}
                    busy={busyKey === entry.key}
                    onUpdate={(value) => void update(entry, value)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {activeCategory === 'general' && (
        <Card title="Energia" sub="Encerra ou reinicia o runtime completo da KAZUMI — não apenas esta página">
          {powerConfirm && (
            <div className="ops-alert warn" style={{ marginBottom: 10 }}>
              {powerConfirm === 'shutdown'
                ? 'Encerrar KAZUMI completamente? Todos os processos dela serão finalizados e a porta 8000 liberada.'
                : 'Reiniciar KAZUMI completamente? A sessão atual termina e uma nova inicia com novo session_id.'}
              {powerApproval && <div style={{ marginTop: 6 }}>Approval crítico pendente: confirme para consumir uma única vez.</div>}
              <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                <ActionButton small variant="danger" busy={busyKey === `__power_${powerConfirm}`}
                  onClick={() => void (powerApproval ? decidePower(true) : requestPower(powerConfirm))}>
                  {powerApproval ? 'Aprovar e executar' : 'Solicitar approval'}
                </ActionButton>
                <ActionButton small onClick={() => void (powerApproval ? decidePower(false) : setPowerConfirm(null))}>Cancelar</ActionButton>
              </div>
            </div>
          )}
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <ActionButton small variant="danger" onClick={() => setPowerConfirm('shutdown')}>
              Encerrar KAZUMI completamente
            </ActionButton>
            <ActionButton small onClick={() => setPowerConfirm('restart')}>
              Reiniciar KAZUMI completamente
            </ActionButton>
          </div>
          <div className="ops-hint" style={{ marginTop: 8 }}>
            O watchdog é desarmado antes da saída — nada é relançado durante o encerramento.
          </div>
        </Card>
      )}
    </div>
  )
}

function SettingRow({ entry, busy, onUpdate }: {
  entry: SettingEntry
  busy: boolean
  onUpdate: (value: unknown) => void
}) {
  if (entry.sensitive) {
    return (
      <tr>
        <td><code style={{ fontSize: 12 }}>{entry.key}</code></td>
        <td>{entry.description}<div className="ops-hint">{entry.configure_via}</div></td>
        <td>
          <span className={`ops-badge`} data-state={typeof entry.current === 'object' && entry.current !== null && (entry.current as { configured?: boolean }).configured ? 'PASS' : 'UNKNOWN'}>
            {(entry.current as { configured?: boolean })?.configured ? 'CONFIGURED' : 'NOT CONFIGURED'}
          </span>
        </td>
      </tr>
    )
  }
  return (
    <tr>
      <td>
        <code style={{ fontSize: 12 }}>{entry.key}</code>
        {entry.requires_restart && (
          <div className="ops-restart-flag">⟳ restart</div>
        )}
      </td>
      <td>{entry.description}</td>
      <td>
        {entry.type === 'bool' && (
          <Toggle
            checked={Boolean(entry.current)}
            disabled={busy}
            label=""
            onChange={(value) => onUpdate(value)}
          />
        )}
        {entry.type === 'enum' && (
          <select
            value={String(entry.current)}
            disabled={busy}
            onChange={(event) => onUpdate(event.target.value)}
            style={{ width: '100%', background: 'var(--ops-bg-2)', border: '1px solid var(--ops-line-strong)', color: 'var(--ops-text)', borderRadius: 6, height: 30, fontSize: 13 }}
          >
            {(entry.options ?? []).map((option) => (
              <option key={option} value={option}>{option}</option>
            ))}
          </select>
        )}
        {(entry.type === 'int' || entry.type === 'float') && (
          <input
            type="number"
            value={Number(entry.current ?? 0)}
            min={entry.minimum ?? undefined}
            max={entry.maximum ?? undefined}
            step={entry.type === 'float' ? 0.05 : 1}
            disabled={busy}
            onBlur={(event) => onUpdate(entry.type === 'int' ? Number(event.target.value) : parseFloat(event.target.value))}
            style={{ width: 110, background: 'var(--ops-bg-2)', border: '1px solid var(--ops-line-strong)', color: 'var(--ops-text)', borderRadius: 6, height: 30, padding: '0 8px', fontSize: 13 }}
          />
        )}
        {entry.type === 'str' && (
          <input
            type="text"
            defaultValue={String(entry.current ?? '')}
            disabled={busy}
            onBlur={(event) => event.target.value !== String(entry.current ?? '') && onUpdate(event.target.value)}
            style={{ width: '100%', background: 'var(--ops-bg-2)', border: '1px solid var(--ops-line-strong)', color: 'var(--ops-text)', borderRadius: 6, height: 30, padding: '0 8px', fontSize: 13 }}
          />
        )}
      </td>
    </tr>
  )
}
