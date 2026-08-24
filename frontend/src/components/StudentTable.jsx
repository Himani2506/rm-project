import { useEffect, useRef, useState } from 'react'

function Switch({ checked, onChange, label, disabled }) {
  return (
    <label className="switch" title={label}>
      <input type="checkbox" checked={checked} onChange={onChange} disabled={disabled} aria-label={label} />
      <span className="switch__track" />
    </label>
  )
}

export default function StudentTable({
  students, threshold, role, selected, onSelect, onToggle, flashId, subjectLabels = [],
}) {
  const isAdmin = role === 'admin'
  const [sort, setSort] = useState({ key: 'total', dir: 'desc' })
  const headerCheckbox = useRef(null)

  const visibleIds = students.filter((s) => !s.quarantined).map((s) => s.id)
  const allSelected = visibleIds.length > 0 && visibleIds.every((id) => selected.has(id))
  const someSelected = visibleIds.some((id) => selected.has(id))

  useEffect(() => {
    if (headerCheckbox.current) headerCheckbox.current.indeterminate = someSelected && !allSelected
  }, [someSelected, allSelected])

  const sorted = [...students].sort((a, b) => {
    const { key, dir } = sort
    const av = a[key] ?? -Infinity
    const bv = b[key] ?? -Infinity
    const cmp = typeof av === 'string' ? av.localeCompare(bv) : av - bv
    return dir === 'asc' ? cmp : -cmp
  })

  const header = (key, label, numeric) => (
    <th
      key={key}
      className={numeric ? 'num-cell' : undefined}
      style={{ cursor: 'pointer' }}
      onClick={() => setSort((s) => ({ key, dir: s.key === key && s.dir === 'desc' ? 'asc' : 'desc' }))}
    >
      {label}{sort.key === key ? (sort.dir === 'desc' ? ' ↓' : ' ↑') : ''}
    </th>
  )

  if (!students.length) {
    return (
      <div className="empty">
        <strong>Nothing to show</strong>
        {isAdmin ? 'Upload a dataset, or lower the score threshold.' : 'No students meet the current threshold.'}
      </div>
    )
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {isAdmin && (
              <th style={{ width: 34 }}>
                <input
                  ref={headerCheckbox}
                  type="checkbox"
                  checked={allSelected}
                  aria-label="Select all listed students"
                  onChange={(e) => onSelect(visibleIds, e.target.checked)}
                />
              </th>
            )}
            {header('name', 'Name')}
            {header('gender', 'Gender')}
            {header('grade', 'Grade', true)}
            {subjectLabels.map((label, i) => header(`subject_${i + 1}`, label, true))}
            {header('total', 'Total', true)}
            <th>Status</th>
            {isAdmin && <th style={{ width: 60 }}>Debar</th>}
          </tr>
        </thead>
        <tbody>
          {sorted.map((s) => {
            const debarred = s.status === 'debarred'
            const below = s.total < threshold
            const classes = [
              debarred ? 'is-debarred' : '',
              s.quarantined ? 'is-quarantined' : '',
              flashId === s.id ? 'flash' : '',
            ].filter(Boolean).join(' ')

            return (
              <tr key={s.id} className={classes}>
                {isAdmin && (
                  <td>
                    <input
                      type="checkbox"
                      checked={selected.has(s.id)}
                      disabled={s.quarantined}
                      aria-label={`Select ${s.name}`}
                      onChange={(e) => onSelect([s.id], e.target.checked)}
                    />
                  </td>
                )}
                <td className="cell-name">
                  {s.name ?? <span className="below">— no name —</span>}
                  {s.imputed ? <span className="pill pill--flag" style={{ marginLeft: 6 }}>imputed</span> : null}
                </td>
                <td>{s.gender}</td>
                <td className="num-cell">{s.grade ?? '—'}</td>
                {subjectLabels.map((_, i) => (
                  <td key={i} className="num-cell">{s[`subject_${i + 1}`] ?? '—'}</td>
                ))}
                <td className={`num-cell total-cell${below ? ' below' : ''}`}>{s.total}</td>
                <td>
                  {s.quarantined ? (
                    <span className="pill pill--flag" title={s.quarantine_reason}>quarantined</span>
                  ) : (
                    <span className={`pill pill--${debarred ? 'debarred' : 'active'}`}>
                      {debarred ? 'debarred' : 'active'}
                    </span>
                  )}
                </td>
                {isAdmin && (
                  <td>
                    <Switch
                      checked={!debarred}
                      disabled={s.quarantined}
                      label={debarred ? `Reinstate ${s.name}` : `Debar ${s.name}`}
                      onChange={() => onToggle(s, debarred ? 'active' : 'debarred')}
                    />
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
