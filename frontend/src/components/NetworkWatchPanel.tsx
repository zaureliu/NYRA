import { useEffect, useMemo, useState } from 'react'

interface Sample { internet_latency_ms:number|null;jitter_ms:number;packet_loss_percent:number;download_bps:number;timestamp:string }
interface Status { enabled:boolean;status:string;uptime_seconds:number;snapshot:Sample&{gateway:string|null;gateway_alive:boolean|null;dns_ok:boolean|null;interface_name:string|null;ip_address:string|null};active_alerts:string[] }

export function NetworkWatchPanel() {
  const [data,setData]=useState<Status|null>(null); const [samples,setSamples]=useState<Sample[]>([]); const [minutes,setMinutes]=useState(1)
  useEffect(()=>{ const load=async()=>{ const [status,metrics]=await Promise.all([fetch('/api/network-watch/status'),fetch(`/api/network-watch/metrics?minutes=${minutes}`)]); if(status.ok)setData(await status.json());if(metrics.ok)setSamples((await metrics.json()).samples)};void load();const timer=setInterval(()=>void load(),3000);return()=>clearInterval(timer)},[minutes])
  const points=useMemo(()=>{ const values=samples.map(item=>item.internet_latency_ms??0);const max=Math.max(1,...values);return values.map((value,index)=>`${values.length<2?0:index/(values.length-1)*100},${30-value/max*28}`).join(' ')},[samples])
  const snapshot=data?.snapshot
  return <section className="panel compact-panel network-panel"><header className="panel-header"><span>NETWORK WATCH</span><small>{data?.enabled?data.status.toUpperCase():'OFF'}</small></header>
    <div className="network-overview"><strong>{snapshot?.internet_latency_ms==null?'—':`${snapshot.internet_latency_ms.toFixed(0)} ms`}</strong><span>Internet</span><strong>{snapshot?.jitter_ms?.toFixed(1)??'—'} ms</strong><span>Jitter</span><strong>{snapshot?.packet_loss_percent?.toFixed(1)??'—'}%</strong><span>Loss</span></div>
    <svg className="network-graph" viewBox="0 0 100 32" preserveAspectRatio="none" aria-label="Histórico de latência"><polyline points={points}/></svg>
    <div className="network-meta"><span>{snapshot?.interface_name??'sem interface'}</span><span>GW {snapshot?.gateway_alive?'OK':snapshot?.gateway_alive===false?'DOWN':'—'}</span><span>DNS {snapshot?.dns_ok?'OK':snapshot?.dns_ok===false?'FAIL':'—'}</span><select value={minutes} onChange={(event)=>setMinutes(Number(event.target.value))}><option value={1}>1 min</option><option value={5}>5 min</option><option value={15}>15 min</option></select></div>
  </section>
}
