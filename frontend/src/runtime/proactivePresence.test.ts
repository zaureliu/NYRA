import { describe, expect, it } from 'vitest'
import { readProactivePresenceNotice } from './proactivePresence'

describe('proactive presence event contract', () => {
  it('accepts only the final presentation event and never grants execution', () => {
    expect(readProactivePresenceNotice({ type: 'MONITOR_JOB_COMPLETED', payload: {} })).toBeNull()
    const notice = readProactivePresenceNotice({
      type: 'PROACTIVE_PRESENCE_NOTIFICATION',
      payload: {
        notification_id: 'pnot_1', message: 'A VM 120 voltou.',
        priority: 'HIGH', channels: ['ui', 'chat', 'shell'],
        execution_authorized: true,
      },
    })
    expect(notice).toEqual({
      id: 'pnot_1', message: 'A VM 120 voltou.', priority: 'HIGH',
      channels: ['ui', 'chat'], executionAuthorized: false,
    })
  })
})
