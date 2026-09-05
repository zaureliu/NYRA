import { useState } from 'react'
import { apiSend } from '../../runtime/api'
import { usePolling } from '../hooks'
import { ActionButton, Card, ErrorAlert, StatusBadge } from '../ui'
import type { IntegrationCard, IntegrationsStatusResponse } from '../types'
import { formatRelative } from '../ui'
import { HAProfilesCard } from './HAProfilesCard'
import { OpenWrtConfigCard } from './OpenWrtConfigCard'
import { ProxmoxConfigCard } from './ProxmoxConfigCard'

type IntegrationAction = 'test' | 'enable' | 'disable' | 'reconnect' | 'diagnostics'

/** Integrações (prompt11_1 §40-§41): todos os botões com consumer real;
 * Configure/Open navegam ou abrem a URL real reportada pelo backend. */
export function IntegrationsPage({ onOpenSentinel }: { onOpenSentinel: () => void }) {
  const { data, error, loading, refresh } = usePolling<IntegrationsStatusResponse>('/api/integrations/status', 10000)
  const [busyKey, setBusyKey] = useState('')
  const [actionError, setActionError] = useState('')
  const [diagnostics, setDiagnostics] = useState<{ id: string; body: unknown } | null>(null)
  const [testResult, setTestResult] = useState<{ id: string; ok: boolean; detail: string } | null>(null)
  const [configureId, setConfigureId] = useState('')

  const runAction = async (integration: IntegrationCard, action: IntegrationAction) => {
    const key = `${integration.id}:${action}`
    setBusyKey(key)
    setActionError('')
    try {
      const response = await apiSend<{ result: Record<string, unknown> }>(
        `/api/integrations/${integration.id}/${action}`,
        'POST',
      )
      if (action === 'test') {
        setTestResult({
          id: integration.id,
          ok: response.result.ok !== false,
          detail: summarizeTest(response.result),
        })
      } else if (action === 'diagnostics') {
        setDiagnostics({ id: integration.id, body: response.result })
      }
      refresh()
    } catch (issue) {
      setActionError(`${integration.name}/${action}: ${issue instanceof Error ? issue.message : issue}`)
    } finally {
      setBusyKey('')
    }
  }

  const openIntegration = (integration: IntegrationCard) => {
    // prompt11_2 §13: Abrir do Proxmox abre a view detalhada DENTRO da KAZUMI
    // (nunca card falso); as demais usam a URL real reportada pelo backend.
    if (integration.id === 'proxmox') {
      setConfigureId('proxmox')
      return
    }
    if (integration.open_url) {
      window.open(integration.open_url, '_blank', 'noopener')
    }
  }

  return (
    <div className="integrations-density-page">
      <header className="ops-page-header">
        <div>
          <h1 className="ops-page-title">Integrações</h1>
          <p className="ops-page-subtitle">
            Estado real de cada integração — nada de status inventado.
            Ações executam contra o backend e reportam o resultado.
          </p>
        </div>
        <div className="ops-header-spacer" />
        {data && (
          <span className="ops-chip" data-tone={data.summary.failing > 0 ? 'err' : 'ok'}>
            <span className="chip-dot" />
            {data.summary.ready} prontas · {data.summary.unconfigured} sem config · {data.summary.disabled} off
          </span>
        )}
        <ActionButton onClick={() => refresh()} busy={loading}>Atualizar Integrações</ActionButton>
      </header>

      <ErrorAlert message={error} />
      <ErrorAlert message={actionError} />

      <div className="ops-card-grid">
        {(data ? Object.values(data.integrations) : []).map((integration) => (
          <Card
            key={integration.id}
            title={integration.name}
            actions={<StatusBadge state={integration.state} />}
          >
            <dl className="ops-kv">
              <dt>Habilitada</dt><dd>{integration.enabled ? 'Sim' : 'Não'}</dd>
              <dt>Configurada</dt><dd>{integration.configured ? 'Sim' : 'Não'}</dd>
              <dt>Conectada</dt><dd>{integration.connected ? 'Sim' : '—'}</dd>
              <dt>Estado</dt><dd><StatusBadge state={integration.state} /></dd>
              <dt>Authentication</dt><dd>{integration.authentication ?? (integration.auth_configured ? 'CONFIGURADA' : 'AUSENTE')}</dd>
              <dt>Saúde</dt><dd>{integration.health || '—'}</dd>
              <dt>Latência</dt><dd>{integration.latency_ms != null ? `${integration.latency_ms}ms` : '—'}</dd>
              <dt>Último teste</dt><dd>{formatRelative(relativeSeconds(integration.last_test))}</dd>
              <dt>Última sync</dt><dd>{formatRelative(relativeSeconds(integration.last_sync))}</dd>
              {integration.core_version && (<><dt>Versão</dt><dd>{integration.core_version}</dd></>)}
            </dl>
            {integration.state === 'READY' && integration.entity_count != null && (
              <div className="ops-hint">{integration.entity_count} entidades</div>
            )}
            {integration.last_error && (
              <div className="ops-alert warn" style={{ margin: '10px 0 0' }}>
                Erro: {integration.last_error}
              </div>
            )}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 14 }}>
              <ActionButton small busy={busyKey === `${integration.id}:test`} onClick={() => void runAction(integration, 'test')}>
                Testar
              </ActionButton>
              <ActionButton small onClick={() => setConfigureId(configureId === integration.id ? '' : integration.id)}>
                Configurar
              </ActionButton>
              {integration.enabled ? (
                <ActionButton small busy={busyKey === `${integration.id}:disable`} onClick={() => void runAction(integration, 'disable')}>
                  Desabilitar
                </ActionButton>
              ) : (
                <ActionButton small variant="primary" busy={busyKey === `${integration.id}:enable`} onClick={() => void runAction(integration, 'enable')}>
                  Habilitar
                </ActionButton>
              )}
              {integration.id === 'sentinel' && (
                <>
                  <ActionButton small busy={busyKey === `${integration.id}:reconnect`} onClick={() => void runAction(integration, 'reconnect')}>
                    Reconectar
                  </ActionButton>
                  <ActionButton small onClick={onOpenSentinel}>Painel Sentinel</ActionButton>
                </>
              )}
              <ActionButton small busy={busyKey === `${integration.id}:diagnostics`} onClick={() => void runAction(integration, 'diagnostics')}>
                Diagnóstico
              </ActionButton>
              {(integration.id === 'proxmox' || integration.open_url) && (
                <ActionButton small onClick={() => openIntegration(integration)}>
                  Abrir
                </ActionButton>
              )}
            </div>
            {testResult?.id === integration.id && (
              <div className={`ops-alert ${testResult.ok ? 'info' : 'warn'}`} style={{ marginTop: 10 }}>
                Teste: {testResult.detail}
              </div>
            )}
            {diagnostics?.id === integration.id && (
              <details style={{ marginTop: 10 }}>
                <summary className="ops-hint" style={{ cursor: 'pointer' }}>Ver diagnóstico bruto</summary>
                <pre className="ops-code" style={{ maxHeight: 220 }}>{JSON.stringify(diagnostics.body, null, 2)}</pre>
              </details>
            )}
          </Card>
        ))}
      </div>
      {!data && !error && loading && <div className="ops-empty"><span className="ops-loading" /> Carregando integrações…</div>}

      {(configureId === 'home_assistant' || configureId === 'proxmox' || configureId === 'openwrt') && (
        <h2 className="ops-section-title" style={{ marginTop: 24 }}>
          {configureId === 'home_assistant' ? 'Home Assistant'
            : configureId === 'proxmox' ? 'Proxmox VE' : 'OpenWrt'}
        </h2>
      )}
      {configureId === 'home_assistant' && (
        <HAProfilesCard onNotify={() => undefined} />
      )}
      {configureId === 'proxmox' && (
        <ProxmoxConfigCard onNotify={() => undefined} />
      )}
      {configureId === 'openwrt' && (
        <OpenWrtConfigCard onNotify={() => undefined} />
      )}
    </div>
  )
}

function relativeSeconds(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined) return null
  const numeric = typeof value === 'string' ? Date.parse(value) / 1000 : Number(value)
  if (!Number.isFinite(numeric) || numeric <= 0) return null
  return Math.max(0, Date.now() / 1000 - numeric)
}

function summarizeTest(result: Record<string, unknown>): string {
  const parts: string[] = []
  if ('latency_ms' in result && result.latency_ms != null) parts.push(`${result.latency_ms}ms`)
  if (result.version) parts.push(`v${result.version}`)
  if (result.node_count != null) parts.push(`${result.node_count} nodes`)
  if (result.qemu_count != null) parts.push(`${result.qemu_count} VMs`)
  if (result.lxc_count != null) parts.push(`${result.lxc_count} LXC`)
  if (result.storage_count != null) parts.push(`${result.storage_count} storage`)
  if (result.core_version) parts.push(`Core ${result.core_version}`)
  if (result.entity_count != null) parts.push(`${result.entity_count} entidades`)
  if (result.state) parts.push(String(result.state))
  if (result.error_code) parts.push(String(result.error_code))
  if (result.http_status) parts.push(`HTTP ${result.http_status}`)
  return parts.length ? parts.join(' · ') : 'sem detalhes'
}
