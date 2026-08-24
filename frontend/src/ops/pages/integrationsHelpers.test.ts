/**
 * Testes das integrações HA/Proxmox na Operations UI (prompt11_1 §63-§64).
 *
 * Lógica pura (sem DOM): masking de credencial, badges de estado,
 * filtros de domínio, resumo de teste e rótulos de verificação.
 * Ghost buttons e encoding são cobertos por opsAudit.test.ts.
 */

import { describe, expect, it } from 'vitest'
import {
  authLabel,
  domainFilters,
  entityMatches,
  formatBytes,
  formatUptime,
  guestRiskLabel,
  summarizeProxmoxTest,
  verificationLabel,
} from './integrationsHelpers'

describe('credential masking (§19/§31/§63)', () => {
  it('authLabel nunca vaza valor — só metadado', () => {
    expect(authLabel(true)).toBe('CONFIGURADA')
    expect(authLabel(false)).toBe('AUSENTE')
    expect(authLabel(undefined)).toBe('AUSENTE')
  })
})

describe('status badges coerentes entre Homelab e Integrations (§44)', () => {
  it('risco exibido para power ops', () => {
    expect(guestRiskLabel('start')).toBe('LOW_RISK')
    expect(guestRiskLabel('shutdown')).toBe('ELEVATED')
    expect(guestRiskLabel('reboot')).toBe('ELEVATED')
    // Stop é ELEVATED na UI; a política do backend é MAIS restritiva
    // (DESTRUCTIVE) — a UI nunca faz downgrade.
    expect(guestRiskLabel('stop')).toBe('ELEVATED')
  })

  it('verificationLabel reflete ACT→VERIFY, não HTTP 200', () => {
    expect(verificationLabel({ effect_verified: true })).toBe('VERIFICADO')
    expect(verificationLabel({ verification_status: 'VERIFIED' })).toBe('VERIFICADO')
    expect(verificationLabel({ effect_verified: false })).toBe('FALHOU')
    expect(verificationLabel({ verification_status: 'EXECUTED' }))
      .toBe('EXECUTADO (efeito não confirmado)')
    expect(verificationLabel({})).toBe('EXECUTADO (efeito não confirmado)')
  })
})

describe('entity browser (§23-§24)', () => {
  it('busca por entity_id, friendly name e domínio', () => {
    const row = { entity_id: 'light.sala', friendly_name: 'Lâmpada da Sala', domain: 'light' }
    expect(entityMatches(row, 'sala')).toBe(true)
    expect(entityMatches(row, 'LIGHT')).toBe(true)
    expect(entityMatches(row, 'lâmpada')).toBe(true)
    expect(entityMatches(row, 'clima')).toBe(false)
    expect(entityMatches(row, '')).toBe(true)
  })

  it('filtros mostram somente domains presentes, ordem canônica primeiro', () => {
    const present = ['person', 'sensor', 'weather', 'binary_sensor']
    expect(domainFilters(present)).toEqual(['sensor', 'binary_sensor', 'person', 'weather'])
    expect(domainFilters([])).toEqual([])
  })
})

describe('proxmox inventory helpers (§35)', () => {
  it('summarizeProxmoxTest com sucesso mostra dados reais', () => {
    const text = summarizeProxmoxTest({
      ok: true, version: '8.2.4', node_count: 1, qemu_count: 3,
      lxc_count: 2, storage_count: 4, latency_ms: 12.4,
    })
    expect(text).toContain('v8.2.4')
    expect(text).toContain('1 nodes')
    expect(text).toContain('3 VMs')
    expect(text).toContain('2 LXC')
    expect(text).toContain('12.4ms')
  })

  it('summarizeProxmoxTest com falha mostra código sem inventar dados', () => {
    const text = summarizeProxmoxTest({ ok: false, state: 'AUTH_FAILED', error_code: 'PROXMOX_AUTH_FAILED' })
    expect(text).toContain('PROXMOX_AUTH_FAILED')
    expect(text).not.toContain('nodes')
  })

  it('formata uptime e bytes', () => {
    expect(formatUptime(90061)).toBe('1d 1h')
    expect(formatUptime(3600)).toBe('1h 0min')
    expect(formatUptime(null)).toBe('—')
    expect(formatBytes(1536)).toBe('1.5 KiB')
    expect(formatBytes(undefined)).toBe('—')
  })
})
