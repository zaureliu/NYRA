export const BACKEND_ORIGIN = 'http://127.0.0.1:8000'
export const BACKEND_SOCKET = 'ws://127.0.0.1:8000/api/ws'

export const isTauriRuntime = () => typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

export function backendUrl(value: string): string {
  if (!isTauriRuntime() || !value.startsWith('/')) return value
  return `${BACKEND_ORIGIN}${value}`
}

export function installTauriBackendBridge(): void {
  if (!isTauriRuntime()) return
  const nativeFetch = window.fetch.bind(window)
  window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    if (typeof input === 'string') return nativeFetch(backendUrl(input), init)
    if (input instanceof URL) return nativeFetch(backendUrl(input.toString()), init)
    return nativeFetch(input, init)
  }) as typeof window.fetch
}
