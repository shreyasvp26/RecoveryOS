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

export default function PolicyBlocks() {
  const { status, data, error } = useAsync(
    (_signal) => blockedDecisions({ limit: 200 }),
    [],
  )

  return (
    <div className="screen">
      <header className="page-head">
        <div>
          <h1 className="page-title">Policy &amp; Blocked Actions</h1>
          <p className="page-subtitle">
            Interventions denied by the deterministic policy gate, from persisted
            decisions.
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
        <>
          <Card
            title="Block Categories"
            subtitle="Why interventions were withheld"
            action={<Badge tone="neutral">{data.count} total</Badge>}
          >
            <CategoryGrid categories={data.categories} count={data.count} />
          </Card>

          <Card
            title="Denied Interventions"
            subtitle="Each persisted denial with its triggering rule"
          >
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Event</th>
                    <th>Customer</th>
                    <th>Amount</th>
                    <th>Risk</th>
                    <th>Proposed action</th>
                    <th>Denial rule</th>
                    <th>Category</th>
                    <th>Evaluated</th>
                  </tr>
                </thead>
                <tbody>
                  {data.blocked.map((b, i) => (
                    <tr key={`${b.event_id}-${i}`}>
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
                      <td className="mono dim">{formatTime(b.evaluated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </div>
  )
}
