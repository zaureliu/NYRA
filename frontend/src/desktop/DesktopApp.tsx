import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react'
import { LogicalSize, currentMonitor, getCurrentWindow } from '@tauri-apps/api/window'
import { openUrl } from '@tauri-apps/plugin-opener'
import { listen } from '@tauri-apps/api/event'
import { invoke } from '@tauri-apps/api/core'
import { disable as disableAutostart, enable as enableAutostart, isEnabled as isAutostartEnabled } from '@tauri-apps/plugin-autostart'
import { usePresenceSettings } from './presenceSettings'
import { useAudioLipSync } from '../hooks/useAudioLipSync'
import { useNyraSocket, type NyraEvent } from '../hooks/useNyraSocket'
import { usePushToTalk } from '../hooks/usePushToTalk'
import type { STTResult } from '../runtime/stt'
import { useAlwaysListening } from '../hooks/useAlwaysListening'
import { useAudioSettings } from '../hooks/useAudioSettings'
import { useStreamingAudioQueue, type PlaybackAck } from '../hooks/useStreamingAudioQueue'
import { microphoneStatusLabel } from '../hooks/audioDevices'
import { useGlobalCursorFollow } from './useGlobalCursorFollow'
import { TurnFilter, adoptInputTurn, extractTurnId } from '../runtime/turns'
import { sendChat } from '../runtime/conversation'
import { backendPresenceReport, hasActiveVtsCharacter, nativePresenceConfig, type MouseTrackingMode, type NativePresenceStatus, type VtsBackendStatus } from './vtsPresence'
import { computePresenceMenuLayout, PRESENCE_MENU_LIMITS, type PresenceMenuLayout } from './presenceMenu'
import type { ActivityStatus, EmotionalState } from '../types'
import { readProactivePresenceNotice } from '../runtime/proactivePresence'

const API = 'http://127.0.0.1:8000'
const BASE_SIZE = { width: 480, height: 560 } as const
const applyClickThrough = (enabled: boolean) => invoke('set_click_through', { enabled }).catch(() => getCurrentWindow().setIgnoreCursorEvents(enabled))

export function DesktopApp() {
  const [status, setStatus] = useState<ActivityStatus>('OFFLINE')
  const [state, setState] = useState<EmotionalState>('neutral')
  const [connected, setConnected] = useState(false)
  const [bubble, setBubble] = useState('')
  const [menu, setMenu] = useState(false)
  const [draft, setDraft] = useState('')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsLayout, setSettingsLayout] = useState<PresenceMenuLayout | null>(null)
  const settingsRef = useRef<HTMLElement | null>(null)
  const [startup, setStartup] = useState(false)
  const [showOnline, setShowOnline] = useState(true)
  const [visual, setVisual] = usePresenceSettings()
  const [live2dExternal, setLive2dExternal] = useState(false)
  const [mouseTracking, setMouseTracking] = useState<MouseTrackingMode>('HEAD_EYES')
  const [globalCursorAvailable, setGlobalCursorAvailable] = useState(true)
  const audio = useAudioSettings(API)
  const microphone = audio.settings.microphone
  const lipSyncSent = useRef(0)
  const chooseMicrophone = useCallback((id: string) => { void audio.patch({ microphone: id }) }, [audio.patch])
  const updateGlobalCursorAvailability = useCallback((available: boolean) => setGlobalCursorAvailable(available), [])
  const sendLive2DMouth = useCallback((value: number) => {
    const now=performance.now(); if(value&&now-lipSyncSent.current<50)return; lipSyncSent.current=now
    void fetch(`${API}/api/live2d/lip-sync`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({value})}).catch(()=>undefined)
  },[])
  const { play, stop: stopPlayback } = useAudioLipSync(audio.settings.speaker, sendLive2DMouth, audio.settings.volume)
  const always = useAlwaysListening({
    baseUrl: API,
    deviceId: microphone,
    suspended: status === 'SPEAKING' && !audio.settings.allow_interruption,
    outputPlaying: status === 'SPEAKING',
    onError: (message) => setBubble(message),
    onDeviceSelected: chooseMicrophone,
  })
  useEffect(() => audio.reconcileDevices(always.devices), [always.devices, audio.reconcileDevices])
  useGlobalCursorFollow(live2dExternal, mouseTracking, updateGlobalCursorAvailability)

  const playbackGuard = useCallback(async (playing: boolean, responseId?: string, ack?: PlaybackAck) => {
    await fetch(`${API}/api/listening/playback`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ playing, response_id: responseId, ...ack }) }).catch(() => undefined)
  }, [])

  const streaming = useStreamingAudioQueue(
    play, playbackGuard,
    useCallback((speaking) => setStatus(speaking ? 'SPEAKING' : 'IDLE'), []),
    stopPlayback,
  )
  const turnFilter = useRef(new TurnFilter())
  const pendingTurnRequests = useRef(0)

  const onEvent = useCallback((event: NyraEvent) => {
    const eventTurnId = extractTurnId(event.payload)
    if (adoptInputTurn(turnFilter.current, event.type, eventTurnId)) {
      pendingTurnRequests.current = Math.max(0, pendingTurnRequests.current - 1)
    }
    // Turn isolation: bubble/áudio ignoram eventos tardios de turnos encerrados.
    if (event.type === 'NYRA_RESPONSE' && !turnFilter.current.accept(eventTurnId)) return
    if (event.type === 'TTS_CHUNK_FINISHED' && !turnFilter.current.accept(eventTurnId)) return
    if (event.type === 'TTS_PCM_CHUNK') {
      if (!turnFilter.current.accept(eventTurnId)) return
      streaming.enqueue({url: `pcm16:${Number(event.payload.sample_rate)}:${String(event.payload.pcm ?? '')}`,
        responseId: String(event.payload.response_id), index: Number(event.payload.index) * 1000 + Number(event.payload.packet_index),
        sentenceIndex: Number(event.payload.index), final: Boolean(event.payload.final)})
    }
    if (event.type === 'TTS_FINISHED' && event.payload.audio_url && event.payload.source !== 'proactive_presence' && !turnFilter.current.accept(eventTurnId)) return
    if (event.type === 'NYRA_RESPONSE' && visual.speechBubble) {
      const value = String(event.payload.display_text ?? event.payload.text ?? '')
      setBubble(value.length > 220 ? `${value.slice(0, 217)}…` : value)
    }
    if (event.type === 'TTS_FINISHED' && event.payload.audio_url) {
      const responseId = String(event.payload.response_id ?? '') || undefined
      streaming.enqueue({url: `${API}${event.payload.audio_url}`, responseId: responseId ?? 'notification', index: 0})
    }
    if (event.type === 'TTS_CHUNK_FINISHED' && event.payload.audio_url) {
      if (visual.speechBubble && event.payload.display_text) setBubble(String(event.payload.display_text))
      streaming.enqueue({
        url: `${API}${String(event.payload.audio_url)}`,
        responseId: String(event.payload.response_id ?? 'stream'), index: Number(event.payload.index ?? 0),
      })
    }
    if (event.type === 'SHELL_EXECUTION_STARTED') {
      setStatus('THINKING')
      if (visual.speechBubble) setBubble(`Executando diagnóstico local\n${String(event.payload.command ?? '')}`.slice(0, 220))
    }
    if (event.type === 'SHELL_APPROVAL_REQUIRED' && visual.speechBubble) {
      setBubble('Esta operação precisa de autorização explícita antes de executar.')
    }
    if (event.type === 'REMOTE_SHELL_EXECUTION_STARTED') {
      setStatus('THINKING')
      if (visual.speechBubble) setBubble(`Investigando ${String(event.payload.host ?? 'host confiável')}\n${String(event.payload.command ?? '')}`.slice(0, 220))
    }
    if (event.type === 'REMOTE_SHELL_APPROVAL_REQUIRED' && visual.speechBubble) {
      setBubble(`A ação remota em ${String(event.payload.host ?? 'host')} precisa de autorização explícita.`)
    }
    if (event.type === 'AGENT_RUN_STATE_CHANGED' && visual.speechBubble) {
      const labels: Record<string, string> = { OBSERVE: 'Coletando evidências', DIAGNOSE: 'Analisando diagnóstico', PLAN: 'Preparando ação mínima', ACT: 'Executando ação controlada', VERIFY: 'Verificando resultado', WAITING_APPROVAL: 'Aguardando autorização' }
      setBubble(labels[String(event.payload.state ?? '')] ?? 'Investigação em andamento')
    }
    if (event.type === 'UI_COMMAND') {
      const command = String(event.payload.command ?? '')
      if (command === 'open_dashboard') void invoke('open_dashboard')
      if (command === 'show' || command === 'presence_show') void invoke('presence_show')
      if (command === 'hide' || command === 'presence_hide') void invoke('presence_hide')
      if (command === 'presence_toggle') void invoke('presence_toggle')
      if (command === 'presence_status') void invoke('presence_status')
    }
    const proactiveNotice = readProactivePresenceNotice(event)
    if (proactiveNotice) {
      if (proactiveNotice.channels.includes('ui') && visual.speechBubble) {
        setBubble(proactiveNotice.message.slice(0, 220))
      }
    }
    if (event.type === 'SPEECH_CANCELLED') {
      streaming.clear(String(event.payload.response_id ?? '') || undefined); setStatus('LISTENING')
    }
  }, [play, playbackGuard, stopPlayback, streaming, visual.speechBubble])

  useNyraSocket({
    setStatus: useCallback(setStatus, []),
    setState: useCallback(setState, []),
    setConnected: useCallback(setConnected, []),
    url: 'ws://127.0.0.1:8000/api/ws',
    onEvent,
  })

  const handleAudio = useCallback(async (value: STTResult) => {
    if (!value.accepted) throw new Error(value.reason ?? 'Nenhuma fala detectada')
  }, [])

  const { start, stop } = usePushToTalk(handleAudio, microphone, .018, 1400, () => setStatus('IDLE'))
  const startTalking = useCallback(async () => {
    if (status === 'SPEAKING' && audio.settings.allow_interruption) {
      stopPlayback(); streaming.clear()
    }
    await fetch(`${API}/api/conversation/speech-start`, { method: 'POST' }).catch(() => undefined)
    await start()
  }, [audio.settings.allow_interruption, start, status, stopPlayback, streaming])

  useEffect(() => {
    if (!bubble) return
    const timer = window.setTimeout(() => setBubble(''), 9000)
    return () => clearTimeout(timer)
  }, [bubble])

  useEffect(() => {
    if (!connected) { setShowOnline(true); return }
    setShowOnline(true)
    const timer = window.setTimeout(() => setShowOnline(false), 4000)
    return () => clearTimeout(timer)
  }, [connected])

  useEffect(() => {
    const windowHandle = getCurrentWindow()
    void windowHandle.setAlwaysOnTop(visual.alwaysOnTop).catch(() => undefined)
    void applyClickThrough(visual.clickThrough).catch(() => undefined)
    void windowHandle.setSize(new LogicalSize(BASE_SIZE.width * visual.overlayScale, BASE_SIZE.height * visual.overlayScale)).catch(() => undefined)
    void isAutostartEnabled().then(setStartup).catch(() => setStartup(false))
  }, [visual.alwaysOnTop, visual.clickThrough, visual.overlayScale])

  const updateSettingsLayout = useCallback(async () => {
    const element = settingsRef.current
    if (!element) return
    const windowHandle = getCurrentWindow()
    const [position, size, monitor, scaleFactor] = await Promise.all([
      windowHandle.outerPosition(),
      windowHandle.outerSize(),
      currentMonitor(),
      windowHandle.scaleFactor(),
    ])
    if (!monitor) return
    const next = computePresenceMenuLayout({
      windowRect: { x: position.x, y: position.y, width: size.width, height: size.height },
      workArea: {
        x: monitor.workArea.position.x,
        y: monitor.workArea.position.y,
        width: monitor.workArea.size.width,
        height: monitor.workArea.size.height,
      },
      viewport: { width: window.innerWidth, height: window.innerHeight },
      desiredMenu: {
        width: Math.max(PRESENCE_MENU_LIMITS.minWidth, element.scrollWidth),
        height: Math.max(PRESENCE_MENU_LIMITS.minHeight, element.scrollHeight),
      },
      scaleFactor,
    })
    setSettingsLayout((current) => current
      && current.x === next.x && current.y === next.y
      && current.width === next.width && current.maxHeight === next.maxHeight
      && current.horizontal === next.horizontal && current.vertical === next.vertical
      ? current
      : next)
  }, [])

  useEffect(() => {
    if (!settingsOpen) { setSettingsLayout(null); return }
    const windowHandle = getCurrentWindow()
    let disposed = false
    let frame = 0
    let unlistenMoved = () => {}
    let unlistenResized = () => {}
    const schedule = () => {
      if (disposed) return
      window.cancelAnimationFrame(frame)
      frame = window.requestAnimationFrame(() => void updateSettingsLayout().catch(() => undefined))
    }
    const observer = new ResizeObserver(schedule)
    if (settingsRef.current) observer.observe(settingsRef.current)
    void Promise.all([
      windowHandle.onMoved(schedule),
      windowHandle.onResized(schedule),
    ]).then(([moved, resized]) => {
      if (disposed) { moved(); resized(); return }
      unlistenMoved = moved
      unlistenResized = resized
    }).catch(() => undefined)
    window.addEventListener('resize', schedule)
    schedule()
    return () => {
      disposed = true
      window.cancelAnimationFrame(frame)
      observer.disconnect()
      window.removeEventListener('resize', schedule)
      unlistenMoved()
      unlistenResized()
    }
  }, [settingsOpen, updateSettingsLayout])

  useEffect(() => {
    let disposed = false
    const configure = async () => {
      try {
        const response = await fetch(`${API}/api/live2d/settings`)
        const value = await response.json() as VtsBackendStatus
        if (!disposed) {
          setMouseTracking(value.config.mouse_tracking ?? 'HEAD_EYES')
          await invoke('vts_presence_configure', { config: nativePresenceConfig(value) })
        }
      } catch { if (!disposed) setLive2dExternal(false) }
    }
    void configure()
    const timer = window.setInterval(() => void configure(), 2500)
    return () => { disposed = true; window.clearInterval(timer) }
  }, [])

  useEffect(() => {
    let disposed = false
    let lastReport = ''
    let reportInFlight = false
    const read = async () => {
      try {
        const value = await invoke<NativePresenceStatus>('vts_presence_status')
        if (disposed) return
        setLive2dExternal(hasActiveVtsCharacter(value))
        const report = backendPresenceReport(value)
        const serialized = JSON.stringify(report)
        if (serialized !== lastReport && !reportInFlight) {
          reportInFlight = true
          void fetch(`${API}/api/live2d/presence-status`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: serialized,
          }).then((response) => {
            if (response.ok) lastReport = serialized
          }).catch(() => undefined).finally(() => { reportInFlight = false })
        }
      } catch { if (!disposed) setLive2dExternal(false) }
    }
    void read()
    const timer = window.setInterval(() => void read(), 500)
    return () => { disposed = true; window.clearInterval(timer) }
  }, [])

  useEffect(() => {
    let unlisten = () => {}
    void listen<string>('nyra-desktop', (event) => {
      if (event.payload === 'talk-menu') setMenu(true)
      if (event.payload === 'settings') { setMenu(false); setSettingsOpen(true) }
      if (event.payload === 'talk-start') void startTalking()
      if (event.payload === 'talk-stop') stop()
      if (event.payload === 'interactive') setVisual({ ...visual, clickThrough: false })
      if (event.payload === 'click-through') setVisual({ ...visual, clickThrough: true })
      if (event.payload === 'reconnect') location.reload()
      if (event.payload === 'mic-toggle') void fetch(`${API}/api/listening/status`).then((response) => response.json()).then((value) => fetch(`${API}/api/listening/${value.muted ? 'unmute' : 'mute'}`, { method: 'POST' }))
      if (event.payload === 'listening-toggle') void fetch(`${API}/api/listening/status`).then((response) => response.json()).then((value) => fetch(`${API}/api/listening/${value.enabled ? 'stop' : 'start'}`, { method: 'POST' }))
      if (event.payload === 'network-toggle') void fetch(`${API}/api/network-watch/status`).then((response) => response.json()).then((value) => fetch(`${API}/api/network-watch/${value.enabled ? 'stop' : 'start'}`, { method: 'POST' }))
      if (event.payload === 'sentinel-toggle') void fetch(`${API}/api/sentinel-watch/status`).then((response) => response.json()).then((value) => fetch(`${API}/api/sentinel-watch/${value.enabled ? 'stop' : 'start'}`, { method: 'POST' }))
      if (event.payload === 'sentinel-reconnect') void fetch(`${API}/api/sentinel-watch/reconnect`, { method: 'POST' })
      if (event.payload === 'sentinel-open') void fetch(`${API}/api/sentinel-watch/status`).then((response) => response.json()).then((value) => { if (value.host) return openUrl(value.host) })
      if (event.payload === 'quiet-toggle') void fetch(`${API}/api/network-watch/settings`).then((response) => response.json()).then((value) => fetch(`${API}/api/network-watch/settings`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...value.settings, quiet_mode: !value.settings.quiet_mode }) }))
    }).then((dispose) => { unlisten = dispose })
    return () => unlisten()
  }, [setVisual, startTalking, stop, visual])

  const toggleStartup = async () => {
    const next = !startup
    if (next) await enableAutostart(); else await disableAutostart()
    setStartup(next)
  }
  const clickThrough = async () => {
    setMenu(false); setSettingsOpen(false)
    setVisual({ ...visual, clickThrough: true })
    await applyClickThrough(true)
  }
  const send = async () => {
    const text = draft.trim()
    if (!text) return
    setDraft(''); setMenu(false); setStatus('THINKING')
    pendingTurnRequests.current += 1
    await sendChat({ message: text, synthesize: true }).catch(() => setStatus('OFFLINE')).finally(() => { pendingTurnRequests.current = Math.max(0, pendingTurnRequests.current - 1) })
  }

  const settingsStyle = settingsLayout ? {
    left: `${settingsLayout.x}px`,
    top: `${settingsLayout.y}px`,
    right: 'auto',
    bottom: 'auto',
    width: `${settingsLayout.width}px`,
    maxHeight: `${settingsLayout.maxHeight}px`,
  } as CSSProperties : undefined

  return <main className={`desktop-presence state-${state} status-${status.toLowerCase()}`} data-transparent="true" data-live2d={live2dExternal} data-global-cursor={globalCursorAvailable ? 'available' : 'unavailable'}>
    {bubble && visual.speechBubble && <aside className="speech-bubble"><strong>NYRA</strong>{bubble}</aside>}
    <button className="drag-region" aria-label="Arrastar NYRA" onPointerDown={() => void getCurrentWindow().startDragging()} />
    <button className="avatar-button" aria-label="Abrir conversa com NYRA" onClick={() => { setSettingsOpen(false); setMenu((value) => !value) }} />
    {(!connected || showOnline) && <div className={`presence-status ${connected ? 'online' : 'offline'}`}><i/>{connected ? 'ONLINE' : 'OFFLINE'}</div>}
    {(always.config?.privacy_indicator ?? true) && <div className={`mic-indicator ${always.status?.muted || !always.micActive ? 'off' : always.processing ? 'processing' : always.listening ? 'listening' : 'on'}`}><i/>MIC {always.status?.muted ? 'MUTED' : always.micActive ? (always.processing ? 'PROCESSING' : always.listening ? 'LISTENING' : 'ON') : microphoneStatusLabel(always.microphoneAvailability, false)}</div>}
    {menu && <section className="context-card"><textarea autoFocus value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() } }} placeholder="Digite para a NYRA..."/><div><button onClick={() => void send()}>ENVIAR</button><button onClick={() => void invoke('open_dashboard')}>PAINEL</button><button onClick={() => void getCurrentWindow().hide()}>OCULTAR</button></div></section>}
    {settingsOpen && <section ref={settingsRef} className="context-card desktop-settings" style={settingsStyle} data-horizontal={settingsLayout?.horizontal ?? 'start'} data-vertical={settingsLayout?.vertical ?? 'end'}><strong>DESKTOP PRESENCE · VTUBE STUDIO</strong>
      <label>Escala<select value={visual.overlayScale} onChange={(e) => setVisual({ ...visual, overlayScale: Number(e.target.value) })}>{[.5,.75,1,1.25,1.5].map((value) => <option key={value} value={value}>{Math.round(value*100)}%</option>)}</select></label>
      <label>Modelo<select value="current" disabled><option value="current">Modelo atual do VTube Studio</option></select></label>
      <label>Mouse Tracking<select value={mouseTracking} disabled><option value="OFF">Off</option><option value="EYES">Eyes</option><option value="HEAD_EYES">Head + Eyes</option></select></label>
      <label><input type="checkbox" checked={visual.alwaysOnTop} onChange={(e) => setVisual({ ...visual, alwaysOnTop: e.target.checked })}/> Always on top</label>
      <label><input type="checkbox" checked={visual.speechBubble} onChange={(e) => setVisual({ ...visual, speechBubble: e.target.checked })}/> Balão de fala</label>
      <label><input type="checkbox" checked={startup} onChange={() => void toggleStartup()}/> Iniciar com o Windows</label>
      <div><button onClick={() => void clickThrough()}>CLICK-THROUGH</button><button onClick={() => setSettingsOpen(false)}>FECHAR</button></div>
      <small>Ctrl+Shift+I; fallback Ctrl+Alt+I. O tray sempre recupera o modo interativo.</small>
    </section>}
  </main>
}
