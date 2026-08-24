import { useVisualSettings, type VisualSettings as VisualSettingsValue } from '../avatar/visualSettings'

export function VisualSettings() {
  const [settings, setSettings] = useVisualSettings()
  const change = <K extends keyof VisualSettingsValue>(key: K, value: VisualSettingsValue[K]) => setSettings({ ...settings, [key]: value })
  return <div className="settings-group visual-settings"><h3>NYRA AVATAR V2</h3><div className="settings-grid">
    <label>AVATAR VERSION<select value="v2" disabled title="Única opção instalada nesta versão"><option value="v2">Avatar V2 oficial</option></select></label>
    <label>RENDERER<select value="layered" disabled title="Único renderer suportado pelo Avatar V2"><option value="layered">Unified SVG Layers</option></select></label>
    <label>CHARACTER VIEW<select value="bust" disabled title="Enquadramento oficial do asset atual"><option value="bust">Chest-up oficial</option></select></label>
    <label>OVERLAY SCALE<select value={settings.overlayScale} onChange={(e) => change('overlayScale', Number(e.target.value))}>{[.5,.75,1,1.25,1.5].map((value) => <option key={value} value={value}>{Math.round(value * 100)}%</option>)}</select></label>
  </div><div className="toggle-grid">
    <label><input type="checkbox" checked={settings.speechBubble} onChange={(e) => change('speechBubble', e.target.checked)}/> Speech Bubble</label>
    <label><input type="checkbox" checked={settings.idleAnimations} onChange={(e) => change('idleAnimations', e.target.checked)}/> Idle Animations</label>
    <label><input type="checkbox" checked={settings.eyeMovement} onChange={(e) => change('eyeMovement', e.target.checked)}/> Eye Movement</label>
    <label><input type="checkbox" checked={settings.blink} onChange={(e) => change('blink', e.target.checked)}/> Blink</label>
    <label><input type="checkbox" checked={settings.alwaysOnTop} onChange={(e) => change('alwaysOnTop', e.target.checked)}/> Always on Top</label>
    <label><input type="checkbox" checked={settings.clickThrough} onChange={(e) => change('clickThrough', e.target.checked)}/> Click-through</label>
    <label><input type="checkbox" checked={settings.debug} onChange={(e) => change('debug', e.target.checked)}/> Visual Debug</label>
  </div><p>O Avatar V2 usa um canvas único para master, olhos, boca e indicadores dos headphones. Live2D continua opcional para este renderer.</p></div>
}
