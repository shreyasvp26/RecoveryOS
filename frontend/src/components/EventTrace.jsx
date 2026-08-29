import { useState } from 'react'
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
} from './ui.jsx'
import { eventTrace, listEvents, useAsync } from '../core/api.js'
import { formatINR, formatTime } from '../core/format.js'

const EXECUTION_STATE_META = {
  EXECUTED: { tone: 'success', label: 'EXECUTED' },
  POLICY_BLOCKED: { tone: 'danger', label: 'POLICY BLOCKED' },
  NOT_CLASSIFIED: { tone: 'neutral', label: 'NOT CLASSIFIED' },
  NO_EXECUTION_RECORDED: { tone: 'warn', label: 'NO EXECUTION RECORDED' },
}

function Stage({ title, tone = 'neutral', children }) {
  return (
    <li className="stage">
      <span className="stage__dot" style={{ background: `var(--${tone})` }} />
      <div className="stage__body">
        <div className="stage__title">{title}</div>
        {children}
      </div>
    </li>
  )
}

function KeyVal({ k, v }) {
  return (
    <div className="kv">
      <span className="kv__k">{k}</span>
      <span className="kv__v mono">{v ?? '—'}</span>
    </div>
  )
}

/**
 * Format an integer basis-point probability for display (3200 -> "32.00%").
 *
 * Display only. The authoritative value is the integer basis points the
 * backend persisted; nothing here recomputes an economic figure.
 */
function formatBps(bps) {
  if (bps === null || bps === undefined || Number.isNaN(Number(bps))) return '—'
  const value = Number(bps)
  const whole = Math.floor(value / 100)
  const fraction = Math.abs(value % 100)
  return `${whole}.${String(fraction).padStart(2, '0')}%`
}

const SELECTION_REASON_TEXT = {
  max_expected_value:
    'Highest estimated expected value among the policy-approved candidates. Exact ties fall back to the V1 fixed-priority order, then to alphabetical order, so the choice is deterministic.',
  no_allowed_candidate:
    'The deterministic policy gate denied every candidate, so there was no permitted action to evaluate economically.',
  no_candidates:
    'The AI diagnosis produced no actionable candidate intervention to evaluate.',
}

/**
 * The economic optimization stage: the model estimates the backend persisted
 * for each policy-approved candidate, and which one it selected.
 *
 * Every number is read from the persisted optimizer decision. Nothing is
 * hardcoded, nothing is recalculated here, and no benchmark ground truth is
 * available on this surface.
 */
function EconomicOptimizationStage({ optimizerDecisions }) {
  const decisions = Array.isArray(optimizerDecisions) ? optimizerDecisions : []

  if (decisions.length === 0) {
    return (
      <Stage title="Economic Optimization" tone="neutral">
        <EmptyState
          title="No economic decision recorded"
          message="No V2 economic optimizer decision is persisted for this event. That is reported as absent rather than reconstructed: the event may predate economic selection, or it may have been decided by the V1 fixed-priority baseline."
        />
      </Stage>
    )
  }

  const decision = decisions[decisions.length - 1]
  const evaluations = Array.isArray(decision.evaluations) ? decision.evaluations : []
  const selected = decision.selected_intervention
  const actionable = selected && selected !== 'no_action'

  return (
    <Stage title="Economic Optimization" tone={actionable ? 'info' : 'warn'}>
      <div className="meta-row">
        <Badge tone="info">MODEL ESTIMATE</Badge>
        <span className="meta-line">decided {formatTime(decision.decided_at)}</span>
      </div>

      {evaluations.length === 0 ? (
        <EmptyState
          title="No candidate was economically evaluated"
          message="The policy gate approved no candidate, so the optimizer had nothing to price. It never evaluates or resurrects a denied intervention."
        />
      ) : (
        <table className="econ-table">
          <thead>
            <tr>
              <th>Candidate</th>
              <th>Est. recovery p</th>
              <th>Est. recovered</th>
              <th>Est. cost</th>
              <th>Modeled friction</th>
              <th>Est. expected value</th>
            </tr>
          </thead>
          <tbody>
            {evaluations.map((row) => (
              <tr
                key={row.intervention}
                className={row.intervention === selected ? 'econ-table__row--selected' : ''}
              >
                <td className="mono">{row.intervention}</td>
                <td className="mono">{formatBps(row.estimated_probability_bps)}</td>
                <td className="mono">{formatINR(row.expected_recovered_value_paise)}</td>
                <td className="mono">{formatINR(row.intervention_cost_paise)}</td>
                <td className="mono">{formatINR(row.friction_cost_paise)}</td>
                <td className="mono">{formatINR(row.expected_value_paise)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="kv-grid">
        <KeyVal k="Selected" v={selected} />
        <KeyVal
          k="Considered / policy-approved"
          v={`${(decision.candidates_considered || []).length} / ${(decision.allowed_candidates || []).length}`}
        />
      </div>
      <p className="stage__text">
        Why: {SELECTION_REASON_TEXT[decision.selection_reason] || decision.selection_reason}
      </p>
      <p className="stage__text">
        These are RecoveryOS model estimates computed from modeled costs and
        modeled friction, not measured recovery rates and not a simulated
        benchmark outcome. Expected value = estimated recovered amount −
        estimated intervention cost − modeled friction.
      </p>
    </Stage>
  )
}

function ExecutionStage({ executions, summary }) {
  const state = summary.execution_state

  if (executions.length > 0) {
    return (
      <Stage title="Execution" tone="success">
        {executions.map((e, i) => (
          <div key={i} className="execution-row">
            <div className="meta-row">
              <Badge tone={e.execution_mode === 'REAL_RAZORPAY' ? 'brand' : 'info'}>
                {e.execution_mode === 'REAL_RAZORPAY'
                  ? 'REAL RAZORPAY TEST MODE'
                  : 'SIMULATED'}
              </Badge>
              <span className="mono">{e.intervention}</span>
              <Badge tone={e.status === 'SUCCESS' ? 'success' : 'warn'}>{e.status}</Badge>
            </div>
            <div className="kv-grid">
              <KeyVal k="Reference" v={e.external_reference} />
              <KeyVal k="Reported" v={formatTime(e.reported_at)} />
            </div>
            {e.detail && <p className="stage__text mono">{e.detail}</p>}
          </div>
        ))}
        <div className="stage__text">
          Note: execution status records only whether the operation ran. Whether
          revenue was recovered is a benchmark-level, simulated answer — never
          inferred from execution success here.
        </div>
      </Stage>
    )
  }

  if (state === 'POLICY_BLOCKED') {
    return (
      <Stage title="Execution" tone="danger">
        <EmptyState
          title="Blocked by policy"
          message="Every actionable intervention was denied by the deterministic policy gate. A persisted denied decision proves this — no execution was permitted."
        />
      </Stage>
    )
  }

  if (state === 'NOT_CLASSIFIED') {
    return (
      <Stage title="Execution" tone="neutral">
        <EmptyState
          title="No execution"
          message="This event was never AI-classified, so the pipeline had no candidate interventions to authorize or run."
        />
      </Stage>
    )
  }

  return (
    <Stage title="Execution" tone="warn">
      <EmptyState
        title="No execution recorded"
        message="This event was classified but no execution is recorded. That can mean no actionable intervention was available, or the event was never run through execution. It is not labelled 'policy denied' because no persisted decision proves a denial."
      />
    </Stage>
  )
}

function OutcomeStage({ executions, summary }) {
  return (
    <Stage title="Outcome" tone="info">
      <div className="kv-grid">
        <KeyVal k="Execution status" v={summary.execution_status || 'no execution recorded'} />
        <KeyVal k="Execution mode" v={summary.execution_mode || '—'} />
      </div>
      <div className="outcome-note">
        <Badge tone="warn">SIMULATED BENCHMARK</Badge>
        <p>
          Recovery outcome is not available at event level. Simulated recovery is
          measured at batch level by the Phase 9 benchmark; this view never
          invents a per-event recovered amount. Execution success does not imply
          revenue recovered.
        </p>
      </div>
      {executions.some((e) => e.execution_mode === 'REAL_RAZORPAY') && (
        <p className="stage__text">
          This execution ran in real Razorpay Test Mode; its revenue outcome is
          still recorded at the execution layer, not as a recovery figure here.
        </p>
      )}
    </Stage>
  )
}

function Phase12Stage({ phase12 }) {
  if (!phase12) return null

  if (!phase12.closed_loop) {
    return (
      <Stage title="Closed-loop verification (Phase 12)" tone="neutral">
        <EmptyState
          title="No real payment-link loop"
          message="This event has no REAL Razorpay payment-link execution, so there is no verified webhook recovery to close the loop on. This view never fabricates a recovered amount."
        />
      </Stage>
    )
  }

  const paymentLinks = Array.isArray(phase12?.payment_links)
    ? phase12.payment_links
    : []

  return (
    <Stage title="Closed-loop verification (Phase 12)" tone="info">
      {paymentLinks.map((pl) => {
        const recovered = pl.status === 'recovered'
        return (
          <div key={pl.payment_link_id} className="execution-row">
            <div className="meta-row">
              <span className="mono">{pl.payment_link_id}</span>
              <Badge tone={recovered ? 'success' : 'warn'}>
                {recovered ? 'RECOVERED' : 'WAITING'}
              </Badge>
            </div>
            <div className="kv-grid">
              <KeyVal
                k="Recovered via webhook"
                v={recovered ? formatINR(pl.recovered_amount_paise) : 'awaiting verified payment_link.paid webhook'}
              />
              <KeyVal k="Payment" v={pl.payment_id} />
              <KeyVal k="Recovered at" v={recovered ? formatTime(pl.recovered_at) : null} />
            </div>
          </div>
        )
      })}
      {paymentLinks.length === 0 && (
        <EmptyState
          title="No payment links"
          message="No real payment-link outcome is recorded for this event."
        />
      )}
    </Stage>
  )
}

function TraceTimeline({ trace }) {
  const {
    event = {},
    classification,
    policy_decisions = [],
    optimizer_decisions = [],
    executions = [],
    attempts = [],
    summary = {},
  } = trace || {}
  const execMeta = EXECUTION_STATE_META[summary.execution_state] || {
    tone: 'neutral',
    label: summary.execution_state || 'UNKNOWN',
  }
  return (
    <div className="trace">
      <div className="trace__banner">
        <div className="trace__badges">
          <Badge tone={summary.final_decision === 'ALLOW' ? 'success' : summary.final_decision === 'DENY' ? 'danger' : 'neutral'}>
            {summary.final_decision === 'ALLOW'
              ? 'ALLOWED'
              : summary.final_decision === 'DENY'
                ? 'DENIED'
                : String(summary.final_decision).replace(/_/g, ' ').toUpperCase()}
          </Badge>
          <Badge tone={execMeta.tone}>{execMeta.label}</Badge>
        </div>
        <div className="trace__bannermeta">
          <span className="mono">{event.event_id}</span>
          <span>·</span>
          <span>{formatINR(event.amount_paise)}</span>
          <span>·</span>
          <span>{formatTime(event.timestamp)}</span>
        </div>
      </div>

      <ol className="timeline">
        <Stage title="Ingest" tone="info">
          <div className="kv-grid">
            <KeyVal k="Order" v={event.order_id} />
            <KeyVal k="Payment" v={event.payment_id} />
            <KeyVal k="Customer" v={event.customer_id} />
            <KeyVal k="Method" v={event.payment_method} />
            <KeyVal k="Failure" v={event.failure_reason} />
            <KeyVal k="Bank" v={event.bank} />
          </div>
          <div className="meta-row">
            <Badge tone={event.risk_flag === 'fraud_suspect' ? 'danger' : 'success'}>
              {event.risk_flag}
            </Badge>
          </div>
        </Stage>

        <Stage title="AI Diagnosis" tone={classification ? 'info' : 'neutral'}>
          {classification ? (
            <>
              <div className="meta-row">
                <Badge tone="info">{classification.root_cause_category}</Badge>
                <span className="meta-line">confidence {classification.confidence}</span>
              </div>
              <p className="stage__text">{classification.reasoning}</p>
              <div className="candidates">
                {(classification.candidate_interventions || []).map((c) => (
                  <span key={c} className="chip">
                    {c === 'no_action' ? `${c} (advisory)` : c}
                  </span>
                ))}
              </div>
            </>
          ) : (
            <EmptyState title="No AI diagnosis" message="This event has no persisted classification result." />
          )}
        </Stage>

        <li className="stage-separator" aria-hidden="true">
          <span>AI — advisory</span>
          <span className="sep-line" />
          <span>Policy — authoritative</span>
        </li>

        <Stage
          title={`Deterministic Policy${policy_decisions.length ? ` · ${policy_decisions.length}` : ''}`}
          tone={policy_decisions.some((d) => !d.allowed) ? 'danger' : 'success'}
        >
          {policy_decisions.length === 0 && (
            <EmptyState title="No policy decisions" message="No per-candidate policy evaluation was persisted for this event." />
          )}
          {policy_decisions.map((d, i) => (
            <div key={i} className="decision-row">
              <Badge tone={d.allowed ? 'success' : 'danger'}>
                {d.allowed ? 'ALLOW' : `DENY · ${d.denial_reason || 'policy'}`}
              </Badge>
              <span className="meta-line">intervention: {d.proposed_intervention || '—'}</span>
              <span className="meta-line">{formatTime(d.evaluated_at)}</span>
              <div className="rules">
                {(d.policy_rules_applied || []).map((r) => (
                  <span key={r} className="mini-chip">{r}</span>
                ))}
              </div>
            </div>
          ))}
        </Stage>

        <EconomicOptimizationStage optimizerDecisions={optimizer_decisions} />
        <ExecutionStage executions={executions} summary={summary} />
        <OutcomeStage executions={executions} summary={summary} />
        <Phase12Stage phase12={trace.phase12} />

        {attempts && attempts.length > 0 && (
          <Stage title="Attempts" tone="neutral">
            {attempts.map((a, i) => (
              <KeyVal
                key={i}
                k={a.intervention || 'attempt'}
                v={`${a.status || ''} · ${formatTime(a.attempted_at)}`}
              />
            ))}
          </Stage>
        )}
      </ol>
    </div>
  )
}

function EventRow({ event, selected, onSelect }) {
  return (
    <button
      className={`event-row ${selected ? 'event-row--selected' : ''}`}
      onClick={() => onSelect(event.event_id)}
    >
      <div className="event-row__main">
        <span className="mono event-row__id">{event.event_id}</span>
        <span className="event-row__amount">{formatINR(event.amount_paise)}</span>
      </div>
      <div className="event-row__sub">
        <span className="mono">{event.customer_id}</span>
        <span>·</span>
        <span>{event.failure_reason}</span>
        <span>·</span>
        <span>{formatTime(event.timestamp)}</span>
      </div>
      <div className="event-row__flag">
        <Badge tone={event.risk_flag === 'fraud_suspect' ? 'danger' : 'success'}>
          {event.risk_flag}
        </Badge>
      </div>
    </button>
  )
}

export default function EventTrace() {
  const [query, setQuery] = useState('')
  const [riskFlag, setRiskFlag] = useState('')
  const [selectedId, setSelectedId] = useState(null)
  const [debouncedQuery, setDebouncedQuery] = useState('')

  const list = useAsync((_signal) => {
    const params = { query: debouncedQuery || undefined, risk_flag: riskFlag || undefined }
    return listEvents(params)
  }, [debouncedQuery, riskFlag])

  const trace = useAsync(
    (_signal) => (selectedId ? eventTrace(selectedId) : Promise.resolve(null)),
    [selectedId, list.data ? `${list.data.count}:${selectedId}` : selectedId],
  )

  return (
    <div className="screen">
      <header className="page-head">
        <div>
          <h1 className="page-title">Event Decision Trace</h1>
          <p className="page-subtitle">
            The full, persisted decision chain for a single recovery event.
          </p>
        </div>
      </header>

      <Card className="traceshell">
        <div className="trace-controls">
          <input
            className="input"
            placeholder="Search event / customer / order / payment…"
            value={query}
            onKeyDown={(e) => {
              if (e.key === 'Enter') setDebouncedQuery(query)
            }}
            onChange={(e) => setQuery(e.target.value)}
          />
          <select
            className="input select"
            value={riskFlag}
            onChange={(e) => setRiskFlag(e.target.value)}
          >
            <option value="">All risk flags</option>
            <option value="normal">normal</option>
            <option value="fraud_suspect">fraud_suspect</option>
          </select>
        </div>

        <div className="trace-cols">
          <div className="event-list">
            {list.status === 'loading' && <LoadingBlock label="Loading events…" />}
            {list.status === 'error' && <ErrorState message={list.error?.message} retry={() => setDebouncedQuery(debouncedQuery)} />}
            {list.status === 'ok' && (list.data?.count ?? 0) === 0 && (
              <EmptyState title="No matching events" message="No persisted events match the current filters." />
            )}
            {list.status === 'ok' &&
              (list.data?.events ?? []).map((ev) => (
                <EventRow
                  key={ev.event_id}
                  event={ev}
                  selected={selectedId === ev.event_id}
                  onSelect={setSelectedId}
                />
              ))}
          </div>

          <div className="trace-detail">
            {!selectedId && (
              <EmptyState
                title="Select an event"
                message="Choose an event from the list to see its full decision trace."
              />
            )}
            {selectedId && trace.status === 'loading' && <LoadingBlock label="Loading trace…" />}
            {selectedId && trace.status === 'error' && (
              <ErrorState message={trace.error?.message} retry={() => setSelectedId(selectedId)} />
            )}
            {trace.status === 'ok' && trace.data && <TraceTimeline trace={trace.data} />}
          </div>
        </div>
      </Card>
    </div>
  )
}
