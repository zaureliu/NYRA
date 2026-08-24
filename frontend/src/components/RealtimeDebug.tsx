import { useEffect, useState } from 'react'

export function RealtimeDebug() {
  const [value, setValue] = useState<Record<string, unknown>>({})
  useEffect(() => { const load=()=>fetch('/api/realtime/debug').then((r)=>r.json()).then(setValue).catch(()=>undefined); void load(); const timer=setInterval(load,1000); return()=>clearInterval(timer) },[])
  const telemetry = value.telemetry as {timeline?:Array<{timestamp:string;event:string}>;last_metrics?:Record<string,unknown>} | undefined
  return <div className="settings-group realtime-debug"><h3>DEVELOPER · REALTIME</h3>
    <p>STATE {String(value.status ?? '—')} · QUEUE {String(value.sentence_queue ?? 0)}</p>
    <pre>{JSON.stringify({metrics:telemetry?.last_metrics,attention:value.attention,reaction:value.reaction,perception:value.perception,avatar:value.avatar},null,2)}</pre>
    <div>{telemetry?.timeline?.slice(-12).map((item,index)=><small key={`${item.timestamp}-${index}`}>{new Date(item.timestamp).toLocaleTimeString('pt-BR',{hour12:false,fractionalSecondDigits:3})} {item.event}<br/></small>)}</div>
  </div>
}
