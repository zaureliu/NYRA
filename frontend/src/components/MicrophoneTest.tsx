import { useState } from 'react'
import { usePushToTalk } from '../hooks/usePushToTalk'
interface Result { text: string; transcription_seconds?: number; vad?: { speech_detected: boolean } }
export function MicrophoneTest({ microphone }: { microphone: string }) {
  const [result, setResult] = useState<Result | null>(null); const [audioUrl, setAudioUrl] = useState(''); const [error, setError] = useState('')
  const handle = async (blob: Blob) => { if (audioUrl) URL.revokeObjectURL(audioUrl); setAudioUrl(URL.createObjectURL(blob)); const body = new FormData(); body.append('audio', blob, 'microphone-test.webm'); const response = await fetch('/api/speech/transcribe', { method: 'POST', body }); const value = await response.json(); if (!response.ok) throw new Error(value.detail ?? 'Falha no teste'); setResult(value) }
  const { recording, start, stop, metrics, capabilities } = usePushToTalk(handle, microphone, .018, 1400, (message) => setError(message))
  return <div className="settings-group mic-test"><h3>MICROPHONE TEST</h3><div className="level-meter"><i style={{ width: `${Math.min(100, metrics.rms * 450)}%` }}/><b style={{ left: `${Math.min(100, metrics.peak * 100)}%` }}/></div>
    <div className="mic-readout"><span>RMS {metrics.rms.toFixed(3)}</span><span>PICO {metrics.peak.toFixed(3)}</span><span className={metrics.clipping ? 'warn' : ''}>{metrics.clipping ? 'CLIPPING' : 'SEM CLIPPING'}</span><span>{metrics.speechDetected ? 'FALA DETECTADA' : 'SILÊNCIO'}</span></div>
    <p>Browser: AEC {capabilities.echoCancellation ? 'on' : 'n/d'} · NS {capabilities.noiseSuppression ? 'on' : 'n/d'} · AGC {capabilities.autoGainControl ? 'on' : 'n/d'}</p>
    <div className="settings-actions"><button className={recording ? 'danger' : ''} onPointerDown={() => void start()} onPointerUp={stop} onPointerLeave={stop}>{recording ? 'SOLTE PARA TESTAR' : 'TESTAR MICROFONE'}</button>{audioUrl && <audio controls src={audioUrl}/>}</div>
    {result && <p className="transcript"><strong>TRANSCRIÇÃO</strong> {result.text}<br/><small>{result.transcription_seconds?.toFixed(2)} s · Silero VAD {result.vad?.speech_detected ? 'detectou fala' : 'não detectou fala'}</small></p>}{error && <p className="lab-notice error">{error}</p>}</div>
}
