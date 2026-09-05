import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { NetworkSettingsPanel, type NetworkSettingsValue } from './NetworkWatchSettings'

const settings: NetworkSettingsValue = {
  enabled:true,voice_alerts:true,desktop_alerts:true,quiet_mode:false,
  critical_voice_in_quiet:false,interface_interval:1,gateway_interval:2,
  internet_interval:5,dns_interval:15,http_interval:30,latency_warning_ms:100,
  latency_critical_ms:200,packet_loss_warning:5,packet_loss_critical:15,
  jitter_warning_ms:40,alert_cooldown_seconds:300,history_retention_days:30,
  dns_target:'cloudflare.com',internet_targets:['1.1.1.1:443','8.8.8.8:53'],
}

describe('Network settings',()=>{
  it('groups monitor, notifications, probes, thresholds, behavior and tests',()=>{
    const html=renderToStaticMarkup(<NetworkSettingsPanel initialSettings={settings}/>)
    for(const heading of ['MONITOR','NOTIFICATIONS','PROBE INTERVALS','THRESHOLDS','BEHAVIOR','TESTS']) expect(html).toContain(heading)
    expect(html).toContain('Latency warning')
    expect(html).toContain('ms')
    expect(html).toContain('SIMULAR LATÊNCIA')
  })
})
