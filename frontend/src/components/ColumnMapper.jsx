import { useState } from 'react'

const MAX_SUBJECTS = 3

/**
 * Lets an admin say which column plays which role.
 *
 * Shown when a file's columns cannot be identified automatically, and
 * reachable on demand when the guess is wrong. Only the score columns are
 * required — everything else has a sensible fallback.
 */
export default function ColumnMapper({ inspection, onConfirm, onCancel, busy }) {
  const columns = inspection.columns ?? []
  const suggested = inspection.suggested ?? {}
  const [mapping, setMapping] = useState({
    name: suggested.name ?? '',
    gender: suggested.gender ?? '',
    grade: suggested.grade ?? '',
    total: suggested.total ?? '',
    subjects: suggested.subjects ?? [],
  })

  const set = (role, value) => setMapping((m) => ({ ...m, [role]: value }))

  const toggleSubject = (column) => {
    setMapping((m) => {
      const has = m.subjects.includes(column)
      if (has) return { ...m, subjects: m.subjects.filter((c) => c !== column) }
      if (m.subjects.length >= MAX_SUBJECTS) return m
      return { ...m, subjects: [...m.subjects, column] }
    })
  }

  const numericFirst = [...columns].sort((a, b) => b.numeric_fraction - a.numeric_fraction)
  const ready = mapping.subjects.length > 0

  const options = (role) => (
    <select
      value={mapping[role]}
      onChange={(e) => set(role, e.target.value)}
      style={{
        width: '100%', padding: '7px 9px', font: 'inherit',
        border: '1px solid var(--rule-strong)', borderRadius: 'var(--r)', background: 'var(--paper)',
      }}
    >
      <option value="">— none —</option>
      {columns.map((c) => (
        <option key={c.column} value={c.column}>{c.column}</option>
      ))}
    </select>
  )

  return (
    <div className="card">
      <div className="card__head">
        <h2>Match your columns</h2>
      </div>
      <div className="card__body">
        <p style={{ margin: '0 0 14px', color: 'var(--ink-soft)', fontSize: 13 }}>
          {inspection.reason
            ? `${inspection.reason} Pick the columns to use.`
            : 'Confirm which column plays which role.'}{' '}
          <strong>{inspection.filename}</strong> · {inspection.row_count} rows
        </p>

        <div className="eyebrow" style={{ marginBottom: 8 }}>
          Score columns · pick up to {MAX_SUBJECTS} · required
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 16 }}>
          {numericFirst.map((c) => {
            const chosen = mapping.subjects.includes(c.column)
            const order = mapping.subjects.indexOf(c.column) + 1
            const disabled = !chosen && mapping.subjects.length >= MAX_SUBJECTS
            return (
              <button
                key={c.column}
                onClick={() => toggleSubject(c.column)}
                disabled={disabled}
                title={`${Math.round(c.numeric_fraction * 100)}% numeric · ${c.distinct} distinct · e.g. ${c.samples.join(', ')}`}
                style={{
                  borderColor: chosen ? 'var(--pine)' : 'var(--rule-strong)',
                  background: chosen ? 'var(--pine-soft)' : 'var(--paper)',
                  color: chosen ? 'var(--pine)' : 'var(--ink)',
                  fontFamily: 'var(--mono)', fontSize: 12,
                }}
              >
                {chosen ? `${order}. ` : ''}{c.column}
                <span style={{ opacity: 0.55, marginLeft: 6 }}>
                  {Math.round(c.numeric_fraction * 100)}%
                </span>
              </button>
            )
          })}
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 14 }}>
          <div>
            <div className="eyebrow" style={{ marginBottom: 5 }}>Name</div>
            {options('name')}
          </div>
          <div>
            <div className="eyebrow" style={{ marginBottom: 5 }}>Gender</div>
            {options('gender')}
          </div>
          <div>
            <div className="eyebrow" style={{ marginBottom: 5 }}>Grade</div>
            {options('grade')}
          </div>
          <div>
            <div className="eyebrow" style={{ marginBottom: 5 }}>Stated total</div>
            {options('total')}
          </div>
        </div>

        <p style={{ fontSize: 12, color: 'var(--ink-soft)', margin: '0 0 14px' }}>
          Leave a field as <em>none</em> if your file doesn't have it. Without a name column,
          students are numbered. The total is always recalculated from the score columns —
          selecting one only lets the app report where the file disagreed.
        </p>

        {inspection.preview?.length > 0 && (
          <div className="table-wrap" style={{ marginBottom: 14, maxHeight: 170, overflowY: 'auto' }}>
            <table>
              <thead>
                <tr>{columns.map((c) => <th key={c.column}>{c.column}</th>)}</tr>
              </thead>
              <tbody>
                {inspection.preview.map((row, i) => (
                  <tr key={i}>
                    {columns.map((c) => (
                      <td key={c.column} style={{ whiteSpace: 'nowrap', fontSize: 12 }}>
                        {row[c.column]}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div style={{ display: 'flex', gap: 8 }}>
          <button
            className="btn--primary"
            disabled={!ready || busy}
            onClick={() => onConfirm({
              name: mapping.name || null,
              gender: mapping.gender || null,
              grade: mapping.grade || null,
              total: mapping.total || null,
              subjects: mapping.subjects,
            })}
          >
            {busy ? 'Cleaning…' : 'Clean and load'}
          </button>
          <button onClick={onCancel} disabled={busy}>Cancel</button>
          {!ready && (
            <span style={{ alignSelf: 'center', fontSize: 12, color: 'var(--ink-soft)' }}>
              Pick at least one score column.
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
