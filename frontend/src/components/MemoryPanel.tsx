import { useEffect, useState } from 'react'
import type { MemoryRecord } from '../types'

export function MemoryPanel() {
  const [memories, setMemories] = useState<MemoryRecord[]>([])
  const [query, setQuery] = useState('')
  const load = async () => {
    const url = query.trim() ? `/api/memory/search?q=${encodeURIComponent(query)}` : '/api/memory?category=semantic&limit=8'
    const response = await fetch(url)
    if (response.ok) setMemories(await response.json())
  }
  useEffect(() => { void load() }, [])
  const remove = async (memory: MemoryRecord) => {
    if (await fetch(`/api/memory/${memory.category}/${memory.id}`, { method: 'DELETE' }).then((r) => r.ok)) await load()
  }
  return (
    <section className="panel compact-panel">
      <header className="panel-header"><span>MEMÓRIA</span><small>FTS5</small></header>
      <form className="memory-search" onSubmit={(event) => { event.preventDefault(); void load() }}><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Pesquisar memória" /><button type="submit" aria-label="Buscar memórias">Buscar</button></form>
      <div className="memory-list">{memories.length === 0 ? <p className="muted">Nenhuma memória semântica registrada.</p> : memories.map((memory) => <article key={`${memory.category}-${memory.id}`}><span>{memory.category} · {memory.importance}/10</span><p>{memory.content}</p><button onClick={() => void remove(memory)} aria-label="Excluir memória">×</button></article>)}</div>
    </section>
  )
}

