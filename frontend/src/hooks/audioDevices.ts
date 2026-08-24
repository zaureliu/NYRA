export type MicrophoneAvailability = 'checking' | 'available' | 'unavailable' | 'denied' | 'unsupported' | 'error'
export type MicrophonePermission = 'unknown' | 'prompt' | 'granted' | 'denied'

export function audioInputs(devices: MediaDeviceInfo[]) {
  return devices.filter((device) => device.kind === 'audioinput')
}

export function selectMicrophoneDevice(devices: MediaDeviceInfo[], preferred = 'default'): string | null {
  const inputs = audioInputs(devices)
  if (!inputs.length) return null
  if (preferred !== 'default' && inputs.some((device) => device.deviceId === preferred)) return preferred
  return 'default'
}

export function microphoneErrorState(error: unknown): { availability: MicrophoneAvailability; permission: MicrophonePermission; retryOnDeviceChange: boolean } {
  const name = error instanceof DOMException ? error.name : error instanceof Error ? error.name : ''
  if (name === 'NotAllowedError' || name === 'SecurityError') return { availability: 'denied', permission: 'denied', retryOnDeviceChange: false }
  if (name === 'NotFoundError' || name === 'OverconstrainedError' || name === 'NotReadableError') return { availability: 'unavailable', permission: 'granted', retryOnDeviceChange: true }
  return { availability: 'error', permission: 'unknown', retryOnDeviceChange: true }
}

export function microphoneStatusLabel(availability: MicrophoneAvailability, active: boolean) {
  if (active) return 'ATIVO'
  if (availability === 'checking') return 'VERIFICANDO'
  if (availability === 'denied') return 'PERMISSÃO'
  if (availability === 'unavailable') return 'SEM MIC'
  if (availability === 'unsupported') return 'INDISPONÍVEL'
  return 'OFF'
}
