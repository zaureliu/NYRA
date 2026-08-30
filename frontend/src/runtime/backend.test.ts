import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ invoke: vi.fn() }))
vi.mock('@tauri-apps/api/core', () => ({ invoke: mocks.invoke }))

import { backendPath, nyraFetch } from './backend'

const tauriWindow = {
  __TAURI_INTERNALS__: {},
  location: { origin: 'tauri://localhost' },
}

describe('transporte HTTP oficial da release', () => {
  beforeEach(() => {
    mocks.invoke.mockReset()
    vi.stubGlobal('window', tauriWindow)
  })
  afterEach(() => vi.unstubAllGlobals())

  it('preserva método, query, headers, body, status e resposta', async () => {
    mocks.invoke.mockResolvedValue({
      status: 409,
      status_text: 'Conflict',
      headers: [['content-type', 'application/json']],
      body: Array.from(new TextEncoder().encode('{"detail":"busy"}')),
    })

    const response = await nyraFetch('/api/tasks?limit=5', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: true }),
    })

    expect(response.status).toBe(409)
    await expect(response.json()).resolves.toEqual({ detail: 'busy' })
    expect(mocks.invoke).toHaveBeenCalledWith('backend_request', {
      request: expect.objectContaining({
        method: 'PATCH',
        path: '/api/tasks?limit=5',
        headers: [['content-type', 'application/json']],
        body: Array.from(new TextEncoder().encode('{"enabled":true}')),
      }),
    })
  })

  it('não encaminha URL externa para a ponte Tauri', async () => {
    const native = vi.fn().mockResolvedValue(new Response('external'))
    vi.stubGlobal('fetch', native)
    await nyraFetch('https://example.com/value')
    expect(native).toHaveBeenCalledOnce()
    expect(mocks.invoke).not.toHaveBeenCalled()
  })

  it('reconhece somente rotas internas da NYRA', () => {
    expect(backendPath('http://127.0.0.1:8000/api/health')).toBe('/api/health')
    expect(backendPath('/api/network-watch/metrics?minutes=5')).toBe('/api/network-watch/metrics?minutes=5')
    expect(backendPath('https://example.com/api/health')).toBeNull()
  })
})
