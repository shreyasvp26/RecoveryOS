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

function DecisionBadge({ summary }) {
  const map = {
    ALLOW: { tone: 'success', label: 'ALLOWED' },
    DENY: { tone: 'danger', label: 'DENIED' },
    no_action: { tone: 'warn', label: 'NO ACTION' },
    not_classified: { tone: 'neutral', label: 'NOT CLASSIFIED' },
  }
  const m = map[summary.final_decision] || { tone: 'neutral', label: summary.final_decision }
  return <Badge tone={m.tone}>{m.label}</Badge>
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

function TraceTimeline({ trace }) {
  const { event, classification, policy_decisions, executions, attempts, summary } = trace
  return (
    <div className="trace">
      <div className="trace__banner">
        <DecisionBadge summary={summary} />
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

        <Stage
          title="AI Classification"
          tone={classification ? 'info' : 'neutral'}
        >
          {classification ? (
            <>
              <div className="meta-row">
                <Badge tone="info">{classification.root_cause_category}</Badge>
                <span className="meta-line">confidence {classification.confidence}</span>
              </div>
              <p className="stage__text">{classification.reasoning}</p>
              <div className="candidates">
                {(classification.candidate_interventions || []).map((c) => (
                  <span key={c} className="chip">{c}</span>
                ))}
              </div>
            </>
          ) : (
            <EmptyState title="No AI classification" message="This event has no persisted classification result." />
          )}
        </Stage>

        <Stage
          title={`Policy Gate${policy_decisions.length > 1 ? 's' : ''}`}
          tone={policy_decisions.some((d) => !d.allowed) ? 'danger' : 'success'}
        >
          {policy_decisions.length === 0 && (
            <EmptyState title="No policy decision" message="No policy evaluation was persisted for this event." />
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

        <Stage title="Execution" tone={executions.length ? 'success' : 'neutral'}>
          {executions.length === 0 && (
            <EmptyState title="Not executed" message="Policy did not permit an execution for this event." />
          )}
          {executions.map((e, i) => (
            <div key={i} className="execution-row">
              <div className="meta-row">
                <Badge tone="success">{e.execution_mode}</Badge>
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
          {attempts && attempts.length > 0 && (
            <div className="attempts">
              <div className="stage__title small">Attempts</div>
              {attempts.map((a, i) => (
                <KeyVal key={i} k={a.intervention || a.method || 'attempt'} v={`${a.status || a.attempted_at || ''} · ${formatTime(a.attempted_at)}`} />
              ))}
            </div>
          )}
        </Stage>
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
            {list.status === 'ok' && list.data.count === 0 && (
              <EmptyState title="No matching events" message="No persisted events match the current filters." />
            )}
            {list.status === 'ok' &&
              list.data.events.map((ev) => (
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
