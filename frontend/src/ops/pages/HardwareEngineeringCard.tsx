import { useState } from 'react'
import { apiSend } from '../../runtime/api'
import { usePolling } from '../hooks'
import { ActionButton, Card, ErrorAlert, StatusBadge, Toggle } from '../ui'

export interface HardwareStatus {
  full: boolean
  project_root: string
  project?: { name: string; build: { success?: boolean }; flash: { success?: boolean; effect_verified?: boolean } } | null
  goals: Array<{ goal_id: string; desired_effect: string; state: string; response: string; simulated: boolean; steps: Array<{ phase: string }> }>
  serial: { open_handles: number }
  research: { sources?: Array<{ url: string; title: string; retrieved_at: string; stale: boolean }> }
}

export function HardwareSummary({ value }: { value: HardwareStatus }) {
  const goal = value.goals.at(-1)
  return <>
    <dl className="diagnostic-grid">
      <div><dt>Workspace</dt><dd style={{ overflowWrap: 'anywhere' }}>{value.project_root}</dd></div>
      <div><dt>Projeto</dt><dd>{value.project?.name || 'Nenhum projeto'}</dd></div>
      <div><dt>Build</dt><dd>{value.project?.build.success === true ? 'Compilado' : 'Não confirmado'}</dd></div>
      <div><dt>Flash</dt><dd>{value.project?.flash.success === true ? 'Gravado — verificar efeito' : 'Não confirmado'}</dd></div>
      <div><dt>Serial</dt><dd>{value.serial.open_handles} conexão(ões) aberta(s)</dd></div>
    </dl>
    {goal && <div aria-label="Hardware task">
      <StatusBadge state={goal.state} /> {goal.simulated && <strong>SIMULATED</strong>}
      <p>{goal.steps.at(-1)?.phase || goal.desired_effect}</p>
      <p>{goal.response}</p>
    </div>}
    {!!value.research.sources?.length && <details><summary>Fontes da pesquisa</summary>
      <ul>{value.research.sources.map(source => <li key={source.url}>
        <a href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}</a>
        {' — '}{new Date(source.retrieved_at).toLocaleString('pt-BR')}{source.stale ? ' (cache antigo)' : ''}
      </li>)}</ul>
    </details>}
  </>
}

export function HardwareEngineeringCard() {
  const status = usePolling<HardwareStatus>('/api/hardware/status', 5000, { noStore: true })
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [reply, setReply] = useState('')
  async function configure(full: boolean) {
    setBusy(true); setError('')
    try { await apiSend('/api/hardware/settings', 'PUT', { full }); status.refresh() }
    catch { setError('Não foi possível atualizar o modo de execução.') }
    finally { setBusy(false) }
  }
  async function submit() {
    setBusy(true); setError('')
    try {
      const result = await apiSend<{ response: string }>('/api/hardware/goals', 'POST', { text }, 120000)
      setReply(result.response); status.refresh()
    } catch { setError('Não foi possível concluir o pedido. Consulte o estado da tarefa antes de repetir.') }
    finally { setBusy(false) }
  }
  return <Card title="Hardware Engineering" sub="Objetivos locais, descoberta real e verificação. Pesquisa externa apenas quando solicitada ou necessária ao objetivo.">
    {status.data && <>
      <Toggle checked={status.data.full} onChange={full => void configure(full)} disabled={busy} label="FULL — receitas de hardware local autorizadas" />
      <HardwareSummary value={status.data} />
    </>}
    <label>Objetivo<input value={text} maxLength={1000} onChange={event => setText(event.target.value)}
      placeholder="Descobre o que é essa placa" style={{ width: '100%', boxSizing: 'border-box', minWidth: 0 }} /></label>
    <ActionButton onClick={submit} busy={busy} disabled={text.trim().length < 3}>Executar objetivo</ActionButton>
    <ErrorAlert message={error} />
    {reply && <p role="status">{reply}</p>}
  </Card>
}
