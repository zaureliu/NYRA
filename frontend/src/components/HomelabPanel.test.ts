import { describe, expect, it } from 'vitest'
import { formatUptime, hostDetailLine, hostStateClass, summarizeOverview, type HomelabHostState } from './HomelabPanel'

const host = (overrides: Partial<HomelabHostState> = {}): HomelabHostState => ({
  host_id: 'proxmox',
  address: '192.168.1.2',
  overall_state: 'ONLINE',
  reachable: true,
  integration_state: 'ONLINE',
  integration_error_code: null,
  integration_detail: {},
  cached: false,
  ...overrides,
})

describe('hostStateClass', () => {
  it('maps normalized states to visual classes', () => {
    expect(hostStateClass('ONLINE')).toBe('ok')
    expect(hostStateClass('OFFLINE')).toBe('fail')
    expect(hostStateClass('UNREACHABLE')).toBe('warn')
    expect(hostStateClass('AUTHENTICATION_FAILED')).toBe('warn')
    expect(hostStateClass('DISABLED')).toBe('idle')
  })

  it('falls back to idle for unknown states', () => {
    expect(hostStateClass('SOMETHING_ELSE')).toBe('idle')
    expect(hostStateClass('')).toBe('idle')
  })
})

describe('formatUptime', () => {
  it('formats days/hours/minutes', () => {
    expect(formatUptime(90061)).toBe('1d 1h')
    expect(formatUptime(3660)).toBe('1h 1m')
    expect(formatUptime(120)).toBe('2m')
  })

  it('returns dash for missing or invalid values', () => {
    expect(formatUptime(undefined)).toBe('—')
    expect(formatUptime(0)).toBe('—')
    expect(formatUptime(-5)).toBe('—')
    expect(formatUptime('abc')).toBe('—')
  })
})

describe('summarizeOverview', () => {
  it('orders known hosts first and keeps extras', () => {
    const overview = {
      generated_at: 1,
      hosts: [
        host({ host_id: 'dc1' }),
        host({ host_id: 'home_assistant' }),
        host({ host_id: 'openwrt' }),
        host({ host_id: 'proxmox' }),
        host({ host_id: 'extra_host' }),
      ],
      summary: {},
    }
    const result = summarizeOverview(overview)
    expect(result.map(item => item.host_id)).toEqual([
      'openwrt', 'proxmox', 'home_assistant', 'dc1', 'extra_host',
    ])
  })

  it('returns empty for null or empty overview', () => {
    expect(summarizeOverview(null)).toEqual([])
    expect(summarizeOverview({ generated_at: 0, hosts: [], summary: {} })).toEqual([])
  })
})

describe('hostDetailLine', () => {
  it('explains missing credentials honestly', () => {
    expect(hostDetailLine(host({ integration_error_code: 'PROXMOX_AUTH_MISSING' }))).toContain('token')
    expect(hostDetailLine(host({ integration_error_code: 'HA_AUTH_MISSING' }))).toContain('token')
  })

  it('humanizes other error codes and shows versions', () => {
    expect(hostDetailLine(host({ integration_error_code: 'HA_API_UNAVAILABLE' }))).toBe('ha api unavailable')
    expect(hostDetailLine(host({ integration_detail: { version: '2026.8.3' } }))).toBe('v2026.8.3')
  })

  it('falls back to state text', () => {
    expect(hostDetailLine(host())).toBe('')
  })
})
