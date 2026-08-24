import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api.js'
import { useLiveUpdates } from './useLiveUpdates.js'
import UploadPanel from './components/UploadPanel.jsx'
import ColumnMapper from './components/ColumnMapper.jsx'
import StudentTable from './components/StudentTable.jsx'
import { AuditFeed, StatsCard, ThresholdCard } from './components/Controls.jsx'

const DEFAULT_THRESHOLD = 150

function useDebounced(value, delay = 150) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return debounced
}

function Login({ onSignIn }) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('admin')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    setBusy(true)
    setError('')
    try {
      onSignIn(await api.login(username, password))
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <div className="card login">
        <div className="card__body">
          <div className="eyebrow">CDIE · Recruitment Manager Portal</div>
          <h1>Student shortlisting</h1>
          <p>Sign in to upload a cohort or review the current shortlist.</p>

          {error && <div className="banner banner--error">{error}</div>}

          <div className="field">
            <label htmlFor="u">Username</label>
            <input id="u" value={username} onChange={(e) => setUsername(e.target.value)}
                   onKeyDown={(e) => e.key === 'Enter' && submit()} />
          </div>
          <div className="field">
            <label htmlFor="p">Password</label>
            <input id="p" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                   onKeyDown={(e) => e.key === 'Enter' && submit()} />
          </div>

          <button className="btn--primary" style={{ width: '100%' }} onClick={submit} disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>

          <div className="demo-hint">
            Demo accounts: <code>admin / admin</code> manages the cohort,{' '}
            <code>student / student</code> sees the shortlist only. Passwords are
            PBKDF2-hashed and sessions are signed bearer tokens.
          </div>
        </div>
      </div>
    </div>
  )
}

export default function App() {
  const [session, setSession] = useState(null)
  const [threshold, setThreshold] = useState(DEFAULT_THRESHOLD)
  const [students, setStudents] = useState([])
  const [stats, setStats] = useState(null)
  const [report, setReport] = useState(null)
  const [audit, setAudit] = useState([])
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState(new Set())
  const [shortlistOnly, setShortlistOnly] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [toast, setToast] = useState(null)
  const [flashId, setFlashId] = useState(null)
  const [fetchMs, setFetchMs] = useState(null)
  const [truncated, setTruncated] = useState(0)
  const [downloading, setDownloading] = useState(false)
  const [inspection, setInspection] = useState(null)   // pending column mapping
  const [pendingFile, setPendingFile] = useState(null)

  const role = session?.role
  const isAdmin = role === 'admin'
  const debouncedThreshold = useDebounced(threshold)
  const ceiling = Math.max(stats?.max_total ?? 300, 1)
  const subjectLabels = stats?.subject_labels ?? []
  const debouncedSearch = useDebounced(search, 200)
  const toastTimer = useRef(null)
  const lastFile = useRef(null)

  const showToast = useCallback((message, undo) => {
    clearTimeout(toastTimer.current)
    setToast({ message, undo })
    toastTimer.current = setTimeout(() => setToast(null), 6000)
  }, [])

  const loadStudents = useCallback(async () => {
    if (!role) return
    try {
      const data = await api.students(role, {
        minTotal: debouncedThreshold, shortlistOnly, search: debouncedSearch,
      })
      setStudents(data.students)
      setFetchMs(data.query_ms)
      setTruncated(data.truncated ? data.count : 0)
    } catch (e) { setError(e.message) }
  }, [role, debouncedThreshold, shortlistOnly, debouncedSearch])

  const loadStats = useCallback(async () => {
    try { setStats(await api.stats(debouncedThreshold)) } catch { /* transient */ }
  }, [debouncedThreshold])

  const loadAudit = useCallback(async () => {
    if (role !== 'admin') return
    try { setAudit((await api.audit(role)).entries) } catch { /* transient */ }
  }, [role])

  useEffect(() => { loadStudents() }, [loadStudents])
  useEffect(() => { loadStats() }, [loadStats])

  // --- live updates -------------------------------------------------------
  const onLive = useCallback((message) => {
    if (message.type === 'status_changed') {
      setStudents((prev) => prev.map((s) => (s.id === message.student.id ? { ...s, ...message.student } : s)))
      setStats(message.stats)
      setFlashId(message.student.id)
      setTimeout(() => setFlashId(null), 900)
      loadAudit()
    } else if (message.type === 'bulk_status_changed') {
      const patch = new Map(message.students.map((s) => [s.id, s]))
      setStudents((prev) => prev.map((s) => (patch.has(s.id) ? { ...s, ...patch.get(s.id) } : s)))
      setStats(message.stats)
      loadAudit()
    } else if (message.type === 'dataset_replaced') {
      setSelected(new Set())
      loadStudents()
      setStats(message.stats)
    }
  }, [loadAudit, loadStudents])

  const { connected, clients } = useLiveUpdates(onLive, Boolean(session))

  // --- actions ------------------------------------------------------------
  const commit = async (file, mapping) => {
    setBusy(true)
    setError('')
    try {
      const result = await api.upload(file, role, mapping)

      // The server could not identify the columns and is asking us to choose.
      if (result.needs_mapping) {
        setInspection(result)
        setPendingFile(file)
        return
      }

      setInspection(null)
      setPendingFile(null)
      setReport(result.report)
      setSelected(new Set())
      const labels = result.stats?.subject_labels ?? []
      // Land the cutoff mid-distribution. Carrying over a threshold from a
      // previous file can leave a new one showing a shortlist of one, which
      // reads as a broken app rather than a strict filter.
      const buckets = result.stats?.histogram ?? []
      const seen = buckets.reduce((n, b) => n + b.count, 0)
      let running = 0
      const median = buckets.find((b) => (running += b.count) >= seen / 2)
      setThreshold(median ? median.bucket : Math.round((result.stats?.max_total ?? 300) / 2))
      await Promise.all([loadStudents(), loadStats()])
      showToast(
        `Cleaned ${result.report.rows_in} rows in ${result.report.duration_ms} ms` +
        (labels.length ? ` · scoring on ${labels.join(', ')}` : ''),
      )
    } catch (e) {
      setError(e.message)
      setReport(null)
    } finally {
      setBusy(false)
    }
  }

  const upload = (file) => { lastFile.current = file; return commit(file, null) }

  const remap = async () => {
    if (!pendingFile && !lastFile.current) return
    setBusy(true)
    try {
      const file = pendingFile ?? lastFile.current
      setInspection(await api.inspect(file, role))
      setPendingFile(file)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const toggle = async (student, next) => {
    const previous = student.status
    // Optimistic: the row flips immediately, the broadcast confirms it.
    setStudents((prev) => prev.map((s) => (s.id === student.id ? { ...s, status: next } : s)))
    try {
      const result = await api.setStatus(student.id, next, role)
      setStats(result.stats)
      showToast(
        `${student.name} ${next === 'debarred' ? 'debarred' : 'reinstated'}`,
        () => toggle({ ...student, status: next }, previous),
      )
    } catch (e) {
      setStudents((prev) => prev.map((s) => (s.id === student.id ? { ...s, status: previous } : s)))
      setError(e.message)
    }
  }

  const bulk = async (next) => {
    const ids = [...selected]
    if (!ids.length) return
    try {
      const result = await api.setStatusBulk(ids, next, role)
      setStats(result.stats)
      setSelected(new Set())
      showToast(`${ids.length} students ${next === 'debarred' ? 'debarred' : 'reinstated'}`,
        () => api.setStatusBulk(ids, next === 'debarred' ? 'active' : 'debarred', role).then(loadStudents))
    } catch (e) { setError(e.message) }
  }

  const download = async (url, fallbackName) => {
    setDownloading(true)
    setError('')
    try {
      showToast(`Downloaded ${await api.download(url, role, fallbackName)}`)
    } catch (e) {
      setError(e.message)
    } finally {
      setDownloading(false)
    }
  }

  const select = (ids, checked) => {
    setSelected((prev) => {
      const next = new Set(prev)
      ids.forEach((id) => (checked ? next.add(id) : next.delete(id)))
      return next
    })
  }

  const visible = useMemo(
    () => (shortlistOnly ? students : students),
    [students, shortlistOnly],
  )

  if (!session) return <Login onSignIn={setSession} />

  return (
    <>
      <header className="topbar">
        <div className="topbar__mark">RM Shortlisting <span>· CDIE</span></div>
        <div className="topbar__spacer" />
        <span className="presence">
          <span className={`dot${connected ? ' dot--live' : ''}`} />
          {connected ? `live · ${clients} connected` : 'reconnecting…'}
        </span>
        <span className="role-chip">{session.label}</span>
        <button className="btn--sm" onClick={() => { api.logout(); setSession(null) }}>Sign out</button>
      </header>

      <div className="shell">
        <div className="page-head">
          <div className="eyebrow">Technical assessment · Student data pipeline</div>
          <h1>{isAdmin ? 'Cohort management' : 'Current shortlist'}</h1>
          <p>
            {isAdmin
              ? 'Upload a raw cohort file, review what the pipeline corrected, then set a cutoff and export the shortlist. Debarring a student removes them from the shortlist for everyone, immediately.'
              : 'Students meeting the current minimum total score. The list updates as the coordinator adjusts the cutoff.'}
          </p>
        </div>

        <div className="grid">
          <div className="stack">
            {isAdmin && (
              inspection ? (
                <ColumnMapper
                  inspection={inspection}
                  busy={busy}
                  onConfirm={(mapping) => commit(pendingFile, mapping)}
                  onCancel={() => { setInspection(null); setPendingFile(null) }}
                />
              ) : (
                <UploadPanel
                  onUpload={upload}
                  onRemap={lastFile.current ? remap : null}
                  busy={busy}
                  report={report}
                  error={error}
                />
              )
            )}
            <ThresholdCard threshold={threshold} onThreshold={setThreshold} stats={stats} ceiling={ceiling} />
            {isAdmin && <AuditFeed entries={audit} />}
          </div>

          <div className="stack">
            <StatsCard stats={stats} />

            <div className="card">
              <div className="toolbar">
                <input
                  className="search"
                  type="text"
                  placeholder="Search by name"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  aria-label="Search by name"
                />
                <label className="switch" style={{ gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={shortlistOnly}
                    onChange={(e) => setShortlistOnly(e.target.checked)}
                  />
                  <span className="switch__track" />
                  <span style={{ fontSize: 12 }}>Shortlist only</span>
                </label>

                <div className="toolbar__spacer" />

                {isAdmin && selected.size > 0 && (
                  <>
                    <span className="eyebrow">{selected.size} selected</span>
                    <button className="btn--sm btn--danger" onClick={() => bulk('debarred')}>Debar</button>
                    <button className="btn--sm" onClick={() => bulk('active')}>Reinstate</button>
                  </>
                )}
                {isAdmin && (
                  <>
                    {stats?.quarantined > 0 && (
                      <button
                        className="btn--sm"
                        disabled={downloading}
                        onClick={() => download(api.rejectsUrl(), 'quarantined-rows.csv')}
                      >
                        Quarantined rows
                      </button>
                    )}
                    <button
                      className="btn--primary btn--sm"
                      disabled={downloading || !stats?.matched}
                      onClick={() => download(api.exportUrl(debouncedThreshold), 'shortlist.csv')}
                    >
                      {downloading ? 'Preparing…' : 'Export shortlist'}
                    </button>
                  </>
                )}
              </div>

              <StudentTable
                students={visible}
                threshold={debouncedThreshold}
                role={role}
                selected={selected}
                onSelect={select}
                onToggle={toggle}
                flashId={flashId}
                subjectLabels={subjectLabels}
              />

              <div className="latency">
                <span>
                  {visible.length} rows rendered
                  {truncated ? ` of ${truncated} matched — export includes all` : ''}
                </span>
                <span>fetch {fetchMs ?? '—'} ms</span>
                <span style={{ marginLeft: 'auto' }}>cutoff ≥ {debouncedThreshold}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {toast && (
        <div className="toast" role="status">
          <span>{toast.message}</span>
          {toast.undo && (
            <button onClick={() => { toast.undo(); setToast(null) }}>Undo</button>
          )}
        </div>
      )}
    </>
  )
}
