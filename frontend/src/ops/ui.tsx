import { Fragment, type ReactNode } from 'react'
import { useEffect, useState } from 'react'

/** Badge de status padronizado: ícone (dot) + rótulo textual + cor (§15). */
export function StatusBadge({ state, label }: { state: string; label?: string }) {
  const normalized = normalizeState(state)
  return (
    <span className="ops-badge" data-state={normalized}>
      {label ?? prettify(normalized)}
    </span>
  )
}

const STATE_ALIASES: Record<string, string> = {
  ONLINE: 'READY',
  HEALTHY: 'READY',
  CONNECTED: 'READY',
  OK: 'PASS',
  RUNNING: 'READY',
  ACTIVE: 'READY',
  AVAILABLE: 'READY',
}

export function normalizeState(state: string): string {
  const value = String(state || 'UNKNOWN').toUpperCase().replace(/\s+/g, '_')
  return STATE_ALIASES[value] ?? value
}

export function prettify(state: string): string {
  return state.replace(/_/g, ' ')
}

/** Toggle real com estado de carregamento — nunca decorativo. */
export function Toggle({
  checked, onChange, disabled, label,
}: {
  checked: boolean
  onChange: (value: boolean) => void
  disabled?: boolean
  label: string
}) {
  return (
    <label className={`ops-toggle${disabled ? ' disabled' : ''}`}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="ops-toggle-track" aria-hidden="true" />
      <span className="ops-toggle-label">{label}</span>
    </label>
  )
}

/** Botão com loading embutido e proteção contra duplo clique (§48). */
export function ActionButton({
  onClick, children, busy, disabled, variant, small, title,
}: {
  onClick: () => Promise<void> | void
  children: ReactNode
  busy?: boolean
  disabled?: boolean
  variant?: 'primary' | 'danger'
  small?: boolean
  title?: string
}) {
  return (
    <button
      type="button"
      className={`ops-btn${variant ? ` ${variant}` : ''}${small ? ' small' : ''}`}
      disabled={disabled || busy}
      title={title}
      onClick={() => { void onClick() }}
    >
      {busy ? <span className="ops-loading" aria-hidden="true" /> : null}
      {children}
    </button>
  )
}

export function Card({ title, sub, actions, children }: {
  title?: ReactNode
  sub?: ReactNode
  actions?: ReactNode
  children?: ReactNode
}) {
  return (
    <section className="ops-card">
      {(title || actions) && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: sub || children ? 8 : 0 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            {title ? <h3 className="ops-card-title">{title}</h3> : null}
            {sub ? <div className="ops-card-sub">{sub}</div> : null}
          </div>
          {actions}
        </div>
      )}
      {children}
    </section>
  )
}

export function ErrorAlert({ message, hint }: { message: string; hint?: string }) {
  if (!message) return null
  return (
    <div className="ops-alert error" role="alert">
      <strong>Falha:</strong> {message}
      {hint ? <div className="ops-hint" style={{ marginTop: 4 }}>{hint}</div> : null}
    </div>
  )
}

export function Empty({ text }: { text: string }) {
  return <div className="ops-empty">{text}</div>
}

export function KeyValue({ rows }: { rows: Array<[string, ReactNode]> }) {
  return (
    <dl className="ops-kv">
      {rows.map(([key, value]) => (
        <FragmentRow key={key} k={key} v={value} />
      ))}
    </dl>
  )
}

function FragmentRow({ k, v }: { k: string; v: ReactNode }) {
  return (
    <Fragment key={k}>
      <dt>{k}</dt>
      <dd>{v}</dd>
    </Fragment>
  )
}

/** Copia texto para a área de transferência com feedback temporário. */
export function useCopyFeedback(): [boolean, () => void] {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const timer = window.setTimeout(() => setCopied(false), 1500)
    return () => window.clearTimeout(timer)
  }, [copied])
  return [copied, () => setCopied(true)]
}

export function formatRelative(secondsAgo: number | null | undefined): string {
  if (secondsAgo === null || secondsAgo === undefined) return '—'
  if (secondsAgo < 5) return 'agora'
  if (secondsAgo < 60) return `${Math.round(secondsAgo)}s atrás`
  if (secondsAgo < 3600) return `${Math.round(secondsAgo / 60)}min atrás`
  if (secondsAgo < 86400) return `${Math.round(secondsAgo / 3600)}h atrás`
  return `${Math.round(secondsAgo / 86400)}d atrás`
}

export function formatMs(value: number | null | undefined, suffix = 'ms'): string {
  if (value === null || value === undefined) return '—'
  return `${Math.round(value * 10) / 10}${suffix}`
}
