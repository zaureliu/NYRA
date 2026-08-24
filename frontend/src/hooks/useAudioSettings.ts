import { useCallback, useEffect, useRef, useState } from 'react'

export interface AudioSettingsValue {
  microphone: string
  speaker: string
  voice: string
  speech_speed: number
  volume: number
  conversation_mode: 'push_to_talk' | 'wake_word' | 'hands_free'
  always_listening: boolean
  allow_interruption: boolean
}

const DEFAULT_AUDIO: AudioSettingsValue = {
  microphone: 'default', speaker: 'default', voice: 'pf_dora', speech_speed: .97, volume: .9,
  conversation_mode: 'hands_free', always_listening: false, allow_interruption: true,
}

export function reconcileAudioDevices(settings: AudioSettingsValue, devices: MediaDeviceInfo[]): Partial<AudioSettingsValue> {
  if (!devices.length) return {}
  const next: Partial<AudioSettingsValue> = {}
  if (settings.microphone !== 'default' && !devices.some((item) => item.kind === 'audioinput' && item.deviceId === settings.microphone)) next.microphone = 'default'
  if (settings.speaker !== 'default' && !devices.some((item) => item.kind === 'audiooutput' && item.deviceId === settings.speaker)) next.speaker = 'default'
  return next
}

export function useAudioSettings(baseUrl = '') {
  const [settings, setSettings] = useState<AudioSettingsValue>(DEFAULT_AUDIO)
  const [loaded, setLoaded] = useState(false)
  const [notice, setNotice] = useState('')
  const current = useRef(settings); current.current = settings

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${baseUrl}/api/audio/settings`)
      if (!response.ok) throw new Error('Configuração de áudio indisponível')
      const value = await response.json()
      setSettings({ ...DEFAULT_AUDIO, ...value.settings }); setLoaded(true); setNotice('')
    } catch (error) { setNotice(error instanceof Error ? error.message : 'Áudio indisponível') }
  }, [baseUrl])

  const update = useCallback(async (next: AudioSettingsValue) => {
    const response = await fetch(`${baseUrl}/api/audio/settings`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(next),
    })
    const value = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(value.detail ?? 'Não foi possível aplicar o áudio')
    const applied = { ...DEFAULT_AUDIO, ...(value.settings ?? next) }
    setSettings(applied); setLoaded(true); setNotice('Configuração aplicada no runtime.')
    return applied
  }, [baseUrl])

  const patch = useCallback(async (changes: Partial<AudioSettingsValue>) => update({ ...current.current, ...changes }), [update])

  const reconcileDevices = useCallback((devices: MediaDeviceInfo[]) => {
    if (!devices.length || !loaded) return
    const next = reconcileAudioDevices(current.current, devices)
    if (Object.keys(next).length) {
      setNotice('Dispositivo selecionado indisponível. Usando o dispositivo padrão.')
      void patch(next)
    }
  }, [loaded, patch])

  useEffect(() => { void refresh() }, [refresh])
  return { settings, loaded, notice, setNotice, refresh, update, patch, reconcileDevices }
}
