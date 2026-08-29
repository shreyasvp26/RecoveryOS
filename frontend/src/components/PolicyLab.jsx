import { useMemo, useState } from 'react'
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  Stat,
} from './ui.jsx'
import { compareReplays, replayScenarios, useAsync } from '../core/api.js'
import { formatINR, formatRate, humanize } from '../core/format.js'

/**
 * Phase 19 Policy Lab.
 *
 * Every value rendered here comes from the backend replay: the scenarios, the
 * policy parameters, the bounds, the locked protections and all metrics. There
 * are no hardcoded results and no illustrative examples — an empty result is
 * shown as empty, and a rejected policy shows the server's own reason.
 *
 * Replay figures are SIMULATED evaluations. That label travels with every
 * number on this screen and is never softened into production revenue.
 */

const CUSTOM_ID = 'custom'

/** Human labels for the three configurable controls, in form order. */
const PARAMETER_LABELS = {
  max_interventions_per_customer_24h: 'Maximum interventions / 24h',
  event_cooldown_minutes: 'Event cooldown (minutes)',
  daily_spend_cap_paise: 'Daily spend cap (paise)',
}

const PARAMETER_ORDER = [
  'max_interventions_per_customer_24h',
  'event_cooldown_minutes',
  'daily_spend_cap_paise',
]

const DELTA_TONES = {
  newly_blocked: 'danger',
  newly_allowed: 'success',
  selection_changed: 'info',
  authorization_changed: 'warn',
  failure_changed: 'warn',
}

/** Format a policy parameter for display without touching float arithmetic. */
function formatParameter(name, value) {
  if (value === null || value === undefined) return '—'
  if (name === 'daily_spend_cap_paise') return formatINR(value)
  if (name === 'event_cooldown_minutes') return `${value} min`
  return String(value)
}

/** Signed rupee delta, so a worse scenario reads as worse. */
function formatDelta(paise) {
  if (paise === null || paise === undefined) return '—'
  if (paise === 0) return '—'
  return `${paise > 0 ? '+' : ''}${formatINR(paise)}`
}

function toneForDelta(paise) {
  if (!paise) return 'neutral'
  return paise > 0 ? 'success' : 'danger'
}

function ScenarioCard({ scenario, selected, onToggle, disabled }) {
  return (
    <button
      type="button"
      className={`lab-scenario ${selected ? 'lab-scenario--on' : ''}`}
      onClick={() => onToggle(scenario.scenario_id)}
      disabled={disabled}
      aria-pressed={selected}
    >
      <div className="lab-scenario__head">
        <span className="lab-scenario__name">{scenario.name}</span>
        <Badge tone={selected ? 'brand' : 'neutral'}>
          {selected ? 'selected' : 'off'}
        </Badge>
      </div>
      <dl className="lab-params">
        {PARAMETER_ORDER.map((name) => (
          <div key={name}>
            <dt>{PARAMETER_LABELS[name]}</dt>
            <dd className="mono">
              {formatParameter(name, scenario.parameters[name])}
            </dd>
          </div>
        ))}
      </dl>
      {scenario.derivation && (
        <p className="lab-scenario__derivation">{scenario.derivation}</p>
      )}
    </button>
  )
}

function LockedProtections({ protections }) {
  return (
    <div className="lab-locked">
      <div className="lab-locked__title">Immutable safety protections</div>
      <p className="lab-locked__note">
        These are unconditional rules in the policy engine with no setting
        attached. No scenario — built-in or custom — can weaken them, and the
        server rejects any attempt to configure one.
      </p>
      <div className="lab-locked__list">
        {protections.map((p) => (
          <span key={p} className="lab-lock">
            <span aria-hidden="true">🔒</span>
            {humanize(p)}
            <Badge tone="success">LOCKED</Badge>
          </span>
        ))}
      </div>
    </div>
  )
}

function CustomForm({ bounds, values, onChange, enabled, onToggle }) {
  return (
    <div className={`lab-custom ${enabled ? '' : 'lab-custom--off'}`}>
      <label className="lab-custom__toggle">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => onToggle(e.target.checked)}
        />
        <span>Include a custom scenario</span>
      </label>
      <div className="lab-custom__fields">
        {PARAMETER_ORDER.map((name) => {
          const bound = bounds[name] || {}
          return (
            <label key={name} className="lab-field">
              <span className="lab-field__label">{PARAMETER_LABELS[name]}</span>
              <input
                className="input"
                type="number"
                inputMode="numeric"
                step="1"
                min={bound.minimum}
                max={bound.maximum}
                value={values[name]}
                disabled={!enabled}
                onChange={(e) => onChange(name, e.target.value)}
              />
              <span className="lab-field__hint">
                allowed {bound.minimum} – {bound.maximum}
              </span>
            </label>
          )
        })}
      </div>
      <p className="lab-custom__note">
        Values are validated by the server. The bounds shown here come from the
        backend and are a convenience only.
      </p>
    </div>
  )
}

function ComparisonTable({ scenarios }) {
  const rows = [
    {
      label: 'Simulated recovered revenue',
      value: (s) =>
        formatINR(s.metrics.financial.simulated_recovered_revenue_paise),
    },
    {
      label: 'vs reference',
      value: (s) =>
        s.is_reference
          ? 'reference'
          : formatDelta(s.vs_reference.incremental_recovered_revenue_paise),
      tone: (s) =>
        s.is_reference
          ? 'neutral'
          : toneForDelta(s.vs_reference.incremental_recovered_revenue_paise),
    },
    {
      label: 'Recovery rate',
      value: (s) => formatRate(s.metrics.financial.recovery_rate),
    },
    {
      label: 'Interventions',
      value: (s) => s.metrics.intervention.total_interventions,
    },
    {
      label: 'Customers touched',
      value: (s) => s.metrics.intervention.customers_touched,
    },
    {
      label: 'Blocked interventions',
      value: (s) => s.metrics.safety.total_blocked_interventions,
    },
    {
      label: 'Efficiency / intervention',
      value: (s) =>
        s.metrics.intervention.intervention_efficiency_paise === null
          ? '—'
          : formatINR(
              Math.round(s.metrics.intervention.intervention_efficiency_paise),
            ),
    },
    {
      label: 'Fraud interventions',
      value: (s) => s.metrics.safety.fraud_interventions,
    },
    {
      label: 'Terminal interventions',
      value: (s) => s.metrics.safety.terminal_interventions,
    },
    {
      label: 'Unauthorized executions',
      value: (s) => s.metrics.safety.unauthorized_attempts,
    },
    { label: 'Replay failures', value: (s) => s.metrics.failures },
  ]

  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th scope="col">Metric (simulated)</th>
            {scenarios.map((s) => (
              <th key={s.scenario.scenario_id} scope="col">
                {s.scenario.name}
                {s.is_reference && <span className="dim"> (ref)</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.label}>
              <td>{row.label}</td>
              {scenarios.map((s) => (
                <td key={s.scenario.scenario_id} className="mono">
                  {row.tone ? (
                    <Badge tone={row.tone(s)}>{row.value(s)}</Badge>
                  ) : (
                    row.value(s)
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function RuleActivity({ scenarios }) {
  const rules = Object.keys(scenarios[0].metrics.safety.rule_activity)
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th scope="col">Policy rule</th>
            <th scope="col">Configured by</th>
            {scenarios.map((s) => (
              <th key={s.scenario.scenario_id} scope="col">
                {s.scenario.name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rules.map((rule) => {
            const meta = scenarios[0].metrics.safety.rule_activity[rule]
            return (
              <tr key={rule}>
                <td>
                  {humanize(rule)}{' '}
                  {meta.immutable && <Badge tone="success">LOCKED</Badge>}
                </td>
                <td className="mono dim">
                  {meta.configured_by ? humanize(meta.configured_by) : '—'}
                </td>
                {scenarios.map((s) => (
                  <td key={s.scenario.scenario_id} className="mono">
                    {s.metrics.safety.rule_activity[rule].blocked}
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function DecisionDeltas({ scenarios, referenceName }) {
  const withDeltas = scenarios.filter((s) => s.decision_deltas.length > 0)
  const [scenarioId, setScenarioId] = useState(
    withDeltas[0]?.scenario.scenario_id ?? null,
  )
  const active =
    withDeltas.find((s) => s.scenario.scenario_id === scenarioId) ||
    withDeltas[0]

  if (withDeltas.length === 0) {
    return (
      <EmptyState
        title="No decision differences"
        message={
          'Every scenario made the identical decision on every event in this ' +
          'workload. That is a real result, not a missing one: the policy ' +
          'parameters that changed were not load bearing on these events.'
        }
      />
    )
  }

  return (
    <>
      <div className="lab-delta-picker">
        {withDeltas.map((s) => (
          <button
            key={s.scenario.scenario_id}
            type="button"
            className={`btn ${
              s.scenario.scenario_id === active.scenario.scenario_id
                ? 'btn--on'
                : ''
            }`}
            onClick={() => setScenarioId(s.scenario.scenario_id)}
          >
            {s.scenario.name} ({s.decision_deltas.length})
          </button>
        ))}
      </div>
      <p className="panel-note">
        Same event, same classification, same candidate recommendations, same
        optimizer, same hidden outcome model. Only the policy changed.
      </p>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Event</th>
              <th scope="col">Amount</th>
              <th scope="col">Root cause</th>
              <th scope="col">{referenceName}</th>
              <th scope="col">{active.scenario.name}</th>
              <th scope="col">Change</th>
            </tr>
          </thead>
          <tbody>
            {active.decision_deltas.map((d) => (
              <tr key={d.event_id}>
                <td className="mono">{d.event_id}</td>
                <td className="mono">{formatINR(d.amount_paise)}</td>
                <td>{humanize(d.root_cause_category)}</td>
                <td>
                  <DeltaSide side={d.reference} />
                </td>
                <td>
                  <DeltaSide side={d.candidate} />
                </td>
                <td>
                  <Badge tone={DELTA_TONES[d.delta_type] || 'neutral'}>
                    {humanize(d.delta_type)}
                  </Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

function DeltaSide({ side }) {
  const denied = Boolean(side.denial_reason)
  return (
    <div className="lab-side">
      <Badge tone={denied ? 'danger' : 'success'}>
        {denied ? 'DENY' : 'ALLOW'}
      </Badge>
      <span className="mono">{side.selected_intervention}</span>
      {denied && (
        <span className="lab-side__reason">{humanize(side.denial_reason)}</span>
      )}
    </div>
  )
}

export default function PolicyLab() {
  const catalog = useAsync((_signal) => replayScenarios(), [])

  const [selected, setSelected] = useState(['current', 'conservative', 'aggressive'])
  const [customOn, setCustomOn] = useState(false)
  const [customValues, setCustomValues] = useState(null)
  const [run, setRun] = useState({ status: 'idle', data: null, error: null })

  // Seed the custom form from the backend's real current policy, once loaded.
  const defaults = catalog.data?.custom?.defaults
  const values = useMemo(() => {
    if (customValues) return customValues
    if (!defaults) return null
    return Object.fromEntries(
      PARAMETER_ORDER.map((name) => [name, String(defaults[name])]),
    )
  }, [customValues, defaults])

  if (catalog.status === 'loading') {
    return (
      <div className="screen">
        <LoadingBlock label="Loading policy scenarios…" />
      </div>
    )
  }
  if (catalog.status === 'error') {
    return (
      <div className="screen">
        <ErrorState
          message={catalog.error?.message}
          retry={() => window.location.reload()}
        />
      </div>
    )
  }

  const data = catalog.data
  const referenceId = data.reference_scenario_id

  function toggle(id) {
    setSelected((current) =>
      current.includes(id)
        ? current.filter((x) => x !== id)
        : [...current, id],
    )
  }

  function setValue(name, raw) {
    setCustomValues({ ...values, [name]: raw })
  }

  async function runReplay() {
    setRun({ status: 'loading', data: null, error: null })
    const scenarios = selected.map((id) => ({ scenario_id: id }))
    if (customOn) {
      scenarios.push({
        scenario_id: CUSTOM_ID,
        name: 'Custom',
        // Send numbers, never strings: the server rejects a malformed type,
        // and an empty field must fail loudly rather than becoming a zero.
        parameters: Object.fromEntries(
          PARAMETER_ORDER.map((name) => [
            name,
            values[name] === '' ? null : Number(values[name]),
          ]),
        ),
      })
    }
    try {
      const result = await compareReplays({
        scenarios,
        reference_scenario_id: referenceId,
      })
      setRun({ status: 'ok', data: result, error: null })
    } catch (err) {
      setRun({ status: 'error', data: null, error: err })
    }
  }

  const canRun = selected.includes(referenceId) && selected.length > 0
  const result = run.data

  return (
    <div className="screen">
      <header className="page-head">
        <div>
          <h1 className="page-title">Policy Lab</h1>
          <p className="page-subtitle">
            Replay the same workload through the same RecoveryOS decision
            pipeline under a different control policy, and measure exactly what
            changed. All results are SIMULATED evaluations — no Razorpay call,
            no customer-facing action, and no change to the active policy.
          </p>
        </div>
        <Badge tone="warn">SIMULATED</Badge>
      </header>

      <Card
        title="Policy scenarios"
        subtitle="Select the scenarios to replay. The reference scenario is required."
      >
        <div className="lab-scenarios">
          {data.scenarios.map((s) => (
            <ScenarioCard
              key={s.scenario_id}
              scenario={s}
              selected={selected.includes(s.scenario_id)}
              onToggle={toggle}
              disabled={
                s.scenario_id === referenceId && selected.includes(referenceId)
              }
            />
          ))}
        </div>

        {values && (
          <CustomForm
            bounds={data.custom.bounds}
            values={values}
            onChange={setValue}
            enabled={customOn}
            onToggle={setCustomOn}
          />
        )}

        <LockedProtections protections={data.immutable_protections} />

        <div className="lab-actions">
          <button
            className="btn btn--primary"
            onClick={runReplay}
            disabled={!canRun || run.status === 'loading'}
          >
            {run.status === 'loading' ? 'Running replay…' : 'Run Replay'}
          </button>
          {!canRun && (
            <span className="dim">
              The reference scenario must be included in the comparison.
            </span>
          )}
        </div>
      </Card>

      {run.status === 'loading' && <LoadingBlock label="Replaying scenarios…" />}
      {run.status === 'error' && (
        <Card title="Replay rejected">
          <ErrorState message={run.error?.message} />
        </Card>
      )}

      {run.status === 'ok' && result && (
        <>
          <Card
            title="Scenario comparison"
            subtitle={`${result.event_count} events, replayed once per scenario`}
            action={<Badge tone="warn">SIMULATED</Badge>}
          >
            <div className="kpi-grid">
              {result.scenarios.map((s) => (
                <Stat
                  key={s.scenario.scenario_id}
                  label={`${s.scenario.name} — simulated recovered`}
                  value={formatINR(
                    s.metrics.financial.simulated_recovered_revenue_paise,
                  )}
                  sub={
                    s.is_reference
                      ? 'reference policy'
                      : `${formatDelta(
                          s.vs_reference.incremental_recovered_revenue_paise,
                        )} vs reference`
                  }
                  tone={
                    s.is_reference
                      ? 'brand'
                      : toneForDelta(
                          s.vs_reference.incremental_recovered_revenue_paise,
                        )
                  }
                />
              ))}
            </div>
            <ComparisonTable scenarios={result.scenarios} />
            <p className="panel-note">{result.disclaimer}</p>
          </Card>

          <Card
            title="Decision deltas"
            subtitle="Events where the policy change altered what RecoveryOS did"
          >
            <DecisionDeltas
              scenarios={result.scenarios}
              referenceName={
                result.scenarios.find((s) => s.is_reference)?.scenario.name ||
                'Reference'
              }
            />
          </Card>

          <Card
            title="Which rules were load bearing"
            subtitle="Candidate interventions denied per policy rule, from the replay itself"
          >
            <RuleActivity scenarios={result.scenarios} />
            <p className="panel-note">
              A configurable rule showing zero blocks did not fire on this
              workload, so changing its parameter could not have altered this
              result. That is measured from the replay, not assumed.
            </p>
          </Card>

          <Card
            title="Replay integrity"
            subtitle="Computed checks that make this comparison causal"
          >
            <div className="lab-checks">
              {Object.entries(result.fairness).map(([check, ok]) => (
                <span key={check} className="lab-check">
                  <Badge tone={ok ? 'success' : 'danger'}>
                    {ok ? 'PASS' : 'FAIL'}
                  </Badge>
                  {humanize(check)}
                </span>
              ))}
            </div>
            <div className="lab-identity">
              {result.scenarios.map((s) => (
                <div key={s.scenario.scenario_id} className="lab-identity__row">
                  <span>{s.scenario.name}</span>
                  <span className="mono dim">
                    policy {s.identity.policy_fingerprint}
                  </span>
                  <span className="mono dim">
                    world {s.identity.config_fingerprint}
                  </span>
                </div>
              ))}
            </div>
          </Card>
        </>
      )}
    </div>
  )
}
