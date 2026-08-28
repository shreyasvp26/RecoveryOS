import { useState } from 'react'
import './App.css'
import CommandCenter from './components/CommandCenter.jsx'
import EventTrace from './components/EventTrace.jsx'
import PolicyBlocks from './components/PolicyBlocks.jsx'

const NAV = [
  { key: 'command', label: 'Command Center', icon: '◧', Component: CommandCenter },
  { key: 'trace', label: 'Event Decision Trace', icon: '◈', Component: EventTrace },
  { key: 'blocks', label: 'Policy & Blocks', icon: '⛔', Component: PolicyBlocks },
]

function App() {
  const [tab, setTab] = useState('command')
  const active = NAV.find((n) => n.key === tab) || NAV[0]
  const Active = active.Component

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
              className={`nav__item ${tab === n.key ? 'nav__item--active' : ''}`}
              onClick={() => setTab(n.key)}
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
        <Active />
      </main>
    </div>
  )
}

export default App
