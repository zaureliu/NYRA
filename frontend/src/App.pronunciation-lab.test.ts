import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const appSource = readFileSync(fileURLToPath(new URL('./App.tsx', import.meta.url)), 'utf-8')

describe('conversation workspace', () => {
  it('does not mount the Pronunciation Lab or its residual spacer', () => {
    const conversation = appSource.match(/case 'conversation':([\s\S]*?)case 'capabilities':/)?.[1]

    expect(conversation).toBeDefined()
    expect(conversation).toContain('<ConversationPanel')
    expect(conversation).not.toContain('PronunciationLab')
    expect(conversation).not.toContain('height: 14')
    expect(appSource).not.toContain("import { PronunciationLab }")
  })
})
