import type { ChatResponse } from '../types'
import { nyraFetch } from './backend'

export interface ChatPayload {
  message: string
  synthesize: boolean
  turn_id?: string
}

function requestError(body: unknown): Error {
  const envelope = body && typeof body === 'object' ? body as { detail?: unknown } : {}
  const detail = envelope.detail
  if (typeof detail === 'string') return new Error(detail)
  if (detail && typeof detail === 'object') {
    const structured = detail as { error_code?: string; exception_type?: string }
    if (structured.error_code) {
      return new Error(`${structured.error_code} (${structured.exception_type ?? 'erro'})`)
    }
  }
  return new Error('Falha no backend')
}

export async function sendChat(payload: ChatPayload): Promise<ChatResponse> {
  const response = await nyraFetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const body: unknown = await response.json().catch(() => ({}))
  if (!response.ok) throw requestError(body)
  return body as ChatResponse
}
