import { Card, Stat, Badge, LoadingBlock, ErrorState, EmptyState } from './ui.jsx'
import {
  dashboardSummary,
  useAsync,
} from '../core/api.js'
import { formatINR, formatRate } from '../core/format.js'

const STRATEGY_META = {
  recovery_os: { label: 'RecoveryOS', tone: 'brand', desc: 'AI-guided recovery pipeline' },
  naive_retry: { label: 'Naive retry', tone: 'info', desc: 'Blind retry on every failure' },
  no_action: { label: 'No action', tone: 'neutral', desc: 'Do nothing baseline' },
}

function BenchmarkPanel({ bench }) {
  if (!bench || bench.available === false) {
    return (
      <EmptyState
        title="Benchmark comparison unavailable"
        message="No persisted benchmark run is on record. Run the Phase 9 simulated benchmark (e.g. `python -m app.benchmark_store --seed 42 --count 500` from backend/) to populate a comparison. Until then the dashboard will not invent or guess recovery numbers."
      />
    )
  }
  return (
    <div className="benchmark">
      <div className="benchmark__meta">
        <Badge tone="warn">SIMULATED — evaluation, not production revenue</Badge>
        <span className="meta-line">
          Run {bench.run_id} · seed {bench.seed} · {bench.event_count} events ·{' '}
          {bench.evaluation_mode}
        </span>
      </div>
      <div className="strategy-grid">
        {bench.strategies.map((s) => {
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
              <div className="strategy__label">simulated recovered</div>
              <dl className="strategy__details">
                <div><dt>Recovery rate</dt><dd>{formatRate(s.recovery_rate)}</dd></div>
                <div><dt>Efficiency</dt><dd>{formatINR(s.efficiency_paise_per_intervention)}/action</dd></div>
                <div><dt>Interventions</dt><dd>{s.successful_interventions}/{s.interventions_attempted}</dd></div>
              </dl>
            </div>
          )
        })}
      </div>
      <div className="benchmark__delta">
        <div>
          <span className="delta-label">RecoveryOS vs no action</span>
          <span className="delta-value">
            {formatINR(bench.incremental_over_no_action_paise)}
          </span>
        </div>
        <div>
          <span className="delta-label">RecoveryOS vs naive retry</span>
          <span className="delta-value">
            {formatINR(bench.recoveryos_vs_naive_retry_paise)}
          </span>
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
        {notRecovered.categories.map((c) => (
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

  const op = data.operational
  const rec = data.recoverable_revenue
  const executed = op.interventions_executed
  const succeeded = op.interventions_executed_success

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
          Simulated benchmark
        </div>
      </header>

      <div className="kpi-grid">
        <Stat
          label="Revenue at Risk"
          tone="danger"
          value={formatINR(op.revenue_at_risk_paise)}
          hint={op.revenue_at_risk_source}
          sub="sum of ingested failed payments"
        />
        <Stat
          label="Interventions Executed"
          tone="success"
          value={`${succeeded} / ${executed}`}
          sub="successful / total"
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
        <Stat label="Events Ingested" value={op.event_count} sub="persisted failures" />
        <Stat
          label="Policy Decisions"
          tone="info"
          value={op.policy_decisions_total}
          sub="evaluations logged"
        />
      </div>

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
          subtitle="Persisted actions taken or withheld"
        >
          <NotRecoveredPanel notRecovered={data.not_recovered} />
        </Card>
      </div>

      <Card
        title="Simulated Benchmark Comparison"
        subtitle="Phase 9 three-strategy evaluation across the same event set"
        action={<Badge tone="warn">SIMULATED</Badge>}
      >
        <BenchmarkPanel bench={data.benchmark} />
      </Card>
    </div>
  )
}
