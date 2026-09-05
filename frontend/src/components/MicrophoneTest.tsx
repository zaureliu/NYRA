import { useEffect, useState } from 'react'
import { usePushToTalk } from '../hooks/usePushToTalk'
import type { STTResult } from '../runtime/stt'
import { apiGet } from '../runtime/api'

export function MicrophoneTest({ microphone }: { microphone: string }) {
  const [result, setResult] = useState<STTResult | null>(null)
  const [interim, setInterim] = useState('')
  const [providerState, setProviderState] = useState('')
  const [error, setError] = useState('')
  const [benchmark, setBenchmark] = useState(false)
  const [reference, setReference] = useState('')
  const [phrases, setPhrases] = useState<string[]>([])
  const [waiting, setWaiting] = useState(false)
  useEffect(() => { void apiGet<{ phrases: string[] }>('/api/stt/benchmark/phrases').then((value) => setPhrases(value.phrases)).catch(() => undefined) }, [])
  const handle = async (value: STTResult) => { setResult(value); setInterim(''); setWaiting(false) }
  const { recording, start, stop, metrics, capabilities } = usePushToTalk(handle, microphone, .018, 2200,
    (message) => { setError(message); setWaiting(false) },
    { mode: 'diagnostic', benchmark, reference, onEvent: (event) => {
      if (event.type === 'interim') setInterim(event.transcript?.text ?? '')
      if (event.type === 'state') setProviderState(event.state ?? '')
    } })
  return <div className="settings-group mic-test"><h3>MICROPHONE TEST</h3>
    <div className="level-meter"><i style={{ width: `${Math.min(100, metrics.rms * 450)}%` }}/><b style={{ left: `${Math.min(100, metrics.peak * 100)}%` }}/></div>
    <div className="mic-readout"><span>RMS {metrics.rms.toFixed(3)}</span><span>PICO {metrics.peak.toFixed(3)}</span><span>{metrics.clipping ? 'CLIPPING' : 'SEM CLIPPING'}</span><span>{metrics.speechDetected ? 'FALA DETECTADA' : 'SILÊNCIO'}</span></div>
    <p>Browser: AEC {capabilities.echoCancellation ? 'on' : 'n/d'} · NS {capabilities.noiseSuppression ? 'on' : 'n/d'} · AGC {capabilities.autoGainControl ? 'on' : 'n/d'}</p>
    <label><input type="checkbox" checked={benchmark} disabled={recording || waiting} onChange={(event) => setBenchmark(event.target.checked)} /> Comparar Deepgram e Faster-Whisper com o mesmo áudio</label>
    {benchmark && <div className="settings-grid"><label>Frase de referência<select value={reference} onChange={(event) => setReference(event.target.value)}>
      <option value="">Fala livre (sem WER)</option>{phrases.map((phrase) => <option key={phrase} value={phrase}>{phrase}</option>)}
    </select></label></div>}
    <p className="ops-hint">Use fala natural. O teste usa o provider selecionado, não cria um turno no chat e mantém o áudio somente em memória durante a comparação.</p>
    <div className="settings-actions"><button disabled={waiting && !recording} onClick={() => {
      if (recording) { setWaiting(true); stop() }
      else { setResult(null); setError(''); setInterim(''); setWaiting(true); void start() }
    }}>{recording ? 'ENCERRAR TESTE' : waiting ? 'PROCESSANDO…' : 'TESTAR MICROFONE'}</button></div>
    {interim && <p className="transcript" aria-live="polite"><strong>INTERIM</strong> {interim}</p>}
    {providerState && <p className="ops-hint">{providerState}</p>}
    {result?.transcription && <p className="transcript"><strong>FINAL</strong> {result.transcription.text || '(nenhuma fala detectada)'}<br/>
      <small>{result.transcription.provider} · {result.transcription.language}</small></p>}
    {result?.comparison && <details open><summary>Comparação · mesmo sample</summary><pre className="ops-code">{JSON.stringify(result.comparison, null, 2)}</pre></details>}
    {result?.diagnostics && <details><summary>Latência e eventos</summary><pre className="ops-code">{JSON.stringify(result.diagnostics, null, 2)}</pre></details>}
    {error && <p className="lab-notice error">{error}</p>}
  </div>
}
