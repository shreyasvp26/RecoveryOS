import { useCallback, useState } from 'react'
import { Card, Badge, LoadingBlock, ErrorState, EmptyState } from './ui.jsx'
import { recoveryQueue, executeRecovery, useAsync } from '../core/api.js'
import { formatINR, formatTime, humanize } from '../core/format.js'

/**
 * Recovery Operations Center — the operational view of RecoveryOS.
 *
 * Every row here is a projection the backend derived from persisted records.
 * This screen computes no state of its own: it does not decide whether an
 * action is permitted, does not rank interventions, and never calls a payment
 * provider. Pressing Execute asks the server to run its own authoritative
 * flow; the server re-derives the diagnosis, the policy decision and the
 * economic selection, and returns the row it actually recorded.
 *
 * TWO THINGS THIS SCREEN MUST NEVER BLUR
 * --------------------------------------
 * 1. Execution is not recovery. A real Payment Link that was created reads
 *    "Waiting for payment" until a verified webhook confirms it was paid.
 * 2. Simulated is not real. A SIMULATED action is labelled as such on every
 *    row and never shows a recovered amount.
 */

const TABS = [
  { key: '', label: 'All' },
  { key: 'RECOMMENDED', label: 'Recommended' },
  { key: 'POLICY_ALLOWED', label: 'Policy allowed' },
  { key: 'SELECTED', label: 'Selected' },
  { key: 'EXECUTED', label: 'Executed' },
  { key: 'PENDING_OUTCOME', label: 'Pending outcome' },
  { key: 'RECOVERED', label: 'Recovered' },
  { key: 'BLOCKED', label: 'Blocked' },
]

const STATE_TONE = {
  RECOVERED: 'success',
  EXECUTED: 'info',
  PENDING_OUTCOME: 'warn',
  BLOCKED: 'danger',
  FAILED: 'danger',
  SELECTED: 'brand',
  POLICY_ALLOWED: 'brand',
  RECOMMENDED: 'neutral',
  NOT_CLASSIFIED: 'neutral',
}

const SORTS = [
  ['newest', 'Newest'],
  ['amount_desc', 'Amount (high to low)'],
  ['expected_recovery_desc', 'Expected recovery'],
  ['oldest_pending_outcome', 'Oldest pending outcome'],
]

/** The execution mode, worded so a demo viewer cannot mistake one for the other. */
function ModeBadge({ execution }) {
  if (!execution) return <span className="dim">—</span>
  if (execution.execution_mode === 'REAL_RAZORPAY') {
    return <Badge tone="info">REAL_RAZORPAY · Payment Link</Badge>
  }
  return <Badge tone="neutral">SIMULATED</Badge>
}

function PolicyCell({ policy }) {
  if (policy.status === 'BLOCKED') {
    return <Badge tone="danger">{policy.denial_rule_label || 'Blocked'}</Badge>
  }
  if (policy.status === 'ALLOWED') return <Badge tone="success">Allowed</Badge>
  return <span className="dim">Not evaluated</span>
}

/** What the operator can do with this row, given only what the server recorded. */
function RowActions({ row, busy, onExecute, onOpenTrace, onExplain }) {
  const state = row.lifecycle_state
  const hasHistory = row.execution !== null || state === 'BLOCKED'

  return (
    <div className="ops-actions">
      {row.actionable && (
        <button
          className="btn btn--primary"
          disabled={busy}
          onClick={() => onExecute(row.event_id)}
        >
          {busy ? 'Executing…' : 'Execute'}
        </button>
      )}
      {state === 'BLOCKED' && (
        <button className="btn btn--ghost" onClick={() => onExplain(row.event_id)}>
          Why blocked?
        </button>
      )}
      {state === 'PENDING_OUTCOME' && <span className="ops-waiting">Waiting for payment</span>}
      {row.outcome.state === 'PROVIDER_RESULT_UNKNOWN' && (
        // Not an error and not a success: the provider was called and never
        // said what it did. Offering Execute here could create a second link.
        <span className="ops-waiting">Execution uncertain</span>
      )}
      {state === 'RECOVERED' && (
        <span className="ops-recovered">
          Recovered {formatINR(row.outcome.recovered_amount_paise)}
        </span>
      )}
      {hasHistory && (
        <button className="btn btn--ghost" onClick={() => onOpenTrace(row.event_id)}>
          View trace
        </button>
      )}
    </div>
  )
}

function QueueRow({ row, busy, onExecute, onOpenTrace, onExplain, expanded, onToggle }) {
  const action =
    row.selection?.selected_intervention ??
    row.execution?.intervention ??
    (row.diagnosis?.candidate_interventions?.[0] || null)

  return (
    <>
      <tr>
        <td>
          <button className="ops-linkish" onClick={() => onToggle(row.event_id)}>
            <span className="mono">{row.event_id}</span>
          </button>
          <div className="ops-sub">{row.customer_id}</div>
        </td>
        <td>{formatINR(row.amount_paise)}</td>
        <td>{humanize(row.failure_reason)}</td>
        <td>
          {row.diagnosis ? (
            <>
              {humanize(row.diagnosis.root_cause_category)}
              <div className="ops-sub">
                confidence {(Number(row.diagnosis.confidence) * 100).toFixed(0)}%
              </div>
            </>
          ) : (
            <span className="dim">Not diagnosed</span>
          )}
        </td>
        <td>{action ? humanize(action) : <span className="dim">—</span>}</td>
        <td>
          {row.selection?.expected_value_paise != null ? (
            formatINR(row.selection.expected_value_paise)
          ) : (
            <span className="dim">—</span>
          )}
        </td>
        <td>
          <PolicyCell policy={row.policy} />
        </td>
        <td>
          <ModeBadge execution={row.execution} />
        </td>
        <td>
          <Badge tone={STATE_TONE[row.lifecycle_state] || 'neutral'}>
            {humanize(row.lifecycle_state)}
          </Badge>
        </td>
        <td>
          <RowActions
            row={row}
            busy={busy}
            onExecute={onExecute}
            onOpenTrace={onOpenTrace}
            onExplain={onExplain}
          />
        </td>
      </tr>
      {expanded && (
        <tr className="ops-detail-row">
          <td colSpan={10}>
            <div className="kv-grid">
              <div className="kv">
                <span className="kv__k">Diagnosis</span>
                <span className="kv__v">
                  {row.diagnosis?.reasoning || 'No AI diagnosis is recorded for this payment.'}
                </span>
              </div>
              <div className="kv">
                <span className="kv__k">Policy</span>
                <span className="kv__v">
                  {row.policy.status === 'BLOCKED'
                    ? `${row.policy.denial_rule_label} — every candidate was refused by the deterministic gate.`
                    : row.policy.status === 'ALLOWED'
                      ? `Authorized: ${row.policy.allowed_interventions.map(humanize).join(', ')}`
                      : 'The policy gate has not evaluated this payment yet.'}
                </span>
              </div>
              <div className="kv">
                <span className="kv__k">Selection reason</span>
                <span className="kv__v">{row.selection?.selection_reason || '—'}</span>
              </div>
              <div className="kv">
                <span className="kv__k">Outcome</span>
                <span className="kv__v">{row.outcome.note}</span>
              </div>
              <div className="kv">
                <span className="kv__k">Payment link</span>
                <span className="kv__v mono">{row.execution?.payment_link_id || '—'}</span>
              </div>
              <div className="kv">
                <span className="kv__k">Failed at</span>
                <span className="kv__v">{formatTime(row.event_timestamp)}</span>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

export default function RecoveryOps({ onNavigate }) {
  const [tab, setTab] = useState('')
  const [mode, setMode] = useState('')
  const [risk, setRisk] = useState('')
  const [sort, setSort] = useState('newest')
  const [expanded, setExpanded] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [notice, setNotice] = useState(null)
  const [reloadKey, setReloadKey] = useState(0)

  const { status, data, error } = useAsync(
    (_signal) =>
      recoveryQueue({
        lifecycle_state: tab || undefined,
        execution_mode: mode || undefined,
        risk_flag: risk || undefined,
        sort,
      }),
    [tab, mode, risk, sort, reloadKey],
  )

  const reload = useCallback(() => setReloadKey((key) => key + 1), [])

  const execute = (eventId) => {
    setBusyId(eventId)
    setNotice(null)
    executeRecovery(eventId)
      .then((body) => {
        // The server's own words for what it did. Nothing here infers success.
        setNotice({ tone: 'ok', message: describeResult(body) })
      })
      .catch((err) => setNotice({ tone: 'error', message: err.message }))
      .finally(() => {
        setBusyId(null)
        reload()
      })
  }

  const openTrace = (eventId) => {
    if (onNavigate) onNavigate('trace', { eventId })
  }

  if (status === 'loading') return <LoadingBlock label="Loading the recovery queue…" />
  if (status === 'error') return <ErrorState message={error?.message} retry={reload} />

  const counts = data.state_counts || {}
  const rows = data.rows || []

  return (
    <div className="screen">
      <header className="page-head">
        <div>
          <h1 className="page-title">Recovery Operations Center</h1>
          <p className="page-subtitle">
            Failed payments that need attention, what RecoveryOS recommends, whether
            policy allows it, and whether the money actually came back.
          </p>
        </div>
        <div className="source-legend">
          <span className="legend-dot" style={{ background: 'var(--info)' }} />
          REAL_RAZORPAY Test Mode · Payment Link only
        </div>
      </header>

      <div className="tabs">
        {TABS.map(({ key, label }) => (
          <button
            key={key || 'all'}
            className={`tab ${tab === key ? 'tab--active' : ''}`}
            onClick={() => setTab(key)}
          >
            {label}
            {key && <span className="ops-count">{counts[key] ?? 0}</span>}
          </button>
        ))}
      </div>

      <div className="ops-filters">
        <label className="ops-filter">
          <span className="kv__k">Execution mode</span>
          <select className="select" value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="">Any</option>
            <option value="REAL_RAZORPAY">REAL_RAZORPAY</option>
            <option value="SIMULATED">SIMULATED</option>
          </select>
        </label>
        <label className="ops-filter">
          <span className="kv__k">Risk</span>
          <select className="select" value={risk} onChange={(e) => setRisk(e.target.value)}>
            <option value="">Any</option>
            <option value="normal">Normal</option>
            <option value="fraud_suspect">Fraud suspect</option>
          </select>
        </label>
        <label className="ops-filter">
          <span className="kv__k">Sort</span>
          <select className="select" value={sort} onChange={(e) => setSort(e.target.value)}>
            {SORTS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <button className="btn btn--ghost" onClick={reload}>
          Refresh
        </button>
      </div>

      <div className="meta-line">
        Showing {data.count} of {data.total_matched} matching payments · {data.scanned} scanned
        {data.truncated_scan && ' (scan limit reached)'}
      </div>

      {notice && (
        <p className={`panel-note ${notice.tone === 'error' ? 'ops-notice--error' : ''}`}>
          {notice.message}
        </p>
      )}

      <p className="panel-note">
        Execute asks the server to run its own authoritative flow. The deterministic
        policy gate decides what is permitted, the economic optimizer chooses among
        what it authorized, and this screen can override neither. A created Payment
        Link is real but unpaid until a verified Razorpay webhook confirms it.
      </p>

      {rows.length === 0 ? (
        <EmptyState
          title="Nothing in this view"
          message="No persisted payment matches the current filters."
        />
      ) : (
        <Card>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Payment</th>
                  <th>Amount</th>
                  <th>Failure</th>
                  <th>Diagnosis</th>
                  <th>Action</th>
                  <th>Expected recovery</th>
                  <th>Policy</th>
                  <th>Mode</th>
                  <th>State</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <QueueRow
                    key={row.event_id}
                    row={row}
                    busy={busyId === row.event_id}
                    expanded={expanded === row.event_id}
                    onToggle={(id) => setExpanded(expanded === id ? null : id)}
                    onExecute={execute}
                    onOpenTrace={openTrace}
                    onExplain={(id) => setExpanded(id)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  )
}

/** Turn the server's execution result into one honest sentence. */
function describeResult(body) {
  if (body.status === 'execution_success') {
    const execution = body.execution
    if (execution?.execution_mode === 'REAL_RAZORPAY') {
      return `A real Razorpay Test Mode Payment Link was created for ${body.event_id}. It is waiting for payment — this is not a recovery yet.`
    }
    return `${humanize(body.selected_intervention)} was executed for ${body.event_id} in SIMULATED mode. No provider was contacted and no revenue is claimed.`
  }
  if (body.status === 'execution_failed') {
    if (body.row?.outcome?.state === 'PROVIDER_RESULT_UNKNOWN') {
      return `The provider was called for ${body.event_id} and did not return a result RecoveryOS could read. A real Payment Link may exist, so this action will not be attempted again.`
    }
    return `The execution attempt for ${body.event_id} failed: ${body.execution?.detail || 'no detail was reported'}.`
  }
  if (body.status === 'no_action') {
    return body.detail || `Nothing was executed for ${body.event_id}.`
  }
  return body.detail || `${body.event_id}: ${humanize(body.status)}`
}
