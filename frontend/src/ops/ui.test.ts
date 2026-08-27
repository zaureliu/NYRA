import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiRequestError, apiGet } from '../runtime/api'
import { normalizeState, prettify, formatMs, formatRelative } from './ui'

describe('normalização de status (§28-§30)', () => {
  it('mapeia sinônimos para o vocabulário canônico', () => {
    expect(normalizeState('ONLINE')).toBe('READY')
    expect(normalizeState('HEALTHY')).toBe('READY')
    expect(normalizeState('running')).toBe('READY')
    expect(normalizeState('auth failed')).toBe('AUTH_FAILED')
    expect(normalizeState(undefined as unknown as string)).toBe('UNKNOWN')
  })

  it('rótulos legíveis sem underscores', () => {
    expect(prettify('UNCONFIGURED')).toBe('UNCONFIGURED'.replace('_', ' '))
  })

  it('formatações seguras', () => {
    expect(formatMs(null)).toBe('—')
    expect(formatMs(12.34)).toBe('12.3ms')
    expect(formatRelative(null)).toBe('—')
    expect(formatRelative(2)).toBe('agora')
  })
})

describe('api client — envelope de erro (§153/§154)', () => {
  afterEach(() => { vi.restoreAllMocks() })

  it('converte detail string em erro com código HTTP', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'IA local inicializando' }), { status: 503 })))
    const error = await apiGet('/api/x').catch((issue: unknown) => issue)
    expect(error).toBeInstanceOf(ApiRequestError)
    expect((error as ApiRequestError).code).toBe('HTTP_503')
    expect((error as ApiRequestError).message).toContain('IA local inicializando')
  })

  it('preserva envelope estruturado do backend', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      detail: { error_code: 'SENTINEL_UNCONFIGURED', message: 'Token ausente.', stage: 'sentinel', recoverable: true },
    }), { status: 409 })))
    const error = await apiGet('/api/x').catch((issue: unknown) => issue)
    expect((error as ApiRequestError).code).toBe('SENTINEL_UNCONFIGURED')
    expect((error as ApiRequestError).stage).toBe('sentinel')
  })

  it('permite bypass explícito do cache para status em tempo real', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ status: 'online' }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await apiGet('/api/health', 12000, 'no-store')

    expect(fetchMock).toHaveBeenCalledWith('/api/health', expect.objectContaining({ cache: 'no-store' }))
  })
})
