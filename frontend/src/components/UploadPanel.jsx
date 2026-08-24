import { useRef, useState } from 'react'

const LABELS = {
  name_format: 'Name formatting normalised',
  name_typo: 'Name typos corrected',
  gender_normalised: 'Gender values standardised',
  gender_ambiguous: 'Gender left as Unknown',
  grade_parsed: 'Grade values parsed',
  grade_invalid: 'Grades outside 1–12 cleared',
  marks_parsed: 'Marks stripped of stray text',
  marks_out_of_range: 'Marks outside 0–100 cleared',
  marks_imputed: 'Missing marks imputed',
  total_mismatch: 'Totals corrected',
  duplicate_removed: 'Duplicate rows removed',
  quarantined: 'Rows quarantined',
  header_renamed: 'Column headers mapped',
}

const ORDER = Object.keys(LABELS)

export default function UploadPanel({ onUpload, onRemap, busy, report, error }) {
  const input = useRef(null)
  const [over, setOver] = useState(false)
  const [expanded, setExpanded] = useState(null)

  const submit = (file) => { if (file) onUpload(file) }

  const counts = report?.counts ?? {}
  const rows = ORDER.filter((key) => counts[key]).map((key) => ({ key, count: counts[key] }))
  const examples = (key) => (report?.entries ?? []).filter((e) => e.category === key).slice(0, 6)

  return (
    <div className="stack">
      <div className="card">
        <div className="card__head"><h2>Dataset</h2></div>
        <div className="card__body">
          {error && <div className="banner banner--error">{error}</div>}

          <div
            className={`dropzone${over ? ' dropzone--over' : ''}`}
            onDragOver={(e) => { e.preventDefault(); setOver(true) }}
            onDragLeave={() => setOver(false)}
            onDrop={(e) => { e.preventDefault(); setOver(false); submit(e.dataTransfer.files?.[0]) }}
          >
            <button className="btn--primary" onClick={() => input.current?.click()} disabled={busy}>
              {busy ? 'Cleaning…' : 'Choose a file'}
            </button>
            <p>or drop a CSV or Excel file here</p>
            <input
              ref={input}
              type="file"
              accept=".csv,.tsv,.xlsx,.xls"
              hidden
              onChange={(e) => { submit(e.target.files?.[0]); e.target.value = '' }}
            />
          </div>

          {report && (
            <div style={{ marginTop: 14 }}>
              <div className="eyebrow">Pipeline result</div>
              {report.subject_labels?.length > 0 && (
                <p style={{ margin: '6px 0 0', fontSize: 12, color: 'var(--ink-soft)' }}>
                  Scoring on <strong>{report.subject_labels.join(' + ')}</strong>
                  {onRemap && (
                    <button className="btn--sm" style={{ marginLeft: 8 }} onClick={onRemap} disabled={busy}>
                      Change columns
                    </button>
                  )}
                </p>
              )}
              <p style={{ margin: '6px 0 0', fontSize: 13 }}>
                <strong className="num">{report.rows_in}</strong> rows in ·{' '}
                <strong className="num">{report.rows_out}</strong> retained ·{' '}
                <strong className="num">{report.duplicates_removed}</strong> duplicates ·{' '}
                <strong className="num">{report.quarantined}</strong> quarantined
                <br />
                <span style={{ color: 'var(--ink-soft)' }}>
                  cleaned in <span className="num">{report.duration_ms}</span> ms
                </span>
              </p>
            </div>
          )}
        </div>
      </div>

      {rows.length > 0 && (
        <div className="card">
          <div className="card__head">
            <h2>What was changed</h2>
          </div>
          <div className="card__body" style={{ paddingTop: 6 }}>
            {rows.map(({ key, count }) => (
              <div key={key}>
                <div className="log-row" style={{ gridTemplateColumns: '1fr auto auto', alignItems: 'center' }}>
                  <span className="log-cat">{LABELS[key]}</span>
                  <span className="log-count">{count}</span>
                  <button
                    className="btn--sm"
                    onClick={() => setExpanded(expanded === key ? null : key)}
                    aria-expanded={expanded === key}
                  >
                    {expanded === key ? 'Hide' : 'Examples'}
                  </button>
                </div>
                {expanded === key && (
                  <div style={{ padding: '6px 0 10px' }}>
                    {examples(key).map((e, i) => (
                      <div key={i} className="log-detail" style={{ padding: '3px 0', fontSize: 12 }}>
                        row <span className="num">{e.row_ref}</span>:{' '}
                        <code>{e.before || '∅'}</code> → <code>{e.after || '∅'}</code>
                        {e.detail ? <span style={{ color: 'var(--ink-faint)' }}> · {e.detail}</span> : null}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
