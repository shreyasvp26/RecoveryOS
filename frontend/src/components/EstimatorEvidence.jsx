import { useCallback, useState } from 'react'
import { Card, Badge, Stat, LoadingBlock, ErrorState, EmptyState } from './ui.jsx'
import { estimatorEvidence, recalibrateEstimator, useAsync } from '../core/api.js'
import { formatTime, humanize } from '../core/format.js'

/**
 * Estimator Evidence — the adaptive, evidence-calibrated estimator's state.
 *
 * RecoveryOS now wraps its frozen baseline estimator with a versioned,
 * immutable calibration snapshot. Each snapshot records, per intervention, the
 * deterministic posterior computed from REAL_RAZORPAY terminal evidence. A
 * snapshot is only ACTIVE (and only then may it change the probabilities that
 * rank policy-allowed decisions) when an intervention meets every threshold
 * with its own evidence:
 *
 *     total observations >= 10 · recovered >= 1 · not recovered >= 1
 *
 * THREE THINGS THIS SCREEN MUST NEVER BLUR
 * ----------------------------------------
 * 1. Estimation is not execution: this screen changes probabilities only; the
 *    optimizer ranks and policy authorizes. Nothing here executes anything.
 * 2. Calibration evidence comes ONLY from real operational outcomes (verified
 *    webhooks + provider-confirmed expired). Simulation and benchmarks never
 *    enter it.
 * 3. Only a provider-confirmed "expired" settles a link as NOT recovered.
 *    Absence of payment is never treated as failure.
 */

/** Basis points as a percentage. 4400 bps -> "44.0%". */
function formatBps(bps) {
  if (bps === null || bps === undefined) return '—'
  return `${(Number(bps) / 100).toFixed(1)}%`
}

function InterventionRow({ intervention, row, active, version }) {
  return (
    <tr>
      <td>{humanize(intervention)}</td>
      <td>
        {active ? (
          <Badge tone="success">Active · v{version}</Badge>
        ) : (
          <Badge tone="neutral">Baseline</Badge>
        )}
      </td>
      <td>{formatBps(row.baseline_bps)}</td>
      <td>{formatBps(active ? row.posterior_bps : row.baseline_bps)}</td>
      <td>{row.observed_total}</td>
      <td>{row.observed_recovered}</td>
      <td>{row.observed_not_recovered}</td>
    </tr>
  )
}

export default function EstimatorEvidence() {
  const [reloadKey, setReloadKey] = useState(0)
  const [busy, setBusy] = useState(false)
  const [actionMsg, setActionMsg] = useState(null)
  const { status, data, error } = useAsync(() => estimatorEvidence(), [reloadKey])
  const reload = useCallback(() => setReloadKey((key) => key + 1), [])

  const recalibrate = async () => {
    setBusy(true)
    setActionMsg(null)
    try {
      await recalibrateEstimator()
      reload()
      setActionMsg('Rebuilt from current evidence.')
    } catch (err) {
      setActionMsg(err?.message || 'Recalibration failed.')
    } finally {
      setBusy(false)
    }
  }

  if (status === 'loading') return <LoadingBlock label="Loading estimator evidence…" />
  if (status === 'error') return <ErrorState message={error?.message} retry={reload} />

  const latest = data.latest
  const activeVersion = data.active_version
  const evidenced = (latest?.evidenced) || {}
  const activeBps = (latest?.active_bps) || {}
  const rows = Object.entries(evidenced)
  const samples = latest?.samples?.outcome_counts || {}

  return (
    <div className="screen">
      <header className="page-head">
        <div>
          <h1 className="page-title">Estimator Evidence</h1>
          <p className="page-subtitle">
            The calibration phase of estimation. A deterministic, immutable snapshot
            updates the frozen baseline's probabilities from real operational outcomes —
            and only when the evidence gate is met. It ranks; it never executes or
            authorizes.
          </p>
        </div>
        <div className="source-legend">
          <span className="legend-dot" style={{ background: 'var(--success)' }} />
          Active calibration feeds production ranking
        </div>
      </header>

      <Card
        title="Active calibration"
        subtitle={
          latest
            ? `Snapshot v${latest.version} · built ${formatTime(latest.built_at)}`
            : 'No snapshot built yet'
        }
      >
        <div className="stat-grid">
          <Stat
            label="Status"
            value={latest ? (activeVersion ? `Active (v${activeVersion})` : 'Baseline') : 'No snapshot'}
            tone={activeVersion ? 'success' : 'neutral'}
            sub={
              activeVersion
                ? 'Calibrated posteriors are ranking decisions'
                : 'Frozen baseline estimator is ranking decisions'
            }
            hint="Active only when an intervention meets every threshold with its own terminal evidence."
          />
          <Stat
            label="Terminal samples observed"
            value={
              samples.RECOVERED !== undefined ? samples.RECOVERED + samples.NOT_RECOVERED : 0
            }
            tone="info"
            sub={`${samples.RECOVERED ?? 0} recovered · ${samples.NOT_RECOVERED ?? 0} not recovered`}
            hint="Real operational outcomes only. Verified webhook recoveries plus provider-confirmed expiries."
          />
          <Stat
            label="Snapshot history"
            value={data.snapshot_count}
            tone="info"
            sub="Immutable, append-only versions"
            hint="Historical snapshots are never rewritten; every past estimate is reconstructable."
          />
        </div>

        {!latest ? (
          <EmptyState
            title="No calibration snapshot yet"
            message="Run a recalibration once enough real payment_link outcomes exist. Until then, the frozen baseline estimator is unchanged."
          />
        ) : (
          <>
            {!activeVersion && (
              <p className="panel-note">
                <strong>Calibration is inactive.</strong> The latest snapshot ({latest.samples.total}{' '}
                terminal samples) does not yet meet the gate, so production keeps the frozen
                baseline probabilities. This is the honest state — no uncalibrated evidence
                is turned into a probability.
              </p>
            )}
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Intervention</th>
                    <th>Calibration</th>
                    <th>Baseline</th>
                    <th>In use</th>
                    <th>Samples</th>
                    <th>Recovered</th>
                    <th>Not recovered</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map(([intervention, row]) => (
                    <InterventionRow
                      key={intervention}
                      intervention={intervention}
                      row={row}
                      version={latest.version}
                      active={intervention in activeBps}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Card>

      <Card
        title="Recalibrate"
        subtitle="Operator-triggered; appends the next immutable snapshot"
      >
        <p className="panel-note">
          Recalibrating re-reads the persisted real operational evidence (verified webhook
          recoveries plus a read-only provider poll of still-unsettled Real Razorpay Payment
          Links) and appends a new versioned snapshot. It never rewrites a past snapshot or
          a past decision, and it never executes or authorizes anything.
        </p>
        <div className="row-buttons">
          <button
            className="btn"
            onClick={recalibrate}
            disabled={busy}
            title="Rebuild calibration from current evidence and append snapshot v(n+1)"
          >
            {busy ? 'Rebuilding…' : 'Recalibrate now'}
          </button>
          <button className="btn btn--ghost" onClick={reload} disabled={busy}>
            Refresh
          </button>
        </div>
        {actionMsg && <p className="panel-note">{actionMsg}</p>}
        {rows.length > 0 && (
          <p className="panel-note">
            Snapshot history is append-only and versioned (
            {data.snapshots.map((s) => `v${s.version}`).join(', ')}).
          </p>
        )}
      </Card>
    </div>
  )
}
