export type MouseTrackingMode = 'OFF' | 'EYES' | 'HEAD_EYES'

export interface VtsPresenceConfig {
  enabled: boolean
  renderer: 'VTUBE_STUDIO'
  mouse_tracking: MouseTrackingMode
  spout_sender: string
  presence_scale: number
  presence_offset_x: number
  presence_offset_y: number
  frame_watchdog_seconds: number
}

export interface VtsBackendStatus {
  connected: boolean
  authenticated: boolean
  model_loaded: boolean
  config: VtsPresenceConfig
}

export interface NativePresenceStatus {
  state: 'VTS_OFFLINE' | 'VTS_DISCOVERING' | 'VTS_CONNECTING' | 'VTS_WAITING_FRAMES' | 'VTS_ACTIVE' | 'VTS_DEGRADED' | 'VTS_UNAVAILABLE'
  alpha: 'UNKNOWN' | 'VALID' | 'OPAQUE' | 'EMPTY'
  vtsActive: boolean
  sender?: string
  width: number
  height: number
  format?: string
  senderFps: number
  receiverFps: number
  frameCount: number
  droppedFrames: number
  lastFrameAgeMs: number
  adapterMatch: boolean
  senderAdapter?: string
  receiverAdapter?: string
  memoryBytes: number
  error?: string
}

export function nativePresenceConfig(status: VtsBackendStatus) {
  return {
    sender: status.config.spout_sender || 'AUTO',
    scale: status.config.presence_scale ?? 1,
    offsetX: status.config.presence_offset_x ?? 0,
    offsetY: status.config.presence_offset_y ?? 0,
    watchdogSeconds: status.config.frame_watchdog_seconds ?? 12,
  }
}

export const hasActiveVtsCharacter = (status: NativePresenceStatus) =>
  status.state === 'VTS_ACTIVE' && status.alpha === 'VALID' && status.vtsActive

export function backendPresenceReport(status: NativePresenceStatus) {
  return {
    state: status.state,
    alpha: status.alpha,
    vts_active: status.vtsActive,
    sender: status.sender ?? null,
    width: status.width,
    height: status.height,
    format: status.format ?? null,
    sender_fps: status.senderFps,
    receiver_fps: status.receiverFps,
    frame_count: status.frameCount,
    dropped_frames: status.droppedFrames,
    last_frame_age_ms: status.lastFrameAgeMs,
    adapter_match: status.adapterMatch,
    sender_adapter: status.senderAdapter ?? null,
    receiver_adapter: status.receiverAdapter ?? null,
    memory_bytes: status.memoryBytes,
    error: status.error ?? null,
  }
}
