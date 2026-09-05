import { useEffect, useState } from 'react'

export interface PresenceSettings {
  overlayScale: number
  speechBubble: boolean
  alwaysOnTop: boolean
  clickThrough: boolean
}

export const PRESENCE_SETTINGS_KEY = 'nyra-vts-presence'
const LEGACY_VISUAL_SETTINGS_KEY = 'nyra-visual-avatar-v2'
const PRESENCE_SETTINGS_EVENT = 'nyra-presence-settings'

export const DEFAULT_PRESENCE_SETTINGS: PresenceSettings = {
  overlayScale: 1,
  speechBubble: true,
  alwaysOnTop: true,
  clickThrough: false,
}

export function readPresenceSettings(): PresenceSettings {
  try {
    const legacy = JSON.parse(localStorage.getItem(LEGACY_VISUAL_SETTINGS_KEY) ?? '{}')
    const saved = JSON.parse(localStorage.getItem(PRESENCE_SETTINGS_KEY) ?? '{}')
    return { ...DEFAULT_PRESENCE_SETTINGS, ...legacy, ...saved }
  } catch {
    return DEFAULT_PRESENCE_SETTINGS
  }
}

export function savePresenceSettings(value: PresenceSettings) {
  localStorage.setItem(PRESENCE_SETTINGS_KEY, JSON.stringify(value))
  window.dispatchEvent(new CustomEvent<PresenceSettings>(PRESENCE_SETTINGS_EVENT, { detail: value }))
}

export function usePresenceSettings() {
  const [settings, setSettings] = useState(readPresenceSettings)
  useEffect(() => {
    const update = (event: Event) => setSettings((event as CustomEvent<PresenceSettings>).detail ?? readPresenceSettings())
    const storage = () => setSettings(readPresenceSettings())
    window.addEventListener(PRESENCE_SETTINGS_EVENT, update)
    window.addEventListener('storage', storage)
    return () => {
      window.removeEventListener(PRESENCE_SETTINGS_EVENT, update)
      window.removeEventListener('storage', storage)
    }
  }, [])
  const set = (next: PresenceSettings) => { setSettings(next); savePresenceSettings(next) }
  return [settings, set] as const
}
