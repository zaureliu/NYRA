import { apiGet } from './api'

const HEADER_STATUS_PATHS = new Set([
  '/api/health',
  '/api/ollama/readiness',
  '/api/watchdog/status',
  '/api/selfdev/status',
])

export async function readHeaderStatus<T>(path: string): Promise<T> {
  if (!HEADER_STATUS_PATHS.has(path)) throw new Error('HEADER_STATUS_PATH_NOT_ALLOWED')
  return apiGet<T>(path, 12000, 'no-store')
}
