import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

describe('Operations UI Intelligence Platform', () => {
  const source = readFileSync(join(__dirname, 'OverviewPage.tsx'), 'utf-8')

  it('reads the real consolidated backend endpoint', () => {
    expect(source).toContain("usePolling<IntelligenceStatus>('/api/intelligence/status'")
    expect(source).toContain('clearOnError: true')
  })

  it('covers the integrated runtime domains without fabricated telemetry', () => {
    for (const capability of [
      'model_router_v2', 'memory_v2', 'rag_local', 'context_engine',
      'autonomous_tasks_v2', 'event_intelligence', 'trace_replay',
      'skill_catalog', 'browser_control', 'desktop_control', 'diagnostics_engine',
      'open_loops_engine',
      'persona_emotional_runtime',
    ]) {
      expect(source).toContain(`intelligenceState('${capability}')`)
    }
    expect(source).toContain("if (!intelligence.data) return 'UNKNOWN'")
  })

  it('shows the compact World State view from the consolidated status', () => {
    for (const label of [
      'World State Engine', 'Current Focus', 'Current App',
      'Active Tasks', 'Active Monitors', 'Recent Events', 'Freshness',
      'Current emotion', 'Intensity', 'Dialogue policy',
    ]) {
      expect(source).toContain(label)
    }
    expect(source).toContain('world?.snapshot.current_focus?.freshness')
  })

  it('shows compact expandable Open Loop states without creating a task UI', () => {
    for (const label of ['Open Loops & Goals', 'Open', 'Waiting', 'Blocked', 'Recent resolved']) {
      expect(source).toContain(label)
    }
    expect(source).toContain('<details key={key}>')
    expect(source).toContain('lembrar não autoriza executar')
  })
})
