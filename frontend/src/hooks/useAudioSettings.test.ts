import { describe, expect, it } from 'vitest'
import { reconcileAudioDevices, type AudioSettingsValue } from './useAudioSettings'

const value: AudioSettingsValue = {
  microphone: 'usb-mic', speaker: 'usb-speaker', voice: 'pf_dora', speech_speed: .97,
  volume: .9, conversation_mode: 'hands_free', always_listening: true, allow_interruption: true,
  emotion_mode: 'automatic', expressiveness: 'normal',
}
const device = (deviceId: string, kind: MediaDeviceKind) => ({
  deviceId, kind, groupId: '', label: deviceId, toJSON: () => ({}),
} as MediaDeviceInfo)

describe('backend-owned audio device settings', () => {
  it('keeps connected selections and falls back both removed devices', () => {
    expect(reconcileAudioDevices(value, [device('usb-mic', 'audioinput'), device('usb-speaker', 'audiooutput')])).toEqual({})
    expect(reconcileAudioDevices(value, [device('default', 'audioinput'), device('default', 'audiooutput')])).toEqual({
      microphone: 'default', speaker: 'default',
    })
  })

  it('does not overwrite persisted choices before device enumeration completes', () => {
    expect(reconcileAudioDevices(value, [])).toEqual({})
  })
})
