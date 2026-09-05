import { usePolling } from '../hooks'
import { Card, StatusBadge, formatMs, formatRelative } from '../ui'
import { HomelabPanel } from '../../components/HomelabPanel'
import { HAProfilesCard } from './HAProfilesCard'
import { ProxmoxConfigCard } from './ProxmoxConfigCard'
import type { IntegrationCard, IntegrationsStatusResponse } from '../types'

/** Homelab + Integrações compartilham a MESMA fonte (/api/integrations/status
 * e snapshots unificados) — proibido divergir estados entre páginas (§44). */
export function HomelabPage() {
  const { data } = usePolling<IntegrationsStatusResponse>('/api/integrations/status', 10000)
  const ha = data?.integrations?.home_assistant
  const proxmox = data?.integrations?.proxmox

  return (
    <div className="homelab-density-page">
      <header className="ops-page-header">
        <div>
          <h1 className="ops-page-title">Homelab</h1>
          <p className="ops-page-subtitle">
            OpenWrt · Proxmox · Home Assistant · DC1 · Sentinel — leitura read-only por padrão.
          </p>
        </div>
      </header>

      <div className="ops-card-grid">
        <HomeAssistantSummaryCard card={ha} />
        <ProxmoxSummaryCard card={proxmox} />
      </div>

      <HomelabPanel />

      <h2 className="ops-section-title">Home Assistant</h2>
      <HAProfilesCard onNotify={() => undefined} />

      <h2 className="ops-section-title" style={{ marginTop: 24 }}>Proxmox VE</h2>
      <ProxmoxConfigCard onNotify={() => undefined} />
    </div>
  )
}

function HomeAssistantSummaryCard({ card }: { card?: IntegrationCard }) {
  if (!card) {
    return (
      <Card title="Home Assistant">
        <p className="ops-hint">Sem dados ainda.</p>
      </Card>
    )
  }
  return (
    <Card title="Home Assistant" actions={<StatusBadge state={card.state} />}>
      <dl className="ops-kv">
        <dt>Core Version</dt><dd>{card.core_version ?? '—'}</dd>
        <dt>Entities</dt><dd>{card.entity_count ?? '—'}</dd>
        <dt>Active Profile</dt><dd>{card.active_profile ?? '—'}</dd>
        <dt>Latency</dt><dd>{formatMs(card.latency_ms)}</dd>
        <dt>Auth Configured</dt><dd>{card.auth_configured ? 'Sim' : 'Não'}</dd>
        <dt>Last Success</dt><dd>{formatRelative(relativeSeconds(card.last_success))}</dd>
        {card.realtime_events && (<><dt>Realtime Events</dt><dd>{card.realtime_events}</dd></>)}
      </dl>
    </Card>
  )
}

function ProxmoxSummaryCard({ card }: { card?: IntegrationCard }) {
  if (!card) {
    return (
      <Card title="Proxmox VE">
        <p className="ops-hint">Sem dados ainda.</p>
      </Card>
    )
  }
  return (
    <Card title="Proxmox VE" actions={<StatusBadge state={card.state} />}>
      <dl className="ops-kv">
        <dt>Version</dt><dd>{card.version ?? '—'}</dd>
        <dt>Nodes</dt><dd>{card.node_count ?? '—'}</dd>
        <dt>Running VMs</dt><dd>{card.qemu_count ?? 0} QEMU · {card.lxc_count ?? 0} LXC</dd>
        <dt>Storage</dt><dd>{card.storage_count ?? '—'}</dd>
        <dt>Latency</dt><dd>{formatMs(card.latency_ms)}</dd>
        <dt>Last Success</dt><dd>{formatRelative(relativeSeconds(card.last_success))}</dd>
      </dl>
    </Card>
  )
}

function relativeSeconds(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined) return null
  const numeric = typeof value === 'string' ? Date.parse(value) / 1000 : Number(value)
  if (!Number.isFinite(numeric) || numeric <= 0) return null
  return Math.max(0, Date.now() / 1000 - numeric)
}
