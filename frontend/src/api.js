// All calls are same-origin. In development Vite proxies /api and /ws to the
// FastAPI process on :8000; in production FastAPI serves this bundle itself.

// The session token is held in module scope, not localStorage: a token in
// localStorage is readable by any script on the page, and this one only needs
// to live as long as the tab.
let sessionToken = null

export function setToken(token) {
  sessionToken = token
}

function headers(_role, extra = {}) {
  return sessionToken
    ? { Authorization: `Bearer ${sessionToken}`, ...extra }
    : { ...extra }
}

async function handle(response) {
  if (!response.ok) {
    let message = `Request failed (${response.status})`
    try {
      const body = await response.json()
      if (body.detail) message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch { /* response had no JSON body */ }
    throw new Error(message)
  }
  return response.json()
}

export const api = {
  login: async (username, password) => {
    const session = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    }).then(handle)
    setToken(session.token)
    return session
  },

  logout: () => setToken(null),

  // Browsers cannot attach headers to a WebSocket upgrade, so the session
  // token is exchanged for a short-lived ticket passed in the query string.
  wsTicket: () => fetch('/api/ws-ticket', { method: 'POST', headers: headers() }).then(handle),

  inspect: (file, role) => {
    const body = new FormData()
    body.append('file', file)
    return fetch('/api/inspect', { method: 'POST', headers: headers(role), body }).then(handle)
  },

  upload: (file, role, mapping = null) => {
    const body = new FormData()
    body.append('file', file)
    if (mapping) body.append('mapping', JSON.stringify(mapping))
    return fetch('/api/upload', { method: 'POST', headers: headers(role), body }).then(handle)
  },

  students: (role, { minTotal = 0, shortlistOnly = false, search = '' } = {}) => {
    const query = new URLSearchParams({
      min_total: String(minTotal),
      shortlist_only: String(shortlistOnly),
      search,
    })
    return fetch(`/api/students?${query}`, { headers: headers(role) }).then(handle)
  },

  stats: (minTotal = 0) =>
    fetch(`/api/stats?min_total=${minTotal}`, { headers: headers() }).then(handle),

  cleaningLog: (role) => fetch('/api/cleaning-log', { headers: headers(role) }).then(handle),

  audit: (role) => fetch('/api/audit', { headers: headers(role) }).then(handle),

  setStatus: (id, status, role) =>
    fetch(`/api/students/${id}/status`, {
      method: 'PATCH',
      headers: headers(role, { 'Content-Type': 'application/json' }),
      body: JSON.stringify({ status }),
    }).then(handle),

  setStatusBulk: (ids, status, role) =>
    fetch('/api/students/status', {
      method: 'PATCH',
      headers: headers(role, { 'Content-Type': 'application/json' }),
      body: JSON.stringify({ ids, status }),
    }).then(handle),

  // Downloads go through fetch, not a plain <a href>. A browser navigation
  // cannot attach the X-Role header, so an anchor would arrive at the server
  // unauthenticated and be refused.
  download: async (url, role, fallbackName) => {
    const response = await fetch(url, { headers: headers(role) })
    if (!response.ok) {
      let message = `Download failed (${response.status})`
      try {
        const body = await response.json()
        if (body.detail) message = body.detail
      } catch { /* not JSON */ }
      throw new Error(message)
    }

    const disposition = response.headers.get('Content-Disposition') || ''
    const match = disposition.match(/filename="?([^"]+)"?/)
    const blob = await response.blob()
    const href = URL.createObjectURL(blob)

    const link = document.createElement('a')
    link.href = href
    link.download = match ? match[1] : fallbackName
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(href)
    return link.download
  },

  exportUrl: (minTotal) => `/api/export?min_total=${minTotal}`,
  rejectsUrl: () => '/api/export/rejects',
}
