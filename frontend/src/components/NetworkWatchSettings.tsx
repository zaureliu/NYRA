import { useEffect, useState } from 'react'

interface Settings {
  enabled: boolean; voice_alerts: boolean; desktop_alerts: boolean; quiet_mode: boolean; critical_voice_in_quiet: boolean
  interface_interval: number; gateway_interval: number; internet_interval: number; dns_interval: number; http_interval: number
  latency_warning_ms: number; latency_critical_ms: number; packet_loss_warning: number; packet_loss_critical: number
  jitter_warning_ms: number; alert_cooldown_seconds: number; history_retention_days: number; dns_target: string; internet_targets: string[]
}
const DEFAULT: Settings = { enabled:false,voice_alerts:true,desktop_alerts:true,quiet_mode:false,critical_voice_in_quiet:false,interface_interval:1,gateway_interval:2,internet_interval:5,dns_interval:15,http_interval:30,latency_warning_ms:100,latency_critical_ms:200,packet_loss_warning:5,packet_loss_critical:15,jitter_warning_ms:40,alert_cooldown_seconds:300,history_retention_days:30,dns_target:'cloudflare.com',internet_targets:['1.1.1.1:443','8.8.8.8:53'] }

export function NetworkWatchSettings() {
  const [settings, setSettings] = useState(DEFAULT); const [notice, setNotice] = useState('')
  const load = async () => { const response = await fetch('/api/network-watch/settings'); if (response.ok) setSettings((await response.json()).settings) }
  useEffect(() => { void load() }, [])
  const save = async (next = settings) => { const response = await fetch('/api/network-watch/settings', { method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(next) }); setNotice(response.ok ? (next.enabled ? 'Network Watch read-only ativo.' : 'Network Watch parado.') : 'Falha ao salvar Network Watch.') }
  const toggle = (key: keyof Settings, value: boolean) => { const next = { ...settings, [key]: value }; setSettings(next); if (key === 'enabled') void save(next) }
  const inject = async (event: string) => { const response = await fetch('/api/network-watch/debug',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event})}); setNotice(response.ok ? `Evento ${event} injetado.` : 'Debug indisponível.') }
  return <div className="settings-group network-settings"><h3>NETWORK WATCH · READ-ONLY</h3>
    <div className="toggle-grid"><label><input type="checkbox" checked={settings.enabled} onChange={(event) => toggle('enabled',event.target.checked)}/> Enabled</label><label><input type="checkbox" checked={settings.voice_alerts} onChange={(event) => toggle('voice_alerts',event.target.checked)}/> Voice Alerts</label><label><input type="checkbox" checked={settings.desktop_alerts} onChange={(event) => toggle('desktop_alerts',event.target.checked)}/> Desktop Alerts</label><label><input type="checkbox" checked={settings.quiet_mode} onChange={(event) => toggle('quiet_mode',event.target.checked)}/> Quiet Mode</label></div>
    <div className="settings-grid"><NumberField label="Gateway interval" value={settings.gateway_interval} set={(value)=>setSettings({...settings,gateway_interval:value})}/><NumberField label="Internet interval" value={settings.internet_interval} set={(value)=>setSettings({...settings,internet_interval:value})}/><NumberField label="DNS interval" value={settings.dns_interval} set={(value)=>setSettings({...settings,dns_interval:value})}/><NumberField label="Latency warning" value={settings.latency_warning_ms} set={(value)=>setSettings({...settings,latency_warning_ms:value})}/><NumberField label="Latency critical" value={settings.latency_critical_ms} set={(value)=>setSettings({...settings,latency_critical_ms:value})}/><NumberField label="Packet loss warning %" value={settings.packet_loss_warning} set={(value)=>setSettings({...settings,packet_loss_warning:value})}/><NumberField label="Jitter warning" value={settings.jitter_warning_ms} set={(value)=>setSettings({...settings,jitter_warning_ms:value})}/><NumberField label="Cooldown (s)" value={settings.alert_cooldown_seconds} set={(value)=>setSettings({...settings,alert_cooldown_seconds:value})}/></div>
    <div className="settings-actions"><button onClick={() => void save()}>SALVAR</button><button onClick={() => void inject('high_latency')}>SIMULAR LATÊNCIA</button><button onClick={() => void inject('network_recovered')}>SIMULAR RECOVERY</button></div>{notice && <p className="lab-notice">{notice}</p>}
  </div>
}
function NumberField({label,value,set}:{label:string;value:number;set:(value:number)=>void}) { return <label>{label.toUpperCase()}<input type="number" value={value} onChange={(event)=>set(Number(event.target.value))}/></label> }
