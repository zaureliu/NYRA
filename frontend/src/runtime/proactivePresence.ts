export interface ProactivePresenceNotice {
  id: string
  message: string
  priority: 'LOW' | 'NORMAL' | 'HIGH' | 'CRITICAL'
  channels: Array<'ui' | 'chat' | 'voice'>
  executionAuthorized: false
}

export function readProactivePresenceNotice(event: {
  type: string
  payload: Record<string, unknown>
}): ProactivePresenceNotice | null {
  if (event.type !== 'PROACTIVE_PRESENCE_NOTIFICATION') return null
  const allowed = new Set(['ui', 'chat', 'voice'])
  const channels = (Array.isArray(event.payload.channels) ? event.payload.channels : [])
    .map(String)
    .filter((value): value is 'ui' | 'chat' | 'voice' => allowed.has(value))
  const rawPriority = String(event.payload.priority ?? 'NORMAL')
  const priority = ['LOW', 'NORMAL', 'HIGH', 'CRITICAL'].includes(rawPriority)
    ? rawPriority as ProactivePresenceNotice['priority']
    : 'NORMAL'
  return {
    id: String(event.payload.notification_id ?? crypto.randomUUID()),
    message: String(event.payload.message ?? 'Houve uma mudança relevante.').slice(0, 500),
    priority,
    channels,
    // Presentation is never interpreted as tool/action authorization.
    executionAuthorized: false,
  }
}
