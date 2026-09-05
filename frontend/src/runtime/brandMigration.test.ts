import { describe, expect, it } from 'vitest'
import { migrateBrandStorage } from './brandMigration'

describe('legacy product preferences', () => {
  it('copies old keys once, keeps customized new values and unrelated storage', () => {
    const items = new Map([['nyra-active-view', 'voice'], ['nyra-nav-collapsed', 'true'],
      ['kazumi-nav-collapsed', 'false'], ['other-app', 'untouched']])
    const storage = { get length() { return items.size }, key: (index: number) => [...items.keys()][index],
      getItem: (key: string) => items.get(key) ?? null,
      setItem: (key: string, value: string) => { items.set(key, value) } } as Storage
    expect(migrateBrandStorage(storage)).toBe(1)
    expect(items.get('kazumi-active-view')).toBe('voice')
    expect(items.get('kazumi-nav-collapsed')).toBe('false')
    expect(items.get('nyra-active-view')).toBe('voice')
    expect(items.get('other-app')).toBe('untouched')
    expect(migrateBrandStorage(storage)).toBe(0)
  })
})
