// One device claim across NYRA windows. Coordination never carries audio.
const manualOwners = new Map<string, number>()
const subscribers = new Set<() => void>()
const channel = typeof window !== 'undefined' && typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('nyra-microphone-owner') : null
channel?.addEventListener('message', (event) => {
  if (event.data?.type !== 'manual') return
  if (event.data.active) manualOwners.set(String(event.data.owner), Date.now() + 65000)
  else manualOwners.delete(String(event.data.owner))
  subscribers.forEach((callback) => callback())
})

export function manualCaptureActive() {
  for (const [owner, expiry] of manualOwners) if (expiry < Date.now()) manualOwners.delete(owner)
  return manualOwners.size > 0
}
export function onCaptureOwnership(callback: () => void) {
  subscribers.add(callback)
  return () => { subscribers.delete(callback) }
}
export function setManualCapture(active: boolean, owner: string) {
  if (active) manualOwners.set(owner, Date.now() + 65000)
  else manualOwners.delete(owner)
  channel?.postMessage({ type: 'manual', active, owner })
  subscribers.forEach((callback) => callback())
}

export async function acquireMicrophone(): Promise<(() => void) | null> {
  if (!navigator.locks) return () => undefined
  return new Promise((resolve) => {
    void navigator.locks.request('nyra-microphone', { ifAvailable: true }, async (lock) => {
      if (!lock) { resolve(null); return }
      await new Promise<void>((release) => resolve(release))
    }).catch(() => resolve(null))
  })
}
