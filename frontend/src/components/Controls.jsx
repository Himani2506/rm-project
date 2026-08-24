import Histogram from './Histogram.jsx'

export function ThresholdCard({ threshold, onThreshold, stats, ceiling = 300 }) {
  return (
    <div className="card">
      <div className="card__head">
        <h2>Minimum total score</h2>
        <div className="topbar__spacer" />
        <input
          type="number"
          min="0"
          max={ceiling}
          value={threshold}
          onChange={(e) => onThreshold(Math.max(0, Math.min(ceiling, Number(e.target.value) || 0)))}
          style={{ width: 78 }}
          aria-label="Minimum total score"
        />
      </div>
      <div className="card__body" style={{ paddingBottom: 8 }}>
        <input
          type="range"
          min="0"
          max={ceiling}
          step="1"
          value={threshold}
          onChange={(e) => onThreshold(Number(e.target.value))}
          aria-label="Minimum total score slider"
        />
        <Histogram buckets={stats?.histogram ?? []} threshold={threshold} max={ceiling} />
        <div className="eyebrow" style={{ marginTop: 2 }}>
          Eligible cohort by total score · shaded bars clear the bar
        </div>
      </div>
    </div>
  )
}

export function StatsCard({ stats }) {
  const s = stats ?? {}
  return (
    <div className="card">
      <div className="stat-row">
        <div className="stat">
          <div className="stat__value stat__value--accent num">{s.matched ?? '—'}</div>
          <div className="stat__label eyebrow">Shortlisted</div>
        </div>
        <div className="stat">
          <div className="stat__value num">{s.avg_total ?? '—'}</div>
          <div className="stat__label eyebrow">Avg total</div>
        </div>
        <div className="stat">
          <div className="stat__value num">{s.eligible_pool ?? '—'}</div>
          <div className="stat__label eyebrow">Eligible pool</div>
        </div>
        <div className="stat">
          <div className="stat__value num">{s.debarred ?? '—'}</div>
          <div className="stat__label eyebrow">Debarred</div>
        </div>
      </div>
      <div className="latency">
        {(s.subject_labels ?? []).map((label, i) => (
          <span key={label}>{label.toLowerCase()} {s.subject_averages?.[i] ?? '—'}</span>
        ))}
        <span>top {s.top_total ?? '—'}</span>
        <span style={{ marginLeft: 'auto' }}>
          query {s.query_ms ?? '—'} ms
        </span>
      </div>
    </div>
  )
}

export function AuditFeed({ entries }) {
  if (!entries?.length) return null
  return (
    <div className="card">
      <div className="card__head"><h2>Recent activity</h2></div>
      <div className="card__body" style={{ paddingTop: 6, maxHeight: 220, overflowY: 'auto' }}>
        {entries.slice(0, 12).map((e) => (
          <div key={e.id} className="log-row" style={{ gridTemplateColumns: '1fr auto' }}>
            <span className="log-detail">
              <strong>{e.student_name}</strong> {e.from_value} → {e.to_value}
            </span>
            <span className="log-cat">{new Date(e.ts * 1000).toLocaleTimeString()}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
