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

export function ErrorState({ message, retry }) {
  return (
    <div className="state-block state-block--error">
      <div className="state-block__title">Failed to load data</div>
      <p>{message || 'The API could not be reached.'}</p>
      {retry && (
        <button className="btn" onClick={retry}>
          Retry
        </button>
      )}
    </div>
  )
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
