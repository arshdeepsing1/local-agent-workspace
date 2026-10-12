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
  constructor(message, status, detail) { super(message); this.status = status; if (detail !== undefined) this.detail = detail }
}
// A 409 can name the conversation that blocks an action: detail { message, session_id, title }.
// Callers that pass on only error.message can still find that chat with blockingChat(message).
const blockers = new Map()
export function blockingChat(message) { return blockers.get(message) || null }
function errorFor(data, status, fallback) {
  const detail = data?.detail
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') {
    if (typeof detail.session_id === 'string') {
      blockers.delete(detail.message)
      blockers.set(detail.message, detail)
      if (blockers.size > 20) blockers.delete(blockers.keys().next().value)
    }
    return new ApiError(detail.message, status, detail)
  }
  return new ApiError(typeof detail === 'string' ? detail : fallback, status)
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
export async function apiText(path) {
  return (await apiFile(path)).text
}
// The same, with the file name from the response's Content-Disposition header (or null).
export async function apiFile(path, retried = false) {
  const response = await fetch(`/api${path}`, { headers: { 'X-Local-Token': token } })
  if (response.ok) {
    const name = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '')?.[1] || null
    return { text: await response.text(), name }
  }
  const data = await response.json().catch(() => ({}))
  if (response.status === 403 && data.code === 'reconnect_required' && !retried) {
    await refreshToken()
    return apiFile(path, true)
  }
  throw errorFor(data, response.status, `Request failed with HTTP ${response.status}.`)
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
  if (!response.ok) throw errorFor(data, response.status, JSON.stringify(data.detail || data))
  return data
}
