import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ConversationPanel } from './components/ConversationPanel'
import { useAlwaysListening, type AlwaysListeningResult } from './hooks/useAlwaysListening'
import { useAudioSettings } from './hooks/useAudioSettings'
import { useAudioLipSync } from './hooks/useAudioLipSync'
import { useNyraSocket } from './hooks/useNyraSocket'
import { usePushToTalk } from './hooks/usePushToTalk'
import { useStreamingAudioQueue } from './hooks/useStreamingAudioQueue'
import type { ActivityStatus, AvatarControl, ChatMessage, ChatResponse, EmotionalState, Health, ToolActivity } from './types'
import { dashboardOwnsRealtimeAudio } from './runtime/audioOwnership'
import { TurnFilter, adoptInputTurn, extractTurnId } from './runtime/turns'
import { backendUrl, isTauriRuntime } from './runtime/backend'
import { sendChat } from './runtime/conversation'
import { readHeaderStatus } from './runtime/headerStatus'

import { OPS_VIEWS, Sidebar, type OpsView } from './ops/Sidebar'
import { TopStatusBar } from './ops/TopStatusBar'
import { OverviewPage } from './ops/pages/OverviewPage'
import { CapabilitiesPage } from './ops/pages/CapabilitiesPage'
import { IntegrationsPage } from './ops/pages/IntegrationsPage'
import { SentinelPage } from './ops/pages/SentinelPage'
import { HomelabPage } from './ops/pages/HomelabPage'
import { NetworkPage } from './ops/pages/NetworkPage'
import { AutonomyPage } from './ops/pages/AutonomyPage'
import { TasksPage } from './ops/pages/TasksPage'
import { VoicePage } from './ops/pages/VoicePage'
import { UsbDevicesPage } from './ops/pages/UsbDevicesPage'
import { SettingsPageV3 } from './ops/pages/SettingsPageV3'
import { DeveloperPage } from './ops/pages/DeveloperPage'
import { AboutPage } from './ops/pages/AboutPage'

const readInitialView = (): OpsView => {
  const hash = window.location.hash.slice(1) as OpsView
  if (OPS_VIEWS.includes(hash)) return hash
  const saved = localStorage.getItem('nyra-active-view') as OpsView | null
  return saved && OPS_VIEWS.includes(saved) ? saved : 'overview'
}

interface FeedItem {
  id: string
  label: string
  at: number
  severity: 'info' | 'alert'
}

const FEED_EVENT_LABELS: Record<string, string> = {
  USER_SPEECH_FINAL: 'Fala do operador transcrita',
  STT_STARTED: 'Transcrição iniciada',
  LLM_STREAM_STARTED: 'LLM gerando resposta',
  SENTINEL_STATUS_CHANGED: 'Sentinel mudou de estado',
  SENTINEL_EVENT: 'Evento recebido do Sentinel',
  SENTINEL_ALERT: 'Alerta do Sentinel',
  NETWORK_ALERT: 'Alerta de rede',
  JOB_STARTED: 'Job iniciado',
  JOB_FINISHED: 'Job finalizado',
  TASK_CREATED: 'Task criada',
  WORKFLOW_RUN_STARTED: 'Workflow em execução',
  WORKFLOW_RUN_FINISHED: 'Workflow finalizado',
  RECOVERY_EXECUTED: 'Recovery executado',
  RUNTIME_SERVICE_RESTARTED: 'Serviço reiniciado pelo supervisor',
  PROACTIVE_ALERT_FIRED: 'Alerta proativo',
  MONITOR_JOB_CREATED: 'MonitorJob criado',
  MONITOR_JOB_CHANGED: 'MonitorJob detectou mudança',
  MONITOR_JOB_COMPLETED: 'MonitorJob concluído',
  MONITOR_JOB_FAILED: 'MonitorJob falhou',
  MONITOR_JOB_CANCELLED: 'MonitorJob cancelado',
  MONITOR_NOTIFICATION: 'Atualização de monitoramento',
  'usb.device.connected': 'USB conectado',
  'usb.device.disconnected': 'USB removido',
  'usb.device.unknown': 'Novo USB desconhecido',
  'usb.device.com_changed': 'Porta COM alterada',
  usb_monitor_failure: 'Monitor USB degradado',
  ERROR: 'Erro de pipeline',
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [connected, setConnected] = useState(false)
  const [status, setStatus] = useState<ActivityStatus>('OFFLINE')
  const [state, setState] = useState<EmotionalState>('neutral')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [toolActivities, setToolActivities] = useState<ToolActivity[]>([])
  const [busy, setBusy] = useState(false)
  const [avatarControl, setAvatarControl] = useState<Partial<AvatarControl>>({})
  const audio = useAudioSettings()
  const microphone = audio.settings.microphone
  const speaker = audio.settings.speaker
  const [view, setView] = useState<OpsView>(readInitialView)
  const [navCollapsed, setNavCollapsed] = useState(() => localStorage.getItem('nyra-nav-collapsed') === 'true')
  const [feed, setFeed] = useState<FeedItem[]>([])
  const lipSyncSent = useRef(0)
  const chooseMicrophone = useCallback((id: string) => { void audio.patch({ microphone: id }) }, [audio.patch])

  const pushFeed = useCallback((label: string, severity: 'info' | 'alert' = 'info') => {
    setFeed((current) => [...current.slice(-39), {
      id: crypto.randomUUID(), label, at: Date.now() / 1000, severity,
    }])
  }, [])

  const sendLive2DMouth = useCallback((value: number) => {
    const now = performance.now()
    if (value && now - lipSyncSent.current < 50) return
    lipSyncSent.current = now
    void fetch('/api/live2d/lip-sync', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value }) }).catch(() => undefined)
  }, [])
  const { play } = useAudioLipSync(speaker, sendLive2DMouth, audio.settings.volume)

  const setPlaybackGuard = useCallback(async (playing: boolean, responseId?: string) => {
    await fetch('/api/listening/playback', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ playing, response_id: responseId }) }).catch(() => undefined)
  }, [])

  const playResponse = useCallback(async (url: string, responseId?: string) => {
    setStatus('SPEAKING')
    try {
      await play(
        url,
        () => { setStatus('IDLE'); void setPlaybackGuard(false) },
        () => { void setPlaybackGuard(true, responseId) },
      )
    }
    catch { setStatus('IDLE'); await setPlaybackGuard(false) }
  }, [play, setPlaybackGuard])

  const streaming = useStreamingAudioQueue(play, setPlaybackGuard, useCallback((speaking) => setStatus(speaking ? 'SPEAKING' : 'IDLE'), []))
  const turnFilter = useRef(new TurnFilter())
  const pendingTurnRequests = useRef(0)

  const onRealtimeEvent = useCallback((event: { type: string; payload: Record<string, unknown> }) => {
    // §160: UI ignora eventos stale via turn_id; seq é ignorado aqui pois o
    // TurnFilter já garante isolamento por turno e o feed é apenas informativo.
    const feedLabel = FEED_EVENT_LABELS[event.type]
    if (feedLabel) {
      const severity = event.type.includes('ALERT') || event.type === 'ERROR' ? 'alert' : 'info'
      pushFeed(feedLabel, severity)
    }
    const responseId = String(event.payload.response_id ?? '')
    const eventTurnId = extractTurnId(event.payload)
    if (adoptInputTurn(turnFilter.current, event.type, eventTurnId)) {
      pendingTurnRequests.current = Math.max(0, pendingTurnRequests.current - 1)
    }
    if (event.type === 'MONITOR_NOTIFICATION') {
      const content = String(event.payload.message ?? 'Atualização de monitoramento.')
      setMessages((current) => [...current, {
        id: `monitor-${String(event.payload.monitor_id ?? crypto.randomUUID())}-${Date.now()}`,
        role: 'assistant', content, timestamp: new Date(), status: 'complete',
      }])
    }
    if (
      event.type === 'TTS_FINISHED'
      && (event.payload.source === 'monitor_job' || event.payload.source === 'voice_test')
      && event.payload.audio_url
      && dashboardOwnsRealtimeAudio(isTauriRuntime())
    ) {
      void playResponse(
        backendUrl(String(event.payload.audio_url)),
        String(event.payload.response_id ?? '') || undefined,
      )
    }
    const assistantId = responseId ? `assistant-${responseId}` : eventTurnId ? `assistant-${eventTurnId}` : ''
    if (event.type === 'LLM_TOKEN_RECEIVED' && !turnFilter.current.accept(eventTurnId)) return
    if (event.type === 'NYRA_RESPONSE' && !turnFilter.current.accept(eventTurnId)) return
    if (event.type === 'LLM_TOKEN_RECEIVED' && assistantId) {
      const delta = String(event.payload.delta ?? '')
      setMessages((current) => {
        const existing = current.find((message) => message.id === assistantId)
        if (!existing) return [...current, { id: assistantId, role: 'assistant', content: delta, timestamp: new Date(), turnId: eventTurnId ?? undefined, status: 'streaming' }]
        return current.map((message) => message.id === assistantId ? { ...message, content: message.content + delta } : message)
      })
    }
    if (event.type === 'NYRA_RESPONSE' && assistantId) {
      const content = String(event.payload.display_text ?? event.payload.text ?? '')
      setMessages((current) => current.some((message) => message.id === assistantId)
        ? current.map((message) => message.id === assistantId ? { ...message, content, status: 'complete' } : message)
        : [...current, { id: assistantId, role: 'assistant', content, timestamp: new Date(), turnId: eventTurnId ?? undefined, status: 'complete' }])
    }
    if (event.type === 'TTS_CHUNK_FINISHED' && event.payload.audio_url && dashboardOwnsRealtimeAudio(isTauriRuntime())) {
      if (!turnFilter.current.accept(eventTurnId)) return
      streaming.enqueue({ url: backendUrl(String(event.payload.audio_url)), responseId: String(event.payload.response_id ?? 'stream'), index: Number(event.payload.index ?? 0) })
    }
    if (event.type === 'TTS_FINISHED') turnFilter.current.end(eventTurnId)
    if (event.type === 'SPEECH_CANCELLED') streaming.clear()
    if (event.type === 'AVATAR_STATE_CHANGED') setAvatarControl(event.payload as Partial<AvatarControl>)
    if (event.type === 'SHELL_EXECUTION_STARTED') {
      const id = String(event.payload.execution_id ?? crypto.randomUUID())
      const activity: ToolActivity = { id, command: String(event.payload.command ?? ''), riskLevel: String(event.payload.risk_level ?? ''), status: 'running', tool: 'system_shell', agentRunId: String(event.payload.agent_run_id ?? '') }
      setToolActivities((current) => [...current.filter((item) => item.id !== id), activity].slice(-6))
    }
    if (event.type === 'SHELL_EXECUTION_FINISHED') {
      const id = String(event.payload.execution_id ?? '')
      setToolActivities((current) => current.map((item) => item.id === id ? {
        ...item, status: 'finished', exitCode: event.payload.exit_code == null ? null : Number(event.payload.exit_code),
        durationMs: Number(event.payload.duration_ms ?? 0), success: Boolean(event.payload.success),
      } : item))
    }
    if (event.type === 'SHELL_APPROVAL_REQUIRED') {
      const id = `approval-${String(event.payload.approval_id ?? crypto.randomUUID())}`
      const activity: ToolActivity = { id, command: String(event.payload.command ?? ''), riskLevel: String(event.payload.risk_level ?? ''), status: 'approval_required', tool: 'system_shell', agentRunId: String(event.payload.agent_run_id ?? '') }
      setToolActivities((current) => [...current, activity].slice(-6))
    }
    if (event.type === 'REMOTE_SHELL_EXECUTION_STARTED') {
      const id = String(event.payload.execution_id ?? crypto.randomUUID())
      const activity: ToolActivity = {
        id, command: String(event.payload.execution_id ? event.payload.command ?? '' : event.payload.command ?? ''), riskLevel: String(event.payload.risk_level ?? ''),
        status: 'running', tool: 'remote_shell', host: String(event.payload.host ?? ''), agentRunId: String(event.payload.agent_run_id ?? ''),
      }
      setToolActivities((current) => [...current.filter((item) => item.id !== id), activity].slice(-10))
    }
    if (event.type === 'REMOTE_SHELL_EXECUTION_FINISHED') {
      const id = String(event.payload.execution_id ?? '')
      setToolActivities((current) => current.map((item) => item.id === id ? {
        ...item, status: 'finished', exitCode: event.payload.exit_code == null ? null : Number(event.payload.exit_code),
        durationMs: Number(event.payload.duration_ms ?? 0), success: Boolean(event.payload.success),
      } : item))
    }
    if (event.type === 'REMOTE_SHELL_APPROVAL_REQUIRED') {
      const id = `approval-${String(event.payload.approval_id ?? crypto.randomUUID())}`
      const activity: ToolActivity = {
        id, command: String(event.payload.command ?? ''), riskLevel: String(event.payload.risk_level ?? ''),
        status: 'approval_required', tool: 'remote_shell', host: String(event.payload.host ?? ''), agentRunId: String(event.payload.agent_run_id ?? ''),
      }
      setToolActivities((current) => [...current, activity].slice(-10))
    }
    if (event.type === 'AGENT_RUN_STARTED') {
      const id = String(event.payload.agent_run_id ?? crypto.randomUUID())
      const activity: ToolActivity = {
        id, command: String(event.payload.goal ?? 'Agent Run'), riskLevel: 'CONTROLLED', status: 'running', tool: 'agent_run',
        agentRunId: id, detail: String(event.payload.state ?? 'OBSERVE'),
      }
      setToolActivities((current) => [...current.filter((item) => item.id !== id), activity].slice(-10))
    }
    if (event.type === 'AGENT_RUN_STATE_CHANGED') {
      const id = String(event.payload.agent_run_id ?? '')
      setToolActivities((current) => current.map((item) => item.id === id ? { ...item, detail: String(event.payload.state ?? '') } : item))
    }
    if (event.type === 'AGENT_RUN_FINISHED' || event.type === 'AGENT_RUN_CANCELLED') {
      const id = String(event.payload.agent_run_id ?? '')
      setToolActivities((current) => current.map((item) => item.id === id ? {
        ...item, status: 'finished', success: event.type === 'AGENT_RUN_FINISHED' && String(event.payload.status ?? '') === 'COMPLETED',
        detail: event.type === 'AGENT_RUN_CANCELLED' ? 'CANCELLED' : String(event.payload.state ?? event.payload.status ?? 'COMPLETE'),
      } : item))
    }
  }, [streaming, pushFeed, playResponse])

  useNyraSocket({
    setStatus: useCallback((value) => setStatus(value), []),
    setState: useCallback((value) => setState(value), []),
    setConnected: useCallback((value) => setConnected(value), []),
    onEvent: onRealtimeEvent,
  })

  const loadHealth = useCallback(async () => {
    try {
      const value = await readHeaderStatus<Health>('/api/health')
      setHealth(value)
      if (value.status === 'online') setStatus((current) => current === 'OFFLINE' ? 'IDLE' : current)
    } catch { setHealth(null); setStatus('OFFLINE') }
  }, [])

  useEffect(() => { void loadHealth(); const timer = setInterval(() => void loadHealth(), 10000); return () => clearInterval(timer) }, [loadHealth])
  useEffect(() => {
    const syncHash = () => { const next = window.location.hash.slice(1) as OpsView; if (OPS_VIEWS.includes(next)) setView(next) }
    window.addEventListener('hashchange', syncHash)
    return () => window.removeEventListener('hashchange', syncHash)
  }, [])

  const send = useCallback(async (text: string) => {
    const userMessage: ChatMessage = { id: crypto.randomUUID(), role: 'user', content: text, timestamp: new Date() }
    setMessages((current) => [...current, userMessage])
    if (health?.llm_ready === false) {
      setMessages((current) => [...current, {
        id: crypto.randomUUID(),
        role: 'assistant',
        content: 'A IA local ainda está sendo preparada. Tente novamente quando o indicador LLM mostrar ATIVO.',
        timestamp: new Date(),
      }])
      setState('focused')
      setStatus('IDLE')
      return
    }
    setBusy(true)
    setStatus('THINKING')
    pendingTurnRequests.current += 1
    try {
      const value = await sendChat({ message: text, synthesize: true })
      if (value.turn_id) turnFilter.current.begin(value.turn_id)
      setState(value.state)
      const assistantId = value.response_id ? `assistant-${value.response_id}` : crypto.randomUUID()
      setMessages((current) => current.some((message) => message.id === assistantId)
        ? current.map((message) => message.id === assistantId ? { ...message, content: value.response, status: 'complete' } : message)
        : [...current, { id: assistantId, role: 'assistant', content: value.response, timestamp: new Date(), turnId: value.turn_id ?? undefined, status: 'complete' }])
      if (!connected && value.audio_url) {
        setStatus('SPEAKING')
        await playResponse(backendUrl(value.audio_url), value.response_id ?? undefined)
      } else if (!connected && value.audio_urls?.length) {
        value.audio_urls.forEach((url, index) => streaming.enqueue({ url: backendUrl(url), responseId: value.response_id ?? 'fallback', index }))
      } else if (!value.audio_urls?.length) setStatus('IDLE')
    } catch (error) {
      setMessages((current) => [...current, { id: crypto.randomUUID(), role: 'assistant', content: `Falha no canal local: ${error instanceof Error ? error.message : 'erro desconhecido'}`, timestamp: new Date(), status: 'failed' }])
      setState('concerned')
      setStatus('IDLE')
    } finally { setBusy(false); pendingTurnRequests.current = Math.max(0, pendingTurnRequests.current - 1) }
  }, [connected, health?.llm_ready, playResponse, streaming])

  const handleAudio = useCallback(async (blob: Blob) => {
    setStatus('LISTENING')
    setBusy(true)
    try {
      const body = new FormData()
      body.append('audio', blob, 'nyra-input.webm')
      const response = await fetch('/api/conversation/turn', { method: 'POST', body })
      const value = await response.json()
      if (!response.ok || !value.accepted) throw new Error(value.detail ?? 'Nenhuma fala detectada')
      const text = String(value.transcription?.text ?? '')
      const chat = value.chat as ChatResponse
      if (text) setMessages((current) => [...current, { id: crypto.randomUUID(), role: 'user', content: text, timestamp: new Date() }])
      if (chat?.response) setMessages((current) => current.some((item) => item.id === `assistant-${chat.response_id}`) ? current : [...current, { id: `assistant-${chat.response_id}`, role: 'assistant', content: chat.response, timestamp: new Date() }])
      if (!connected) chat?.audio_urls?.forEach((url, index) => streaming.enqueue({ url: backendUrl(url), responseId: chat.response_id ?? 'ptt', index }))
    } catch (error) {
      setMessages((current) => [...current, { id: crypto.randomUUID(), role: 'assistant', content: error instanceof Error ? error.message : 'Falha no microfone', timestamp: new Date() }])
      setStatus('IDLE')
    } finally { setBusy(false) }
  }, [connected, streaming])

  const { recording, start, stop } = usePushToTalk(handleAudio, microphone)
  const startTalking = useCallback(async () => {
    if (status === 'SPEAKING' && audio.settings.allow_interruption) {
      streaming.clear()
    }
    await fetch('/api/conversation/speech-start', { method: 'POST' }).catch(() => undefined)
    await start()
  }, [audio.settings.allow_interruption, start, status, streaming])
  useEffect(() => { if (recording) setStatus('LISTENING') }, [recording])

  const onAlwaysResult = useCallback(async (value: AlwaysListeningResult) => {
    if (!value.accepted || !value.chat || !value.decision) return
    setMessages((current) => {
      const assistantId = `assistant-${value.chat!.response_id}`
      const next = [...current, { id: crypto.randomUUID(), role: 'user' as const, content: value.decision!.text, timestamp: new Date() }]
      return next.some((item) => item.id === assistantId)
        ? next
        : [...next, { id: assistantId, role: 'assistant' as const, content: value.chat!.response, timestamp: new Date() }]
    })
    setState(value.chat.state as EmotionalState)
    if (!connected && value.chat.audio_url) await playResponse(backendUrl(value.chat.audio_url), value.chat.response_id ?? undefined)
    else if (!connected) value.chat.audio_urls?.forEach((url, index) => streaming.enqueue({ url: backendUrl(url), responseId: value.chat!.response_id ?? 'always', index }))
  }, [connected, playResponse, streaming])
  const always = useAlwaysListening({
    deviceId: microphone,
    suspended: status === 'SPEAKING' && !audio.settings.allow_interruption,
    outputPlaying: status === 'SPEAKING',
    onResult: onAlwaysResult,
    onDeviceSelected: chooseMicrophone,
  })
  useEffect(() => audio.reconcileDevices(always.devices), [always.devices, audio.reconcileDevices])
  useEffect(() => { if (always.listening) setStatus('LISTENING'); else if (always.processing) setStatus('THINKING') }, [always.listening, always.processing])

  const navigate = useCallback((next: OpsView) => {
    setView(next)
    localStorage.setItem('nyra-active-view', next)
    window.location.hash = next
  }, [])
  const toggleCollapse = useCallback(() => {
    setNavCollapsed((current) => {
      localStorage.setItem('nyra-nav-collapsed', String(!current))
      return !current
    })
  }, [])

  const activityFeed = useMemo(
    () => feed.map((item) => ({ id: item.id, label: item.label, at: item.at })),
    [feed],
  )

  const renderContent = () => {
    switch (view) {
      case 'overview': return <OverviewPage activityFeed={activityFeed} />
      case 'conversation':
        return (
          <div className="chat-workspace">
            <ConversationPanel
              messages={messages}
              busy={busy}
              recording={recording || always.listening}
              onSend={send}
              onTalkStart={startTalking}
              onTalkEnd={stop}
              toolActivities={toolActivities}
            />
          </div>
        )
      case 'capabilities': return <CapabilitiesPage />
      case 'autonomy': return <AutonomyPage />
      case 'tasks': return <TasksPage />
      case 'homelab': return <HomelabPage />
      case 'network': return <NetworkPage />
      case 'integrations': return <IntegrationsPage onOpenSentinel={() => navigate('sentinel')} />
      case 'sentinel': return <SentinelPage />
      case 'voice': return <VoicePage />
      case 'usb': return <UsbDevicesPage />
      case 'settings': return <SettingsPageV3 />
      case 'developer': return <DeveloperPage />
      case 'about': return <AboutPage />
      default: return <OverviewPage activityFeed={activityFeed} />
    }
  }

  return (
    <div className="ops-shell">
      <TopStatusBar />
      <div className="ops-main">
        <Sidebar active={view} collapsed={navCollapsed} onNavigate={navigate} onToggleCollapse={toggleCollapse} />
        <div className="ops-content" key={view}>
          {renderContent()}
        </div>
      </div>
    </div>
  )
}
