import { Card, Stat, Badge, LoadingBlock, ErrorState, EmptyState } from './ui.jsx'
import {
  dashboardSummary,
  useAsync,
} from '../core/api.js'
import { formatINR, formatRate } from '../core/format.js'

const STRATEGY_META = {
  recovery_os: { label: 'RecoveryOS', tone: 'brand', desc: 'AI-guided recovery pipeline' },
  recoveryos_v1: { label: 'RecoveryOS V1', tone: 'info', desc: 'Fixed-priority selection' },
  recoveryos_v2: { label: 'RecoveryOS V2', tone: 'brand', desc: 'Economic optimizer' },
  oracle: { label: 'Oracle', tone: 'warn', desc: 'Evaluation-only upper bound' },
  naive_retry: { label: 'Naive retry', tone: 'info', desc: 'Blind retry on every failure' },
  no_action: { label: 'No action', tone: 'neutral', desc: 'Do nothing baseline' },
}

/** Render a benchmark-derived value, honestly showing Unavailable when absent. */
function benchValue({ bench, value, formatter }) {
  if (!bench || bench.available === false) {
    return {
      tone: 'neutral',
      text: 'Unavailable',
      sub: 'no persisted benchmark run',
    }
  }
  return { tone: 'brand', text: formatter(value), sub: bench.evaluation_mode }
}

function BenchmarkPanel({ bench }) {
  if (!bench || bench.available === false) {
    return (
      <EmptyState
        title="Benchmark comparison unavailable"
        message="No persisted benchmark run is on record. Run the simulated benchmark (e.g. `python -m app.benchmark_store --seed 42 --count 500` from backend/) to populate a comparison. Until then the dashboard will not invent or guess recovery numbers."
      />
    )
  }
  const isPhase17 = String(bench.methodology || '').startsWith('phase17')
  return (
    <div className="benchmark">
      <div className="benchmark__meta">
        <Badge tone="warn">SIMULATED EVALUATION — synthetic benchmark, not production revenue</Badge>
        <span className="meta-line">
          Run {bench.run_id} · seed {bench.seed} · {bench.event_count} events ·{' '}
          {bench.evaluation_mode}
          {isPhase17 ? ` · ${bench.methodology} · result: ${bench.verdict}` : ''}
        </span>
        <p className="panel-note">
          These figures are produced by RecoveryOS&rsquo;s controlled synthetic
          benchmark against a hidden model of the world. They are not claims of
          production Razorpay recovery performance.
        </p>
      </div>
      <div className="strategy-grid">
        {(bench.strategies || []).map((s) => {
          const meta = STRATEGY_META[s.strategy] || { label: s.strategy, tone: 'neutral', desc: '' }
          return (
            <div key={s.strategy} className="strategy">
              <div className="strategy__head">
                <Badge tone={meta.tone}>{meta.label}</Badge>
                <span className="strategy__desc">{meta.desc}</span>
              </div>
              <div className="strategy__num" style={{ color: `var(--${meta.tone})` }}>
                {formatINR(s.recovered_amount_paise)}
              </div>
              <div className="strategy__label">SIMULATED revenue recovered</div>
              <dl className="strategy__details">
                {s.recovery_rate === undefined ? (
                  <>
                    <div><dt>vs No Action</dt><dd>{formatINR(s.incremental_vs_no_action_paise)}</dd></div>
                    <div><dt>Regret</dt><dd>{formatINR(s.total_regret_paise)}</dd></div>
                    <div><dt>Optimal</dt><dd>{formatRate(s.optimal_selection_rate)}</dd></div>
                    <div><dt>Interventions</dt><dd>{s.interventions_attempted}</dd></div>
                  </>
                ) : (
                  <>
                    <div><dt>Recovery rate</dt><dd>{formatRate(s.recovery_rate)}</dd></div>
                    <div><dt>Efficiency</dt><dd>{formatINR(s.efficiency_paise_per_intervention)}/action</dd></div>
                    <div><dt>Interventions</dt><dd>{s.successful_interventions}/{s.interventions_attempted}</dd></div>
                  </>
                )}
              </dl>
            </div>
          )
        })}
      </div>

      <div className="incremental">
        <h4 className="incremental__title">RecoveryOS vs baselines (SIMULATED)</h4>
        <div className="benchmark__delta">
          <div className="delta-card">
            <span className="delta-label">RecoveryOS vs No Action</span>
            <span className="delta-value">{formatINR(bench.incremental_over_no_action_paise)}</span>
            <span className="delta-note">incremental SIMULATED recovered revenue</span>
          </div>
          {isPhase17 ? (
            <>
              <div className="delta-card">
                <span className="delta-label">V2 vs V1</span>
                <span className="delta-value">{formatINR(bench.v2_vs_v1_paise)}</span>
                <span className="delta-note">incremental SIMULATED recovered revenue</span>
              </div>
              <div className="delta-card">
                <span className="delta-label">V2 Oracle value capture</span>
                <span className="delta-value">{formatRate(bench.v2_oracle_value_capture)}</span>
                <span className="delta-note">
                  share of the decision value the Oracle could add
                </span>
              </div>
              <div className="delta-card">
                <span className="delta-label">Economic regret</span>
                <span className="delta-value">{formatINR(bench.v2_total_regret_paise)}</span>
                <span className="delta-note">
                  V2 total; V1 {formatINR(bench.v1_total_regret_paise)}
                </span>
              </div>
            </>
          ) : (
            <div className="delta-card">
              <span className="delta-label">RecoveryOS vs Naive Retry</span>
              <span className="delta-value">{formatINR(bench.recoveryos_vs_naive_retry_paise)}</span>
              <span className="delta-note">incremental SIMULATED recovered revenue</span>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function NotRecoveredPanel({ notRecovered }) {
  return (
    <div className="notrecovered">
      <p className="panel-note">{notRecovered.note}</p>
      <div className="notrecovered__grid">
        {(notRecovered.categories || []).map((c) => (
          <div key={c.key} className="notrecovered__item">
            <div className="notrecovered__label">{c.label}</div>
            <div className="notrecovered__value">{formatINR(c.amount_paise)}</div>
            <div className="notrecovered__count">{c.count} events not acted on</div>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function CommandCenter() {
  const { status, data, error } = useAsync(
    (_signal) => dashboardSummary(),
    [],
  )

  if (status === 'loading') return <LoadingBlock label="Loading command center…" />

  if (status === 'error') {
    return (
      <ErrorState
        message={error?.message}
        retry={() => window.location.reload()}
      />
    )
  }

  const op = data.operational ?? {}
  const rec = data.recoverable_revenue ?? {}
  const bench = data.benchmark

  const simRecovered = benchValue({
    bench,
    value: bench?.recovery_os_recovered_amount_paise,
    formatter: formatINR,
  })
  const recoveryRate = benchValue({
    bench,
    value: bench?.recovery_os_recovery_rate,
    formatter: formatRate,
  })

  return (
    <div className="screen">
      <header className="page-head">
        <div>
          <h1 className="page-title">Recovery Command Center</h1>
          <p className="page-subtitle">
            Read-only operational view of the AI Revenue Recovery pipeline.
          </p>
        </div>
        <div className="source-legend">
          <span className="legend-dot" style={{ background: 'var(--success)' }} />
          Real ingested payments
          <span className="legend-divider" />
          <span className="legend-dot" style={{ background: 'var(--warn)' }} />
          SIMULATED benchmark
        </div>
      </header>

      <section className="kpi-group">
        <h2 className="group-label">Primary</h2>
        <div className="kpi-grid kpi-grid--primary">
          <Stat
            label="Revenue at Risk"
            tone="danger"
            value={formatINR(op.revenue_at_risk_paise)}
            hint={op.revenue_at_risk_source}
            sub="sum of ingested failed payments"
          />
          <Stat
            label="Recoverable Revenue"
            tone="neutral"
            value="Unavailable"
            sub="no canonical definition"
          />
          <Stat
            label="Simulated Revenue Recovered"
            tone="brand"
            value={simRecovered.text}
            sub={simRecovered.sub}
          />
          <Stat
            label="Recovery Rate"
            tone={recoveryRate.tone}
            value={recoveryRate.text}
            sub={recoveryRate.sub}
          />
        </div>
      </section>

      <section className="kpi-group">
        <h2 className="group-label">Operations</h2>
        <div className="kpi-grid">
          <Stat
            label="Interventions Executed"
            tone="success"
            value={op.interventions_executed}
            sub={`${op.interventions_executed_success} succeeded`}
          />
          <Stat
            label="Blocked Interventions"
            tone="warn"
            value={op.blocked_interventions}
            sub="denied by policy gates"
          />
          <Stat
            label="Fraud Actions Blocked"
            tone="warn"
            value={op.fraud_actions_blocked}
            sub="denied on fraud-suspect events"
          />
        </div>
      </section>

      <div className="panel-grid">
        <Card
          title="Recoverable Revenue"
          subtitle="What the pipeline could reclaim"
          action={<Badge tone="neutral">Definition unavailable</Badge>}
        >
          <div className="unavailable">
            <p>{rec.note}</p>
            <EmptyState
              title="No canonical value"
              message="The repository defines no recoverable-revenue metric, and the hidden outcome model is evaluation ground truth that is intentionally not exposed. This field is shown as unavailable rather than guessed."
            />
          </div>
        </Card>

        <Card
          title="Revenue Not Recovered"
          subtitle="Durable, persisted reasons only"
        >
          <NotRecoveredPanel notRecovered={data.not_recovered} />
        </Card>
      </div>

      <Card
        title="Simulated Benchmark Comparison"
        subtitle="Phase 9 three-strategy evaluation across the same event set"
        action={<Badge tone="warn">SIMULATED</Badge>}
      >
        <BenchmarkPanel bench={bench} />
      </Card>
    </div>
  )
}
