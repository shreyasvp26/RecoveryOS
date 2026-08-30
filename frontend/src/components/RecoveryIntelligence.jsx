import { useCallback, useState } from 'react'
import { Card, Badge, Stat, LoadingBlock, ErrorState, EmptyState } from './ui.jsx'
import { recoveryIntelligence, useAsync } from '../core/api.js'
import { formatINR, humanize } from '../core/format.js'

/**
 * Recovery Intelligence — what RecoveryOS predicted, and what was observed.
 *
 * Every number on this screen comes from the backend, which derives it from
 * persisted optimizer decisions, execution outcomes and verified Razorpay
 * webhook recoveries. Nothing here is hardcoded, estimated client-side, or
 * carried over from a benchmark simulation.
 *
 * FOUR THINGS THIS SCREEN MUST NEVER BLUR
 * ---------------------------------------
 * 1. A verified recovery count is NOT a recovery rate. Positive evidence alone
 *    cannot form a denominator, so the two are displayed as separate facts and
 *    the rate stays absent until the backend says a rate exists.
 * 2. Below the backend's minimum sample threshold, no observed rate and no
 *    conclusion is shown — the cell reads "Insufficient observations".
 * 3. A pending Payment Link is waiting, not failed, and never counts as an
 *    observation either way.
 * 4. A SIMULATED intervention produces no operational observation at all.
 */

const REASON_LABELS = {
  simulated_execution: 'Simulated execution (no provider outcome exists)',
  awaiting_outcome: 'Waiting for payment on a real Payment Link',
  ambiguous_provider_result: 'Provider result could not be read',
  execution_failed: 'Execution attempt failed (not a payment outcome)',
  missing_payment_link_id: 'Real execution recorded no Payment Link id',
  missing_prediction: 'Verified recovery with no persisted prediction',
}

const SEGMENT_LABELS = {
  payment_method: 'Payment method',
  bank: 'Bank',
  failure_reason: 'Failure reason',
}

/** Basis points as a percentage string. 6667 bps -> "66.7%". */
function formatBps(bps) {
  if (bps === null || bps === undefined) return '—'
  return `${(Number(bps) / 100).toFixed(1)}%`
}

/** A calibration gap in percentage points, sign preserved. -500 bps -> "-5.0 pp". */
function formatGap(bps) {
  if (bps === null || bps === undefined) return '—'
  const pp = Number(bps) / 100
  return `${pp > 0 ? '+' : ''}${pp.toFixed(1)} pp`
}

function gapTone(bps) {
  if (bps === null || bps === undefined) return 'neutral'
  if (bps < -500) return 'danger'
  if (bps > 500) return 'info'
  return 'success'
}

/** The one honest cell for "not enough evidence to say". */
function Insufficient() {
  return <span className="dim">Insufficient observations</span>
}

function ObservedCell({ row }) {
  if (!row.sufficient_observations) return <Insufficient />
  return formatBps(row.observed_recovery_rate_bps)
}

function GapCell({ row }) {
  if (!row.sufficient_observations) return <Insufficient />
  return (
    <Badge tone={gapTone(row.calibration_gap_bps)}>
      {formatGap(row.calibration_gap_bps)}
    </Badge>
  )
}

function PerformanceTable({ rows, keyHeader, renderKey }) {
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>{keyHeader}</th>
            <th>Predicted</th>
            <th>Observed</th>
            <th>Gap</th>
            <th title="Terminal outcomes available for calibration">Samples</th>
            <th>Verified recoveries</th>
            <th>Attempts</th>
            <th>Avg recovered</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key}>
              <td>{renderKey ? renderKey(row.key) : row.key}</td>
              <td>{formatBps(row.mean_predicted_probability_bps)}</td>
              <td>
                <ObservedCell row={row} />
              </td>
              <td>
                <GapCell row={row} />
              </td>
              <td>{row.calibration_observations}</td>
              <td>{row.verified_recoveries}</td>
              <td>{row.attempts}</td>
              <td>
                {row.average_recovered_amount_paise === null ? (
                  <span className="dim">—</span>
                ) : (
                  formatINR(row.average_recovered_amount_paise)
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function RecoveryIntelligence({ onNavigate }) {
  const [reloadKey, setReloadKey] = useState(0)
  const { status, data, error } = useAsync(() => recoveryIntelligence(), [reloadKey])
  const reload = useCallback(() => setReloadKey((key) => key + 1), [])

  if (status === 'loading') return <LoadingBlock label="Loading recovery evidence…" />
  if (status === 'error') return <ErrorState message={error?.message} retry={reload} />

  const calibration = data.calibration
  const interventions = data.interventions || []
  const segments = data.segments || {}
  const value = data.expected_vs_realized
  const evidence = data.evidence
  const population = evidence.population
  const reasons = Object.entries(evidence.ineligible_reasons || {}).filter(
    ([, count]) => count > 0,
  )

  return (
    <div className="screen">
      <header className="page-head">
        <div>
          <h1 className="page-title">Recovery Intelligence</h1>
          <p className="page-subtitle">
            What RecoveryOS predicted, measured against what was actually observed.
            Observed recovery comes only from verified Razorpay webhooks correlated to
            a real Payment Link — never from execution success, simulation, or benchmark
            results.
          </p>
        </div>
        <div className="source-legend">
          <span className="legend-dot" style={{ background: 'var(--info)' }} />
          Operational world only · minimum {calibration.minimum_observations} observations
        </div>
      </header>

      <Card title="Evidence summary" subtitle="Prediction vs verified outcome">
        <div className="stat-grid">
          <Stat
            label="Verified recoveries"
            value={calibration.verified_recoveries}
            tone={calibration.verified_recoveries > 0 ? 'success' : 'neutral'}
            sub="Payments confirmed by a signed webhook"
            hint="Authoritative positive evidence. This is a count of real recoveries, not a recovery rate."
          />
          <Stat
            label="Observed recovery rate"
            value={
              calibration.sufficient_observations
                ? formatBps(calibration.observed_recovery_rate_bps)
                : 'Insufficient observations'
            }
            tone={calibration.sufficient_observations ? 'success' : 'neutral'}
            sub={`${calibration.recovered_observations} recovered / ${calibration.not_recovered_observations} not recovered`}
            hint="Requires terminal outcomes on both sides. A count of recoveries divided by itself would always be 100%."
          />
          <Stat
            label="Predicted recovery"
            value={formatBps(calibration.mean_predicted_probability_bps)}
            tone="brand"
            sub="Mean of the estimates the optimizer actually used"
            hint="Read from the persisted optimizer decisions. Never recomputed with a newer estimator."
          />
          <Stat
            label="Calibration gap"
            value={
              calibration.sufficient_observations
                ? formatGap(calibration.calibration_gap_bps)
                : '—'
            }
            tone={
              calibration.sufficient_observations
                ? gapTone(calibration.calibration_gap_bps)
                : 'neutral'
            }
            sub="Observed minus predicted, in percentage points"
          />
          <Stat
            label="Calibration samples"
            value={calibration.calibration_observations}
            tone={calibration.sufficient_observations ? 'info' : 'warn'}
            sub={`Terminal outcomes · ${calibration.total_observations} executions projected`}
            hint="Only settled outcomes (recovered or not recovered) can form a recovery rate. Pending and unknown are excluded."
          />
        </div>

        {!calibration.sufficient_observations && (
          <p className="panel-note">
            <strong>No observed recovery rate is reported.</strong>{' '}
            {calibration.status_detail} The minimum sample of{' '}
            {calibration.minimum_observations} applies to terminal outcomes, not to
            verified recoveries. The predicted figure is a model estimate and does not
            depend on sample size.
          </p>
        )}
        {!population.complete && (
          <p className="panel-note">
            These figures cover {population.projected_executions} of{' '}
            {population.total_executions} recorded executions and are therefore a
            bounded sample of history, not overall performance.
          </p>
        )}
      </Card>

      <Card
        title="Expected vs realized value"
        subtitle="The optimizer's own estimate next to the amounts the provider reported"
      >
        {value.compared_observations === 0 ? (
          <EmptyState
            title="Nothing to compare yet"
            message="A comparison needs both a persisted expected recovered value and a provider-reported amount on the same verified recovery."
          />
        ) : (
          <div className="stat-grid">
            <Stat
              label="Expected recovered value"
              value={formatINR(value.expected_recovered_value_paise)}
              tone="brand"
              sub="Modelled estimate at decision time"
            />
            <Stat
              label="Realized recovered amount"
              value={formatINR(value.realized_recovered_amount_paise)}
              tone="success"
              sub="Trusted amounts from verified webhooks"
            />
            <Stat
              label="Compared observations"
              value={value.compared_observations}
              tone={value.sufficient_observations ? 'info' : 'warn'}
              sub={
                value.sufficient_observations
                  ? 'Above the minimum sample threshold'
                  : 'Below the minimum sample threshold'
              }
            />
          </div>
        )}
        <p className="panel-note">
          This is not profit and it is not revenue uplift. It places a modelled estimate
          next to what the provider actually reported, for the recoveries where both
          figures exist.
        </p>
      </Card>

      <Card
        title="Intervention performance"
        subtitle="Only interventions that were actually executed appear here"
      >
        {interventions.length === 0 ? (
          <EmptyState
            title="No executions recorded"
            message="No intervention has been executed yet, so there is nothing to measure."
          />
        ) : (
          <PerformanceTable
            rows={interventions}
            keyHeader="Intervention"
            renderKey={humanize}
          />
        )}
      </Card>

      {Object.keys(SEGMENT_LABELS).map((dimension) => {
        const rows = segments[dimension] || []
        if (rows.length === 0) return null
        return (
          <Card
            key={dimension}
            title={`Segment signals · ${SEGMENT_LABELS[dimension]}`}
            subtitle="Performance conclusions are only drawn where the sample threshold is met"
          >
            <PerformanceTable rows={rows} keyHeader={SEGMENT_LABELS[dimension]} />
          </Card>
        )
      })}

      <Card
        title="Why executions were excluded"
        subtitle="Uncertainty is reported, never converted into success or failure"
      >
        {reasons.length === 0 ? (
          <EmptyState
            title="Nothing excluded"
            message="Every projected execution produced a calibration-eligible observation."
          />
        ) : (
          <div className="kv-grid">
            {reasons.map(([reason, count]) => (
              <div className="kv" key={reason}>
                <span className="kv__k">{REASON_LABELS[reason] || humanize(reason)}</span>
                <span className="kv__v">{count}</span>
              </div>
            ))}
          </div>
        )}
        <p className="panel-note">
          Prediction source: persisted optimizer decisions. Execution source: persisted
          execution outcomes. Recovery source: verified webhook recoveries, correlated by
          Payment Link id. Benchmark and Policy Lab simulations never enter these
          figures.
        </p>
        {onNavigate && (
          <button className="btn btn--ghost" onClick={() => onNavigate('ops')}>
            Open Recovery Operations
          </button>
        )}
      </Card>
    </div>
  )
}
