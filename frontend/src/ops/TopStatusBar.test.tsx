import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const polling = vi.hoisted(() => ({
  states: new Map<string, { data: unknown; loading: boolean; error: string; refresh: () => void }>(),
}))

vi.mock('./hooks', () => ({
  usePolling: (path: string) => polling.states.get(path) ?? {
    data: null,
    loading: true,
    error: '',
    refresh: () => {},
  },
}))

import { TopStatusBar } from './TopStatusBar'

function setPolling(path: string, data: unknown, error = '') {
  polling.states.set(path, { data, loading: false, error, refresh: () => {} })
}

describe('TopStatusBar', () => {
  beforeEach(() => {
    polling.states.clear()
    setPolling('/api/tasks?limit=10', { tasks: [] })
  })

  it('usa health e readiness reais sem confundir WebSocket ou modelo default com ativo', () => {
    setPolling('/api/health', {
      status: 'online',
      character: 'NYRA',
      llm: true,
      llm_ready: true,
      ollama: { state: 'OLLAMA_READY', ready: true, model: 'qwen3.5:9b', keep_alive: '1h' },
      memory: true,
      stt: true,
      tts: true,
      model: 'qwen3:8b',
    })
    setPolling('/api/ollama/readiness', {
      state: 'OLLAMA_READY',
      ready: true,
      model: 'qwen3.5:9b',
    })
    setPolling('/api/watchdog/status', {
      success: true,
      running: true,
      stale: false,
      heartbeat_age_seconds: 1.2,
    })
    setPolling('/api/selfdev/status', {
      state: 'READY',
      unread_notifications: 2,
    })

    const html = renderToStaticMarkup(<TopStatusBar />)

    expect(html).toContain('NYRA: ONLINE')
    expect(html).toContain('Backend: ONLINE')
    expect(html).toContain('Voz: PRONTA')
    expect(html).toContain('Watchdog: ATIVO')
    expect(html).toContain('Self-Dev: READY')
    expect(html).toContain('READY')
    expect(html).toContain('qwen3.5:9b')
    expect(html).toContain('modelo ativo/residente: qwen3.5:9b')
    expect(html).toContain('modelo configurado/default: qwen3:8b')
    expect(html).not.toContain('UNKNOWN')
    expect(html).not.toContain('warmup')
  })

  it('preserva os estados conhecidos de Watchdog e Self-Dev em vez de inferi-los pelo health', () => {
    setPolling('/api/health', {
      status: 'online',
      character: 'NYRA',
      llm: true,
      llm_ready: true,
      ollama: { state: 'OLLAMA_READY', ready: true, model: 'qwen3.5:9b', keep_alive: '1h' },
      memory: true,
      stt: true,
      tts: false,
    })
    setPolling('/api/ollama/readiness', { state: 'OLLAMA_READY', ready: true, model: 'qwen3.5:9b' })
    setPolling('/api/watchdog/status', { success: true, running: false })
    setPolling('/api/selfdev/status', { state: 'OFF', unread_notifications: 0 })

    const html = renderToStaticMarkup(<TopStatusBar />)

    expect(html).toContain('NYRA: ONLINE')
    expect(html).toContain('Voz: PARCIAL')
    expect(html).toContain('Watchdog: INATIVO')
    expect(html).toContain('Self-Dev: OFF')
    expect(html).not.toContain('UNKNOWN')
  })

  it('não conserva snapshot stale quando os endpoints deixam de responder', () => {
    setPolling('/api/health', null, 'Failed to fetch')
    setPolling('/api/ollama/readiness', null, 'Failed to fetch')
    setPolling('/api/watchdog/status', null, 'Failed to fetch')
    setPolling('/api/selfdev/status', null, 'Failed to fetch')

    const html = renderToStaticMarkup(<TopStatusBar />)

    expect(html).toContain('NYRA: OFFLINE')
    expect(html).toContain('Backend: OFFLINE')
    expect(html).toContain('Watchdog: ERROR')
    expect(html).toContain('Self-Dev: ERROR')
    expect(html).not.toContain('warmup')
  })
})
