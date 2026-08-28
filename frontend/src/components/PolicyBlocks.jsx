import { useState } from 'react'
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
} from './ui.jsx'
import { blockedDecisions, useAsync } from '../core/api.js'
import { formatINR, formatTime } from '../core/format.js'

const TONE_BY_CATEGORY = {
  fraud: 'danger',
  terminal: 'warn',
  retry_limit: 'warn',
  cooldown: 'info',
  duplicate: 'info',
  spend_cap: 'info',
}

function keyOf(item) {
  return `${item.event_id}:${item.proposed_intervention || ''}:${item.evaluated_at}`
}

function CategoryGrid({ categories, count }) {
  return (
    <div className="cat-grid">
      <div className="cat-card cat-card--total">
        <div className="cat-card__value">{count}</div>
        <div className="cat-card__label">blocked interventions</div>
      </div>
      {categories.map((c) => (
        <div key={c.key} className="cat-card">
          <Badge tone={TONE_BY_CATEGORY[c.key] || 'neutral'}>{c.label}</Badge>
          <div className="cat-card__value">{c.count}</div>
        </div>
      ))}
    </div>
  )
}

function EvidenceRow({ label, value }) {
  return (
    <div className="evidence-row">
      <span className="evidence-label">{label}</span>
      <span className="evidence-value mono">{value ?? '—'}</span>
    </div>
  )
}

function DetailPanel({ item, onClose }) {
  const ev = item.evidence || {}
  const lastTime = ev.last_attempted_at ? formatTime(ev.last_attempted_at) : null
  return (
    <div className="detail-panel" role="region" aria-label="Why wasn't this recovered?">
      <div className="detail-panel__head">
        <h3 className="detail-panel__title">Why wasn't this recovered?</h3>
        <button className="btn btn--ghost" onClick={onClose} aria-label="Close detail">
          Close
        </button>
      </div>

      <div className="detail-panel__verdict">
        <div className="detail-verdict-row">
          <span className="detail-verdict-label">Decision</span>
          <Badge tone="danger">DENIED</Badge>
        </div>
        <div className="detail-verdict-row">
          <span className="detail-verdict-label">Policy category</span>
          <Badge tone={TONE_BY_CATEGORY[item.category] || 'neutral'}>{item.category_label}</Badge>
        </div>
        <div className="detail-verdict-row">
          <span className="detail-verdict-label">Rule</span>
          <span className="evidence-value">{item.rule_label || item.denial_reason}</span>
        </div>
      </div>

      <div className="detail-panel__grid">
        <div className="evidence-group">
          <h4 className="evidence-group__title">Event</h4>
          <EvidenceRow label="Event" value={item.event_id} />
          <EvidenceRow label="Customer" value={item.customer_id} />
          <EvidenceRow label="Amount" value={formatINR(item.amount_paise)} />
          <EvidenceRow label="Risk flag" value={item.risk_flag} />
        </div>
        <div className="evidence-group">
          <h4 className="evidence-group__title">Proposed action</h4>
          <EvidenceRow label="Intervention" value={item.proposed_intervention} />
          <EvidenceRow label="Evaluated at" value={formatTime(item.evaluated_at)} />
          <EvidenceRow label="Previous attempts" value={ev.previous_attempts} />
          <EvidenceRow label="Last intervention" value={ev.last_intervention} />
          <EvidenceRow label="Last attempt" value={lastTime} />
        </div>
      </div>

      <div className="detail-panel__system">
        <span className="detail-verdict-label">System action</span>
        <Badge tone="danger">STOPPED</Badge>
        <span className="detail-note">
          No operator override exists in V1. The deterministic policy gate is
          authoritative and this intervention was not executed.
        </span>
      </div>
    </div>
  )
}

export default function PolicyBlocks() {
  const { status, data, error } = useAsync(
    (_signal) => blockedDecisions({ limit: 200 }),
    [],
  )
  const [selectedKey, setSelectedKey] = useState(null)

  const selected = data?.blocked?.find((b) => keyOf(b) === selectedKey) || null

  return (
    <div className="screen">
      <header className="page-head">
        <div>
          <h1 className="page-title">Policy &amp; Blocked Actions</h1>
          <p className="page-subtitle">
            Interventions denied by the deterministic policy gate, from persisted
            decisions. Safety stops are never overridable in V1.
          </p>
        </div>
      </header>

      {status === 'loading' && <LoadingBlock label="Loading blocked decisions…" />}
      {status === 'error' && (
        <ErrorState message={error?.message} retry={() => window.location.reload()} />
      )}

      {status === 'ok' && data.count === 0 && (
        <Card title="Blocked Actions">
          <EmptyState
            title="No blocked interventions"
            message="Every evaluated intervention so far passed the policy gates. None were denied."
          />
        </Card>
      )}

      {status === 'ok' && data.count > 0 && (
        <div className="blocks-layout">
          <div className="blocks-main">
            <Card
              title="Block Categories"
              subtitle="Why interventions were withheld"
              action={<Badge tone="neutral">{data.count} total</Badge>}
            >
              <CategoryGrid categories={data.categories || []} count={data.count} />
            </Card>

            <Card
              title="Denied Interventions"
              subtitle="Select a row to see why it was not recovered"
            >
              <div className="table-wrap">
                <table className="data-table data-table--selectable">
                  <thead>
                    <tr>
                      <th scope="col">Event</th>
                      <th scope="col">Customer</th>
                      <th scope="col">Amount</th>
                      <th scope="col">Risk</th>
                      <th scope="col">Proposed action</th>
                      <th scope="col">Denial rule</th>
                      <th scope="col">Category</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(data.blocked || []).map((b, i) => {
                      const k = keyOf(b)
                      const isSel = k === selectedKey
                      return (
                        <tr
                          key={`${k}-${i}`}
                          className={isSel ? 'row--selected' : ''}
                          tabIndex={0}
                          role="button"
                          aria-pressed={isSel}
                          aria-label={`Show why ${b.event_id} was not recovered`}
                          onClick={() => setSelectedKey(isSel ? null : k)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              setSelectedKey(isSel ? null : k)
                            }
                          }}
                        >
                          <td className="mono">{b.event_id}</td>
                          <td className="mono">{b.customer_id}</td>
                          <td className="mono">{formatINR(b.amount_paise)}</td>
                          <td>
                            <Badge tone={b.risk_flag === 'fraud_suspect' ? 'danger' : 'success'}>
                              {b.risk_flag}
                            </Badge>
                          </td>
                          <td className="mono">{b.proposed_intervention || '—'}</td>
                          <td>
                            <Badge tone={TONE_BY_CATEGORY[b.category] || 'neutral'}>
                              {b.rule_label || b.denial_reason}
                            </Badge>
                          </td>
                          <td>{b.category_label}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </Card>
          </div>

          {selected ? (
            <DetailPanel item={selected} onClose={() => setSelectedKey(null)} />
          ) : (
            <aside className="blocks-hint">
              <EmptyState
                title="Select a blocked intervention"
                message="Pick a denied row to inspect the persisted evidence behind the stop."
              />
            </aside>
          )}
        </div>
      )}
    </div>
  )
}
