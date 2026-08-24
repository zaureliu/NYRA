import { useCallback, useEffect, useRef, useState } from 'react'
import { LogicalSize, getCurrentWindow } from '@tauri-apps/api/window'
import { openUrl } from '@tauri-apps/plugin-opener'
import { listen } from '@tauri-apps/api/event'
import { invoke } from '@tauri-apps/api/core'
import { disable as disableAutostart, enable as enableAutostart, isEnabled as isAutostartEnabled } from '@tauri-apps/plugin-autostart'
import { AvatarRenderer } from '../avatar/AvatarRenderer'
import { useVisualSettings } from '../avatar/visualSettings'
import { useAudioLipSync } from '../hooks/useAudioLipSync'
import { useNyraSocket, type NyraEvent } from '../hooks/useNyraSocket'
import { usePushToTalk } from '../hooks/usePushToTalk'
import { useAlwaysListening } from '../hooks/useAlwaysListening'
import { useAudioSettings } from '../hooks/useAudioSettings'
import { useStreamingAudioQueue } from '../hooks/useStreamingAudioQueue'
import { microphoneStatusLabel } from '../hooks/audioDevices'
import { useGlobalCursorFollow } from './useGlobalCursorFollow'
import { TurnFilter, extractTurnId } from '../runtime/turns'
import type { ActivityStatus, AvatarControl, EmotionalState } from '../types'

const API = 'http://127.0.0.1:8000'
const BASE_SIZES = { bust: { width: 480, height: 560 }, full_body: { width: 420, height: 620 } } as const
const applyClickThrough = (enabled: boolean) => invoke('set_click_through', { enabled }).catch(() => getCurrentWindow().setIgnoreCursorEvents(enabled))

export function DesktopApp() {
  const [status, setStatus] = useState<ActivityStatus>('OFFLINE')
  const [state, setState] = useState<EmotionalState>('neutral')
  const [connected, setConnected] = useState(false)
  const [bubble, setBubble] = useState('')
  const [menu, setMenu] = useState(false)
  const [draft, setDraft] = useState('')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [startup, setStartup] = useState(false)
  const [showOnline, setShowOnline] = useState(true)
  const [visual, setVisual] = useVisualSettings()
  const [avatarControl, setAvatarControl] = useState<Partial<AvatarControl>>({})
  const [live2dExternal, setLive2dExternal] = useState(false)
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
  const { mouth, play, stop: stopPlayback } = useAudioLipSync(audio.settings.speaker, sendLive2DMouth, audio.settings.volume)
  const always = useAlwaysListening({
    baseUrl: API,
    deviceId: microphone,
    suspended: status === 'SPEAKING' && !audio.settings.allow_interruption,
    outputPlaying: status === 'SPEAKING',
    onError: (message) => setBubble(message),
    onDeviceSelected: chooseMicrophone,
  })
  useEffect(() => audio.reconcileDevices(always.devices), [always.devices, audio.reconcileDevices])
  useGlobalCursorFollow(live2dExternal, updateGlobalCursorAvailability)

  const playbackGuard = useCallback(async (playing: boolean, responseId?: string) => {
    await fetch(`${API}/api/listening/playback`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ playing, response_id: responseId }) }).catch(() => undefined)
  }, [])

  const streaming = useStreamingAudioQueue(
    play, playbackGuard,
    useCallback((speaking) => setStatus(speaking ? 'SPEAKING' : 'IDLE'), []),
  )
  const turnFilter = useRef(new TurnFilter())
  const pendingTurnRequests = useRef(0)

  const onEvent = useCallback((event: NyraEvent) => {
    const eventTurnId = extractTurnId(event.payload)
    if (event.type === 'USER_TEXT_RECEIVED' && pendingTurnRequests.current > 0) {
      pendingTurnRequests.current -= 1
      turnFilter.current.begin(eventTurnId)
    }
    // Turn isolation: bubble/áudio ignoram eventos tardios de turnos encerrados.
    if (event.type === 'NYRA_RESPONSE' && !turnFilter.current.accept(eventTurnId)) return
    if (event.type === 'TTS_CHUNK_FINISHED' && !turnFilter.current.accept(eventTurnId)) return
    if (event.type === 'TTS_FINISHED' && event.payload.audio_url && !turnFilter.current.accept(eventTurnId)) return
    if (event.type === 'NYRA_RESPONSE' && visual.speechBubble) {
      const value = String(event.payload.display_text ?? event.payload.text ?? '')
      setBubble(value.length > 220 ? `${value.slice(0, 217)}…` : value)
    }
    if (event.type === 'TTS_FINISHED' && event.payload.audio_url) {
      setStatus('SPEAKING')
      void playbackGuard(true).then(() => play(`${API}${event.payload.audio_url}`, () => { setStatus('IDLE'); void playbackGuard(false) })).catch(() => { setStatus('IDLE'); void playbackGuard(false) })
    }
    if (event.type === 'TTS_CHUNK_FINISHED' && event.payload.audio_url) {
      if (visual.speechBubble && event.payload.display_text) setBubble(String(event.payload.display_text))
      streaming.enqueue({
        url: `${API}${String(event.payload.audio_url)}`,
        responseId: String(event.payload.response_id ?? 'stream'), index: Number(event.payload.index ?? 0),
      })
    }
    if (event.type === 'AVATAR_STATE_CHANGED') setAvatarControl(event.payload as Partial<AvatarControl>)
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
    if (event.type === 'NETWORK_ALERT' && event.payload.desktop !== false) {
      setBubble(String(event.payload.message ?? 'Mudança detectada na conexão.'))
    }
    if (event.type === 'SENTINEL_ALERT' && event.payload.desktop !== false) {
      setBubble(String(event.payload.display_text ?? event.payload.message ?? 'Novo alerta do Sentinel.'))
    }
    if (event.type === 'SENTINEL_STATUS_CHANGED') {
      const next = String(event.payload.state ?? '')
      if (next === 'CONNECTED') setBubble('Sentinel connected')
      if (next === 'AUTH_FAILED') setBubble('Sentinel encontrado · token rejeitado')
      if (next === 'INCOMPATIBLE') setBubble('Sentinel encontrado · protocolo incompatível')
    }
    if (event.type === 'SPEECH_CANCELLED') {
      stopPlayback(); streaming.clear(); setStatus('LISTENING'); void playbackGuard(false)
    }
  }, [play, playbackGuard, stopPlayback, streaming, visual.speechBubble])

  useNyraSocket({
    setStatus: useCallback(setStatus, []),
    setState: useCallback(setState, []),
    setConnected: useCallback(setConnected, []),
    url: 'ws://127.0.0.1:8000/api/ws',
    onEvent,
  })

  const handleAudio = useCallback(async (blob: Blob) => {
    const body = new FormData()
    body.append('audio', blob, 'desktop-ptt.webm')
    const response = await fetch(`${API}/api/conversation/turn`, { method: 'POST', body })
    const value = await response.json()
    if (!response.ok || !value.accepted) throw new Error(value.detail ?? 'Nenhuma fala detectada')
  }, [])

  const { recording, start, stop } = usePushToTalk(handleAudio, microphone, .018, 1400, () => setStatus('IDLE'))
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
    const base = BASE_SIZES[visual.characterView]
    void windowHandle.setSize(new LogicalSize(base.width * visual.overlayScale, base.height * visual.overlayScale)).catch(() => undefined)
    void isAutostartEnabled().then(setStartup).catch(() => setStartup(false))
  }, [visual.alwaysOnTop, visual.characterView, visual.clickThrough, visual.overlayScale])

  useEffect(() => {
    const poll=()=>void fetch(`${API}/api/live2d/settings`).then(r=>r.json()).then(value=>setLive2dExternal(Boolean(value.model_loaded)&&value.config?.renderer!=='CURRENT')).catch(()=>setLive2dExternal(false))
    poll();const timer=window.setInterval(poll,5000);return()=>window.clearInterval(timer)
  },[])

  useEffect(() => {
    let unlisten = () => {}
    void listen<string>('nyra-desktop', (event) => {
      if (event.payload === 'talk-menu') setMenu(true)
      if (event.payload === 'settings') setSettingsOpen(true)
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
    await fetch(`${API}/api/chat`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: text, synthesize: true }) }).catch(() => setStatus('OFFLINE')).finally(() => { pendingTurnRequests.current = Math.max(0, pendingTurnRequests.current - 1) })
  }

  return <main className={`desktop-presence state-${state} status-${status.toLowerCase()}`} data-transparent="true" data-live2d={live2dExternal} data-global-cursor={globalCursorAvailable ? 'available' : 'fallback'}>
    {bubble && visual.speechBubble && <aside className="speech-bubble"><strong>NYRA</strong>{bubble}</aside>}
    <button className="drag-region" aria-label="Arrastar NYRA" onPointerDown={() => void getCurrentWindow().startDragging()} />
    <button className="avatar-button" aria-label="Abrir conversa com NYRA" onClick={() => setMenu((value) => !value)}>
      {!live2dExternal&&<AvatarRenderer
        state={state} status={recording ? 'LISTENING' : status} mouth={mouth} variant="desktop"
        avatarVersion={visual.avatarVersion} renderer={visual.renderer}
        characterView={visual.characterView}
        idleAnimations={visual.idleAnimations} eyeMovement={visual.eyeMovement}
        blink={visual.blink} debug={visual.debug}
        control={avatarControl}
        pointerSource="desktop-global"
      />}
    </button>
    {(!connected || showOnline) && <div className={`presence-status ${connected ? 'online' : 'offline'}`}><i/>{connected ? 'ONLINE' : 'OFFLINE'}</div>}
    {(always.config?.privacy_indicator ?? true) && <div className={`mic-indicator ${always.status?.muted || !always.micActive ? 'off' : always.processing ? 'processing' : always.listening ? 'listening' : 'on'}`}><i/>MIC {always.status?.muted ? 'MUTED' : always.micActive ? (always.processing ? 'PROCESSING' : always.listening ? 'LISTENING' : 'ON') : microphoneStatusLabel(always.microphoneAvailability, false)}</div>}
    {menu && <section className="context-card"><textarea autoFocus value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() } }} placeholder="Digite para a NYRA..."/><div><button onClick={() => void send()}>ENVIAR</button><button onClick={() => void invoke('open_dashboard')}>PAINEL</button><button onClick={() => void getCurrentWindow().hide()}>OCULTAR</button></div></section>}
    {settingsOpen && <section className="context-card desktop-settings"><strong>DESKTOP PRESENCE · AVATAR V2</strong>
      <label>Escala<select value={visual.overlayScale} onChange={(e) => setVisual({ ...visual, overlayScale: Number(e.target.value) })}>{[.5,.75,1,1.25,1.5].map((value) => <option key={value} value={value}>{Math.round(value*100)}%</option>)}</select></label>
      <label>Character View<select value="bust" disabled><option value="bust">Avatar V2 / Portrait</option></select></label>
      <label><input type="checkbox" checked={visual.alwaysOnTop} onChange={(e) => setVisual({ ...visual, alwaysOnTop: e.target.checked })}/> Always on top</label>
      <label><input type="checkbox" checked={visual.speechBubble} onChange={(e) => setVisual({ ...visual, speechBubble: e.target.checked })}/> Balão de fala</label>
      <label><input type="checkbox" checked={visual.idleAnimations} onChange={(e) => setVisual({ ...visual, idleAnimations: e.target.checked })}/> Animações idle</label>
      <label><input type="checkbox" checked={visual.eyeMovement} onChange={(e) => setVisual({ ...visual, eyeMovement: e.target.checked })}/> Movimento dos olhos</label>
      <label><input type="checkbox" checked={visual.blink} onChange={(e) => setVisual({ ...visual, blink: e.target.checked })}/> Blink</label>
      <label><input type="checkbox" checked={visual.debug} onChange={(e) => setVisual({ ...visual, debug: e.target.checked })}/> Visual Debug</label>
      <label><input type="checkbox" checked={startup} onChange={() => void toggleStartup()}/> Iniciar com o Windows</label>
      <div><button onClick={() => void clickThrough()}>CLICK-THROUGH</button><button onClick={() => setSettingsOpen(false)}>FECHAR</button></div>
      <small>Ctrl+Shift+I; fallback Ctrl+Alt+I. O tray sempre recupera o modo interativo.</small>
    </section>}
  </main>
}
