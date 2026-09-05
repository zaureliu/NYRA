import { useEffect, useRef } from 'react'
import { listen } from '@tauri-apps/api/event'
import { invoke } from '@tauri-apps/api/core'
import type { ActivityStatus, EmotionalState } from '../types'
import { isTauriRuntime } from '../runtime/backend'

export interface KazumiEvent { type: string; payload: Record<string, unknown> }
interface SocketHandlers {
  setStatus: (status: ActivityStatus) => void
  setState: (state: EmotionalState) => void
  setConnected: (connected: boolean) => void
  url?: string
  onEvent?: (event: KazumiEvent) => void
}

export const reconnectDelay = (attempt: number) => Math.min(30000, 1000 * (2 ** Math.min(attempt, 5)))

export function useKazumiSocket({ setStatus, setState, setConnected, url, onEvent }: SocketHandlers) {
  const reconnectRef = useRef<number | undefined>(undefined)
  const eventRef = useRef(onEvent); eventRef.current = onEvent
  useEffect(() => {
    const handleEvent = (event: KazumiEvent) => {
      if (event.type === 'USER_SPEECH_RECEIVED') setStatus('LISTENING')
      if (event.type === 'USER_SPEECH_STARTED' || event.type === 'USER_SPEECH_PARTIAL') setStatus('LISTENING')
      if (event.type === 'USER_SPEECH_FINAL' || event.type === 'STT_STARTED') setStatus('TRANSCRIBING')
      if (event.type === 'LLM_PROCESSING') setStatus('THINKING')
      if (event.type === 'LLM_STREAM_STARTED') setStatus('THINKING')
      if (event.type === 'SHELL_EXECUTION_STARTED') setStatus('TOOL_EXECUTION')
      if (event.type === 'REMOTE_SHELL_EXECUTION_STARTED' || event.type === 'AGENT_RUN_STARTED' || event.type === 'AGENT_RUN_STATE_CHANGED') setStatus('TOOL_EXECUTION')
      if (event.type === 'TTS_STARTED' && !event.payload.streaming) setStatus('SPEAKING')
      if (event.type === 'TTS_FINISHED' && !event.payload.streaming) setStatus('IDLE')
      if (event.type === 'USER_INTERRUPTED') setStatus('INTERRUPTED')
      if (event.type === 'REALTIME_STATUS_CHANGED' && event.payload.status && event.payload.status !== 'IDLE') setStatus(event.payload.status as ActivityStatus)
      if (event.type === 'CONVERSATION_STATE_CHANGED' && event.payload.state) setStatus(event.payload.state as ActivityStatus)
      if (event.type === 'STATE_CHANGED') setState(event.payload.current as EmotionalState)
      if (event.type === 'KAZUMI_EMOTION_CHANGED' && event.payload.emotion) setState(event.payload.emotion as EmotionalState)
      if (event.type === 'KAZUMI_RESPONSE' && event.payload.state) setState(event.payload.state as EmotionalState)
      eventRef.current?.(event)
    }

    if (isTauriRuntime()) {
      let stopped = false
      let disposeEvent = () => {}
      let disposeConnection = () => {}
      const setup = async () => {
        const disposers = await Promise.all([
          listen<KazumiEvent>('kazumi-backend-event', (event) => handleEvent(event.payload)),
          listen<boolean>('kazumi-backend-connection', (event) => {
            if (stopped) return
            setConnected(event.payload)
            setStatus(event.payload ? 'IDLE' : 'OFFLINE')
          }),
        ])
        if (stopped) {
          disposers.forEach((dispose) => dispose())
          return
        }
        ;[disposeEvent, disposeConnection] = disposers
        const connected = await invoke<boolean>('start_conversation_bridge')
        if (!stopped && connected) {
          setConnected(true)
          setStatus('IDLE')
        }
      }
      void setup().catch(() => {
        if (!stopped) {
          setConnected(false)
          setStatus('OFFLINE')
        }
      })
      return () => {
        stopped = true
        disposeEvent()
        disposeConnection()
      }
    }

    let socket: WebSocket | undefined; let stopped = false; let attempt = 0
    const connect = () => {
      const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(url ?? `${protocol}://${location.host}/api/ws`)
      socket.onopen = () => { attempt = 0; setConnected(true); setStatus('IDLE') }
      socket.onclose = () => { setConnected(false); setStatus('OFFLINE'); if (!stopped) reconnectRef.current = window.setTimeout(connect, reconnectDelay(attempt++)) }
      socket.onerror = () => socket?.close()
      socket.onmessage = (message) => {
        try {
          handleEvent(JSON.parse(message.data) as KazumiEvent)
        } catch { /* eventos inválidos não derrubam a presença */ }
      }
    }
    connect()
    return () => { stopped = true; if (reconnectRef.current) clearTimeout(reconnectRef.current); socket?.close() }
  }, [setConnected, setState, setStatus, url])
}
