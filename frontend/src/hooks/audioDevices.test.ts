import { describe, expect, it } from 'vitest'
import { microphoneErrorState, microphoneStatusLabel, selectMicrophoneDevice } from './audioDevices'

const device = (deviceId: string, kind: MediaDeviceKind = 'audioinput') => ({ deviceId, kind, groupId: '', label: deviceId, toJSON: () => ({}) } as MediaDeviceInfo)

describe('automatic microphone selection', () => {
  it('keeps a valid preference and falls back to the system default', () => {
    const devices = [device('default'), device('usb-mic'), device('speakers', 'audiooutput')]
    expect(selectMicrophoneDevice(devices, 'usb-mic')).toBe('usb-mic')
    expect(selectMicrophoneDevice(devices, 'removed-mic')).toBe('default')
    expect(selectMicrophoneDevice([], 'default')).toBeNull()
  })

  it('keeps denied and absent-device failures non-fatal and explicit', () => {
    expect(microphoneErrorState(new DOMException('denied', 'NotAllowedError'))).toMatchObject({ availability: 'denied', permission: 'denied' })
    expect(microphoneErrorState(new DOMException('missing', 'NotFoundError'))).toMatchObject({ availability: 'unavailable', retryOnDeviceChange: true })
    expect(microphoneStatusLabel('unavailable', false)).toBe('SEM MIC')
  })
})
