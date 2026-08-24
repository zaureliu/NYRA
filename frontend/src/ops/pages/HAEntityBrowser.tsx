import { useCallback, useEffect, useState } from 'react'
import { apiGet, apiSend } from '../../runtime/api'
import { ActionButton, Empty, ErrorAlert, StatusBadge, formatRelative } from '../ui'
import {
  domainFilters,
  entityMatches,
  verificationLabel,
} from './integrationsHelpers'
import type {
  HAActionResponse,
  HAEntitiesResponse,
  HAEntityDetail,
  HAEntityRow,
} from '../types'

/** Entity browser compacto (prompt11_1 §23-§26). Dados reais do backend;
 * ações executam service call + readback VERIFY — nunca só HTTP 200. */
export function HAEntityBrowser() {
  const [search, setSearch] = useState('')
  const [domain, setDomain] = useState('')
  const [listing, setListing] = useState<HAEntitiesResponse | null>(null)
  const [detail, setDetail] = useState<HAEntityDetail | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [actionInfo, setActionInfo] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams()
      if (domain) params.set('domain', domain)
      if (search.trim()) params.set('search', search.trim())
      params.set('limit', '50')
      const data = await apiGet<HAEntitiesResponse>(
        `/api/home-assistant/entities?${params.toString()}`)
      setListing(data)
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setLoading(false)
    }
  }, [domain, search])

  useEffect(() => {
    const timer = window.setTimeout(() => { void load() }, 250)
    return () => window.clearTimeout(timer)
  }, [load])

  const openDetail = async (entityId: string) => {
    setError('')
    setActionInfo('')
    try {
      setDetail(await apiGet<HAEntityDetail>(
        `/api/home-assistant/entities/${encodeURIComponent(entityId)}`))
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : String(issue))
    }
  }

  const runService = async (service: string) => {
    if (!detail) return
    setError('')
    try {
      const response = await apiSend<HAActionResponse>(
        `/api/home-assistant/entities/${encodeURIComponent(detail.entity_id)}/service`,
        'POST',
        { service },
      )
      setActionInfo(`${service} → ${verificationLabel(response)}`)
      // Readback imediato na UI também (o backend já verificou o efeito).
      await openDetail(detail.entity_id)
      void load()
    } catch (issue) {
      setError(issue instanceof Error ? issue.message : String(issue))
    }
  }

  const visible = (listing?.entities ?? []).filter((row) =>
    entityMatches(row, search))
  const domains = domainFilters(listing?.domains_present ?? [])

  return (
    <div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <input
          type="text"
          placeholder="Buscar entity id, nome ou domínio…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          style={{ flex: '1 1 220px', background: 'var(--ops-bg-2)', border: '1px solid var(--ops-line-strong)', color: 'var(--ops-text)', borderRadius: 6, height: 30, padding: '0 10px', fontSize: 13 }}
        />
        <ActionButton small busy={loading} onClick={() => void load()}>Atualizar</ActionButton>
      </div>

      {domains.length > 0 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
          <button
            type="button"
            className={`ops-chip${domain === '' ? ' active' : ''}`}
            data-tone={domain === '' ? 'ok' : undefined}
            onClick={() => setDomain('')}
          >
            todos
          </button>
          {domains.map((item) => (
            <button
              key={item}
              type="button"
              className={`ops-chip${domain === item ? ' active' : ''}`}
              data-tone={domain === item ? 'ok' : undefined}
              onClick={() => setDomain(domain === item ? '' : item)}
            >
              {item}
            </button>
          ))}
        </div>
      )}

      <ErrorAlert message={error} />
      {actionInfo && (
        <div className="ops-alert info" style={{ marginTop: 8 }}>{actionInfo}</div>
      )}

      <div className="table-scroll" style={{ marginTop: 10 }}>
        <table className="ops-table">
          <thead>
            <tr>
              <th>Entity ID</th><th>Friendly Name</th><th>Domain</th>
              <th>State</th><th>Last Changed</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((row) => (
              <tr key={row.entity_id} style={{ cursor: 'pointer' }}
                onClick={() => { void openDetail(row.entity_id) }}>
                <td>{row.entity_id}</td>
                <td>{row.friendly_name || '—'}</td>
                <td>{row.domain}</td>
                <td><StatusBadge state={row.state === 'unavailable' ? 'OFFLINE' : row.state.toUpperCase()} label={row.state} /></td>
                <td>{formatRelative(relativeSeconds(row.last_changed))}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!loading && visible.length === 0 && !error && (
          <Empty text="Nenhuma entidade encontrada (ou integração não READY)." />
        )}
      </div>

      {detail && (
        <div className="ops-card" style={{ marginTop: 12 }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6 }}>
            <strong>{detail.entity_id}</strong>
            <StatusBadge state={detail.state.toUpperCase()} label={detail.state} />
            <span style={{ flex: 1 }} />
            <ActionButton small onClick={() => setDetail(null)}>Fechar</ActionButton>
          </div>
          <dl className="ops-kv">
            <dt>friendly_name</dt>
            <dd>{String(safeAttr(detail, 'friendly_name') ?? '—')}</dd>
            <dt>last_changed</dt><dd>{detail.last_changed || '—'}</dd>
            <dt>last_updated</dt><dd>{detail.last_updated || '—'}</dd>
          </dl>
          <details style={{ marginTop: 6 }}>
            <summary className="ops-hint" style={{ cursor: 'pointer' }}>Atributos seguros</summary>
            <pre className="ops-code" style={{ maxHeight: 200 }}>
              {JSON.stringify(detail.safe_attributes ?? detail.attributes ?? {}, null, 2)}
            </pre>
          </details>
          {(detail.supported_services ?? []).filter((s) =>
            ['turn_on', 'turn_off', 'toggle'].includes(s)).length > 0 && (
            <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
              {(detail.supported_services ?? [])
                .filter((service) => ['turn_on', 'turn_off', 'toggle'].includes(service))
                .map((service) => (
                  <ActionButton key={service} small variant={service === 'turn_off' ? 'danger' : 'primary'}
                    onClick={() => void runService(service)}>
                    {serviceLabel(service)}
                  </ActionButton>
                ))}
            </div>
          )}
        </div>
      )}
    </div>
  )

  function safeAttr(target: HAEntityDetail, key: string): unknown {
    const source = target.safe_attributes ?? target.attributes ?? {}
    return (source as Record<string, unknown>)[key]
  }
}

function serviceLabel(service: string): string {
  if (service === 'turn_on') return 'Turn On'
  if (service === 'turn_off') return 'Turn Off'
  if (service === 'toggle') return 'Toggle'
  return service
}

function relativeSeconds(iso: string): number | null {
  if (!iso) return null
  const parsed = Date.parse(iso) / 1000
  if (!Number.isFinite(parsed) || parsed <= 0) return null
  return Math.max(0, Date.now() / 1000 - parsed)
}
