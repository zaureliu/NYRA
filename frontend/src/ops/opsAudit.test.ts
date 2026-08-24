/**
 * Auditoria estrutural da Operations UI V3 (prompt11 Parte AI §179-§182).
 *
 * Estes testes leem os fontes reais e falham se:
 *  - existir <button> sem onClick (ghost button, §47/§180);
 *  - reaparecer mojibake de encoding (§16-§18/§181);
 *  - a navegação divergir entre OPS_VIEWS, grupos do sidebar e o switch do App.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { NAV_GROUPS, OPS_VIEWS } from './Sidebar'

const SRC_ROOT = join(__dirname, '..')

function* walkTsFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    if (['node_modules', 'dist', '__pycache__'].includes(entry)) continue
    const full = join(dir, entry)
    const stat = statSync(full)
    if (stat.isDirectory()) yield* walkTsFiles(full)
    else if (/\.(tsx?|css)$/.test(entry)) yield full
  }
}

const allSources = (): string[] => [...walkTsFiles(SRC_ROOT)]

describe('ghost buttons (§46/§47)', () => {
  it('todo <button> em .tsx possui handler ou é submit de formulário', () => {
    const offenders: string[] = []
    const handlerPattern = /(?:onClick|onPointerDown|onPointerUp)\s*=|type=["']submit["']/
    for (const file of allSources()) {
      if (!file.endsWith('.tsx')) continue
      const text = readFileSync(file, 'utf-8')
      const regex = /<button\b/g
      let match: RegExpExecArray | null
      while ((match = regex.exec(text)) !== null) {
        const start = match.index
        // fim da tag = primeiro '>' que não pertence a uma arrow function '=>'
        let closeIndex = start
        do {
          closeIndex = text.indexOf('>', closeIndex + 1)
          if (closeIndex === -1) break
        } while (text[closeIndex - 1] === '=')
        if (closeIndex === -1) continue
        const tag = text.slice(start, closeIndex + 1)
        if (!handlerPattern.test(tag)) {
          const line = text.slice(0, start).split('\n').length
          offenders.push(`${relative(file)}:${line}`)
        }
      }
    }
    expect(offenders, `botões fantasma: ${offenders.join(', ')}`).toEqual([])
  })
})

describe('encoding audit (§16-§18)', () => {
  it('zero mojibake em tsx/ts/css', () => {
    const offenders: string[] = []
    const selfPath = join(SRC_ROOT, 'ops', 'opsAudit.test.ts')
    const patterns: Array<[RegExp, string]> = [
      [/\uFFFD/, 'replacement char'],
      [/Ã[/\u0080-\u00FF]/u, 'Ã pair'],
      [/â€[™œž]/u, 'smart-quote mojibake'],
      [/\u00C2\u00A0/, 'nbsp mojibake'],
      // ícones do shell antigo gravados com double-encoding (â˜° / âš™)
      [/\u00E2\u02DC\u00B0/, 'ícone hamburger quebrado'],
      [/\u00E2\u0161\u2122/, 'ícone engrenagem quebrado'],
    ]
    for (const file of allSources()) {
      if (file === selfPath) continue // este arquivo contém os padrões por definição
      const text = readFileSync(file, 'utf-8')
      for (const [pattern, label] of patterns) {
        if (pattern.test(text)) offenders.push(`${relative(file)} (${label})`)
      }
    }
    expect(offenders, `mojibake detectado: ${offenders.join(', ')}`).toEqual([])
  })
})

describe('navegação consistente', () => {
  it('OPS_VIEWS cobre exatamente os itens do sidebar', () => {
    const navViews = NAV_GROUPS.flatMap((group) => group.items.map((item) => item.view))
    expect(new Set(navViews).size).toBe(navViews.length)
    expect([...navViews].sort()).toEqual([...OPS_VIEWS].sort())
  })

  it('App.tsx roteia todas as views', () => {
    const appSource = readFileSync(join(SRC_ROOT, 'App.tsx'), 'utf-8')
    const missing = OPS_VIEWS.filter((view) => !appSource.includes(`case '${view}':`))
    expect(missing, `views sem case no switch: ${missing.join(', ')}`).toEqual([])
  })

  it('nenhuma view órfã do shell antigo permanece no hash inicial', () => {
    // benchmark virou seção do Developer; dashboard virou overview.
    const appSource = readFileSync(join(SRC_ROOT, 'App.tsx'), 'utf-8')
    expect(appSource).not.toMatch(/'dashboard'/)
    expect(appSource).not.toMatch(/APP_VIEWS/)
  })
})

function relative(file: string): string {
  return file.slice(SRC_ROOT.length + 1)
}
