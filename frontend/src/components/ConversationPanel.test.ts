import { describe, expect, it } from 'vitest'
import { boundedComposerHeight, groupToolActivities, shouldFollowConversation } from './ConversationPanel'
import type { ToolActivity } from '../types'

const activity = (overrides: Partial<ToolActivity>): ToolActivity => ({
  id: 't1', command: 'Get-Date', riskLevel: 'READ_ONLY', status: 'finished',
  tool: 'system_shell', ...overrides,
})

describe('modern conversation behavior', () => {
  it('limits composer growth and delegates excess text to internal scrolling', () => {
    expect(boundedComposerHeight(42)).toBe(42)
    expect(boundedComposerHeight(144)).toBe(144)
    expect(boundedComposerHeight(480)).toBe(144)
  })

  it('follows new messages only when pinned, initial, or sent by the user', () => {
    expect(shouldFollowConversation(true, 'assistant', 8)).toBe(true)
    expect(shouldFollowConversation(false, 'user', 8)).toBe(true)
    expect(shouldFollowConversation(false, 'assistant', 0)).toBe(true)
    expect(shouldFollowConversation(false, 'assistant', 8)).toBe(false)
  })

  it('groups tool activity per agent run and keeps approvals out of groups', () => {
    const activities: ToolActivity[] = [
      activity({ id: 'a1', agentRunId: 'run_1' }),
      activity({ id: 'a2', agentRunId: 'run_1' }),
      activity({ id: 'r1', tool: 'remote_shell', host: 'gateway', agentRunId: '' }),
      activity({ id: 'ap1', status: 'approval_required' }),
    ]
    const groups = groupToolActivities(activities)
    expect(groups).toHaveLength(2)
    expect(groups[0]).toMatchObject({ key: 'run_1', label: 'System Shell' })
    expect(groups[0].items.map((item) => item.id)).toEqual(['a1', 'a2'])
    expect(groups[1]).toMatchObject({ key: 'direct:remote_shell', label: 'Remote Shell' })
    expect(groups.flatMap((group) => group.items).some((item) => item.id === 'ap1')).toBe(false)
  })

  it('returns no groups for a clean conversation', () => {
    expect(groupToolActivities([])).toEqual([])
  })
})
