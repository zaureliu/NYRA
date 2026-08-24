import { useEffect, useState } from 'react'
import type { AvatarRendererId, CharacterView } from './AvatarRenderer'

export interface VisualSettings {
  avatarVersion: 'v2' | 'v3'
  renderer: AvatarRendererId
  characterView: CharacterView
  overlayScale: number
  speechBubble: boolean
  idleAnimations: boolean
  eyeMovement: boolean
  blink: boolean
  alwaysOnTop: boolean
  clickThrough: boolean
  debug: boolean
}

export const VISUAL_SETTINGS_KEY = 'nyra-visual-avatar-v2'
export const VISUAL_SETTINGS_EVENT = 'nyra-visual-settings'
export const DEFAULT_VISUAL_SETTINGS: VisualSettings = {
  avatarVersion: 'v2', renderer: 'layered', characterView: 'bust', overlayScale: 1,
  speechBubble: true, idleAnimations: true, eyeMovement: true, blink: true,
  alwaysOnTop: true, clickThrough: false, debug: false,
}

export function readVisualSettings(): VisualSettings {
  try {
    const saved = { ...DEFAULT_VISUAL_SETTINGS, ...JSON.parse(localStorage.getItem(VISUAL_SETTINGS_KEY) ?? '{}') }
    return { ...saved, avatarVersion: 'v2', renderer: 'layered', characterView: 'bust' }
  }
  catch { return DEFAULT_VISUAL_SETTINGS }
}

export function saveVisualSettings(value: VisualSettings) {
  localStorage.setItem(VISUAL_SETTINGS_KEY, JSON.stringify(value))
  window.dispatchEvent(new CustomEvent<VisualSettings>(VISUAL_SETTINGS_EVENT, { detail: value }))
}

export function useVisualSettings() {
  const [settings, setSettings] = useState(readVisualSettings)
  useEffect(() => {
    const update = (event: Event) => setSettings((event as CustomEvent<VisualSettings>).detail ?? readVisualSettings())
    const storage = () => setSettings(readVisualSettings())
    window.addEventListener(VISUAL_SETTINGS_EVENT, update)
    window.addEventListener('storage', storage)
    return () => { window.removeEventListener(VISUAL_SETTINGS_EVENT, update); window.removeEventListener('storage', storage) }
  }, [])
  const set = (next: VisualSettings) => { setSettings(next); saveVisualSettings(next) }
  return [settings, set] as const
}
