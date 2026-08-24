import { useCallback, useEffect, useRef, useState } from 'react'
import { candidateBadge, fmtBytes, startRunUserFeedback, type ProfilesOverview } from './benchmarkLogic'

type RunEntry = {
  run_id: string
  kind: string
  state: 'QUEUED' | 'RUNNING' | 'DONE' | 'FAILED'
  error_code?: string | null
  result?: Record<string, unknown> | null
}

type Baseline = { label: string; model_id?: string; created_at?: string }

const json = async (response: Response) => {
  try { return await response.json() } catch { return {} }
}

export function BenchmarkPanel() {
  const [profiles, setProfiles] = useState<ProfilesOverview>({})
  const [modelId, setModelId] = useState('')
  const [runs, setRuns] = useState<RunEntry[]>([])
  const [baselines, setBaselines] = useState<Baseline[]>([])
  const [baselineLabel, setBaselineLabel] = useState('qwen3-8b')
  const [compareA, setCompareA] = useState('')
  const [compareB, setCompareB] = useState('')
  const [comparison, setComparison] = useState<Record<string, unknown> | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const pollRef = useRef<number | null>(null)

  const refreshBaselines = useCallback(async () => {
    const payload = await json(await fetch('/api/benchmark/baselines'))
    setBaselines(payload.baselines ?? [])
  }, [])

  const refreshAll = useCallback(async () => {
    try {
      const overview = await json(await fetch('/api/benchmark/profiles'))
      setProfiles(overview ?? {})
      if (!modelId && overview?.current_official_model) setModelId(overview.current_official_model)
      const runsPayload = await json(await fetch('/api/benchmark/runs'))
      setRuns(runsPayload.runs ?? [])
      await refreshBaselines()
    } catch {
      setError('Backend indisponível para o Benchmark Lab.')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelId, refreshBaselines])

  useEffect(() => { void refreshAll() }, [refreshAll])

  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current) }, [])

  const startRun = async (kind: 'perf' | 'quality' | 'full') => {
    setError(null); setNotice(null)
    const target = modelId.trim()
    if (!target) { setError('Informe o model_id do benchmark.'); return }
    const response = await fetch(`/api/benchmark/${kind}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_id: target }),
    })
    const feedback = startRunUserFeedback(await json(response), target, kind)
    if (feedback.error) { setError(feedback.error); return }
    if (feedback.notice) setNotice(feedback.notice)
    const payload = await json(response)
    if (!payload.run_id) return
    if (pollRef.current) window.clearInterval(pollRef.current)
    pollRef.current = window.setInterval(async () => {
      const entry = await json(await fetch(`/api/benchmark/runs/${payload.run_id}`))
      setRuns((current) => [entry, ...current.filter((item) => item.run_id !== entry.run_id)])
      if (entry.state === 'DONE' || entry.state === 'FAILED') {
        if (pollRef.current) window.clearInterval(pollRef.current)
        await refreshBaselines()
      }
    }, 2000)
  }

  const saveBaseline = async (runId: string) => {
    const response = await fetch('/api/benchmark/baselines/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId, label: baselineLabel }),
    })
    const payload = await json(response)
    if (payload.success) { setNotice(`Baseline "${payload.label}" salva.`); await refreshBaselines() }
    else setError(payload.error_code === 'RUN_NOT_FOUND' ? 'Run não encontrada.' : 'Falha ao salvar baseline.')
  }

  const compare = async () => {
    setError(null)
    const response = await fetch('/api/benchmark/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ baseline: compareA, candidate: compareB }),
    })
    setComparison(await json(response))
  }

  const resultOf = (entry: RunEntry): Record<string, any> => entry.result ?? {}
  const qualityTotals = (entry: RunEntry) => (resultOf(entry).quality as any)?.totals ?? {}
  const perfSummary = (entry: RunEntry) => (resultOf(entry).summary as any) ?? {}

  return <section className="benchmark-panel">
    <div className="benchmark-header">
      <div>
        <h3>Model Benchmark</h3>
        <p>Modelo oficial atual: <strong>{profiles.current_official_model ?? '—'}</strong>
          {profiles.active_model && profiles.active_model !== profiles.current_official_model ? ` (ativo temporário: ${profiles.active_model})` : ''}</p>
      </div>
      <label className="benchmark-model-field">
        model_id
        <input value={modelId} onChange={(event) => setModelId(event.target.value)} placeholder="ex.: qwen3:8b" spellCheck={false}/>
      </label>
    </div>

    {profiles.candidates?.length ? <ul className="benchmark-candidates">
      {profiles.candidates.map((candidate) => <li key={candidate.profile_id}>
        <span>{candidate.label}</span>
        <span className={`benchmark-badge ${candidate.installed ? 'installed' : 'missing'}`}>
          {candidateBadge(candidate)}
        </span>
      </li>)}
    </ul> : null}

    <div className="benchmark-actions">
      <button onClick={() => void startRun('perf')}>Rodar Performance</button>
      <button onClick={() => void startRun('quality')}>Rodar Qualidade</button>
      <button onClick={() => void startRun('full')}>Rodar Completo</button>
    </div>

    {error && <p className="benchmark-error" role="alert">{error}</p>}
    {notice && <p className="benchmark-notice">{notice}</p>}

    <div className="benchmark-runs">
      {runs.length === 0 && <p className="benchmark-empty">Nenhuma execução ainda nesta sessão.</p>}
      {runs.map((entry) => {
        const summary = perfSummary(entry)
        const totals = qualityTotals(entry)
        return <article key={entry.run_id} className={`benchmark-run state-${entry.state.toLowerCase()}`}>
          <header><strong>{entry.kind.toUpperCase()}</strong><span>{entry.run_id}</span>
            <span className={`benchmark-badge ${entry.state === 'DONE' ? 'installed' : entry.state === 'FAILED' ? 'missing' : ''}`}>{entry.state}</span></header>
          {entry.error_code && <p className="benchmark-error">{entry.error_code}</p>}
          {entry.state === 'DONE' && <dl className="benchmark-metrics">
            <div><dt>TTFT mediano</dt><dd>{summary.ttft_ms_median_warm != null ? `${summary.ttft_ms_median_warm} ms` : '—'}</dd></div>
            <div><dt>Tokens/s mediano</dt><dd>{summary.tokens_per_second_median_warm ?? '—'}</dd></div>
            <div><dt>VRAM</dt><dd>{fmtBytes(resultOf(entry).vram_bytes_loaded)}</dd></div>
            <div><dt>RAM usada</dt><dd>{fmtBytes(resultOf(entry).ram_used_bytes)}</dd></div>
            <div><dt>Tool accuracy</dt><dd>{totals.tool_accuracy ?? '—'}</dd></div>
            <div><dt>Multi-step</dt><dd>{totals.multi_step_score ?? '—'}</dd></div>
            <div><dt>Grounding</dt><dd>{totals.grounding_score ?? '—'}</dd></div>
            <div><dt>Recovery</dt><dd>{totals.recovery_score ?? '—'}</dd></div>
          </dl>}
          {entry.state === 'DONE' && <footer className="benchmark-run-actions">
            <input value={baselineLabel} onChange={(event) => setBaselineLabel(event.target.value)} placeholder="nome da baseline" aria-label="Nome da baseline"/>
            <button onClick={() => void saveBaseline(entry.run_id)}>Salvar como baseline</button>
          </footer>}
        </article>
      })}
    </div>

    <div className="benchmark-compare">
      <h3>Comparação e promoção</h3>
      <div className="benchmark-compare-row">
        <select value={compareA} onChange={(event) => setCompareA(event.target.value)} aria-label="Baseline atual">
          <option value="">baseline atual…</option>
          {baselines.map((item) => <option key={item.label} value={item.label}>{item.label} ({item.model_id ?? '?'})</option>)}
        </select>
        <span>vs</span>
        <select value={compareB} onChange={(event) => setCompareB(event.target.value)} aria-label="Candidato">
          <option value="">candidato…</option>
          {baselines.map((item) => <option key={item.label} value={item.label}>{item.label} ({item.model_id ?? '?'})</option>)}
        </select>
        <button disabled={!compareA || !compareB} onClick={() => void compare()}>Comparar</button>
      </div>
      {baselines.length > 0 && <p className="benchmark-hint">Promover é sempre manual (Brain Lab → Selecionar cérebro com confirmação). Rollback imediato via “Restaurar”.</p>}
      {comparison?.success === false && typeof comparison.error_code === 'string' &&
        <p className="benchmark-error">{String(comparison.error_code)}</p>}
      {comparison?.success === true && <table className="benchmark-table">
        <thead><tr><th>Métrica</th><th>Atual</th><th>Candidato</th><th>Critério</th><th>OK?</th></tr></thead>
        <tbody>
          {(comparison.criteria as any[]).map((criterion) => <tr key={criterion.metric}>
            <td>{criterion.metric}</td><td>{String(criterion.current)}</td><td>{String(criterion.candidate)}</td>
            <td>{String(criterion.requirement)}</td><td>{criterion.passed ? '✓' : '✗'}</td></tr>)}
        </tbody>
      </table>}
      {typeof comparison?.recommendation === 'string' && <p className="benchmark-recommendation">{comparison.recommendation}</p>}
    </div>
  </section>
}
