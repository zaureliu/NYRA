export type PresenceRendererMode = 'AUTO' | 'INTERNAL' | 'VTUBE_STUDIO' | 'CURRENT' | 'LIVE2D'

export interface VtsPresenceConfig {
  enabled: boolean
  renderer: PresenceRendererMode
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
  state: 'INTERNAL_ACTIVE' | 'VTS_DISCOVERING' | 'VTS_CONNECTING' | 'VTS_WAITING_FRAMES' | 'VTS_ACTIVE' | 'VTS_DEGRADED' | 'FALLBACK_INTERNAL'
  alpha: 'UNKNOWN' | 'VALID' | 'OPAQUE' | 'EMPTY'
  fallbackActive: boolean
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

const rendererMode = (mode: PresenceRendererMode) => {
  if (mode === 'CURRENT') return 'INTERNAL'
  if (mode === 'LIVE2D') return 'VTUBE_STUDIO'
  return mode
}

export function nativePresenceConfig(status: VtsBackendStatus) {
  const requested = rendererMode(status.config.renderer)
  const apiReady = status.config.enabled && status.connected && status.authenticated && status.model_loaded
  return {
    mode: requested === 'INTERNAL' || !apiReady ? 'INTERNAL' : requested,
    sender: status.config.spout_sender || 'AUTO',
    scale: status.config.presence_scale ?? 1,
    offsetX: status.config.presence_offset_x ?? 0,
    offsetY: status.config.presence_offset_y ?? 0,
    watchdogSeconds: status.config.frame_watchdog_seconds ?? 12,
  }
}

export const canReplaceInternalAvatar = (status: NativePresenceStatus) =>
  status.state === 'VTS_ACTIVE' && status.alpha === 'VALID' && !status.fallbackActive

export function backendPresenceReport(status: NativePresenceStatus) {
  return {
    state: status.state,
    alpha: status.alpha,
    fallback_active: status.fallbackActive,
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
