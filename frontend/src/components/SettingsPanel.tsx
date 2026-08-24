import { useEffect, useState } from 'react'
import { AudioSettings } from './AudioSettings'
import { BrainLab } from './BrainLab'
import { Live2DSettings } from './Live2DSettings'
import { NetworkWatchSettings } from './NetworkWatchSettings'
import { SentinelSettings } from './SentinelSettings'
import { SkillsSettings } from './SkillsSettings'
import { VisualSettings } from './VisualSettings'
import type { MicrophoneAvailability, MicrophonePermission } from '../hooks/audioDevices'
import type { AudioSettingsValue } from '../hooks/useAudioSettings'

interface Props {
  audio: AudioSettingsValue
  audioNotice?: string
  devices: MediaDeviceInfo[]
  microphoneAvailability: MicrophoneAvailability
  microphonePermission: MicrophonePermission
  onAudioSave: (value: AudioSettingsValue) => Promise<unknown>
}

export function SettingsPanel({
  audio, audioNotice, devices, microphoneAvailability, microphonePermission, onAudioSave,
}: Props) {
  const [adultMode, setAdultMode] = useState(false)
  const [adultConfirmed, setAdultConfirmed] = useState(false)

  useEffect(() => { fetch('/api/settings/adult-mode').then((response) => response.json()).then((value) => setAdultMode(Boolean(value.enabled))).catch(() => undefined) }, [])

  const toggleAdultMode = async (enabled: boolean) => {
    let confirmed = adultConfirmed
    if (enabled && !confirmed) {
      confirmed = window.confirm('Você confirma que é maior de 18 anos e deseja habilitar linguagem madura não explícita?')
      setAdultConfirmed(confirmed)
      if (!confirmed) return
    }
    const response = await fetch('/api/settings/adult-mode', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled, confirmed_18_plus: enabled ? confirmed : false }),
    })
    if (response.ok) setAdultMode(enabled)
  }

  return <div className="settings-workspace">
    <section className="workspace-card device-settings-card"><header className="section-title"><div><span className="eyebrow">CONVERSATION ENGINE V2</span><h3>Áudio e conversa</h3><p>Somente controles ligados ao runtime real.</p></div></header><AudioSettings value={audio} devices={devices} microphoneAvailability={microphoneAvailability} microphonePermission={microphonePermission} notice={audioNotice} onSave={onAudioSave}/></section>
    <section className="workspace-card"><label className="adult-toggle"><span><input type="checkbox" checked={adultMode} onChange={(event) => void toggleAdultMode(event.target.checked)}/> Modo adulto (+18)</span><small>Linguagem madura não explícita; exige confirmação.</small></label></section>

    <div className="settings-accordions">
      <details className="settings-accordion" open>
        <summary><span><b>01</b><strong>Cérebro local</strong><small>Modelo Ollama, residência e benchmark.</small></span><i>+</i></summary>
        <div className="settings-section-grid"><BrainLab/></div>
      </details>
      <details className="settings-accordion">
        <summary><span><b>02</b><strong>Visual e avatar</strong><small>Renderer, Desktop Presence e bridge Live2D.</small></span><i>+</i></summary>
        <div className="settings-section-grid"><VisualSettings/><Live2DSettings/></div>
      </details>
      <details className="settings-accordion">
        <summary><span><b>03</b><strong>Integrações e permissões</strong><small>Network Watch, Sentinel e skills allowlisted.</small></span><i>+</i></summary>
        <div className="settings-section-grid"><NetworkWatchSettings/><SentinelSettings/><SkillsSettings/></div>
      </details>
    </div>
  </div>
}
