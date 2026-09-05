/** One-release compatibility: preserve operator UI preferences from NYRA. */
export function migrateBrandStorage(storage: Storage): number {
  let copied = 0
  for (let index = 0; index < storage.length; index++) {
    const key = storage.key(index)
    if (!key || !/^nyra[-_:]/i.test(key)) continue
    const target = key.replace(/^nyra/i, 'kazumi')
    if (storage.getItem(target) !== null) continue
    const value = storage.getItem(key)
    if (value !== null) { storage.setItem(target, value); copied++ }
  }
  return copied
}

try {
  migrateBrandStorage(localStorage)
  migrateBrandStorage(sessionStorage)
} catch { /* Storage-disabled browsers can still render a fresh local UI. */ }
