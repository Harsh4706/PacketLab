const API_BASE = (import.meta.env.VITE_API_URL || 'http://localhost:5000').replace(/\/$/, '')

async function request(path, options = {}) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), options.timeout ?? 7000)
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      signal: controller.signal,
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.error || body.detail || body.message || `Request failed (${response.status})`)
    return body
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('Request timed out. Check that the PacketLab API is reachable.')
    throw error
  } finally {
    clearTimeout(timer)
  }
}

export const api = {
  health: () => request('/api/health', { timeout: 5000 }),
  sendMessage: (payload) => request('/api/send', { method: 'POST', body: JSON.stringify(payload) }),
  compare: (payload) => request('/api/compare', { method: 'POST', body: JSON.stringify(payload) }),
  remoteStatus: () => request('/api/remote/status'),
  startRemoteServer: (payload) => request('/api/remote/server/start', { method: 'POST', body: JSON.stringify(payload) }),
  stopRemoteServer: () => request('/api/remote/server/stop', { method: 'POST' }),
  sendFromRemoteServer: (payload) => request('/api/remote/server/send', { method: 'POST', body: JSON.stringify(payload) }),
  receiveRemote: (sessionId) => request(`/api/remote/client/receive?session_id=${encodeURIComponent(sessionId)}`),
  sendRemote: (payload) => request('/api/remote/client/send', { method: 'POST', body: JSON.stringify(payload), timeout: 10000 }),
  connectRemote: (payload) => request('/api/remote/client/connect', { method: 'POST', body: JSON.stringify(payload), timeout: 10000 }),
  contactRemote: (payload) => request('/api/remote/client/contact', { method: 'POST', body: JSON.stringify(payload), timeout: 10000 }),
}
