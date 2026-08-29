import { useState } from 'react'
import { Card, Badge, LoadingBlock, ErrorState, EmptyState } from './ui.jsx'
import { listIncidents, incidentEvents, replayIncident, useAsync } from '../core/api.js'
import { formatINR, formatTime, humanize } from '../core/format.js'

/**
 * Revenue Health — RecoveryOS's system-level view.
 *
 * Every value rendered here is computed by the backend detector from the
 * persisted workload and its simulated evaluation. Nothing on this screen is
 * hardcoded, and no example incident is ever shown: if the dataset produces no
 * degradation, this screen says so rather than pretending otherwise.
 *
 * LANGUAGE: these are potential recovery-performance degradations observed in
 * a controlled evaluation, never a claim that a bank or provider is down, and
 * never a claim about actual merchant revenue.
 */

const SEVERITY_TONE = {
  CRITICAL: 'danger',
  HIGH: 'danger',
  MEDIUM: 'warn',
  LOW: 'info',
}

/** Render integer basis points as a percentage string (display only). */
function formatBps(bps) {
  if (bps === null || bps === undefined) return '—'
  return `${(Number(bps) / 100).toFixed(1)}%`
}

/** Render a basis-point movement as signed percentage points. */
function formatPpDelta(bps) {
  if (bps === null || bps === undefined) return '—'
  const pp = Number(bps) / 100
  const arrow = pp > 0 ? '↑' : pp < 0 ? '↓' : ''
  return `${arrow}${Math.abs(pp).toFixed(1)} pp`
}

function windowLabel(window) {
  if (!window) return '—'
  return `${formatTime(window.start)} → ${formatTime(window.end)}`
}

function IncidentCard({ incident, selected, onSelect }) {
  const tone = SEVERITY_TONE[incident.severity] || 'neutral'
  return (
    <button
      className={`incident-card ${selected ? 'incident-card--active' : ''}`}
      onClick={() => onSelect(incident.incident_id)}
    >
      <div className="incident-card__head">
        <Badge tone={tone}>{incident.severity}</Badge>
        <span className="incident-card__status">{incident.status}</span>
      </div>
      <div className="incident-card__segment">{incident.segment.label}</div>
      <div className="incident-card__metric">
        Recovery rate {formatPpDelta(incident.deltas.recovery_rate_delta_bps)}
      </div>
      <div className="incident-card__money">
        {formatINR(incident.impact.simulated_revenue_at_risk_paise)}
      </div>
      <div className="incident-card__note">simulated revenue at risk</div>
    </button>
  )
}

function WhatChanged({ incident }) {
  const rows = [
    {
      label: 'Recovery rate',
      baseline: formatBps(incident.baseline.recovery_rate_bps),
      current: formatBps(incident.current.recovery_rate_bps),
      delta: formatPpDelta(incident.deltas.recovery_rate_delta_bps),
    },
    {
      label: 'Unrecovered rate',
      baseline: formatBps(incident.baseline.unrecovered_rate_bps),
      current: formatBps(incident.current.unrecovered_rate_bps),
      delta: formatPpDelta(incident.deltas.unrecovered_rate_delta_bps),
    },
  ]
  return (
    <div className="table-wrap">
      <table className="data-table">
      <thead>
        <tr>
          <th>Metric</th>
          <th>Baseline window</th>
          <th>Observation window</th>
          <th>Change</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.label}>
            <td>{row.label}</td>
            <td>{row.baseline}</td>
            <td>{row.current}</td>
            <td>{row.delta}</td>
          </tr>
        ))}
      </tbody>
      </table>
    </div>
  )
}

function AffectedEvents({ incidentId, onOpenTrace }) {
  const { status, data, error } = useAsync((_signal) => incidentEvents(incidentId), [incidentId])

  if (status === 'loading') return <LoadingBlock label="Loading affected payments…" />
  if (status === 'error') return <ErrorState message={error?.message} />
  if (!data?.count) {
    return <EmptyState title="No affected payments" message="This incident covers no unrecovered payments." />
  }

  return (
    <div className="affected">
      <p className="panel-note">
        {data.count} payments in {data.segment.label} stayed unrecovered in the
        observation window. Selecting one opens its existing Event Decision Trace.
      </p>
      <div className="table-wrap">
        <table className="data-table">
        <thead>
          <tr>
            <th>Event</th>
            <th>Amount</th>
            <th>Failure reason</th>
            <th>Timestamp</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {data.events.map((item) => (
            <tr key={item.event.event_id}>
              <td className="mono">{item.event.event_id}</td>
              <td>{formatINR(item.event.amount_paise)}</td>
              <td>{humanize(item.event.failure_reason)}</td>
              <td>{formatTime(item.event.timestamp)}</td>
              <td>
                <button className="btn btn--ghost" onClick={() => onOpenTrace(item.event.event_id)}>
                  Decision trace
                </button>
              </td>
            </tr>
          ))}
        </tbody>
        </table>
      </div>
    </div>
  )
}

function PolicyInvestigation({ incidentId }) {
  const [run, setRun] = useState({ status: 'idle', data: null, error: null })

  const start = () => {
    setRun({ status: 'running', data: null, error: null })
    replayIncident(incidentId)
      .then((data) => setRun({ status: 'ok', data, error: null }))
      .catch((err) => setRun({ status: 'error', data: null, error: err }))
  }

  return (
    <div className="investigation">
      <p className="panel-note">
        Replays the affected payments through the existing Policy Lab: the same
        events and the same classifications, with only the policy configuration
        changed. The comparison is SIMULATED, performs no payment action, and
        does not change the policy the live system runs on.
      </p>
      <button className="btn btn--primary" onClick={start} disabled={run.status === 'running'}>
        {run.status === 'running' ? 'Replaying affected batch…' : 'Investigate with Policy Lab'}
      </button>

      {run.status === 'error' && <ErrorState message={run.error?.message} retry={start} />}
      {run.status === 'ok' && (
        <>
          <div className="meta-line">
            Affected batch of {run.data.event_count} payments · {run.data.replay_mode} ·{' '}
            {run.data.incident_replay_id}
          </div>
          <div className="table-wrap">
            <table className="data-table">
            <thead>
              <tr>
                <th>Policy</th>
                <th>Simulated recovered</th>
                <th>vs current policy</th>
                <th>Interventions</th>
                <th>Blocked</th>
              </tr>
            </thead>
            <tbody>
              {run.data.scenarios.map((arm) => (
                <tr key={arm.scenario.scenario_id}>
                  <td>
                    {arm.scenario.name}
                    {arm.is_reference && <span className="tag">reference</span>}
                  </td>
                  <td>{formatINR(arm.metrics.financial.simulated_recovered_revenue_paise)}</td>
                  <td>{formatINR(arm.vs_reference.incremental_recovered_revenue_paise)}</td>
                  <td>{arm.metrics.intervention.total_interventions}</td>
                  <td>{arm.metrics.safety.total_blocked_interventions}</td>
                </tr>
              ))}
            </tbody>
            </table>
          </div>
          <p className="panel-note">
            These payments are affected precisely because they stayed unrecovered
            under the current policy, so the reference arm recovers nothing on
            this batch by construction. Read the comparison as “would an
            alternative policy have recovered any of these?”, not as an overall
            ranking of policies — the Policy Lab’s full-workload comparison
            answers that.
          </p>
        </>
      )}
    </div>
  )
}

function IncidentDetail({ incident, onOpenTrace }) {
  const [tab, setTab] = useState('evidence')
  const contributor = incident.evidence.leading_observed_contributor

  return (
    <div className="incident-detail">
      <div className="incident-detail__head">
        <div>
          <h3 className="card__title">
            Recovery performance degradation — {incident.segment.label}
          </h3>
          <p className="card__subtitle">
            Potential payment degradation detected in the observed dataset. Not an
            outage claim and not a production revenue figure.
          </p>
        </div>
        <Badge tone={SEVERITY_TONE[incident.severity] || 'neutral'}>{incident.severity}</Badge>
      </div>

      <div className="kpi-grid">
        <div className="stat">
          <div className="stat__label">Observed recovery rate</div>
          <div className="stat__value">{formatBps(incident.current.recovery_rate_bps)}</div>
          <div className="stat__sub">baseline {formatBps(incident.baseline.recovery_rate_bps)}</div>
        </div>
        <div className="stat">
          <div className="stat__label">Change</div>
          <div className="stat__value" style={{ color: 'var(--danger)' }}>
            {formatPpDelta(incident.deltas.recovery_rate_delta_bps)}
          </div>
          <div className="stat__sub">recovery-rate movement</div>
        </div>
        <div className="stat">
          <div className="stat__label">Affected payments</div>
          <div className="stat__value">{incident.impact.affected_event_count}</div>
          <div className="stat__sub">
            of {incident.impact.current_window_events} in the observation window
          </div>
        </div>
        <div className="stat">
          <div className="stat__label">Simulated revenue at risk</div>
          <div className="stat__value" style={{ color: 'var(--warn)' }}>
            {formatINR(incident.impact.simulated_revenue_at_risk_paise)}
          </div>
          <div className="stat__sub">modelled estimate</div>
        </div>
      </div>

      <div className="tabs">
        {[
          ['evidence', 'What changed'],
          ['events', 'Affected events'],
          ['policy', 'Policy Lab'],
        ].map(([key, label]) => (
          <button
            key={key}
            className={`tab ${tab === key ? 'tab--active' : ''}`}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'evidence' && (
        <div className="incident-evidence">
          <WhatChanged incident={incident} />

          <div className="kv-grid">
            <div className="kv">
              <span className="kv__k">Bank</span>
              <span className="kv__v">{incident.segment.bank || 'all banks'}</span>
            </div>
            <div className="kv">
              <span className="kv__k">Payment method</span>
              <span className="kv__v">{incident.segment.payment_method || 'all methods'}</span>
            </div>
            <div className="kv">
              <span className="kv__k">Failure reason</span>
              <span className="kv__v">{incident.segment.failure_reason || 'all reasons'}</span>
            </div>
            <div className="kv">
              <span className="kv__k">Observation window</span>
              <span className="kv__v">{windowLabel(incident.windows.current)}</span>
            </div>
            <div className="kv">
              <span className="kv__k">Baseline window</span>
              <span className="kv__v">{windowLabel(incident.windows.baseline)}</span>
            </div>
            <div className="kv">
              <span className="kv__k">Observation-window payment value</span>
              <span className="kv__v">{formatINR(incident.impact.current_window_amount_paise)}</span>
            </div>
            <div className="kv">
              <span className="kv__k">Leading observed contributor</span>
              <span className="kv__v">
                {contributor
                  ? `${contributor.failure_reason} (${contributor.current_count} now, ${contributor.baseline_count} in baseline)`
                  : 'not determinable from this evidence'}
              </span>
            </div>
            <div className="kv">
              <span className="kv__k">Detected at</span>
              <span className="kv__v">{formatTime(incident.detected_at)}</span>
            </div>
          </div>

          <h4 className="incremental__title">Top failure reasons (observation window)</h4>
          <div className="table-wrap">
            <table className="data-table">
            <thead>
              <tr>
                <th>Failure reason</th>
                <th>Now</th>
                <th>Baseline</th>
                <th>Change</th>
              </tr>
            </thead>
            <tbody>
              {incident.evidence.top_failure_reasons.map((row) => (
                <tr key={row.failure_reason}>
                  <td>{humanize(row.failure_reason)}</td>
                  <td>{row.current_count}</td>
                  <td>{row.baseline_count}</td>
                  <td>
                    {row.increase_vs_baseline > 0 ? '+' : ''}
                    {row.increase_vs_baseline}
                  </td>
                </tr>
              ))}
            </tbody>
            </table>
          </div>
          <p className="panel-note">
            Leading observed contributor is the most frequent failure reason in the
            observation window. RecoveryOS has not established causality, so it is
            never reported as a confirmed root cause.
          </p>
        </div>
      )}

      {tab === 'events' && (
        <AffectedEvents incidentId={incident.incident_id} onOpenTrace={onOpenTrace} />
      )}
      {tab === 'policy' && <PolicyInvestigation incidentId={incident.incident_id} />}
    </div>
  )
}

export default function RevenueHealth({ onNavigate }) {
  const [selectedId, setSelectedId] = useState(null)
  const { status, data, error } = useAsync((_signal) => listIncidents(), [])

  if (status === 'loading') return <LoadingBlock label="Analysing revenue health…" />
  if (status === 'error') {
    return <ErrorState message={error?.message} retry={() => window.location.reload()} />
  }

  const incidents = data.incidents ?? []
  const selected =
    incidents.find((incident) => incident.incident_id === selectedId) ?? incidents[0] ?? null

  const openTrace = (eventId) => {
    if (onNavigate) onNavigate('trace', { eventId })
  }

  return (
    <div className="screen">
      <header className="page-head">
        <div>
          <h1 className="page-title">Revenue Health</h1>
          <p className="page-subtitle">
            Where recovery performance is degrading in the observed dataset, and
            which payment decisions that covers.
          </p>
        </div>
        <div className="source-legend">
          <span className="legend-dot" style={{ background: 'var(--warn)' }} />
          SIMULATED evaluation · modelled impact
        </div>
      </header>

      <div className="meta-line">
        Potential incidents: {data.count} · {data.analysed_event_count} payments analysed ·{' '}
        {data.detection.window_days}-day observation window vs the preceding{' '}
        {data.detection.window_days} days · threshold{' '}
        {data.detection.degradation_threshold_bps / 100} pp
      </div>
      <p className="panel-note">{data.disclaimer}</p>

      {incidents.length === 0 ? (
        <EmptyState
          title="No degradation detected"
          message="No segment in the persisted workload meets the detection rule (minimum sample in both windows and at least a 15 percentage-point recovery-rate fall). Nothing is shown rather than an example incident."
        />
      ) : (
        <div className="revenue-health">
          <div className="incident-list">
            {incidents.map((incident) => (
              <IncidentCard
                key={incident.incident_id}
                incident={incident}
                selected={selected?.incident_id === incident.incident_id}
                onSelect={setSelectedId}
              />
            ))}
          </div>
          <Card className="incident-shell">
            {selected && <IncidentDetail incident={selected} onOpenTrace={openTrace} />}
          </Card>
        </div>
      )}
    </div>
  )
}
