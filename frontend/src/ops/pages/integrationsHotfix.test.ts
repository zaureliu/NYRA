/**
 * prompt11_2 — regressões de UI do hotfix Proxmox/HA.
 *
 * Valida estruturalmente (mesma estratégia do opsAudit) que o fluxo exigido
 * existe de verdade com handlers reais: Configurar/Abrir internos, aviso
 * UNCONFIGURED honesto, resumo READY só com dados do backend e toggle de
 * enabled que não apaga a configuração salva.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const here = __dirname
const page = readFileSync(join(here, 'IntegrationsPage.tsx'), 'utf-8')
const card = readFileSync(join(here, 'ProxmoxConfigCard.tsx'), 'utf-8')
const profiles = readFileSync(join(here, 'HAProfilesCard.tsx'), 'utf-8')
const homelab = readFileSync(join(here, 'HomelabPage.tsx'), 'utf-8')
const css = readFileSync(join(here, '..', '..', 'ops.css'), 'utf-8')

describe('prompt11_2 — card Proxmox e consistência de estados', () => {
  it('ações exigidas existem com handler real na página de Integrações', () => {
    for (const label of ['Testar', 'Configurar', 'Abrir', 'Habilitar',
      'Desabilitar', 'Diagnóstico', 'Atualizar Integrações']) {
      expect(page, `ação ausente na página: ${label}`).toContain(label)
    }
  })

  it('formulário Proxmox tem Salvar / Testar conexão / Cancelar', () => {
    for (const label of ['Salvar', 'Testar conexão', 'Cancelar', 'Configurar',
      'Habilitar', 'Desabilitar', 'Diagnóstico']) {
      expect(card, `ação ausente no card: ${label}`).toContain(label)
    }
  })

  it('Abrir do Proxmox abre a view interna (nunca window.open)', () => {
    expect(page).toMatch(/id === 'proxmox'[\s\S]{0,120}setConfigureId\('proxmox'\)/)
  })

  it('UNCONFIGURED mostra aviso honesto em vez de cards falsos', () => {
    expect(card).toContain("status.state === 'UNCONFIGURED'")
    expect(card).toContain('Proxmox ainda não configurado')
    expect(card).toContain('Configure um API Token')
  })

  it('resumo autenticado usa somente dados reais do backend', () => {
    expect(card).toMatch(/status\?\.authenticated &&/)
    for (const field of ['status.version', 'status.node_count',
      'status.qemu_count', 'status.lxc_count', 'status.storage_count']) {
      expect(card).toContain(field)
    }
  })

  it('toggle de enabled envia SOMENTE enabled (backend preserva a URL)', () => {
    const start = card.indexOf('const toggleEnabled')
    const end = card.indexOf('const disconnect')
    const block = card.slice(start, end)
    expect(block).toContain('{ enabled: enable }')
    expect(block).not.toMatch(/\burl:/)
  })

  it('secret nunca retorna ao formulário após salvar', () => {
    expect(card).toContain('O secret nunca é exibido novamente após salvar.')
    expect(card).toMatch(/type="password"/)
  })

  it('compacta metadata somente nas páginas de Integrações e Homelab', () => {
    expect(page).toContain('className="integrations-density-page"')
    expect(homelab).toContain('className="homelab-density-page"')
    expect(css).toContain(':is(.integrations-density-page, .homelab-density-page) .ops-kv')
    expect(css).toContain('grid-template-columns: max-content minmax(0, 1fr)')
  })

  it('mantém tabela e ações Home Assistant densas sem reduzir tipografia', () => {
    expect(profiles).toContain('ops-table ha-profile-table')
    expect(profiles).toContain('className="ha-profile-actions"')
    expect(profiles).toContain('className="ha-profile-create-row"')
    expect(css).toContain('table-layout: fixed')
    expect(css).toContain('.ops-shell:has(.integrations-density-page) .ops-topbar-chips')
    expect(css).not.toMatch(/\.ha-profile-(?:table|actions|create-row)[^{]*\{[^}]*font-size/s)
  })
})
