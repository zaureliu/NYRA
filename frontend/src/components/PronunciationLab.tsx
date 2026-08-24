import { useEffect, useState } from 'react'
import { backendUrl } from '../runtime/backend'

interface Rule { canonical: string; aliases: string[]; category: string; strategy: string; spoken_form?: string | null; provider_overrides: Record<string, string>; enabled: boolean; priority: number }
interface Preview { display?: string; speech_text: string; applied_rules: Array<{term:string; strategy:string; spoken_form?:string}>; detected_terms: string[]; warnings: string[] }

const EMPTY: Rule = { canonical: '', aliases: [], category: 'user_defined', strategy: 'spoken_alias', spoken_form: '', provider_overrides: {}, enabled: true, priority: 100 }

export function PronunciationLab({ speaker }: { speaker: string }) {
  const [rules, setRules] = useState<Rule[]>([])
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<Rule>(EMPTY)
  const [text, setText] = useState('O OpenWrt está conectado ao Proxmox pela VLAN 20. O backend FastAPI usa WebSocket, enquanto o Docker publica o serviço atrás do Nginx.')
  const [provider, setProvider] = useState('edge_tts')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  const load = async () => { const response = await fetch(`/api/pronunciation/lexicon?query=${encodeURIComponent(query)}`); const value = await response.json(); setRules(value.rules ?? []) }
  useEffect(() => { void load() }, [])
  const runPreview = async () => { setBusy(true); try { const response = await fetch('/api/pronunciation/preview', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ text, provider }) }); const value = await response.json(); setPreview(value) } finally { setBusy(false) } }
  const play = async (speech: string) => { const response = await fetch('/api/voice/synthesize', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ provider, voice: provider === 'edge_tts' ? 'pt-BR-ThalitaMultilingualNeural' : provider === 'kokoro' ? 'pf_dora' : 'default', text: speech, state: 'neutral', edge_rate: '-5%', edge_pitch: '+0Hz', edge_volume: '+0%' }) }); const value = await response.json(); if (!response.ok) { setNotice(value.detail ?? 'Provider indisponível'); return }; const audio = new Audio(backendUrl(value.audio_url)); if (speaker !== 'default' && 'setSinkId' in audio) await (audio as HTMLAudioElement & {setSinkId(id:string):Promise<void>}).setSinkId(speaker); await audio.play() }
  const save = async () => { if (!selected.canonical || !selected.spoken_form) return; const response = await fetch('/api/pronunciation/rules', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(selected) }); setNotice(response.ok ? 'Pronúncia salva e ativa sem reiniciar.' : 'Não foi possível salvar'); await load() }

  return <div className="settings-group pronunciation-lab"><h3>PRONUNCIATION LAB PT-BR</h3>
    <div className="settings-grid"><label>BUSCAR TERMO<input value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') void load() }}/></label><label>PROVIDER<select value={provider} onChange={(e) => setProvider(e.target.value)}><option value="default">Auto</option><option value="edge_tts">Edge TTS</option><option value="kokoro">Kokoro</option><option value="chatterbox_multilingual_v3">Chatterbox</option></select></label></div>
    <div className="pronunciation-terms">{rules.slice(0, 30).map((rule) => <button key={rule.canonical} onClick={() => setSelected(rule)}>{rule.canonical}<small>{rule.category}</small></button>)}</div>
    <div className="settings-grid"><label>TERMO<input value={selected.canonical} onChange={(e) => setSelected({...selected, canonical:e.target.value})}/></label><label>ESTRATÉGIA<select value={selected.strategy} onChange={(e) => setSelected({...selected, strategy:e.target.value})}><option value="provider_native">Provider Native</option><option value="spoken_alias">Spoken Alias</option><option value="spell_letters">Spell Letters</option><option value="expand">Expand</option><option value="number_sequence">Number Sequence</option><option value="custom">Custom</option></select></label><label>PRONÚNCIA / SPOKEN FORM<input value={selected.spoken_form ?? ''} onChange={(e) => setSelected({...selected, spoken_form:e.target.value})}/></label></div>
    <label>TEXTO DE TESTE<textarea rows={3} value={text} onChange={(e) => setText(e.target.value)}/></label>
    <div className="settings-actions"><button disabled={busy} onClick={() => void runPreview()}>PREVIEW PIPELINE</button><button disabled={busy} onClick={() => void play(text)}>OUVIR ORIGINAL</button><button disabled={busy || !preview} onClick={() => void play(preview?.speech_text ?? text)}>OUVIR CORRIGIDO</button><button disabled={!selected.canonical} onClick={() => void save()}>SALVAR PARA NYRA</button><button onClick={() => setSelected(EMPTY)}>RESETAR</button></div>
    {preview && <div className="pronunciation-preview"><strong>DISPLAY</strong><p>{text}</p><strong>SPEECH</strong><p>{preview.speech_text}</p><strong>REGRAS APLICADAS</strong><div>{preview.applied_rules.map((rule, index) => <span key={`${rule.term}-${index}`} className="term-chip">{rule.term} · {rule.strategy}{rule.spoken_form ? ` · ${rule.spoken_form}` : ''}</span>)}</div></div>}
    {notice && <p className="lab-notice">{notice}</p>}
  </div>
}
