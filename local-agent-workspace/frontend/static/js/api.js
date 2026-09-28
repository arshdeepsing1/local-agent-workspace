let token = ''
let reconnecting = null
export function setToken(value) { token = value }
export function refreshToken() {
  if (!reconnecting) reconnecting = (async () => {
    const response = await fetch('/api/bootstrap')
    if (!response.ok) throw new Error('Cannot reconnect to the local server.')
    const data = await response.json()
    setToken(data.token)
  })().finally(() => { reconnecting = null })
  return reconnecting
}
export function streamUrl(id) {
  return `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/sessions/${id}/stream`
}
export function streamProtocols() { return ['local-workspace', token] }
export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status }
}
export async function api(path, method = 'GET', body, retried = false) {
  return request(path, method, body === undefined ? undefined : JSON.stringify(body), retried)
}
// Preserve the uploaded JSON text so the server can reject duplicate keys and
// invalid numbers before any parser has silently normalized them.
export async function apiJsonText(path, body) {
  return request(path, 'POST', body)
}
// Download a non-JSON response (for example a CSV export) with the local token.
export async function apiText(path, retried = false) {
  const response = await fetch(`/api${path}`, { headers: { 'X-Local-Token': token } })
  if (response.ok) return response.text()
  const data = await response.json().catch(() => ({}))
  if (response.status === 403 && data.code === 'reconnect_required' && !retried) {
    await refreshToken()
    return apiText(path, true)
  }
  throw new ApiError(typeof data.detail === 'string' ? data.detail : `Request failed with HTTP ${response.status}.`, response.status)
}
async function request(path, method, body, retried = false) {
  const response = await fetch(`/api${path}`, {
    method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': token },
    body,
  })
  const data = await response.json()
  // This response is emitted before the server executes the request, so one
  // retry after replacing an expired local token cannot duplicate an action.
  if (response.status === 403 && data.code === 'reconnect_required' && !retried) {
    await refreshToken()
    return request(path, method, body, true)
  }
  if (!response.ok) throw new ApiError(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data), response.status)
  return data
}
