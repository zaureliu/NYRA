import { useCallback, useEffect, useState } from 'react'
import type { MouseTrackingMode } from '../desktop/vtsPresence'

type Config = {
  enabled: boolean
  renderer: 'VTUBE_STUDIO'
  host: string
  port: number
  auto_connect: boolean
  model_id: string | null
  lip_sync: boolean
  mouse_tracking: MouseTrackingMode
  physics_intensity: number
  target_fps: number
  spout_sender: string
  presence_scale: number
  presence_offset_x: number
  presence_offset_y: number
  frame_watchdog_seconds: number
  state_hotkeys: Record<string, string>
  emotion_map: Record<string, unknown>
  debug: boolean
}

type Presence = {
  state?: string
  alpha?: string
  vts_active?: boolean
  sender?: string
  width?: number
  height?: number
  receiver_fps?: number
  error?: string
}

type TrackingStatus = {
  mode: MouseTrackingMode
  target_hz: number
  actual_hz: number
  average_cost_ms: number
  p95_cost_ms: number
  eyes_available: boolean
  head_available: boolean
}

type Status = {
  state: string
  installed: boolean
  connected: boolean
  authenticated: boolean
  model_loaded: boolean
  model?: string
  model_id?: string
  parameter_count: number
  hotkeys?: Array<{ id?: string; name?: string; type?: string }>
  last_error?: string
  token_configured: boolean
  config: Config
  vts_presence?: Presence
  mouse_tracking?: TrackingStatus
}

export function Live2DSettings() {
  const [value, setValue] = useState<Status | null>(null)
  const [busy, setBusy] = useState(false)
  const load = useCallback(() => fetch('/api/live2d/settings').then((response) => response.json()).then(setValue).catch(() => setValue(null)), [])
  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), 2500)
    return () => window.clearInterval(timer)
  }, [load])

  const save = async (config: Config) => {
    setBusy(true)
    try {
      const response = await fetch('/api/live2d/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(config),
      })
      setValue(await response.json())
    } finally { setBusy(false) }
  }
  const call = async (path: string) => {
    setBusy(true)
    try {
      const response = await fetch(`/api/live2d/${path}`, { method: 'POST' })
      setValue(await response.json())
    } finally { setBusy(false) }
  }
  if (!value) return <div className="settings-group"><h3>DESKTOP PRESENCE · VTUBE STUDIO</h3><p>Carregando…</p></div>
  const c = value.config
  const presence = value.vts_presence ?? {}
  const tracking = value.mouse_tracking
  const change = <K extends keyof Config>(key: K, next: Config[K]) => void save({ ...c, [key]: next })
  const spoutLabel = presence.sender
    ? `${presence.sender} · ${presence.state ?? 'WAITING'}${presence.width ? ` · ${presence.width}×${presence.height}` : ''}`
    : `${presence.state ?? 'VTS_UNAVAILABLE'}${presence.error ? ` · ${presence.error}` : ''}`

  return <div className="settings-group live2d-settings">
    <h3>DESKTOP PRESENCE · VTUBE STUDIO</h3>
    <p>API <b>{value.state}</b> · modelo atual <b>{value.model ?? 'ausente'}</b></p>
    <p>Spout <b>{spoutLabel}</b> · alpha {presence.alpha ?? 'UNKNOWN'} · personagem {presence.vts_active ? 'VISÍVEL' : 'INDISPONÍVEL'}</p>
    <div className="settings-grid">
      <label>MODELO<input value="Modelo atual do VTube Studio" disabled/></label>
      <label>MOUSE TRACKING<select value={c.mouse_tracking} onChange={(event) => change('mouse_tracking', event.target.value as MouseTrackingMode)}><option value="OFF">Off</option><option value="EYES">Eyes</option><option value="HEAD_EYES">Head + Eyes</option></select></label>
      <label>API HOST<input value={c.host} onChange={(event) => change('host', event.target.value)}/></label>
      <label>API PORT<input type="number" value={c.port} onChange={(event) => change('port', Number(event.target.value))}/></label>
      <label>SPOUT SENDER<input value={c.spout_sender ?? 'AUTO'} onChange={(event) => change('spout_sender', event.target.value || 'AUTO')}/></label>
    </div>
    <div className="toggle-grid">
      <label><input type="checkbox" checked={c.enabled} onChange={(event) => change('enabled', event.target.checked)}/> Usar VTube Studio</label>
      <label><input type="checkbox" checked={c.auto_connect} onChange={(event) => change('auto_connect', event.target.checked)}/> Auto Connect</label>
      <label><input type="checkbox" checked={c.lip_sync} onChange={(event) => change('lip_sync', event.target.checked)}/> Lip Sync da fala</label>
    </div>
    <div className="inline-actions">
      <button disabled={busy || !c.enabled} onClick={() => void call('connect')}>CONNECT</button>
      <button disabled={busy || !c.enabled || value.authenticated} onClick={() => void call('authorize')}>AUTHORIZE</button>
      <button disabled={busy} onClick={() => void call('disconnect')}>DISCONNECT</button>
    </div>
    {!value.authenticated && c.enabled && <small>Autorize “NYRA Avatar Bridge” uma vez no popup oficial do VTube Studio. Sem VTS disponível, nenhum personagem alternativo é renderizado.</small>}
    <small>Mouse: {tracking?.mode ?? c.mouse_tracking} · {tracking?.actual_hz ?? 0}/{tracking?.target_hz ?? 30} Hz · olhos {tracking?.eyes_available ? 'OK' : 'não encontrados'} · cabeça {tracking?.head_available ? 'OK' : 'não encontrada'}.</small>
    <small>Hotkeys NYRA descobertos: {value.hotkeys?.filter((item) => item.name?.toUpperCase().startsWith('NYRA_')).map((item) => item.name).join(', ') || 'nenhum (mapeamento visual opcional)'}.</small>
  </div>
}
