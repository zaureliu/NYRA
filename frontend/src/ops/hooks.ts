import { useCallback, useEffect, useRef, useState } from 'react'
import { apiGet } from '../runtime/api'

export interface PollingState<T> {
  data: T | null
  error: string
  loading: boolean
  refresh: () => void
}

/** Polling com backoff quando o backend está offline (evita loop agressivo). */
export function usePolling<T>(path: string | null, intervalMs: number): PollingState<T> {
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
        const result = await apiGet<T>(path)
        if (!aliveRef.current) return
        setData(result)
        setError('')
        failuresRef.current = 0
      } catch (issue) {
        if (!aliveRef.current) return
        failuresRef.current += 1
        setError(issue instanceof Error ? issue.message : String(issue))
      } finally {
        if (aliveRef.current) setLoading(false)
        // Backoff: cada falha consecutiva dobra o intervalo (máx 4×).
        const backoffMultiplier = Math.min(4, 1 + failuresRef.current)
        const delay = intervalMs * backoffMultiplier
        timer = window.setTimeout(load, delay)
      }
    }

    load()
    return () => { if (timer) window.clearTimeout(timer) }
  }, [path, intervalMs, tick])

  const refresh = useCallback(() => setTick((value) => value + 1), [])
  return { data, error, loading, refresh }
}
