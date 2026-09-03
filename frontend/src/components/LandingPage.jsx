import { dashboardSummary, useAsync } from '../core/api.js'
import { formatINR, formatRate } from '../core/format.js'

const PILLARS = [
  {
    icon: '◎',
    title: 'AI Intelligence',
    desc: 'Understands payment failures, predicts recovery potential, and recommends the best recovery action.',
  },
  {
    icon: '⛊',
    title: 'Policy First',
    desc: 'Deterministic safety controls decide what actions are actually permitted. No overrides.',
  },
  {
    icon: '▶',
    title: 'Autonomous Execution',
    desc: 'Executes authorized recovery actions through bounded provider integrations.',
  },
  {
    icon: '✓',
    title: 'Verified Outcomes',
    desc: 'Separates "action executed" from "payment recovered" and requires authoritative evidence.',
  },
]

const FLOW_STEPS = [
  { label: 'Failed Payment', icon: '✕' },
  { label: 'AI Diagnosis', icon: '◎' },
  { label: 'Policy Gate', icon: '⛊' },
  { label: 'Optimization', icon: '≈' },
  { label: 'Recovery Action', icon: '▶' },
  { label: 'Verified Outcome', icon: '✓' },
]

function DashboardPreview({ data }) {
  if (!data) return null
  const op = data.operational ?? {}
  const bench = data.benchmark

  return (
    <div className="lp-preview">
      <div className="lp-preview__frame">
        <div className="lp-preview__topbar">
          <div className="lp-preview__dots">
            <span /><span /><span />
          </div>
          <span className="lp-preview__title">Recovery Command Center</span>
        </div>
        <div className="lp-preview__body">
          <div className="lp-preview__kpi-row">
            <div className="lp-preview__kpi lp-preview__kpi--danger">
              <span className="lp-preview__kpi-label">Revenue at Risk</span>
              <span className="lp-preview__kpi-value lp-preview__kpi-value--danger">
                {formatINR(op.revenue_at_risk_paise)}
              </span>
            </div>
            <div className="lp-preview__kpi">
              <span className="lp-preview__kpi-label">Simulated Recovered</span>
              <span className="lp-preview__kpi-value lp-preview__kpi-value--brand">
                {bench ? formatINR(bench.recovery_os_recovered_amount_paise) : '—'}
              </span>
            </div>
            <div className="lp-preview__kpi">
              <span className="lp-preview__kpi-label">Recovery Rate</span>
              <span className="lp-preview__kpi-value">
                {bench ? formatRate(bench.recovery_os_recovery_rate) : '—'}
              </span>
            </div>
          </div>
          <div className="lp-preview__kpi-row">
            <div className="lp-preview__kpi">
              <span className="lp-preview__kpi-label">Interventions Executed</span>
              <span className="lp-preview__kpi-value lp-preview__kpi-value--success">
                {op.interventions_executed}
              </span>
            </div>
            <div className="lp-preview__kpi">
              <span className="lp-preview__kpi-label">Blocked</span>
              <span className="lp-preview__kpi-value lp-preview__kpi-value--warn">
                {op.blocked_interventions}
              </span>
            </div>
            <div className="lp-preview__kpi">
              <span className="lp-preview__kpi-label">Fraud Blocked</span>
              <span className="lp-preview__kpi-value lp-preview__kpi-value--warn">
                {op.fraud_actions_blocked}
              </span>
            </div>
          </div>
          {bench && (
            <div className="lp-preview__bench">
              <div className="lp-preview__bench-header">
                <span className="lp-preview__bench-badge">SIMULATED</span>
                <span className="lp-preview__bench-label">V2 Benchmark — {bench.verdict}</span>
              </div>
              <div className="lp-preview__bench-bar">
                <div className="lp-preview__bench-track">
                  <div
                    className="lp-preview__bench-fill"
                    style={{ width: `${Math.round((bench.v2_oracle_value_capture || 0) * 100)}%` }}
                  />
                </div>
                <span className="lp-preview__bench-pct">
                  {Math.round((bench.v2_oracle_value_capture || 0) * 100)}% Oracle capture
                </span>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default function LandingPage({ onEnterDashboard }) {
  const { data } = useAsync((_signal) => dashboardSummary(), [])

  return (
    <div className="lp">
      {/* Ambient background */}
      <div className="lp__bg">
        <div className="lp__orb lp__orb--1" />
        <div className="lp__orb lp__orb--2" />
        <div className="lp__orb lp__orb--3" />
        <div className="lp__grid-overlay" />
      </div>

      {/* Nav */}
      <nav className="lp-nav">
        <div className="lp-nav__brand">
          <div className="lp-nav__mark">⌁</div>
          <span className="lp-nav__name">RecoveryOS</span>
        </div>
        <div className="lp-nav__links">
          <a href="#product" className="lp-nav__link">Product</a>
          <a href="#how-it-works" className="lp-nav__link">How It Works</a>
          <a href="#safety" className="lp-nav__link">Safety</a>
        </div>
        <button className="lp-nav__cta" onClick={onEnterDashboard}>
          View Dashboard
          <span className="lp-nav__cta-arrow">→</span>
        </button>
      </nav>

      {/* Hero */}
      <section className="lp-hero">
        <div className="lp-hero__inner">
          <div className="lp-hero__content">
            <span className="lp-hero__label">RECOVERYOS</span>
            <h1 className="lp-hero__headline">
              Recover revenue
              <br />
              that would otherwise
              <br />
              stay <span className="lp-hero__accent">lost.</span>
            </h1>
            <p className="lp-hero__desc">
              RecoveryOS is an AI-powered revenue recovery operating system that
              detects payment failures, decides the best recovery action, executes
              safely under policy, and verifies real-world outcomes.
            </p>
            <div className="lp-hero__actions">
              <button className="lp-btn lp-btn--primary" onClick={onEnterDashboard}>
                View Dashboard
                <span className="lp-btn__arrow">→</span>
              </button>
              <a href="#how-it-works" className="lp-btn lp-btn--ghost">
                See how it works
              </a>
            </div>
          </div>
          <div className="lp-hero__visual">
            <DashboardPreview data={data} />
          </div>
        </div>
      </section>

      {/* Product Pillars */}
      <section className="lp-section" id="product">
        <div className="lp-section__inner">
          <h2 className="lp-section__title">Built for recovery operations</h2>
          <p className="lp-section__sub">
            Every component is designed so that AI recommends, policy decides,
            and outcomes require evidence.
          </p>
          <div className="lp-pillars">
            {PILLARS.map((p) => (
              <div key={p.title} className="lp-pillar">
                <div className="lp-pillar__icon">{p.icon}</div>
                <h3 className="lp-pillar__title">{p.title}</h3>
                <p className="lp-pillar__desc">{p.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Safety / Trust */}
      <section className="lp-section lp-section--alt" id="safety">
        <div className="lp-section__inner lp-safety">
          <div className="lp-safety__content">
            <h2 className="lp-section__title">AI advises. Policy decides.</h2>
            <p className="lp-safety__text">
              The LLM never has direct authority over a money-moving action.
              A deterministic six-rule policy gate is always authoritative.
              No overrides. No shortcuts. Safety controls remain in place.
            </p>
            <div className="lp-safety__rules">
              <div className="lp-safety__rule">
                <span className="lp-safety__rule-dot lp-safety__rule-dot--success" />
                Fraud protection — always enforced
              </div>
              <div className="lp-safety__rule">
                <span className="lp-safety__rule-dot lp-safety__rule-dot--success" />
                Terminal failure blocking
              </div>
              <div className="lp-safety__rule">
                <span className="lp-safety__rule-dot lp-safety__rule-dot--success" />
                Duplicate intervention prevention
              </div>
              <div className="lp-safety__rule">
                <span className="lp-safety__rule-dot lp-safety__rule-dot--success" />
                Rolling customer intervention caps
              </div>
              <div className="lp-safety__rule">
                <span className="lp-safety__rule-dot lp-safety__rule-dot--success" />
                Event cooldown enforcement
              </div>
              <div className="lp-safety__rule">
                <span className="lp-safety__rule-dot lp-safety__rule-dot--success" />
                Global daily spend cap
              </div>
            </div>
          </div>
          <div className="lp-safety__visual">
            <div className="lp-safety__card">
              <div className="lp-safety__card-label">Policy Verdict</div>
              <div className="lp-safety__card-row">
                <span className="lp-safety__card-key">Intervention</span>
                <span className="lp-safety__card-val">payment_link</span>
              </div>
              <div className="lp-safety__card-row">
                <span className="lp-safety__card-key">AI Confidence</span>
                <span className="lp-safety__card-val">0.82</span>
              </div>
              <div className="lp-safety__card-row">
                <span className="lp-safety__card-key">Policy Gate</span>
                <span className="lp-safety__card-val lp-safety__card-val--success">ALLOW</span>
              </div>
              <div className="lp-safety__card-divider" />
              <div className="lp-safety__card-note">
                AI proposed. Policy authorized. Executor bounded.
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* How It Works */}
      <section className="lp-section" id="how-it-works">
        <div className="lp-section__inner">
          <h2 className="lp-section__title">How it works</h2>
          <p className="lp-section__sub">
            From failed payment to verified recovery — a closed control loop.
          </p>
          <div className="lp-flow">
            {FLOW_STEPS.map((step, i) => (
              <div key={step.label} className="lp-flow__step">
                <div className="lp-flow__node">
                  <span className="lp-flow__icon">{step.icon}</span>
                </div>
                <span className="lp-flow__label">{step.label}</span>
                {i < FLOW_STEPS.length - 1 && <div className="lp-flow__connector" />}
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Final CTA */}
      <section className="lp-section lp-section--cta">
        <div className="lp-section__inner lp-cta-block">
          <h2 className="lp-cta-block__title">See RecoveryOS in action</h2>
          <p className="lp-cta-block__desc">
            Explore the full Command Center — operational data, decision traces,
            policy controls, and recovery intelligence.
          </p>
          <button className="lp-btn lp-btn--primary lp-btn--lg" onClick={onEnterDashboard}>
            View Dashboard
            <span className="lp-btn__arrow">→</span>
          </button>
        </div>
      </section>

      {/* Footer */}
      <footer className="lp-footer">
        <div className="lp-footer__inner">
          <div className="lp-footer__brand">
            <div className="lp-nav__mark lp-nav__mark--sm">⌁</div>
            <span>RecoveryOS</span>
          </div>
          <span className="lp-footer__tag">AI Revenue Recovery Control</span>
        </div>
      </footer>
    </div>
  )
}
