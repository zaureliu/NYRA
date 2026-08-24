/** Helpers puros das integrações HA/Proxmox (prompt11_1 §40-§43, §63). */

export const HA_DOMAIN_FILTERS = [
  'light', 'switch', 'sensor', 'binary_sensor', 'climate',
  'media_player', 'person', 'device_tracker', 'automation',
] as const

export type GuestAction = 'start' | 'shutdown' | 'reboot' | 'stop'

/** Risco exibido na UI; a política real do backend pode ser MAIS restritiva
 * (Stop é DESTRUCTIVE no Homelab Control Plane — nunca downgrade aqui). */
export function guestRiskLabel(action: GuestAction): string {
  return action === 'start' ? 'LOW_RISK' : 'ELEVATED'
}

export function authLabel(authConfigured: boolean | undefined): string {
  return authConfigured ? 'CONFIGURADA' : 'AUSENTE'
}

/** §24: somente domains realmente presentes, ordem canônica primeiro. */
export function domainFilters(present: string[]): string[] {
  const set = new Set(present)
  const ordered = HA_DOMAIN_FILTERS.filter((domain) => set.has(domain))
  const extras = present
    .filter((domain) => !(HA_DOMAIN_FILTERS as readonly string[]).includes(domain))
    .sort()
  return [...ordered, ...extras]
}

/** Busca client-side complementar por entity_id/friendly name/domain (§23). */
export function entityMatches(row: {
  entity_id: string
  friendly_name: string
  domain: string
}, query: string): boolean {
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  return (
    row.entity_id.toLowerCase().includes(needle) ||
    row.friendly_name.toLowerCase().includes(needle) ||
    row.domain.toLowerCase().includes(needle)
  )
}

export function formatUptime(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  let rest = Math.max(0, Math.round(seconds))
  const days = Math.floor(rest / 86400)
  rest -= days * 86400
  const hours = Math.floor(rest / 3600)
  rest -= hours * 3600
  const minutes = Math.floor(rest / 60)
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}min`
  return `${minutes}min`
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${Math.round(value * 10) / 10} ${units[unit]}`
}

export function summarizeProxmoxTest(result: Record<string, unknown>): string {
  if (result.ok) {
    const parts = [
      `v${String(result.version ?? '?')}`,
      `${String(result.node_count ?? 0)} nodes`,
      `${String(result.qemu_count ?? 0)} VMs`,
      `${String(result.lxc_count ?? 0)} LXC`,
      `${String(result.storage_count ?? 0)} storage`,
    ]
    if (result.latency_ms != null) parts.push(`${result.latency_ms}ms`)
    return parts.join(' · ')
  }
  const code = result.error_code ?? result.state ?? 'falha'
  return result.message ? `${code}: ${result.message}` : String(code)
}

/** Rótulo do resultado ACT→VERIFY (§26): 200 sozinho não prova efeito. */
export function verificationLabel(response: { effect_verified?: boolean | null; verification_status?: string }): string {
  if (response.verification_status === 'VERIFIED' || response.effect_verified === true) return 'VERIFICADO'
  if (response.verification_status === 'VERIFICATION_FAILED' || response.effect_verified === false) return 'FALHOU'
  return 'EXECUTADO (efeito não confirmado)'
}
