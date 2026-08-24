import { usePolling } from '../hooks'
import { Card, Empty, ErrorAlert, StatusBadge } from '../ui'
import { BenchmarkPanel } from '../../components/BenchmarkPanel'
import { RealtimeDebug } from '../../components/RealtimeDebug'
import { RuntimeServicesPanel } from '../../components/RuntimeServicesPanel'
import { SkillsSettings } from '../../components/SkillsSettings'
import { BrainLab } from '../../components/BrainLab'
import type { HealthReport, SubsystemHealthEntry } from '../types'

interface ToolInfo {
  name: string
  risk: string
  enabled?: boolean
  description?: string
}

export function DeveloperPage() {
  const health = usePolling<HealthReport>('/api/health_report', 20000)
  const tools = usePolling<{ tools?: ToolInfo[] } | ToolInfo[]>('/api/tools', 30000)
  const turns = usePolling<Record<string, unknown>>('/api/turns/metrics', 10000)

  const toolList: ToolInfo[] = Array.isArray(tools.data)
    ? tools.data
    : (tools.data?.tools ?? [])

  return (
    <div>
      <header className="ops-page-header">
        <div>
          <h1 className="ops-page-title">Developer</h1>
          <p className="ops-page-subtitle">
            Observabilidade interna: health matrix, tool inspector com risco,
            métricas de turnos e serviços gerenciados. Nenhum segredo é exibido.
          </p>
        </div>
      </header>

      <ErrorAlert message={health.error} />

      <h2 className="ops-section-title">Health Matrix</h2>
      <Card sub={`Overall: ${health.data?.overall ?? '—'} · gerado ${health.data?.generated_at ?? '—'}`}>
        {health.data ? (
          <SubsystemTable subsystems={Object.values(health.data.subsystems)} summary={health.data.summary} />
        ) : (
          <Empty text="Health report indisponível." />
        )}
      </Card>

      <h2 className="ops-section-title">Tool Inspector</h2>
      <Card sub={`${toolList.length} ferramentas registradas`}>
        {toolList.length === 0 ? (
          <Empty text="Registry de tools indisponível." />
        ) : (
          <div className="table-scroll">
            <table className="ops-table">
              <thead><tr><th>Tool</th><th>Risco</th><th>Descrição</th></tr></thead>
              <tbody>
                {toolList.map((tool) => (
                  <tr key={tool.name}>
                    <td><strong>{tool.name}</strong></td>
                    <td><StatusBadge state={riskToState(tool.risk)} /></td>
                    <td>{tool.description ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <h2 className="ops-section-title">Métricas de turnos</h2>
      <Card>
        {turns.data ? (
          <pre className="ops-code" style={{ maxHeight: 220 }}>{JSON.stringify(turns.data, null, 2).slice(0, 1600)}</pre>
        ) : <Empty text="Sem métricas ainda." />}
      </Card>

      <h2 className="ops-section-title">Serviços gerenciados</h2>
      <RuntimeServicesPanel />

      <h2 className="ops-section-title">Cérebro / modelos</h2>
      <BrainLab />

      <h2 className="ops-section-title">Benchmark lab</h2>
      <BenchmarkPanel />

      <h2 className="ops-section-title">Skills</h2>
      <SkillsSettings />

      <h2 className="ops-section-title">Realtime debug</h2>
      <RealtimeDebug />
    </div>
  )
}

function riskToState(risk: string): string {
  const value = String(risk || '').toUpperCase()
  if (value === 'READ_ONLY') return 'READY'
  if (value === 'DYNAMIC') return 'DEGRADED'
  if (!value) return 'UNKNOWN'
  return value
}

function SubsystemTable({ subsystems, summary }: {
  subsystems: SubsystemHealthEntry[]
  summary: Record<string, number>
}) {
  return (
    <>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
        {Object.entries(summary).map(([key, value]) => (
          <span key={key} className="ops-chip">
            <span className="chip-dot" />{key}: {value}
          </span>
        ))}
      </div>
      <div className="table-scroll">
        <table className="ops-table">
          <thead><tr><th>Subsistema</th><th>Estado</th><th>Dependências</th><th>Último erro</th></tr></thead>
          <tbody>
            {subsystems.map((entry) => (
              <tr key={entry.name}>
                <td><strong>{entry.name}</strong></td>
                <td><StatusBadge state={entry.state} /></td>
                <td>{(entry.dependencies ?? []).join(', ') || '—'}</td>
                <td>{entry.last_error ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
