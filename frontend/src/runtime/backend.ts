import { invoke } from '@tauri-apps/api/core'

export const BACKEND_ORIGIN = 'http://127.0.0.1:8000'
export const BACKEND_SOCKET = 'ws://127.0.0.1:8000/api/ws'

interface BackendBridgeResponse {
  status: number
  status_text: string
  headers: Array<[string, string]>
  body: number[]
}

interface BackendBridgeRequest {
  method: string
  path: string
  headers: Array<[string, string]>
  body: number[]
}

let nativeFetch: typeof fetch | null = null
let installed = false

export const isTauriRuntime = () => typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

function isBackendPath(path: string): boolean {
  return path === '/api' || path.startsWith('/api/') || path === '/health'
}

export function backendPath(input: RequestInfo | URL): string | null {
  const raw = input instanceof Request ? input.url : input instanceof URL ? input.toString() : input
  if (raw.startsWith('/')) {
    const parsed = new URL(raw, BACKEND_ORIGIN)
    const path = `${parsed.pathname}${parsed.search}`
    return isBackendPath(parsed.pathname) ? path : null
  }
  let parsed: URL
  try {
    parsed = new URL(raw)
  } catch {
    return null
  }
  const localBackend = parsed.origin === BACKEND_ORIGIN
  const packagedRelative = typeof window !== 'undefined'
    && parsed.origin === window.location.origin
    && isBackendPath(parsed.pathname)
  if (!localBackend && !packagedRelative) return null
  return `${parsed.pathname}${parsed.search}`
}

export function backendUrl(value: string): string {
  if (!isTauriRuntime() || !value.startsWith('/')) return value
  return `${BACKEND_ORIGIN}${value}`
}

function abortError(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException('The operation was aborted.', 'AbortError')
}

async function invokeWithAbort<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) throw abortError(signal)
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(abortError(signal))
    signal.addEventListener('abort', abort, { once: true })
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort))
  })
}

async function tauriBackendFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const path = backendPath(input)
  if (!path) return (nativeFetch ?? globalThis.fetch.bind(globalThis))(input, init)

  const source = input instanceof Request ? input : `${BACKEND_ORIGIN}${path}`
  const request = new Request(source, init)
  const body = request.body
    ? Array.from(new Uint8Array(await request.arrayBuffer()))
    : []
  const bridgeRequest: BackendBridgeRequest = {
    method: request.method.toUpperCase(),
    path,
    headers: Array.from(request.headers.entries()),
    body,
  }

  let response: BackendBridgeResponse
  try {
    response = await invokeWithAbort(
      invoke<BackendBridgeResponse>('backend_request', { request: bridgeRequest }),
      request.signal,
    )
  } catch (issue) {
    if (request.signal.aborted) throw abortError(request.signal)
    throw new Error(typeof issue === 'string' ? issue : 'Falha no transporte local da NYRA')
  }

  const noBody = response.body.length === 0 || [101, 204, 205, 304].includes(response.status)
  return new Response(noBody ? null : new Uint8Array(response.body), {
    status: response.status,
    statusText: response.status_text,
    headers: response.headers,
  })
}

export function nyraFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  if (!isTauriRuntime()) return globalThis.fetch(input, init)
  return tauriBackendFetch(input, init)
}

export function installTauriBackendBridge(): void {
  if (!isTauriRuntime() || installed) return
  nativeFetch = window.fetch.bind(window)
  installed = true
  window.fetch = nyraFetch
}

export async function backendObjectUrl(value: string): Promise<string> {
  if (!isTauriRuntime() || !backendPath(value)) return backendUrl(value)
  const response = await nyraFetch(value)
  if (!response.ok) throw new Error(`BACKEND_MEDIA_HTTP_${response.status}`)
  return URL.createObjectURL(await response.blob())
}

export function releaseBackendObjectUrl(value: string): void {
  if (value.startsWith('blob:')) URL.revokeObjectURL(value)
}
