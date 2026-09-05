import { useMemo, useState, type ReactNode } from 'react'
import { apiSend } from '../../runtime/api'
import { usePolling } from '../hooks'
import { ActionButton, Card, Empty, ErrorAlert, StatusBadge, Toggle } from '../ui'
import { HardwareEngineeringCard } from './HardwareEngineeringCard'

interface UsbStatus {
  monitor_state: string
  connected_count: number
  known_count: number
  unknown_count: number
  system_internal_count?: number
  event_source?: string
  last_event?: { timestamp?: string; description?: string; event_type?: string } | null
  last_error?: string | null
}

export interface UsbDevice {
  device_id: string
  name: string
  friendly_name?: string | null
  category?: string | null
  manufacturer?: string | null
  product?: string | null
  vid?: string | null
  pid?: string | null
  vid_pid?: string | null
  serial?: string | null
  device_instance_id?: string | null
  container_id?: string | null
  device_class?: string | null
  com_port?: string | null
  drive_letter?: string | null
  volume_label?: string | null
  filesystem?: string | null
  size_bytes?: number | null
  interface_name?: string | null
  network_state?: string | null
  status: string
  relevance: string
  registered: boolean
  known: boolean
  trusted: boolean
  note?: string | null
  first_seen: string
  last_seen: string
  last_connection: string
  last_disconnection?: string | null
  identity_confidence: string
  identity_basis: string
  identity_changed?: boolean
}

interface UsbHistory {
  event_id: number
  timestamp: string
  event_type: string
  device_id: string
  name: string
  friendly_name?: string | null
  vid_pid?: string | null
  com_port?: string | null
  drive_letter?: string | null
  known: boolean
  level: string
  description: string
}

const CATEGORIES = [
  '', 'Hardware Lab', 'Armazenamento', 'Áudio', 'Vídeo', 'HID', 'Rede',
  'Serial', 'Smartphone', 'Hub', 'Outro',
]

function shownName(device: UsbDevice): string {
  return device.friendly_name || device.name
}

function formatDate(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('pt-BR')
}

function formatSize(value?: number | null): string {
  if (!value) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1 }
  return `${size.toFixed(unit >= 3 ? 1 : 0)} ${units[unit]}`
}

function available(rows: Array<[string, string | number | null | undefined]>): Array<[string, string | number]> {
  return rows.filter((row): row is [string, string | number] => row[1] !== null && row[1] !== undefined && row[1] !== '')
}

export function UsbDevicesPage() {
  const status = usePolling<UsbStatus>('/api/usb/status', 4000, { noStore: true })
  const connected = usePolling<{ devices: UsbDevice[] }>('/api/usb/devices/connected', 4000, { noStore: true })
  const known = usePolling<{ devices: UsbDevice[] }>('/api/usb/devices/known', 6000, { noStore: true })
  const history = usePolling<{ history: UsbHistory[] }>('/api/usb/history?limit=200', 5000, { noStore: true })
  const [selected, setSelected] = useState<UsbDevice | null>(null)
  const [editing, setEditing] = useState<UsbDevice | null>(null)
  const [friendlyName, setFriendlyName] = useState('')
  const [category, setCategory] = useState('')
  const [trusted, setTrusted] = useState(false)
  const [note, setNote] = useState('')
  const [filter, setFilter] = useState('ALL')
  const [busy, setBusy] = useState('')
  const [actionError, setActionError] = useState('')
  const [notice, setNotice] = useState('')

  const refreshAll = () => {
    status.refresh(); connected.refresh(); known.refresh(); history.refresh()
  }

  const beginEdit = (device: UsbDevice) => {
    setEditing(device)
    setFriendlyName(device.friendly_name || device.name)
    setCategory(device.category || '')
    setTrusted(device.trusted)
    setNote(device.note || '')
    setActionError('')
  }

  const save = async () => {
    if (!editing) return
    setBusy(`save:${editing.device_id}`)
    setActionError('')
    try {
      await apiSend(`/api/usb/devices/${editing.device_id}`, 'PUT', {
        friendly_name: friendlyName.trim() || editing.name,
        category: category || null,
        trusted,
        note: note.trim() || null,
        registered: true,
      })
      setNotice(`${friendlyName.trim() || editing.name} salvo no registry USB.`)
      setEditing(null)
      refreshAll()
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally { setBusy('') }
  }

  const toggleTrusted = async (device: UsbDevice) => {
    setBusy(`trust:${device.device_id}`)
    setActionError('')
    try {
      await apiSend(`/api/usb/devices/${device.device_id}`, 'PUT', {
        registered: true, trusted: !device.trusted,
      })
      refreshAll()
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally { setBusy('') }
  }

  const forget = async (device: UsbDevice) => {
    if (!window.confirm(`Esquecer o registro de ${shownName(device)}?`)) return
    setBusy(`forget:${device.device_id}`)
    setActionError('')
    try {
      await apiSend(`/api/usb/devices/${device.device_id}`, 'DELETE')
      setNotice(`${shownName(device)} removido dos dispositivos conhecidos.`)
      refreshAll()
    } catch (issue) {
      setActionError(issue instanceof Error ? issue.message : String(issue))
    } finally { setBusy('') }
  }

  const filteredHistory = useMemo(() => (history.data?.history ?? []).filter((event) => {
    if (filter === 'ALL') return true
    if (filter === 'CONNECTED') return event.event_type.includes('connected')
    if (filter === 'DISCONNECTED') return event.event_type.includes('disconnected')
    if (filter === 'NEW') return event.event_type.includes('unknown') || event.event_type.includes('identity_changed')
    if (filter === 'COM') return event.event_type.includes('com_changed')
    return true
  }), [filter, history.data?.history])

  const lastEvent = status.data?.last_event
  const errors = [status.error, connected.error, known.error, history.error, actionError].filter(Boolean)

  return (
    <div>
      <header className="ops-page-header">
        <div>
          <h1 className="ops-page-title">Dispositivos USB</h1>
          <p className="ops-page-subtitle">
            Presença e metadados PnP do Windows. A KAZUMI não lê arquivos, áudio, teclas ou conteúdo dos dispositivos.
          </p>
        </div>
        <div className="ops-header-spacer" />
        <ActionButton busy={status.loading} onClick={async () => {
          setBusy('refresh')
          try { await apiSend('/api/usb/refresh', 'POST'); refreshAll() }
          catch (issue) { setActionError(issue instanceof Error ? issue.message : String(issue)) }
          finally { setBusy('') }
        }}>Atualizar</ActionButton>
      </header>

      <ErrorAlert message={errors[0] || ''} hint="O monitor continua em fallback quando a fonte PnP nativa fica indisponível." />
      {notice && <div className="ops-alert info">{notice}</div>}

      <h2 className="ops-section-title">Visão Geral</h2>
      <div className="usb-overview-grid">
        <Card title="USB Monitor"><StatusBadge state={status.data?.monitor_state || 'STARTING'} /></Card>
        <Card title="Conectados"><div className="usb-stat-value">{status.data?.connected_count ?? '—'}</div></Card>
        <Card title="Conhecidos"><div className="usb-stat-value">{status.data?.known_count ?? '—'}</div></Card>
        <Card title="Desconhecidos"><div className="usb-stat-value">{status.data?.unknown_count ?? '—'}</div></Card>
        <Card title="Último evento" sub={lastEvent?.event_type || status.data?.event_source || 'Aguardando evento'}>
          <div>{lastEvent?.description || 'Nenhum evento após o baseline.'}</div>
          {lastEvent?.timestamp && <div className="ops-hint">{formatDate(lastEvent.timestamp)}</div>}
        </Card>
      </div>

      <h2 className="ops-section-title">Conectados Agora</h2>
      <HardwareEngineeringCard />
      <Card>
        {(connected.data?.devices ?? []).length === 0 ? <Empty text="Nenhum dispositivo USB relevante conectado." /> : (
          <div className="table-scroll"><table className="ops-table">
            <thead><tr><th>Nome</th><th>Categoria</th><th>VID:PID / Serial</th><th>Interface</th><th>Status</th><th>Confiável</th><th>Ações</th></tr></thead>
            <tbody>{(connected.data?.devices ?? []).map((device) => (
              <tr key={device.device_id}>
                <td><strong>{shownName(device)}</strong>{device.friendly_name && <div className="ops-hint">{device.name}</div>}</td>
                <td>{device.category || '—'}</td>
                <td>{device.vid_pid || '—'}<div className="ops-hint">{device.serial || 'sem serial USB'}</div></td>
                <td>{device.com_port || device.drive_letter || device.interface_name || '—'}</td>
                <td><StatusBadge state={device.registered ? 'CONNECTED' : 'UNKNOWN'} label={device.registered ? 'CONHECIDO' : 'DESCONHECIDO'} /></td>
                <td>{device.trusted ? 'SIM' : 'NÃO'}</td>
                <td><div className="usb-actions">
                  {!device.registered && <ActionButton small onClick={() => beginEdit(device)}>Registrar</ActionButton>}
                  <ActionButton small onClick={() => setSelected(device)}>Detalhes</ActionButton>
                </div></td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </Card>

      <h2 className="ops-section-title">Dispositivos Conhecidos</h2>
      <Card sub="Confiável significa reconhecido pelo operador; não implica autenticação, certificação ou proteção contra spoofing.">
        {(known.data?.devices ?? []).length === 0 ? <Empty text="Nenhum dispositivo USB registrado." /> : (
          <div className="table-scroll"><table className="ops-table">
            <thead><tr><th>Nome</th><th>Categoria</th><th>Última conexão</th><th>Última interface</th><th>Status</th><th>Ações</th></tr></thead>
            <tbody>{(known.data?.devices ?? []).map((device) => (
              <tr key={device.device_id}>
                <td><strong>{shownName(device)}</strong><div className="ops-hint">{device.vid_pid || device.identity_basis}</div></td>
                <td>{device.category || '—'}</td>
                <td>{formatDate(device.last_connection)}</td>
                <td>{device.com_port || device.drive_letter || '—'}</td>
                <td><StatusBadge state={device.status} /></td>
                <td><div className="usb-actions">
                  <ActionButton small onClick={() => beginEdit(device)}>Editar</ActionButton>
                  <ActionButton small busy={busy === `trust:${device.device_id}`} onClick={() => toggleTrusted(device)}>{device.trusted ? 'Não confiável' : 'Confiável'}</ActionButton>
                  <ActionButton small variant="danger" busy={busy === `forget:${device.device_id}`} onClick={() => forget(device)}>Esquecer</ActionButton>
                </div></td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </Card>

      <h2 className="ops-section-title">Histórico de Eventos</h2>
      <Card actions={
        <label className="usb-filter">Filtro
          <select value={filter} onChange={(event) => setFilter(event.target.value)}>
            <option value="ALL">Todos</option><option value="CONNECTED">Conectado</option>
            <option value="DISCONNECTED">Removido</option><option value="NEW">Novo</option>
            <option value="COM">COM</option>
          </select>
        </label>
      }>
        {filteredHistory.length === 0 ? <Empty text="Nenhum evento nesse filtro." /> : (
          <div className="table-scroll"><table className="ops-table">
            <thead><tr><th>Quando</th><th>Evento</th><th>Dispositivo</th><th>Interface</th><th>Nível</th></tr></thead>
            <tbody>{filteredHistory.map((event) => (
              <tr key={event.event_id}>
                <td>{formatDate(event.timestamp)}</td><td>{event.description}</td>
                <td>{event.friendly_name || event.name}<div className="ops-hint">{event.vid_pid || ''}</div></td>
                <td>{event.com_port || event.drive_letter || '—'}</td><td><StatusBadge state={event.level === 'WARNING' ? 'WARN' : event.level} label={event.level} /></td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </Card>

      {selected && <UsbModal title="Detalhes do dispositivo" onClose={() => setSelected(null)}>
        <div className="usb-details">
          {available([
            ['Nome', selected.name], ['Nome amigável', selected.friendly_name], ['Categoria', selected.category],
            ['Fabricante', selected.manufacturer], ['Produto', selected.product], ['VID', selected.vid], ['PID', selected.pid],
            ['Serial', selected.serial], ['Device Instance ID', selected.device_instance_id], ['Container ID', selected.container_id],
            ['Classe', selected.device_class], ['Porta COM', selected.com_port], ['Drive', selected.drive_letter],
            ['Volume', selected.volume_label], ['Filesystem', selected.filesystem], ['Tamanho', selected.size_bytes ? formatSize(selected.size_bytes) : null],
            ['Interface', selected.interface_name], ['Primeira vez visto', formatDate(selected.first_seen)], ['Última vez visto', formatDate(selected.last_seen)],
            ['Última conexão', formatDate(selected.last_connection)], ['Status', selected.status], ['Confiável', selected.trusted ? 'SIM' : 'NÃO'],
            ['Confidence', selected.identity_confidence], ['Fingerprint', selected.device_id], ['Base da identidade', selected.identity_basis],
          ]).map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}
        </div>
      </UsbModal>}

      {editing && <UsbModal title={editing.registered ? 'Editar dispositivo' : 'Registrar dispositivo'} onClose={() => setEditing(null)}>
        <div className="ops-field"><label>Nome amigável</label><input type="text" value={friendlyName} onChange={(event) => setFriendlyName(event.target.value)} /></div>
        <div className="ops-field"><label>Categoria</label><select value={category} onChange={(event) => setCategory(event.target.value)}>
          {CATEGORIES.map((value) => <option key={value || 'none'} value={value}>{value || 'Sem categoria'}</option>)}
        </select></div>
        <div className="ops-field"><Toggle checked={trusted} onChange={setTrusted} label="Confiável (reconhecido pelo usuário)" /></div>
        <div className="ops-field"><label>Observação</label><textarea rows={3} value={note} onChange={(event) => setNote(event.target.value)} /></div>
        <ErrorAlert message={actionError} />
        <div className="usb-modal-actions"><ActionButton onClick={() => setEditing(null)}>Cancelar</ActionButton><ActionButton variant="primary" busy={busy.startsWith('save:')} onClick={save}>Salvar</ActionButton></div>
      </UsbModal>}
    </div>
  )
}

function UsbModal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return <div className="usb-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section className="usb-modal" role="dialog" aria-modal="true" aria-label={title}>
      <div className="usb-modal-header"><h3>{title}</h3><button type="button" onClick={onClose} aria-label="Fechar">×</button></div>
      {children}
    </section>
  </div>
}
