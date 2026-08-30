import { nyraFetch } from './backend'

export interface ApiErrorEnvelope {
  error_code: string
  message: string
  stage?: string
  recoverable?: boolean
}

export class ApiRequestError extends Error {
  readonly code: string
  readonly stage: string
  readonly recoverable: boolean
  readonly status: number

  constructor(status: number, envelope: Partial<ApiErrorEnvelope>) {
    super(envelope.message || `Falha HTTP ${status}`)
    this.code = envelope.error_code || `HTTP_${status}`
    this.stage = envelope.stage || 'backend'
    this.recoverable = envelope.recoverable ?? true
    this.status = status
  }
}

function extractDetail(detail: unknown): Partial<ApiErrorEnvelope> {
  if (typeof detail === 'string') return { message: detail }
  if (detail && typeof detail === 'object') return detail as Partial<ApiErrorEnvelope>
  return {}
}

export async function apiGet<T>(path: string, timeoutMs = 12000, cache: RequestCache = 'default'): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await nyraFetch(path, { signal: controller.signal, cache })
    if (!response.ok) {
      let detail: unknown = undefined
      try { detail = (await response.json()).detail } catch { /* corpo vazio */ }
      throw new ApiRequestError(response.status, extractDetail(detail))
    }
    return await response.json() as T
  } finally {
    clearTimeout(timer)
  }
}

export async function apiSend<T>(
  path: string,
  method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
  body?: unknown,
  timeoutMs = 15000,
): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await nyraFetch(path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    })
    if (!response.ok) {
      let detail: unknown = undefined
      try { detail = (await response.json()).detail } catch { /* corpo vazio */ }
      throw new ApiRequestError(response.status, extractDetail(detail))
    }
    return await response.json() as T
  } finally {
    clearTimeout(timer)
  }
}
