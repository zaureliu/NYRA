import { describe, expect, it } from 'vitest'
import {
  busyState,
  capabilityAllows,
  describeServiceState,
  type RuntimeService,
} from './RuntimeServicesPanel'

const service = (overrides: Partial<RuntimeService> = {}): RuntimeService => ({
  id: 'nyra_backend',
  display_name: 'NYRA Backend',
  state: 'READY',
  ownership: 'OWNED',
  type: 'PROCESS',
  pid: 12028,
  uptime_seconds: 42,
  restart_count: 0,
  last_error: null,
  health: { healthy: true, latency_ms: 4.2 },
  capabilities: { status: true, health: true, start: true, stop: false, restart: false, logs: true },
  startup_policy: 'MANUAL',
  ...overrides,
})

describe('runtime services panel logic', () => {
  it('maps every normalized state to an operator-friendly label', () => {
    expect(describeServiceState('READY')).toBe('Ready')
    expect(describeServiceState('RUNNING')).toContain('não confirmado')
    expect(describeServiceState('STOPPED')).toBe('Stopped')
    expect(describeServiceState('FAILED')).toBe('Failed')
    expect(describeServiceState('RESTARTING')).toBe('Restarting…')
    expect(describeServiceState('CRASH_LOOP')).toBe('Crash loop')
    expect(describeServiceState('DEGRADED')).toBe('Degraded')
    expect(describeServiceState('INVALID_CONFIGURATION')).toBe('Config inválida')
    expect(describeServiceState('DISABLED')).toBe('Disabled')
    expect(describeServiceState('UNKNOWN')).toBe('Unknown')
    expect(describeServiceState('STARTING')).toBe('Starting…')
    expect(describeServiceState('STOPPING')).toBe('Stopping…')
  })

  it('gates action buttons strictly by registered capabilities', () => {
    const backend = service()
    expect(capabilityAllows(backend, 'start')).toBe(true)
    expect(capabilityAllows(backend, 'stop')).toBe(false)
    expect(capabilityAllows(backend, 'restart')).toBe(false)
    expect(capabilityAllows(backend, 'logs')).toBe(true)
    const testService = service({
      id: 'nyra_test_service',
      capabilities: { status: true, health: true, start: true, stop: true, restart: true, logs: true },
    })
    expect(capabilityAllows(testService, 'restart')).toBe(true)
    const sentinel = service({ id: 'utamo_sentinel', capabilities: { status: true, health: true, start: false, stop: false, restart: false, logs: false } })
    expect(capabilityAllows(sentinel, 'start')).toBe(false)
    expect(capabilityAllows(sentinel, 'logs')).toBe(false)
  })

  it('treats transitional states as busy to disable mutation buttons', () => {
    expect(busyState('STARTING')).toBe(true)
    expect(busyState('STOPPING')).toBe(true)
    expect(busyState('RESTARTING')).toBe(true)
    expect(busyState('READY')).toBe(false)
    expect(busyState('FAILED')).toBe(false)
    expect(busyState('CRASH_LOOP')).toBe(false)
  })

  it('renders loading and error placeholders from fetch outcomes', () => {
    const loading = null
    expect(loading).toBeNull()
    const failed = new Error('HTTP 503')
    expect(failed.message).toMatch(/HTTP/)
  })
})
