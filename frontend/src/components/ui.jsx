import { useState } from 'react'

/** Shared presentational primitives for the RecoveryOS Command Center. */

const TONES = {
  success: 'var(--success)',
  warn: 'var(--warn)',
  danger: 'var(--danger)',
  info: 'var(--info)',
  brand: 'var(--brand)',
  neutral: 'var(--text-dim)',
}

const TONE_SOFT = {
  success: 'var(--success-soft)',
  warn: 'var(--warn-soft)',
  danger: 'var(--danger-soft)',
  info: 'var(--info-soft)',
  brand: 'var(--brand-soft)',
  neutral: 'transparent',
}

export function Card({ title, subtitle, action, children, className = '' }) {
  return (
    <section className={`card ${className}`}>
      {(title || action) && (
        <header className="card__head">
          <div>
            {title && <h3 className="card__title">{title}</h3>}
            {subtitle && <p className="card__subtitle">{subtitle}</p>}
          </div>
          {action && <div className="card__action">{action}</div>}
        </header>
      )}
      <div className="card__body">{children}</div>
    </section>
  )
}

export function Badge({ tone = 'neutral', children }) {
  return (
    <span
      className="badge"
      style={{
        color: TONES[tone],
        background: TONE_SOFT[tone],
        borderColor: `${TONES[tone]}44`,
      }}
    >
      {children}
    </span>
  )
}

export function Stat({ label, value, sub, tone = 'neutral', hint }) {
  return (
    <div className="stat">
      <div className="stat__label">
        {label}
        {hint && <span className="stat__hint" title={hint}>ⓘ</span>}
      </div>
      <div className="stat__value" style={{ color: TONES[tone] }}>{value}</div>
      {sub && <div className="stat__sub">{sub}</div>}
    </div>
  )
}

export function Spinner() {
  return <span className="spinner" role="status" aria-label="loading" />
}

export function LoadingBlock({ label = 'Loading…' }) {
  return (
    <div className="state-block">
      <Spinner />
      <span>{label}</span>
    </div>
  )
}

export function ErrorBanner({ message, onRetry, retry }) {
  const [key, setKey] = useState('')
  const handleRetry = onRetry || retry || (() => window.location.reload())
  const msgStr = typeof message === 'string' ? message : (message?.message || '')
  const isAuthError =
    msgStr.toLowerCase().includes('operator credentials') ||
    msgStr.toLowerCase().includes('operator api key')

  const handleSave = (e) => {
    e.preventDefault()
    const trimmed = key.trim()
    if (trimmed) {
      if (typeof window !== 'undefined' && window.localStorage) {
        window.localStorage.setItem('recoveryos_operator_api_key', trimmed)
      }
      if (typeof onRetry === 'function') {
        onRetry()
      } else if (typeof retry === 'function') {
        retry()
      } else if (typeof window !== 'undefined') {
        window.location.reload()
      }
    }
  }

  return (
    <div className="state-block state-block--error">
      <div className="state-block__title">Failed to load data</div>
      <p>{msgStr || 'The API could not be reached.'}</p>
      {isAuthError && (
        <form onSubmit={handleSave} style={{ display: 'flex', gap: '8px', marginTop: '12px', justifyContent: 'center' }}>
          <input
            type="password"
            className="input"
            placeholder="Enter operator API key"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            style={{ maxWidth: '320px' }}
          />
          <button type="submit" className="btn btn--primary">
            Save Key &amp; Retry
          </button>
        </form>
      )}
      {!isAuthError && handleRetry && (
        <button className="btn" onClick={handleRetry}>
          Retry
        </button>
      )}
    </div>
  )
}

export function ErrorState({ message, retry, onRetry }) {
  return <ErrorBanner message={message} retry={retry} onRetry={onRetry} />
}

export function EmptyState({ title = 'No data yet', message }) {
  return (
    <div className="state-block state-block--empty">
      <div className="state-block__title">{title}</div>
      <p>{message}</p>
    </div>
  )
}

export function SkeletonRows({ rows = 3, cols = 4 }) {
  return (
    <div className="skeleton-grid" style={{ gridTemplateColumns: `repeat(${cols}, 1fr)` }}>
      {Array.from({ length: rows * cols }).map((_, i) => (
        <div key={i} className="skeleton" />
      ))}
    </div>
  )
}
