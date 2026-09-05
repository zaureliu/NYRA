import type { ActivityStatus, EmotionalState, Health } from '../types'

interface Props { health: Health | null; connected: boolean; status: ActivityStatus; state: EmotionalState }

export function StatusPanel({ health, connected, status, state }: Props) {
  const checks = health ? [
    ['LLM', health.llm, health.llm && health.llm_ready === false ? 'PREPARANDO' : undefined],
    ['MEMÓRIA', health.memory, undefined],
    ['STT', health.stt, undefined],
    ['TTS', health.tts, undefined],
  ] as const : []
  return (
    <section className="panel status-panel">
      <header className="panel-header"><span>SISTEMA</span><small>{health?.model ?? '—'}</small></header>
      <div className="identity-status"><div><h1>KAZUMI</h1><p>HOMELAB INTELLIGENCE</p></div><span className={`online-pill ${connected ? '' : 'offline'}`}>{connected ? 'ONLINE' : 'OFFLINE'}</span></div>
      <div className="state-grid"><div><label>ATIVIDADE</label><strong>{status}</strong></div><div><label>ESTADO</label><strong>{state.toUpperCase()}</strong></div></div>
      <ul className="service-list">{checks.map(([name, ok, override]) => <li key={name}><span>{name}</span><i className={ok ? 'ok' : 'fail'} />{override ?? (ok ? 'ATIVO' : 'INDISPONÍVEL')}</li>)}</ul>
    </section>
  )
}
