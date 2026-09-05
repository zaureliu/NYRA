import { useEffect, useState } from 'react'
import { kazumiFetch } from '../runtime/backend'

export interface NetworkSettingsValue {
  enabled: boolean; voice_alerts: boolean; desktop_alerts: boolean; quiet_mode: boolean; critical_voice_in_quiet: boolean
  interface_interval: number; gateway_interval: number; internet_interval: number; dns_interval: number; http_interval: number
  latency_warning_ms: number; latency_critical_ms: number; packet_loss_warning: number; packet_loss_critical: number
  jitter_warning_ms: number; alert_cooldown_seconds: number; history_retention_days: number; dns_target: string; internet_targets: string[]
}

const DEFAULT: NetworkSettingsValue = {
  enabled:false, voice_alerts:true, desktop_alerts:true, quiet_mode:false, critical_voice_in_quiet:false,
  interface_interval:1, gateway_interval:2, internet_interval:5, dns_interval:15, http_interval:30,
  latency_warning_ms:100, latency_critical_ms:200, packet_loss_warning:5, packet_loss_critical:15,
  jitter_warning_ms:40, alert_cooldown_seconds:300, history_retention_days:30,
  dns_target:'cloudflare.com', internet_targets:['1.1.1.1:443','8.8.8.8:53'],
}

export function NetworkSettingsPanel({ initialSettings }: { initialSettings?: NetworkSettingsValue }) {
  const [settings,setSettings]=useState(initialSettings ?? DEFAULT)
  const [notice,setNotice]=useState('')
  const [saving,setSaving]=useState(false)
  useEffect(()=>{
    if(initialSettings)return
    let active=true
    let retry:number|undefined
    const load=()=>void kazumiFetch('/api/network-watch/settings').then(async response=>{
      if(!response.ok)throw new Error(String(response.status))
      if(active){setSettings((await response.json() as {settings:NetworkSettingsValue}).settings);setNotice('')}
    }).catch(()=>{if(active){setNotice('Aguardando o backend para carregar a configuração.');retry=window.setTimeout(load,3000)}})
    load()
    return()=>{active=false;if(retry)window.clearTimeout(retry)}
  },[initialSettings])
  const save=async(next=settings)=>{setSaving(true);try{const response=await kazumiFetch('/api/network-watch/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(next)});setNotice(response.ok?'Configuração salva.':'Falha ao salvar a configuração.')}catch{setNotice('Backend indisponível durante o salvamento.')}finally{setSaving(false)}}
  const toggle=(key:keyof NetworkSettingsValue,value:boolean)=>{const next={...settings,[key]:value};setSettings(next);if(key==='enabled')void save(next)}
  const inject=async(event:string)=>{try{const response=await kazumiFetch('/api/network-watch/debug',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event})});setNotice(response.ok?'Evento de teste registrado na timeline como simulação.':'Simulação indisponível.')}catch{setNotice('Simulação indisponível.')}}
  return <section className="network-settings-panel">
    <SettingsGroup title="MONITOR"><Toggle label="Enabled" checked={settings.enabled} onChange={value=>toggle('enabled',value)}/></SettingsGroup>
    <SettingsGroup title="NOTIFICATIONS"><Toggle label="Desktop alerts" checked={settings.desktop_alerts} onChange={value=>toggle('desktop_alerts',value)}/><Toggle label="Voice alerts" checked={settings.voice_alerts} onChange={value=>toggle('voice_alerts',value)}/><Toggle label="Quiet mode" checked={settings.quiet_mode} onChange={value=>toggle('quiet_mode',value)}/><Toggle label="Critical voice in quiet" checked={settings.critical_voice_in_quiet} onChange={value=>toggle('critical_voice_in_quiet',value)}/></SettingsGroup>
    <SettingsGroup title="PROBE INTERVALS"><NumberField label="Interface" unit="s" min={0.5} max={60} step={0.5} value={settings.interface_interval} onChange={value=>setSettings({...settings,interface_interval:value})}/><NumberField label="Gateway" unit="s" min={1} max={300} value={settings.gateway_interval} onChange={value=>setSettings({...settings,gateway_interval:value})}/><NumberField label="Internet" unit="s" min={2} max={600} value={settings.internet_interval} onChange={value=>setSettings({...settings,internet_interval:value})}/><NumberField label="DNS" unit="s" min={5} max={3600} value={settings.dns_interval} onChange={value=>setSettings({...settings,dns_interval:value})}/></SettingsGroup>
    <SettingsGroup title="THRESHOLDS"><NumberField label="Latency warning" unit="ms" min={10} max={5000} value={settings.latency_warning_ms} onChange={value=>setSettings({...settings,latency_warning_ms:value})}/><NumberField label="Latency critical" unit="ms" min={20} max={10000} value={settings.latency_critical_ms} onChange={value=>setSettings({...settings,latency_critical_ms:value})}/><NumberField label="Jitter warning" unit="ms" min={1} max={5000} value={settings.jitter_warning_ms} onChange={value=>setSettings({...settings,jitter_warning_ms:value})}/><NumberField label="Packet loss warning" unit="%" min={0} max={100} value={settings.packet_loss_warning} onChange={value=>setSettings({...settings,packet_loss_warning:value})}/><NumberField label="Packet loss critical" unit="%" min={0} max={100} value={settings.packet_loss_critical} onChange={value=>setSettings({...settings,packet_loss_critical:value})}/></SettingsGroup>
    <SettingsGroup title="BEHAVIOR"><NumberField label="Event cooldown" unit="s" min={10} max={86400} value={settings.alert_cooldown_seconds} onChange={value=>setSettings({...settings,alert_cooldown_seconds:value})}/></SettingsGroup>
    <SettingsGroup title="TESTS"><button type="button" onClick={()=>void inject('high_latency')}>SIMULAR LATÊNCIA</button><button type="button" onClick={()=>void inject('network_recovered')}>SIMULAR RECOVERY</button></SettingsGroup>
    <div className="network-settings-footer"><button type="button" onClick={()=>void save()} disabled={saving}>{saving?'SALVANDO…':'SALVAR CONFIGURAÇÃO'}</button>{notice&&<p role="status">{notice}</p>}</div>
  </section>
}

export function NetworkWatchSettings(){return <NetworkSettingsPanel/>}
function SettingsGroup({title,children}:{title:string;children:React.ReactNode}){return <fieldset><legend>{title}</legend><div>{children}</div></fieldset>}
function Toggle({label,checked,onChange}:{label:string;checked:boolean;onChange:(value:boolean)=>void}){return <label className="network-toggle"><span>{label}</span><input type="checkbox" checked={checked} onChange={event=>onChange(event.target.checked)}/></label>}
function NumberField({label,unit,value,min,max,step=1,onChange}:{label:string;unit:string;value:number;min:number;max:number;step?:number;onChange:(value:number)=>void}){return <label className="network-number-field"><span>{label}</span><span className="network-input-with-unit"><input aria-label={label} type="number" value={value} min={min} max={max} step={step} onChange={event=>onChange(Number(event.target.value))}/><em>{unit}</em></span></label>}
