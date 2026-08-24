import { useMemo } from 'react'
import { usePolling } from '../hooks'
import { Card, Empty, ErrorAlert, StatusBadge, formatRelative } from '../ui'
import { SentinelSettings } from '../../components/SentinelSettings'

interface SentinelStatus {
  enabled: boolean
  state: string
  host: string | null
  sentinel_version: string | null
  bridge_version: string
  connected_since: string | null
  events_received: number
  reconnect_count: number
  token_configured: boolean
  last_error: string
}

interface SentinelEvent {
  event_id: string
  timestamp: string
  severity: string
  title: string
  type?: string
  entity?: { name?: string; type?: string }
}

interface HomelabHost {
  host_id: string
  address: string
  reachable: boolean
  overall_state: string
}

interface OverviewShape {
  hosts: HomelabHost[]
  summary: Record<string, number>
}

export function SentinelPage() {
  const status = usePolling<SentinelStatus>('/api/sentinel-watch/status', 5000)
  const events = usePolling<{ events: SentinelEvent[] }>('/api/sentinel-watch/events?hours=24&limit=25', 10000)
  const homelab = usePolling<OverviewShape>('/api/homelab/overview', 20000)

  const alertEvents = useMemo(
    () => (events.data?.events ?? []).filter((event) => String(event.severity).toUpperCase() !== 'INFO'),
    [events.data],
  )

  return (
    <div>
      <header className="ops-page-header">
        <div>
          <h1 className="ops-page-title">UTAMO Sentinel</h1>
          <p className="ops-page-subtitle">
            Sentinel = percepção de rede. NYRA = raciocínio e ação.
            Nenhum scanner duplicado; apenas a bridge oficial é consumida.
          </p>
        </div>
        <div className="ops-header-spacer" />
        <StatusBadge state={status.data ? status.data.state : 'OFFLINE'} />
      </header>

      <ErrorAlert message={status.error} hint="O backend continua operando com o Sentinel offline." />

      {!status.data?.enabled && (
        <div className="ops-alert info">Sentinel integration disabled — habilite abaixo em Configuração.</div>
      )}

      <div className="ops-grid-2">
        <Card title="Conexão" sub="Bridge Socket.IO /integrations/nyra">
          <dl className="ops-kv">
            <dt>Estado</dt><dd>{status.data?.state ?? '—'}</dd>
            <dt>Host</dt><dd>{status.data?.host ?? '—'}</dd>
            <dt>Versão Sentinel</dt><dd>{status.data?.sentinel_version ?? '—'}</dd>
            <dt>Bridge</dt><dd>v{status.data?.bridge_version ?? '1'}</dd>
            <dt>Autenticação configurada</dt><dd>{status.data?.token_configured ? 'Sim' : 'Não'}</dd>
            <dt>Eventos recebidos</dt><dd>{status.data?.events_received ?? 0}</dd>
            <dt>Reconexões</dt><dd>{status.data?.reconnect_count ?? 0}</dd>
            <dt>Último erro</dt><dd>{status.data?.last_error || '—'}</dd>
          </dl>
        </Card>

        <Card title="Alertas recentes (24h)" sub={`Severidade ≠ INFO · ${alertEvents.length}`}>
          {alertEvents.length === 0 ? (
            <Empty text="Nenhum alerta nas últimas 24 horas." />
          ) : (
            <div className="table-scroll">
              <table className="ops-table">
                <thead>
                  <tr><th>Severidade</th><th>Título</th><th>Origem</th><th>Quando</th></tr>
                </thead>
                <tbody>
                  {alertEvents.slice(0, 10).map((event) => (
                    <tr key={event.event_id}>
                      <td><strong>{String(event.severity).toUpperCase()}</strong></td>
                      <td>{event.title}</td>
                      <td>{event.entity?.name ?? '—'}</td>
                      <td>{formatRelative(relative(event.timestamp))}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card title="Eventos (24h)" sub={`${events.data?.events.length ?? 0} eventos`}>
          {(events.data?.events ?? []).length === 0 ? (
            <Empty text="Sem eventos recebidos do Sentinel." />
          ) : (
            <div className="table-scroll">
              <table className="ops-table">
                <thead>
                  <tr><th>Severidade</th><th>Tipo</th><th>Título</th><th>Quando</th></tr>
                </thead>
                <tbody>
                  {(events.data?.events ?? []).slice(0, 12).map((event) => (
                    <tr key={event.event_id}>
                      <td>{String(event.severity).toUpperCase()}</td>
                      <td>{event.type ?? '—'}</td>
                      <td>{event.title}</td>
                      <td>{formatRelative(relative(event.timestamp))}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card title="Remote Nodes" sub="Nós remotos reportados pelo Sentinel">
          <Empty text="O bridge v1 não publica remote nodes dedicados. O OpenWrt aparece no painel Homelab quando o registry o inclui." />
        </Card>
      </div>

      <h2 className="ops-section-title">Hosts monitorados (Homelab Registry)</h2>
      <Card>
        <div className="table-scroll">
          <table className="ops-table">
            <thead>
              <tr><th>Endereço</th><th>Identificador</th><th>Acessível</th><th>Estado</th></tr>
            </thead>
            <tbody>
              {(homelab.data?.hosts ?? []).map((host) => (
                <tr key={host.host_id}>
                  <td><strong>{host.address}</strong></td>
                  <td>{host.host_id}</td>
                  <td>{host.reachable ? 'Sim' : 'Não'}</td>
                  <td><StatusBadge state={host.overall_state} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!homelab.data && <Empty text="Homelab indisponível." />}
      </Card>

      <h2 className="ops-section-title">Configuração</h2>
      <SentinelSettings />
    </div>
  )
}

function relative(timestampIso: string): number | null {
  if (!timestampIso) return null
  const parsed = Date.parse(timestampIso) / 1000
  if (!Number.isFinite(parsed)) return null
  return Math.max(0, Date.now() / 1000 - parsed)
}
