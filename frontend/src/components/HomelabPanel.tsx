import { useEffect, useState } from 'react'

interface HomelabStatus {
  proactive_mode: boolean; poll_interval: number; last_stats: Record<string, number>
  proxmox_configured: boolean; openwrt_configured: boolean; enabled?: boolean
}

export interface HomelabHostState {
  host_id: string; address: string; overall_state: string; reachable: boolean
  integration_state?: string; integration_error_code?: string | null
  integration_detail?: Record<string, unknown>; cached?: boolean
}
interface HomelabOverview { generated_at: number; hosts: HomelabHostState[]; summary: Record<string, number> }

const HOST_ORDER = ['openwrt', 'proxmox', 'home_assistant', 'dc1']
const STATE_CLASS: Record<string, string> = {
  ONLINE: 'ok', OFFLINE: 'fail', UNREACHABLE: 'warn', DEGRADED: 'warn',
  AUTHENTICATION_FAILED: 'warn', INTEGRATION_UNAVAILABLE: 'idle', DISABLED: 'idle', UNKNOWN: 'idle',
}

export function hostStateClass(state: string): string {
  return STATE_CLASS[state] ?? 'idle'
}

export function formatUptime(seconds: unknown): string {
  const value = typeof seconds === 'number' ? seconds : Number(seconds)
  if (!Number.isFinite(value) || value <= 0) return '—'
  const days = Math.floor(value / 86400)
  const hours = Math.floor((value % 86400) / 3600)
  const minutes = Math.floor((value % 3600) / 60)
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

export function summarizeOverview(overview: HomelabOverview | null): HomelabHostState[] {
  if (!overview?.hosts?.length) return []
  const byId = new Map(overview.hosts.map(host => [host.host_id, host]))
  return [...HOST_ORDER, ...overview.hosts.map(h => h.host_id)]
    .filter((id, index, all) => byId.has(id) && all.indexOf(id) === index)
    .map(id => byId.get(id) as HomelabHostState)
}

export function hostDetailLine(host: HomelabHostState): string {
  const detail = host.integration_detail ?? {}
  if (host.integration_error_code === 'PROXMOX_AUTH_MISSING') return 'token não configurado'
  if (host.integration_error_code === 'HA_AUTH_MISSING') return 'token não configurado'
  if (host.integration_error_code) {
    const label = String(host.integration_error_code).replace(/_/g, ' ').toLowerCase()
    return label
  }
  const version = typeof detail.version === 'string' ? detail.version : ''
  if (version) return `v${version}`
  if (typeof detail.uptime_s === 'number' && detail.uptime_s > 0) return `up ${formatUptime(detail.uptime_s)}`
  return ''
}

export function HomelabPanel() {
  const [data, setData] = useState<HomelabStatus | null>(null)
  const [hosts, setHosts] = useState<HomelabHostState[]>([])
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const [statusResponse, overviewResponse] = await Promise.all([
          fetch('/api/homelab/status'),
          fetch('/api/homelab/overview'),
        ])
        if (cancelled) return
        if (statusResponse.ok) setData(await statusResponse.json())
        if (overviewResponse.ok) {
          const overview = await overviewResponse.json()
          setHosts(summarizeOverview(overview))
        }
      } catch { /* silent like peer panels */ }
    }
    void load()
    const timer = setInterval(() => void load(), 15000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [])
  const stats = data?.last_stats ?? {}
  return (
    <section className="panel compact-panel homelab-panel">
      <header className="panel-header"><span>HOMELAB</span><small>POLL {data?.poll_interval ?? 60}s</small></header>
      <ul className="integration-list homelab-hosts">
        {hosts.length === 0 && <li>HOSTS <span>—</span></li>}
        {hosts.map(host => (
          <li key={host.host_id}>
            <i className={hostStateClass(host.overall_state)} />
            <strong>{host.host_id.replace('_', ' ').toUpperCase()}</strong>
            <span className="homelab-host-detail">{hostDetailLine(host) || host.overall_state.toLowerCase()}</span>
            <em className={`homelab-state ${hostStateClass(host.overall_state)}`}>{host.overall_state}</em>
          </li>
        ))}
      </ul>
      <div className="metric-row"><Metric label="CPU" value={stats.cpu_percent} /><Metric label="RAM" value={stats.memory_percent} /><Metric label="DISCO" value={stats.disk_percent} /></div>
      <ul className="integration-list"><li>PROXMOX API <span>{data?.proxmox_configured ? 'READY' : 'NÃO CONFIGURADO'}</span></li><li>PROATIVO <span>{data?.proactive_mode ? 'ON' : 'OFF'}</span></li></ul>
    </section>
  )
}

function Metric({ label, value }: { label: string; value?: number }) {
  return <div className="metric"><span>{label}</span><strong>{value === undefined ? '—' : `${value.toFixed(0)}%`}</strong><i><b style={{ width: `${Math.min(value ?? 0, 100)}%` }} /></i></div>
}
