import { FormEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { ChatMessage, ToolActivity } from '../types'

interface Props {
  messages: ChatMessage[]
  busy: boolean
  recording: boolean
  onSend: (message: string) => Promise<void>
  onTalkStart: () => Promise<void>
  onTalkEnd: () => void
  toolActivities?: ToolActivity[]
}

const COMPOSER_MAX_HEIGHT = 144
export const boundedComposerHeight = (scrollHeight: number) => Math.min(Math.max(scrollHeight, 0), COMPOSER_MAX_HEIGHT)
export const shouldFollowConversation = (pinned: boolean, lastRole: ChatMessage['role'] | undefined, previousLength: number) => pinned || lastRole === 'user' || previousLength === 0

const TOOL_TRACE_KEY = 'kazumi-show-tool-trace'
export const readToolTracePreference = (): boolean => localStorage.getItem(TOOL_TRACE_KEY) === 'true'

/** Execução de tools fora do fluxo da conversa (closure §2): por padrão o chat
 * mostra só mensagens e aprovações reais; detalhes técnicos ficam em um
 * agrupador compacto por Agent Run, expansível, ou visíveis no modo técnico. */
export function groupToolActivities(activities: ToolActivity[]): Array<{ key: string; label: string; items: ToolActivity[] }> {
  const groups = new Map<string, ToolActivity[]>()
  for (const activity of activities) {
    if (activity.status === 'approval_required') continue // aprovação nunca é escondida (§33)
    const key = activity.agentRunId || `direct:${activity.tool}`
    const list = groups.get(key) ?? []
    list.push(activity)
    groups.set(key, list)
  }
  return [...groups.entries()].map(([key, items]) => {
    const label = items[0].tool === 'agent_run' ? 'Agent Run' : items[0].tool === 'remote_shell' ? 'Remote Shell' : 'System Shell'
    return { key, label, items }
  })
}

export function ConversationPanel({ messages, busy, recording, onSend, onTalkStart, onTalkEnd, toolActivities = [] }: Props) {
  const [input, setInput] = useState('')
  const [pinnedToBottom, setPinnedToBottom] = useState(true)
  const [newMessages, setNewMessages] = useState(false)
  const [showToolTrace, setShowToolTrace] = useState(readToolTracePreference)
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({})
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const pinnedRef = useRef(true)
  const previousLength = useRef(0)

  const approvals = toolActivities.filter((activity) => activity.status === 'approval_required')
  const toolGroups = groupToolActivities(toolActivities)

  const toggleToolTrace = () => {
    setShowToolTrace((current) => {
      localStorage.setItem(TOOL_TRACE_KEY, String(!current))
      return !current
    })
  }

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const container = scrollRef.current
    if (!container) return
    container.scrollTo({ top: container.scrollHeight, behavior })
    pinnedRef.current = true
    setPinnedToBottom(true)
    setNewMessages(false)
  }, [])

  useEffect(() => {
    if (messages.length === previousLength.current) return
    const lastMessage = messages.at(-1)
    const shouldFollow = shouldFollowConversation(pinnedRef.current, lastMessage?.role, previousLength.current)
    previousLength.current = messages.length
    if (shouldFollow) requestAnimationFrame(() => scrollToBottom(lastMessage?.role === 'user' ? 'auto' : 'smooth'))
    else setNewMessages(true)
  }, [messages, scrollToBottom])

  const resizeComposer = useCallback((textarea: HTMLTextAreaElement) => {
    textarea.style.height = '0px'
    textarea.style.height = `${boundedComposerHeight(textarea.scrollHeight)}px`
    textarea.style.overflowY = textarea.scrollHeight > COMPOSER_MAX_HEIGHT ? 'auto' : 'hidden'
  }, [])

  useLayoutEffect(() => { if (inputRef.current) resizeComposer(inputRef.current) }, [input, resizeComposer])

  const handleScroll = () => {
    const container = scrollRef.current
    if (!container) return
    const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 72
    pinnedRef.current = atBottom
    setPinnedToBottom(atBottom)
    if (atBottom) setNewMessages(false)
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const value = input.trim()
    if (!value || busy) return
    setInput('')
    requestAnimationFrame(() => { if (inputRef.current) resizeComposer(inputRef.current) })
    await onSend(value)
  }

  return <section className="panel conversation-panel" aria-label="Conversa com KAZUMI">
    <header className="conversation-header">
      <div><span className="eyebrow">CANAL LOCAL</span><h2>Conversa com KAZUMI</h2><p>Contexto privado, resposta em streaming e voz sincronizada.</p></div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <label className="conversation-trace-toggle" title="Mostrar execução de tools na conversa (modo técnico)">
          <input type="checkbox" checked={showToolTrace} onChange={toggleToolTrace} />
          <span>Modo técnico</span>
        </label>
        <span className={`conversation-presence ${busy ? 'thinking' : ''}`}><i/>{busy ? 'PROCESSANDO' : 'PRONTA'}</span>
      </div>
    </header>

    <div className="messages" ref={scrollRef} onScroll={handleScroll} aria-live="polite" aria-relevant="additions" data-testid="message-scroll-region">
      {messages.length === 0 && <div className="empty-state"><span className="node-symbol" aria-hidden="true">N</span><h3>Canal aberto</h3><p>Inicie uma conversa por texto ou mantenha pressionado para falar.</p></div>}
      <div className="message-flow">
        {messages.map((message) => <article key={message.id} className={`message ${message.role}`}>
          <div className="message-meta"><strong>{message.role === 'assistant' ? 'KAZUMI' : 'VOCÊ'}</strong><time>{message.timestamp.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })}</time></div>
          <p>{message.content}</p>
        </article>)}
        {approvals.map((activity) => <aside key={activity.id} className={`tool-activity ${activity.status}`} aria-label="Aprovação necessária">
          <div><strong>{activity.tool === 'remote_shell' ? `REMOTE SHELL${activity.host ? ` · ${activity.host}` : ''}` : 'SYSTEM SHELL'}</strong><span>{activity.riskLevel}</span></div>
          <code>{activity.command}</code>
          <small>aprovação explícita necessária</small>
        </aside>)}
        {toolGroups.map((group) => {
          const expanded = showToolTrace || expandedGroups[group.key]
          const running = group.items.filter((item) => item.status === 'running').length
          return (
            <div key={group.key} className="tool-group" data-expanded={expanded}>
              <button type="button" className="tool-group-summary" aria-expanded={expanded}
                onClick={() => setExpandedGroups((current) => ({ ...current, [group.key]: !current[group.key] }))}>
                {group.label} · {group.items.length} tool{group.items.length === 1 ? '' : 's'} executada{group.items.length === 1 ? '' : 's'}{running ? ` · ${running} em execução` : ''}
                <span className="tool-group-chevron" aria-hidden="true">{expanded ? '▾' : '▸'}</span>
              </button>
              {expanded && group.items.map((activity) => <aside key={activity.id} className={`tool-activity ${activity.status}`} aria-label="Atividade controlada da KAZUMI">
                <div><strong>{activity.tool === 'remote_shell' ? `REMOTE SHELL${activity.host ? ` · ${activity.host}` : ''}` : activity.tool === 'agent_run' ? 'AGENT RUN' : 'SYSTEM SHELL'}</strong><span>{activity.riskLevel}</span></div>
                <code>{activity.command}</code>
                <small>{activity.detail || (activity.status === 'running' ? (activity.tool === 'remote_shell' ? 'executando diagnóstico remoto confiável' : activity.tool === 'agent_run' ? 'investigação autônoma controlada' : 'executando diagnóstico local') : activity.tool === 'agent_run' ? (activity.success ? 'concluído' : 'interrompido') : `exit ${activity.exitCode ?? '—'} · ${Math.round(activity.durationMs ?? 0)} ms`)}</small>
              </aside>)}
            </div>
          )
        })}
        {busy && <div className="thinking-line" role="status"><span className="thinking-dots"><i/><i/><i/></span><span>Organizando contexto e resposta</span></div>}
      </div>
    </div>

    {!pinnedToBottom && <button className={`jump-to-latest ${newMessages ? 'has-new' : ''}`} onClick={() => scrollToBottom()} aria-label="Voltar para a mensagem mais recente">
      <span>↓</span>{newMessages ? 'Nova mensagem' : 'Voltar ao fim'}
    </button>}

    <form className="composer" onSubmit={submit}>
      <div className="composer-field">
        <textarea ref={inputRef} value={input} onChange={(event) => setInput(event.target.value)} placeholder="Escreva uma mensagem para a KAZUMI…" rows={1} aria-label="Mensagem para KAZUMI"
          onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }}/>
        <span className="composer-hint">Enter envia · Shift + Enter quebra linha</span>
      </div>
      <div className="composer-actions">
        <button type="button" className={`talk-button ${recording ? 'recording' : ''}`} disabled={busy}
          onPointerDown={() => void onTalkStart()} onPointerUp={onTalkEnd} onPointerCancel={onTalkEnd} onPointerLeave={() => recording && onTalkEnd()}>
          <span className="mic-icon" aria-hidden="true">●</span><span>{recording ? 'Solte para enviar' : 'Falar'}</span>
        </button>
        <button className="send-button" type="submit" disabled={busy || !input.trim()} aria-label="Enviar mensagem"><span>Enviar</span><b aria-hidden="true">↑</b></button>
      </div>
    </form>
  </section>
}
