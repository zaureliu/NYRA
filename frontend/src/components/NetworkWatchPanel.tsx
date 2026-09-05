import { useEffect, useMemo, useRef, useState } from 'react'
import { nyraFetch } from '../runtime/backend'

export interface NetworkTarget {
  kind: string
  address: string | null
  state: string
  reachable: boolean | null
  latency_ms: number | null
  last_probe_at: string | null
  last_success_at: string | null
  last_failure_at: string | null
  last_transition_at: string | null
  recent_success_ratio: number | null
}

export interface NetworkInterface {
  name: string | null
  type: string | null
  ipv4: string | null
  ipv6: string | null
  link_up: boolean | null
  link_speed_mbps: number | null
  mtu: number | null
  bytes_rx: number | null
  bytes_tx: number | null
  packets_rx: number | null
  packets_tx: number | null
  errors_rx: number | null
  errors_tx: number | null
  drops_rx: number | null
  drops_tx: number | null
  rx_bytes_per_sec: number | null
  tx_bytes_per_sec: number | null
  rx_packets_per_sec: number | null
  tx_packets_per_sec: number | null
}

export interface NetworkSample {
  timestamp: string
  health: string
  internet_latency_ms: number | null
  jitter_ms: number | null
  packet_loss_percent: number | null
  rx_bytes_per_sec: number | null
  tx_bytes_per_sec: number | null
  rx_packets_per_sec: number | null
  tx_packets_per_sec: number | null
  interface: NetworkInterface | null
  local_interface: NetworkTarget | null
  gateway_state: NetworkTarget | null
  dns_state: NetworkTarget | null
  internet_state: NetworkTarget | null
}

export interface NetworkStatus {
  enabled: boolean
  running: boolean
  status: string
  health: string
  uptime_seconds: number
  snapshot: NetworkSample
  active_alerts: string[]
}

export interface NetworkEventItem {
  timestamp: string
  severity: string
  type: string
  message: string
  simulated?: boolean
}

export const formatRate = (bytesPerSecond: number | null | undefined): string =>
  bytesPerSecond == null || !Number.isFinite(bytesPerSecond)
    ? 'UNAVAILABLE'
    : `${(bytesPerSecond * 8 / 1_000_000).toLocaleString('pt-BR', { maximumFractionDigits: 2 })} Mbps`

export const formatBytes = (value: number | null | undefined): string => {
  if (value == null || !Number.isFinite(value)) return 'UNAVAILABLE'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let amount = Math.max(0, value)
  let unit = 0
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1 }
  return `${amount.toLocaleString('pt-BR', { maximumFractionDigits: unit === 0 ? 0 : 2 })} ${units[unit]}`
}

export const formatNumber = (value: number | null | undefined, digits = 0): string =>
  value == null || !Number.isFinite(value)
    ? 'UNAVAILABLE'
    : value.toLocaleString('pt-BR', { maximumFractionDigits: digits })

export function mergeSamples(previous: NetworkSample[], incoming: NetworkSample[], limit = 900): NetworkSample[] {
  const merged = new Map(previous.map((sample) => [sample.timestamp, sample]))
  incoming.forEach((sample) => merged.set(sample.timestamp, sample))
  return Array.from(merged.values())
    .sort((left, right) => Date.parse(left.timestamp) - Date.parse(right.timestamp))
    .slice(-limit)
}

function MetricCard({ label, value, detail, tone = 'neutral' }: { label: string; value: string; detail?: string; tone?: string }) {
  return <article className={`network-metric-card tone-${tone}`}><span>{label}</span><strong>{value}</strong>{detail && <small>{detail}</small>}</article>
}

interface SeriesDefinition { key: keyof NetworkSample; label: string; color: string; suffix: string }

function TimeSeriesChart({ title, subtitle, samples, series }: { title: string; subtitle: string; samples: NetworkSample[]; series: SeriesDefinition[] }) {
  const values = series.flatMap((item) => samples.map((sample) => typeof sample[item.key] === 'number' ? sample[item.key] as number : null).filter((value): value is number => value != null))
  const max = Math.max(1, ...values)
  const pathFor = (key: keyof NetworkSample) => samples.map((sample, index) => {
    const value = sample[key]
    if (typeof value !== 'number') return null
    const x = samples.length < 2 ? 0 : index / (samples.length - 1) * 100
    return `${x.toFixed(2)},${(36 - value / max * 32).toFixed(2)}`
  }).filter(Boolean).join(' ')
  const time = (index: number) => samples[index] ? new Date(samples[index].timestamp).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—'
  return <article className="network-chart-card">
    <header><div><h3>{title}</h3><p>{subtitle}</p></div><div className="network-chart-legend">{series.map((item) => <span key={String(item.key)}><i style={{ background: item.color }} />{item.label}</span>)}</div></header>
    {values.length < 2 ? <div className="network-empty">COLLECTING DATA</div> : <>
      <svg className="network-timeseries" viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label={title}>
        <path className="network-grid-line" d="M0 4H100 M0 20H100 M0 36H100" />
        {series.map((item) => <polyline key={String(item.key)} points={pathFor(item.key)} style={{ stroke: item.color }} />)}
        {samples.map((sample, index) => index % Math.max(1, Math.floor(samples.length / 30)) === 0 ? series.map((item) => {
          const value = sample[item.key]
          if (typeof value !== 'number') return null
          const x = samples.length < 2 ? 0 : index / (samples.length - 1) * 100
          const y = 36 - value / max * 32
          return <circle key={`${item.key}-${sample.timestamp}`} cx={x} cy={y} r="0.65" style={{ fill: item.color }}><title>{`${item.label}: ${value.toLocaleString('pt-BR', { maximumFractionDigits: 2 })}${item.suffix} · ${time(index)}`}</title></circle>
        }) : null)}
      </svg>
      <div className="network-time-axis"><span>{time(0)}</span><span>{time(Math.floor(samples.length / 2))}</span><span>{time(samples.length - 1)}</span></div>
    </>}
  </article>
}

function QualityCharts({ samples }: { samples: NetworkSample[] }) {
  return <div className="network-quality-stack">
    <TimeSeriesChart title="QUALIDADE DA CONEXÃO" subtitle="Latência e jitter observados nos probes existentes" samples={samples} series={[
      { key: 'internet_latency_ms', label: 'Latência', color: '#62d6d0', suffix: ' ms' },
      { key: 'jitter_ms', label: 'Jitter', color: '#e2b86d', suffix: ' ms' },
    ]} />
    <TimeSeriesChart title="PERDA DE PACOTES" subtitle="Janela móvel dos probes reais de conectividade" samples={samples} series={[
      { key: 'packet_loss_percent', label: 'Packet loss', color: '#ef7284', suffix: '%' },
    ]} />
  </div>
}

function ThroughputChart({ samples }: { samples: NetworkSample[] }) {
  const converted = useMemo(() => samples.map((sample) => ({
    ...sample,
    rx_bytes_per_sec: sample.rx_bytes_per_sec == null ? null : sample.rx_bytes_per_sec * 8 / 1_000_000,
    tx_bytes_per_sec: sample.tx_bytes_per_sec == null ? null : sample.tx_bytes_per_sec * 8 / 1_000_000,
  })), [samples])
  return <TimeSeriesChart title="THROUGHPUT RX / TX" subtitle="Taxa real da interface ativa, calculada por diferença de contadores" samples={converted} series={[
    { key: 'rx_bytes_per_sec', label: 'RX / download', color: '#70dbc1', suffix: ' Mbps' },
    { key: 'tx_bytes_per_sec', label: 'TX / upload', color: '#8da7ff', suffix: ' Mbps' },
  ]} />
}

function InterfaceTrafficPanel({ value }: { value: NetworkInterface | null }) {
  const fields = [
    ['IPv4', value?.ipv4 ?? 'UNAVAILABLE'], ['IPv6', value?.ipv6 ?? 'UNAVAILABLE'],
    ['Link', value?.link_up == null ? 'UNAVAILABLE' : value.link_up ? 'UP' : 'DOWN'],
    ['Link speed', value?.link_speed_mbps == null ? 'UNAVAILABLE' : `${formatNumber(value.link_speed_mbps, 1)} Mbps`],
    ['MTU', formatNumber(value?.mtu)], ['Bytes RX', formatBytes(value?.bytes_rx)],
    ['Bytes TX', formatBytes(value?.bytes_tx)], ['Packets RX', formatNumber(value?.packets_rx)],
    ['Packets TX', formatNumber(value?.packets_tx)], ['Packets RX/s', formatNumber(value?.rx_packets_per_sec, 1)],
    ['Packets TX/s', formatNumber(value?.tx_packets_per_sec, 1)], ['Errors RX / TX', `${formatNumber(value?.errors_rx)} / ${formatNumber(value?.errors_tx)}`],
    ['Drops RX / TX', `${formatNumber(value?.drops_rx)} / ${formatNumber(value?.drops_tx)}`],
  ]
  return <article className="network-surface network-interface-panel"><header><div><span>INTERFACE / TRAFFIC</span><strong>{value?.name ?? 'UNAVAILABLE'}</strong></div><em>{value?.type ?? 'TIPO INDISPONÍVEL'}</em></header><dl>{fields.map(([label, content]) => <div key={label}><dt>{label}</dt><dd>{content}</dd></div>)}</dl></article>
}

function ConnectivityTargets({ snapshot }: { snapshot: NetworkSample }) {
  const entries: Array<[string, NetworkTarget | null]> = [['LOCAL INTERFACE', snapshot.local_interface], ['GATEWAY', snapshot.gateway_state], ['DNS', snapshot.dns_state], ['INTERNET', snapshot.internet_state]]
  return <section className="network-target-section"><div className="network-section-heading"><h2>DESTINOS / CONNECTIVITY</h2><span>últimos resultados verificados</span></div><div className="network-target-grid">{entries.map(([label, target]) => <article key={label} className={`network-target state-${target?.state ?? 'unavailable'}`}>
    <header><span>{label}</span><strong>{(target?.state ?? 'unavailable').toUpperCase()}</strong></header>
    <p>{target?.address ?? 'UNAVAILABLE'}</p>
    <dl><div><dt>LATENCY</dt><dd>{target?.latency_ms == null ? 'UNAVAILABLE' : `${formatNumber(target.latency_ms, 1)} ms`}</dd></div><div><dt>SUCCESS</dt><dd>{target?.recent_success_ratio == null ? 'UNAVAILABLE' : `${formatNumber(target.recent_success_ratio, 1)}%`}</dd></div><div><dt>LAST OK</dt><dd>{formatTimestamp(target?.last_success_at)}</dd></div><div><dt>TRANSITION</dt><dd>{formatTimestamp(target?.last_transition_at)}</dd></div></dl>
  </article>)}</div></section>
}

function formatTimestamp(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : 'UNAVAILABLE'
}

function NetworkEventTimeline({ events }: { events: NetworkEventItem[] }) {
  return <section className="network-surface network-events"><header><div><span>NETWORK EVENTS</span><strong>Eventos recentes</strong></div><small>{events.length} exibidos</small></header>{events.length === 0 ? <div className="network-empty">NENHUM EVENTO RECENTE</div> : <ol>{events.map((event, index) => <li key={`${event.timestamp}-${index}`} className={`severity-${event.severity}`}><time>{formatTimestamp(event.timestamp)}</time><strong>{event.severity === 'recovery' ? 'NOTICE' : event.severity.toUpperCase()}</strong><p>{event.message}</p>{event.simulated && <span className="simulation-badge">SIMULAÇÃO</span>}</li>)}</ol>}</section>
}

export function NetworkObservabilityView({ data, samples, events, loading = false, error = null }: { data: NetworkStatus | null; samples: NetworkSample[]; events: NetworkEventItem[]; loading?: boolean; error?: string | null }) {
  const snapshot = data?.snapshot
  const iface = snapshot?.interface ?? null
  const health = data?.enabled ? (data.health || snapshot?.health || 'collecting') : 'disabled'
  if (!data && loading) return <section className="network-dashboard network-surface"><div className="network-empty">COLLECTING DATA</div></section>
  return <section className="network-dashboard" aria-busy={loading}>
    {error && <div className="network-api-error" role="alert">Dados temporariamente indisponíveis: {error}</div>}
    <div className="network-summary-grid">
      <MetricCard label="NETWORK HEALTH" value={health.toUpperCase()} detail={data?.running ? 'monitor active' : 'monitor stopped'} tone={health} />
      <MetricCard label="LATENCY" value={snapshot?.internet_latency_ms == null ? 'UNAVAILABLE' : `${formatNumber(snapshot.internet_latency_ms, 1)} ms`} />
      <MetricCard label="JITTER" value={snapshot?.jitter_ms == null ? 'UNAVAILABLE' : `${formatNumber(snapshot.jitter_ms, 1)} ms`} />
      <MetricCard label="PACKET LOSS" value={snapshot?.packet_loss_percent == null ? 'UNAVAILABLE' : `${formatNumber(snapshot.packet_loss_percent, 1)}%`} />
      <MetricCard label="DOWNLOAD / RX" value={formatRate(iface?.rx_bytes_per_sec)} detail="interface throughput" />
      <MetricCard label="UPLOAD / TX" value={formatRate(iface?.tx_bytes_per_sec)} detail="interface throughput" />
      <MetricCard label="ACTIVE INTERFACE" value={iface?.name ?? 'UNAVAILABLE'} detail={iface?.type ?? undefined} tone={iface?.link_up ? 'healthy' : 'neutral'} />
    </div>
    {snapshot ? <>
      <QualityCharts samples={samples} />
      <ThroughputChart samples={samples} />
      <InterfaceTrafficPanel value={iface} />
      <ConnectivityTargets snapshot={snapshot} />
      <NetworkEventTimeline events={events} />
    </> : <div className="network-empty">COLLECTING DATA</div>}
  </section>
}

export function NetworkWatchPanel() {
  const [data, setData] = useState<NetworkStatus | null>(null)
  const [samples, setSamples] = useState<NetworkSample[]>([])
  const [events, setEvents] = useState<NetworkEventItem[]>([])
  const [minutes, setMinutes] = useState(5)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const latestTimestamp = useRef<string | null>(null)

  useEffect(() => {
    let active = true
    latestTimestamp.current = null
    setSamples([])
    const load = async () => {
      try {
        const since = latestTimestamp.current ? `&since=${encodeURIComponent(latestTimestamp.current)}` : ''
        const [statusResponse, metricsResponse] = await Promise.all([
          nyraFetch('/api/network-watch/status'),
          nyraFetch(`/api/network-watch/metrics?minutes=${minutes}${since}`),
        ])
        if (!statusResponse.ok || !metricsResponse.ok) throw new Error(`HTTP ${statusResponse.status}/${metricsResponse.status}`)
        const nextStatus = await statusResponse.json() as NetworkStatus
        const nextSamples = (await metricsResponse.json() as { samples: NetworkSample[]; history_limit: number })
        if (!active) return
        setData(nextStatus)
        setSamples((current) => mergeSamples(current, nextSamples.samples, nextSamples.history_limit || 900))
        const latest = nextSamples.samples.at(-1)?.timestamp
        if (latest) latestTimestamp.current = latest
        setError(null)
      } catch (issue) {
        if (active) setError(issue instanceof Error ? issue.message : 'transport error')
      } finally {
        if (active) setLoading(false)
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 3000)
    return () => { active = false; window.clearInterval(timer) }
  }, [minutes])

  useEffect(() => {
    let active = true
    const loadEvents = async () => {
      try {
        const response = await nyraFetch('/api/network-watch/events?hours=24&limit=30')
        if (response.ok && active) setEvents((await response.json() as { events: NetworkEventItem[] }).events)
      } catch { /* status polling owns the visible transport error */ }
    }
    void loadEvents()
    const timer = window.setInterval(() => void loadEvents(), 10000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  return <>
    <div className="network-toolbar"><span>HISTÓRICO</span><div role="group" aria-label="Janela de tempo">{[1, 5, 15].map((value) => <button key={value} className={minutes === value ? 'active' : ''} onClick={() => setMinutes(value)}>{value} min</button>)}</div></div>
    <NetworkObservabilityView data={data} samples={samples} events={events} loading={loading} error={error} />
  </>
}
