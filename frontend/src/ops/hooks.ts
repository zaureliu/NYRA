import { useCallback, useEffect, useRef, useState } from 'react'
import { apiGet } from '../runtime/api'
import { readHeaderStatus } from '../runtime/headerStatus'

export interface PollingState<T> {
  data: T | null
  error: string
  loading: boolean
  refresh: () => void
}

export interface PollingOptions {
  clearOnError?: boolean
  headerStatus?: boolean
  retryIntervalMs?: number
  noStore?: boolean
}

/** Polling com backoff quando o backend está offline (evita loop agressivo). */
export function usePolling<T>(path: string | null, intervalMs: number, options: PollingOptions = {}): PollingState<T> {
  const clearOnError = options.clearOnError ?? false
  const headerStatus = options.headerStatus ?? false
  const retryIntervalMs = options.retryIntervalMs
  const cache: RequestCache = options.noStore ? 'no-store' : 'default'
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [tick, setTick] = useState(0)
  const failuresRef = useRef(0)
  const aliveRef = useRef(true)

  useEffect(() => {
    aliveRef.current = true
    return () => { aliveRef.current = false }
  }, [])

  useEffect(() => {
    if (!path) {
      setData(null)
      setError('')
      return
    }
    let timer: number | undefined

    const load = async () => {
      setLoading(true)
      try {
        const result = headerStatus
          ? await readHeaderStatus<T>(path)
          : await apiGet<T>(path, 12000, cache)
        if (!aliveRef.current) return
        setData(result)
        setError('')
        failuresRef.current = 0
      } catch (issue) {
        if (!aliveRef.current) return
        failuresRef.current += 1
        if (clearOnError) setData(null)
        setError(issue instanceof Error ? issue.message : String(issue))
      } finally {
        if (aliveRef.current) setLoading(false)
        // Status de startup pode recuperar rapidamente; demais consumers
        // preservam o backoff histórico para evitar polling agressivo.
        const retryDelay = retryIntervalMs == null
          ? intervalMs * Math.min(4, 1 + failuresRef.current)
          : Math.min(intervalMs, retryIntervalMs * (2 ** Math.min(Math.max(0, failuresRef.current - 1), 4)))
        const delay = failuresRef.current > 0 ? retryDelay : intervalMs
        timer = window.setTimeout(load, delay)
      }
    }

    load()
    return () => { if (timer) window.clearTimeout(timer) }
  }, [path, intervalMs, tick, cache, clearOnError, headerStatus, retryIntervalMs])

  const refresh = useCallback(() => setTick((value) => value + 1), [])
  return { data, error, loading, refresh }
}
