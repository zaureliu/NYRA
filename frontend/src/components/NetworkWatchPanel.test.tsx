import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import {
  formatBytes, formatRate, mergeSamples, NetworkObservabilityView,
  type NetworkSample, type NetworkStatus,
} from './NetworkWatchPanel'

function sample(timestamp = '2026-09-03T12:00:00Z'): NetworkSample {
  const target = { kind:'internet', address:'multi-probe', state:'healthy', reachable:true, latency_ms:12, last_probe_at:timestamp, last_success_at:timestamp, last_failure_at:null, last_transition_at:null, recent_success_ratio:100 }
  return {
    timestamp, health:'healthy', internet_latency_ms:12, jitter_ms:1.5,
    packet_loss_percent:0, rx_bytes_per_sec:1_250_000, tx_bytes_per_sec:125_000,
    rx_packets_per_sec:40, tx_packets_per_sec:20,
    interface:{ name:'Ethernet', type:'ethernet', ipv4:'192.168.1.2', ipv6:null,
      link_up:true, link_speed_mbps:1000, mtu:1500, bytes_rx:5_000_000,
      bytes_tx:2_000_000, packets_rx:1200, packets_tx:800, errors_rx:0,
      errors_tx:0, drops_rx:0, drops_tx:0, rx_bytes_per_sec:1_250_000,
      tx_bytes_per_sec:125_000, rx_packets_per_sec:40, tx_packets_per_sec:20 },
    local_interface:{...target,kind:'local_interface',address:'Ethernet'},
    gateway_state:{...target,kind:'gateway',address:'192.168.1.1'},
    dns_state:{...target,kind:'dns',address:'cloudflare.com'}, internet_state:target,
  }
}

function status(value = sample()): NetworkStatus {
  return { enabled:true, running:true, status:'online', health:'healthy', uptime_seconds:30, snapshot:value, active_alerts:[] }
}

describe('Network Observability V2', () => {
  it('formats real throughput and accumulated traffic with explicit units', () => {
    expect(formatRate(1_250_000)).toContain('10')
    expect(formatRate(null)).toBe('UNAVAILABLE')
    expect(formatBytes(1_048_576)).toContain('MB')
  })

  it('merges incremental history without duplicate samples and caps it', () => {
    const a=sample('2026-09-03T12:00:00Z'), b=sample('2026-09-03T12:00:01Z'), c=sample('2026-09-03T12:00:02Z')
    expect(mergeSamples([a,b],[b,c],2).map(item=>item.timestamp)).toEqual([b.timestamp,c.timestamp])
  })

  it('renders health, traffic, packets, targets, charts and human events', () => {
    const current=sample()
    const next=sample('2026-09-03T12:00:01Z')
    const html=renderToStaticMarkup(<NetworkObservabilityView data={status(current)} samples={[current,next]} events={[{timestamp:current.timestamp,severity:'warning',type:'high_latency',message:'Latência elevada detectada: 184 ms.'}]}/>)
    expect(html).toContain('NETWORK HEALTH')
    expect(html).toContain('THROUGHPUT RX / TX')
    expect(html).toContain('Packets RX/s')
    expect(html).toContain('DESTINOS / CONNECTIVITY')
    expect(html).toContain('Latência elevada detectada')
    expect(html).not.toContain('high_latency')
  })

  it('renders collecting and missing-data states without invented zeroes', () => {
    const html=renderToStaticMarkup(<NetworkObservabilityView data={null} samples={[]} events={[]} loading/>)
    expect(html).toContain('COLLECTING DATA')
    expect(formatRate(undefined)).toBe('UNAVAILABLE')
  })

  it('renders API errors without dropping the last valid snapshot', () => {
    const current=sample()
    const html=renderToStaticMarkup(<NetworkObservabilityView data={status(current)} samples={[current]} events={[]} error="BACKEND_OFFLINE"/>)
    expect(html).toContain('BACKEND_OFFLINE')
    expect(html).toContain('Ethernet')
  })
})
