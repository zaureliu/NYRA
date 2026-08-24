import { useMemo, useState } from 'react'
import { apiSend } from '../../runtime/api'
import { usePolling } from '../hooks'
import { ActionButton, Card, ErrorAlert, StatusBadge, Toggle } from '../ui'
import type { CapabilitiesResponse, Capability } from '../types'

const CATEGORY_LABELS: Record<string, string> = {
  operations: 'Operação',
  desktop: 'Desktop',
  voice: 'Voz',
  autonomy: 'Autonomia',
  homelab: 'Homelab',
  integrations: 'Integrações',
  network: 'Rede',
}

const CATEGORY_ORDER = ['operations', 'desktop', 'voice', 'autonomy', 'homelab', 'integrations', 'network']

export function CapabilitiesPage() {
  const { data, error, loading, refresh } = usePolling<CapabilitiesResponse>('/api/capabilities', 8000)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [actionError, setActionError] = useState('')
  const [notice, setNotice] = useState('')

  const grouped = useMemo(() => {
    const map = new Map<string, Capability[]>()
    for (const capability of data?.capabilities ?? []) {
      const list = map.get(capability.category) ?? []
      list.push(capability)
      map.set(capability.category, list)
    }
    return [...map.entries()].sort(
      (a, b) => CATEGORY_ORDER.indexOf(a[0]) - CATEGORY_ORDER.indexOf(b[0]),
    )
  }, [data])

  const toggleCapability = async (capability: Capability, enabled: boolean) => {
    setBusyId(capability.id)
    setActionError('')
    setNotice('')
    try {
      const result = await apiSend<{ restart_required?: boolean; runtime_state?: string }>(
        `/api/capabilities/${capability.id}`,
        'PUT',
        { enabled },
      )
      setNotice(
        result.restart_required
          ? `${capability.name}: valor salvo. Aplicado no próximo restart do backend.`
          : `${capability.name}: ${enabled ? 'habilitada' : 'desabilitada'} e verificada em runtime.`,
      )
      refresh()
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div>
      <header className="ops-page-header">
        <div>
          <h1 className="ops-page-title">Capabilities</h1>
          <p className="ops-page-subtitle">
            Feature Control Center — cada toggle altera o backend de verdade,
            com persistência e verificação de saúde.
          </p>
        </div>
        <div className="ops-header-spacer" />
        {data && (
          <span className="ops-chip" data-tone={data.summary.restart_required > 0 ? 'warn' : 'ok'}>
            <span className="chip-dot" />
            {data.summary.enabled}/{data.summary.total} ativas
            {data.summary.restart_required > 0 ? ` · ${data.summary.restart_required} aguardam restart` : ''}
          </span>
        )}
        <ActionButton onClick={() => refresh()} busy={loading}>Atualizar</ActionButton>
      </header>

      <ErrorAlert message={error} hint="Verifique se o backend está em execução." />
      <ErrorAlert message={actionError} />
      {notice && <div className="ops-alert info">{notice}</div>}

      {!data && !error && loading && (
        <div className="ops-empty"><span className="ops-loading" /> Carregando capabilities…</div>
      )}

      {grouped.map(([category, capabilities]) => (
        <div key={category}>
          <h2 className="ops-section-title">{CATEGORY_LABELS[category] ?? category}</h2>
          <div className="ops-card-grid">
            {capabilities.map((capability) => (
              <Card
                key={capability.id}
                title={capability.name}
                sub={capability.description}
                actions={
                  capability.toggleable ? (
                    <Toggle
                      checked={capability.enabled}
                      disabled={busyId === capability.id}
                      label=""
                      onChange={(value) => void toggleCapability(capability, value)}
                    />
                  ) : (
                    <span className="ops-hint" title="Gerenciada pelo subsistema pai">derivada</span>
                  )
                }
              >
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
                  <StatusBadge state={capability.runtime_state} />
                  {capability.restart_required && (
                    <span className="ops-restart-flag">⟳ Restart required</span>
                  )}
                  {!capability.configured && <span className="ops-hint">não configurada</span>}
                </div>
                <div className="ops-card-sub" style={{ marginTop: 8 }}>
                  Consumer: <code style={{ fontSize: 12 }}>{capability.consumer}</code>
                </div>
                {capability.last_error && (
                  <div className="ops-alert warn" style={{ margin: '8px 0 0' }}>
                    Último erro: {capability.last_error}
                  </div>
                )}
              </Card>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
