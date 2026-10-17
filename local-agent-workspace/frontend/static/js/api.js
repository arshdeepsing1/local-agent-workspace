let token = ''
let reconnecting = null
// A request that could not reach the local server at all (the server stopped or restarts).
export const OFFLINE_MESSAGE = 'Cannot reach the local server. Check that it is still running.'
// fetch() rejects with a TypeError on a network failure; that becomes an ApiError with status 0 (the
// original error stays on error.cause). Any other rejection keeps its own message.
function offline(error) {
  if (!(error instanceof TypeError)) throw error
  const wrapped = new ApiError(OFFLINE_MESSAGE, 0)
  wrapped.cause = error
  throw wrapped
}
export function setToken(value) { token = value }
export function refreshToken() {
  if (!reconnecting) reconnecting = (async () => {
    const response = await fetch('/api/bootstrap').catch(offline)
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
  // A FastAPI validation error (HTTP 422) lists the problems: show the first one, named by its field.
  if (Array.isArray(detail) && typeof detail[0]?.msg === 'string') {
    const field = Array.isArray(detail[0].loc) ? detail[0].loc[detail[0].loc.length - 1] : null
    return new ApiError(`${typeof field === 'string' ? `${field}: ` : ''}${detail[0].msg}`, status)
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
export async function apiFile(path) {
  const { response, name } = await download(path)
  return { text: await response.text(), name }
}
// A file's bytes as a Blob (the Workspace preview and Download: GET /file/raw), with its file name or null.
export async function apiBlob(path) {
  const { response, name } = await download(path)
  return { blob: await response.blob(), name }
}
async function download(path, retried = false) {
  const response = await fetch(`/api${path}`, { headers: { 'X-Local-Token': token } }).catch(offline)
  if (response.ok) {
    return { response, name: /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '')?.[1] || null }
  }
  const data = await response.json().catch(() => ({}))
  if (response.status === 403 && data.code === 'reconnect_required' && !retried) {
    await refreshToken()
    return download(path, true)
  }
  throw errorFor(data, response.status, `Request failed with HTTP ${response.status}.`)
}
// Upload a file's bytes as the request body (a file attached in the composer), its name URL-encoded in
// X-File-Name (header values cannot carry every character); answers the server's JSON.
export async function apiUpload(path, file) {
  return request(path, 'POST', file, false, { 'Content-Type': 'application/octet-stream',
    'X-File-Name': encodeURIComponent(file.name || 'attachment') })
}
const UNREADABLE = Symbol('unreadable')  // a body that is not JSON (an HTML error page, an empty body)
async function request(path, method, body, retried = false, headers = {}) {
  const response = await fetch(`/api${path}`, {
    method, headers: { 'Content-Type': 'application/json', 'X-Local-Token': token, ...headers },
    body,
  }).catch(offline)
  // A request that was sent and then lost is not sent again: it may have run.
  const data = await response.json().catch(() => UNREADABLE)
  if (data === UNREADABLE) throw new ApiError(`The local server sent an unexpected response (HTTP ${response.status}).`, response.status)
  // This response is emitted before the server executes the request, so one
  // retry after replacing an expired local token cannot duplicate an action.
  if (response.status === 403 && data?.code === 'reconnect_required' && !retried) {
    await refreshToken()
    return request(path, method, body, true, headers)
  }
  if (!response.ok) throw errorFor(data, response.status, JSON.stringify(data?.detail || data))
  return data
}
