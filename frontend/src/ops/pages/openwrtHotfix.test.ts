/**
 * openwrt_config_hotfix — regressões de UI do fluxo Configurar OpenWrt.
 *
 * Valida estruturalmente (mesma estratégia do integrationsHotfix) que:
 *   * Configurar no card OpenWrt renderiza o OpenWrtConfigCard;
 *   * Salvar / Testar conexão / Cancelar existem com handlers reais;
 *   * senha é write-only via Credential Broker (nunca exibida/recebida);
 *   * após salvar mostra apenas "Authentication configured: YES";
 *   * estados coerentes: UNCONFIGURED/AUTH_MISSING, AUTH_FAILED,
 *     OFFLINE, READY.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const here = __dirname
const page = readFileSync(join(here, 'IntegrationsPage.tsx'), 'utf-8')
const card = readFileSync(join(here, 'OpenWrtConfigCard.tsx'), 'utf-8')

describe('openwrt hotfix — fluxo Configurar OpenWrt', () => {
  it('página importa e renderiza o card quando configureId === openwrt', () => {
    expect(page).toContain("import { OpenWrtConfigCard } from './OpenWrtConfigCard'")
    expect(page).toMatch(/configureId === 'openwrt' && \(\s*<OpenWrtConfigCard/)
    expect(page).toMatch(/configureId === 'home_assistant' \|\| configureId === 'proxmox' \|\| configureId === 'openwrt'/)
  })

  it('card tem Host/URL, Usuário SSH e Senha SSH', () => {
    for (const label of ['Host/URL', 'Usuário SSH', 'Senha SSH']) {
      expect(card, `campo ausente: ${label}`).toContain(label)
    }
  })

  it('ações reais: Salvar, Testar conexão, Cancelar', () => {
    for (const label of ['Salvar', 'Testar conexão', 'Cancelar']) {
      expect(card, `ação ausente no card: ${label}`).toContain(label)
    }
  })

  it('usa os endpoints reais de config/teste do backend', () => {
    expect(card).toContain("usePolling<OpenWrtConfigStatus>('/api/openwrt/config'")
    expect(card).toMatch(/apiSend\('\/api\/openwrt\/config', 'PUT'/)
    expect(card).toMatch(/apiSend<Record<string, unknown>>\('\/api\/openwrt\/test', 'POST'/)
  })

  it('senha é write-only: input password, nunca exibida novamente após salvar', () => {
    expect(card).toMatch(/type="password"/)
    expect(card).toMatch(/autoComplete="new-password"/)
    expect(card).toContain('nunca é exibida novamente após salvar')
    expect(card).toContain('(configured)')
  })

  it('senha só é enviada quando preenchida (nunca apaga credencial do broker)', () => {
    const start = card.indexOf('await apiSend')
    const end = card.indexOf('setForm(null)', start)
    const block = card.slice(start, end)
    expect(block).toContain('editing.password.trim() ? { password: editing.password.trim() } : {}')
  })

  it('após salvar mostra apenas Authentication configured: YES', () => {
    expect(card).toContain("`Authentication configured: ${status.auth_configured ? 'YES' : 'NO'}`")
  })

  it('estados coerentes presentes na UI', () => {
    expect(card).toContain("status.state === 'UNCONFIGURED'")
    expect(card).toContain("status?.state === 'AUTH_FAILED'")
    expect(card).toContain("status?.state === 'OFFLINE'")
    expect(card).toContain('REMOTE_AUTH_FAILED')
  })

  it('UNCONFIGURED mostra aviso honesto em vez de dados falsos', () => {
    expect(card).toContain('OpenWrt ainda não configurado')
  })
})
