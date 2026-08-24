import { useState } from 'react'
import { usePolling } from '../hooks'
import { Card, ErrorAlert, StatusBadge, ActionButton } from '../ui'
import type { AboutInfo, ReleaseHealthInfo, ReleaseRevalidationInfo } from '../types'

function formatStamp(value: number | string | undefined): string {
  if (value == null) return '—'
  const numeric = typeof value === 'string' ? Date.parse(value) / 1000 : Number(value)
  if (!Number.isFinite(numeric) || numeric <= 0) return String(value)
  return new Date(numeric * 1000).toLocaleString('pt-BR')
}

export function AboutPage() {
  const about = usePolling<AboutInfo>('/api/about', 60000)
  const release = usePolling<ReleaseHealthInfo>('/api/release/health', 15000)
  const [copied, setCopied] = useState(false)
  const [revalidating, setRevalidating] = useState(false)
  const [revalidateError, setRevalidateError] = useState('')

  const copyBundle = async () => {
    try {
      const bundle = await fetch('/api/support/bundle').then((response) => response.text())
      await navigator.clipboard.writeText(bundle)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      /* clipboard indisponível */
    }
  }

  const revalidate = async () => {
    setRevalidating(true)
    setRevalidateError('')
    try {
      await fetch('/api/release/revalidate', { method: 'POST' })
    } catch (issue) {
      setRevalidateError(issue instanceof Error ? issue.message : String(issue))
    } finally {
      setRevalidating(false)
      void release.refresh()
    }
  }

  const revalidation = release.data?.revalidation
  const running = revalidation?.state === 'RUNNING'
  const progress = revalidation?.progress

  return (
    <div>
      <header className="ops-page-header">
        <div>
          <h1 className="ops-page-title">Sobre</h1>
          <p className="ops-page-subtitle">Versão unificada do produto e saúde de release.</p>
        </div>
      </header>

      <ErrorAlert message={about.error} />
      <ErrorAlert message={revalidateError} />

      <div className="ops-grid-2">
        <Card title={`NYRA ${about.data?.version ?? '…'}`} sub="Local-first · sem nuvem obrigatória">
          <dl className="ops-kv">
            <dt>Versão</dt><dd>{about.data?.version ?? '—'}</dd>
            {Object.entries(about.data?.components ?? {}).map(([key, value]) => (
              <FragmentRowInline key={key} k={key} v={value} />
            ))}
            <dt>Modelo ativo</dt><dd>{about.data?.model ?? '—'}</dd>
            <dt>Build (git)</dt><dd>{release.data?.git_head ?? '—'}</dd>
          </dl>
          <div className="ops-hint" style={{ marginTop: 10 }}>{about.data?.license_note}</div>
        </Card>

        <Card
          title="Release Readiness"
          actions={<StatusBadge state={
            release.data?.freshness === 'STALE' && release.data?.state !== 'RED'
              ? 'STALE'
              : release.data?.state ?? 'UNKNOWN'
          } />}
          sub="Critérios objetivos; pendências são honestas, não escondidas"
        >
          <div className="ops-hint" style={{ marginBottom: 8 }}>
            Última validação: {formatStamp(latestValidation(release.data))} · freshness: {release.data?.freshness ?? '—'}
          </div>
          {(release.data?.criteria ?? []).map((criterion) => (
            <div key={criterion.id} style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '6px 0', borderBottom: '1px solid var(--ops-line)' }}>
              <StatusBadge state={criterion.state} />
              <div style={{ flex: 1 }}>
                <strong style={{ fontSize: 13 }}>{criterion.id}</strong>
                <div className="ops-card-sub">{criterion.detail}</div>
              </div>
            </div>
          ))}
          {running && (
            <div className="ops-alert info" style={{ marginTop: 10 }} role="status">
              Revalidação em execução ({progress?.step_index ?? '?'}/{progress?.total_steps ?? '?'} · {progress?.current_step ?? revalidation?.current_step ?? 'preparando'}) — iniciada {formatStamp(revalidation?.started_at)}
            </div>
          )}
          <div style={{ display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
            <ActionButton small onClick={() => void revalidate()} disabled={running}
              busy={running && !revalidating}>
              {running ? 'Revalidando…' : 'Revalidar'}
            </ActionButton>
            <ActionButton small onClick={() => void copyBundle()}>{copied ? 'Copiado ✓' : 'Copiar support bundle'}</ActionButton>
          </div>
        </Card>
      </div>
    </div>
  )
}

function latestValidation(release: ReleaseHealthInfo | null): number | undefined {
  const ages = (release?.criteria ?? [])
    .map((item) => item.artifact_age_seconds)
    .filter((age): age is number => typeof age === 'number')
  if (!ages.length) return undefined
  return Date.now() / 1000 - Math.min(...ages)
}

function FragmentRowInline({ k, v }: { k: string; v: string }) {
  return (
    <>
      <dt>{k}</dt>
      <dd>{v}</dd>
    </>
  )
}
