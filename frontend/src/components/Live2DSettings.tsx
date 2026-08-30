import { useCallback, useEffect, useState } from 'react'

type Renderer = 'AUTO' | 'INTERNAL' | 'VTUBE_STUDIO' | 'CURRENT' | 'LIVE2D'
type Config = {
  enabled: boolean; renderer: Renderer; host: string; port: number; auto_connect: boolean
  model_id: string | null; lip_sync: boolean; cursor_attention: boolean
  physics_intensity: number; target_fps: number; spout_sender: string
  presence_scale: number; presence_offset_x: number; presence_offset_y: number
  frame_watchdog_seconds: number; state_hotkeys: Record<string, string>; debug: boolean
}
type Presence = {
  state?: string; alpha?: string; fallback_active?: boolean; sender?: string
  width?: number; height?: number; receiver_fps?: number; error?: string
}
type Status = {
  state: string; installed: boolean; connected: boolean; authenticated: boolean
  model_loaded: boolean; model?: string; model_id?: string; parameter_count: number
  hotkeys?: Array<{ id?: string; name?: string; type?: string }>; last_error?: string
  token_configured: boolean; config: Config; vts_presence?: Presence
}

const normalizedRenderer = (value: Renderer): 'AUTO' | 'INTERNAL' | 'VTUBE_STUDIO' => {
  if (value === 'CURRENT') return 'INTERNAL'
  if (value === 'LIVE2D') return 'VTUBE_STUDIO'
  return value
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
  const change = <K extends keyof Config>(key: K, next: Config[K]) => void save({ ...c, [key]: next })
  const renderer = normalizedRenderer(c.renderer)
  const spoutLabel = presence.sender
    ? `${presence.sender} · ${presence.state ?? 'WAITING'}${presence.width ? ` · ${presence.width}×${presence.height}` : ''}`
    : `${presence.state ?? 'INTERNAL_ACTIVE'}${presence.error ? ` · ${presence.error}` : ''}`

  return <div className="settings-group live2d-settings">
    <h3>DESKTOP PRESENCE · VTUBE STUDIO</h3>
    <p>API <b>{value.state}</b> · modelo <b>{value.model ?? 'ausente'}</b></p>
    <p>Spout <b>{spoutLabel}</b> · alpha {presence.alpha ?? 'UNKNOWN'} · fallback {presence.fallback_active === false ? 'OFF' : 'ON'}</p>
    <div className="settings-grid">
      <label>RENDERER<select value={renderer} onChange={(event) => change('renderer', event.target.value as Renderer)}><option>AUTO</option><option>INTERNAL</option><option>VTUBE_STUDIO</option></select></label>
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
    {!value.authenticated && c.enabled && <small>Autorize “NYRA Avatar Bridge” uma vez no popup oficial do VTube Studio. O avatar interno permanece ativo até API, frames e alpha estarem válidos.</small>}
    <small>Hotkeys NYRA descobertos: {value.hotkeys?.filter((item) => item.name?.toUpperCase().startsWith('NYRA_')).map((item) => item.name).join(', ') || 'nenhum (mapeamento visual opcional)'}. Head/eyes continuam sob controle do tracking do VTube Studio.</small>
  </div>
}
