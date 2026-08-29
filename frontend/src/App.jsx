import { useState } from 'react'
import './App.css'
import CommandCenter from './components/CommandCenter.jsx'
import EventTrace from './components/EventTrace.jsx'
import PolicyBlocks from './components/PolicyBlocks.jsx'
import PolicyLab from './components/PolicyLab.jsx'
import RevenueHealth from './components/RevenueHealth.jsx'

const NAV = [
  { key: 'command', label: 'Command Center', icon: '◧', Component: CommandCenter },
  { key: 'health', label: 'Revenue Health', icon: '◉', Component: RevenueHealth },
  { key: 'trace', label: 'Event Decision Trace', icon: '◈', Component: EventTrace },
  { key: 'blocks', label: 'Policy & Blocks', icon: '⛔', Component: PolicyBlocks },
  { key: 'lab', label: 'Policy Lab', icon: '⚗', Component: PolicyLab },
]

function App() {
  // A screen can hand the operator to another screen with context: Revenue
  // Health opens an affected payment in the EXISTING Event Decision Trace
  // rather than growing a second event view of its own.
  const [route, setRoute] = useState({ tab: 'command', params: {} })
  const active = NAV.find((n) => n.key === route.tab) || NAV[0]
  const Active = active.Component
  const navigate = (tab, params = {}) => setRoute({ tab, params })

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand__mark">⌁</div>
          <div>
            <div className="brand__name">RecoveryOS</div>
            <div className="brand__tag">Revenue Recovery Control</div>
          </div>
        </div>
        <nav className="nav">
          {NAV.map((n) => (
            <button
              key={n.key}
              className={`nav__item ${route.tab === n.key ? 'nav__item--active' : ''}`}
              onClick={() => navigate(n.key)}
            >
              <span className="nav__icon">{n.icon}</span>
              <span>{n.label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar__foot">
          <div className="foot-row">
            <span className="legend-dot" style={{ background: 'var(--success)' }} />
            <span>Read-only operator view</span>
          </div>
          <div className="foot-row">
            <span className="legend-dot" style={{ background: 'var(--warn)' }} />
            <span>SIMULATED figures labelled</span>
          </div>
        </div>
      </aside>

      <main className="content">
        <Active key={active.key} onNavigate={navigate} {...route.params} />
      </main>
    </div>
  )
}

export default App
