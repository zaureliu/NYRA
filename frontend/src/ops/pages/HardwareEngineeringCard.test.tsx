import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { HardwareSummary, type HardwareStatus } from './HardwareEngineeringCard'

const status: HardwareStatus = { full: false, project_root: 'E:\\NYRA-Projects', goals: [], serial: { open_handles: 0 }, research: {} }

describe('hardware evidence presentation', () => {
  it('does not manufacture a project, build, flash or physical effect', () => {
    const html = renderToStaticMarkup(<HardwareSummary value={status} />)
    expect(html).toContain('Nenhum projeto')
    expect(html.match(/Não confirmado/g)).toHaveLength(2)
    expect(html).not.toContain('LED ativo')
  })
  it('keeps upload separate from effect verification and labels simulation', () => {
    const html = renderToStaticMarkup(<HardwareSummary value={{ ...status,
      project: { name: 'project', build: { success: true }, flash: { success: true } },
      goals: [{ goal_id: 'test', desired_effect: 'led_on', state: 'BLOCKED', response: 'Sem verificação.', simulated: true, steps: [] }] }} />)
    expect(html).toContain('Gravado — verificar efeito')
    expect(html).toContain('SIMULATED')
    expect(html).toContain('Sem verificação.')
  })
  it('shows URL, retrieval time and stale cache marker without inventing provenance', () => {
    const html = renderToStaticMarkup(<HardwareSummary value={{ ...status, research: { sources: [{
      url: 'https://docs.platformio.org/', title: 'PlatformIO', retrieved_at: '2026-09-05T00:00:00Z', stale: true,
    }] } }} />)
    expect(html).toContain('https://docs.platformio.org/')
    expect(html).toContain('cache antigo')
  })
})
