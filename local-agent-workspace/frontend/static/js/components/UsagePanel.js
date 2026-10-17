import { html, useCallback, useEffect, useMemo, useRef, useState } from '../lib.js'
import { api, apiText } from '../api.js'
import { EFFORT_LABELS } from '../types.js'
import { agentLabel } from '../subagents.js'

const integer = new Intl.NumberFormat()
const dbu = new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 })
const formatDbu = value => value > 0 && value < .0001 ? '<0.0001' : dbu.format(value)

function tokenValue(call, key) {
  const value = call.usage?.[key]
  return typeof value === 'number' ? value : null
}

function totalInput(call) {
  const uncached = tokenValue(call, 'input_tokens')
  return uncached == null ? null : uncached
    + (tokenValue(call, 'cache_read_input_tokens') ?? 0)
    + (tokenValue(call, 'cache_creation_input_tokens') ?? 0)
}

const tokenGeometry = value => value ?? 0
const formatTokens = value => value == null ? 'Unavailable' : integer.format(value)
const formatCallTokens = value => value == null ? '—' : integer.format(value)

function TokenChart({ calls }) {
  const recent = useMemo(() => calls.slice(-24), [calls])
  const maximum = Math.max(1, ...recent.flatMap(call => [
    tokenGeometry(totalInput(call)),
    tokenGeometry(tokenValue(call, 'output_tokens')),
  ]))
  const width = 640
  const plotHeight = 145
  const slot = recent.length ? width / recent.length : width
  const barWidth = Math.max(2, Math.min(10, slot * .3))
  return html`<figure class="usage-chart" aria-label="Input and output tokens by model call">
    <div class="usage-legend" aria-hidden="true"><span><i class="usage-input" />Input incl. cache</span><span><i class="usage-output" />Output</span></div>
    <svg role="img" aria-labelledby="usage-chart-title usage-chart-desc" viewBox="0 0 640 190" preserveAspectRatio="none">
      <title id="usage-chart-title">Tokens by model call</title>
      <desc id="usage-chart-desc">Grouped bars compare provider-reported input including cache with output tokens for the most recent ${recent.length} calls.</desc>
      ${[0, .5, 1].map(fraction => html`<line key=${fraction} x1="0" x2=${width} y1=${plotHeight - plotHeight * fraction + 8} y2=${plotHeight - plotHeight * fraction + 8} class="usage-grid" />`)}
      ${recent.map((call, index) => {
        const center = slot * index + slot / 2
        const input = totalInput(call)
        const output = tokenValue(call, 'output_tokens')
        const inputHeight = tokenGeometry(input) / maximum * plotHeight
        const outputHeight = tokenGeometry(output) / maximum * plotHeight
        const label = `${call.purpose} call ${calls.length - recent.length + index + 1}: ${input == null ? 'unavailable' : integer.format(input)} input, ${output == null ? 'unavailable' : integer.format(output)} output tokens`
        return html`<g key=${call.id} aria-label=${label}>
          <title>${label}</title>
          <rect class="usage-input" x=${center - barWidth - 1} y=${plotHeight + 8 - inputHeight} width=${barWidth} height=${inputHeight} rx="1" />
          <rect class="usage-output" x=${center + 1} y=${plotHeight + 8 - outputHeight} width=${barWidth} height=${outputHeight} rx="1" />
        </g>`
      })}
      <text x="4" y="183">oldest</text><text x="636" y="183" text-anchor="end">newest</text>
    </svg>
  </figure>`
}

// The latest agent request as Databricks reported it, next to the app's own estimate.
function LastInput({ report }) {
  if (!report || typeof report.input_tokens !== 'number') {
    return html`<div class="usage-last-input"><small>Last model input</small><p class="muted">No reported input yet.</p></div>`
  }
  return html`<div class="usage-last-input" role="group" aria-label="Last model input">
    <small>Last model input${report.partial ? ' (partial)' : ''}</small>
    <strong>${integer.format(report.input_tokens)} tokens</strong>
    <span>${typeof report.percent_of_input_budget === 'number' ? `${report.percent_of_input_budget}% of input budget` : 'Input budget unknown'}</span>
    <span>${typeof report.percent_of_model_window === 'number' ? `${report.percent_of_model_window}% of model window` : 'Model window unknown'}</span>
    <span>${typeof report.estimated_tokens === 'number' ? `App estimate ~${integer.format(report.estimated_tokens)}` : 'No app estimate'}</span>
  </div>`
}

const MAX_LIMIT_DBU = 100000

// The chat's spending limit (backend spending.py): a soft cap on the estimated DBU of the chat and its
// subagents, checked before each agent model request. Its own value wins over the Settings default;
// a subagent follows its parent's.
function SpendingLimit({ session, spending, onSaved }) {
  const limit = spending.limit_dbu
  const [draft, setDraft] = useState(limit == null ? '0' : String(limit))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  // The draft follows a new server value, never just a first render: a value typed before the mount's effects ran
  // (a slow page) would otherwise be replaced with the old one and saved.
  const shown = useRef(`${limit}:${spending.source}`)
  useEffect(() => {
    const key = `${limit}:${spending.source}`
    if (shown.current === key) return
    shown.current = key
    setDraft(limit == null ? '0' : String(limit))
  }, [limit, spending.source])
  const own = spending.source === 'chat'
  const follows = spending.follows_parent ?? !!session.is_subagent
  const save = async value => {
    setSaving(true); setError('')
    try {
      await api(`/sessions/${encodeURIComponent(session.id)}/spending-limit`, 'PUT', { spending_limit_dbu: value })
      await onSaved()
    } catch (cause) { setError(cause.message) } finally { setSaving(false) }
  }
  const submit = event => {
    event.preventDefault()
    const value = draft.trim() === '' ? NaN : Number(draft)
    if (!Number.isFinite(value) || value < 0 || value > MAX_LIMIT_DBU) {
      setError(`Enter a number of DBU from 0 (no limit) to ${MAX_LIMIT_DBU.toLocaleString('en-US')}.`)
      return
    }
    void save(value)
  }
  const spent = spending.spent_dbu || 0
  const over = limit != null && spent >= limit
  const summary = limit == null ? `${formatDbu(spent)} DBU · no limit` : `${formatDbu(spent)} of ${formatDbu(limit)} DBU`
  const details = [
    follows ? 'Follows its parent conversation\'s limit; the total includes the parent and its other subagents.'
      : limit == null ? (own ? 'No limit for this chat.' : 'No limit (Settings → Agent sets a default for every chat).')
      : own ? 'This chat\'s own limit.' : spending.source === 'project' ? 'The default from this project\'s settings file (lower than Settings → Agent).'
      : 'The default from Settings → Agent.',
    spending.subagents_dbu > 0 ? `Includes ${formatDbu(spending.subagents_dbu)} DBU from subagents.` : '',
    spending.unpriced_calls ? `${spending.unpriced_calls === 1 ? '1 call' : `${spending.unpriced_calls} calls`} to endpoints without price data ${spending.unpriced_calls === 1 ? 'is' : 'are'} not counted.` : '',
  ].filter(Boolean)
  return html`<div class=${`usage-spending${over ? ' usage-spending-over' : ''}`} role="group" aria-label="Spending limit">
    <div class="usage-spending-head"><small>Spending limit</small><strong>${summary}</strong>
      ${limit != null ? html`<span class="usage-spending-percent">${spending.percent}%</span>` : null}</div>
    ${limit != null ? html`<meter min="0" max=${limit} low=${limit * .8} high=${limit * .999} optimum="0" value=${Math.min(spent, limit)}
      aria-label=${`Estimated spend: ${spending.percent}% of the limit`} />` : null}
    ${details.map(text => html`<p class="field-help" key=${text}>${text}</p>`)}
    ${over && !follows ? html`<p class="usage-warning" role="status">The agent stops before its next model request until you raise the limit.</p>` : null}
    ${follows ? null : html`<form class="usage-spending-form" noValidate onSubmit=${submit}>
      <label>Limit for this chat (DBU, 0 = none)<input type="number" min="0" max=${MAX_LIMIT_DBU} step="any" inputmode="decimal"
        value=${draft} onInput=${event => setDraft(event.currentTarget.value)} /></label>
      <button class="outline" disabled=${saving}>${saving ? 'Saving…' : 'Save limit'}</button>
      <button type="button" class="outline" disabled=${saving || !own} onClick=${() => void save(null)}>Use Settings default</button>
    </form>`}
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
  </div>`
}

// The chat's turn limits (backend turns.py): a token and a time limit per turn, its own or the Settings default.
// At a limit the agent gets one wrap-up request and the turn ends; a subagent follows its parent's.
const MAX_TURN_TOKENS = 100000000
const MAX_TURN_MINUTES = 1440

function TurnLimits({ session, limits, onSaved }) {
  const tokens = limits.turn_token_limit
  const minutes = limits.turn_time_limit_minutes
  const [draft, setDraft] = useState({ tokens: String(tokens ?? 0), minutes: String(minutes ?? 0) })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const shown = useRef(`${tokens}:${minutes}:${limits.token_source}:${limits.time_source}`)  // As in SpendingLimit.
  useEffect(() => {
    const key = `${tokens}:${minutes}:${limits.token_source}:${limits.time_source}`
    if (shown.current === key) return
    shown.current = key
    setDraft({ tokens: String(tokens ?? 0), minutes: String(minutes ?? 0) })
  }, [tokens, minutes, limits.token_source, limits.time_source])
  const own = limits.token_source === 'chat' || limits.time_source === 'chat'
  const save = async body => {
    setSaving(true); setError('')
    try {
      await api(`/sessions/${encodeURIComponent(session.id)}/turn-limits`, 'PUT', body)
      await onSaved()
    } catch (cause) { setError(cause.message) } finally { setSaving(false) }
  }
  const submit = event => {
    event.preventDefault()
    const value = (text, maximum) => /^\d+$/.test(text.trim()) && Number(text) <= maximum ? Number(text) : null
    const body = { turn_token_limit: value(draft.tokens, MAX_TURN_TOKENS), turn_time_limit_minutes: value(draft.minutes, MAX_TURN_MINUTES) }
    if (body.turn_token_limit == null || body.turn_time_limit_minutes == null) {
      setError(`Enter whole numbers: tokens from 0 (no limit) to ${MAX_TURN_TOKENS.toLocaleString('en-US')}, minutes from 0 to ${MAX_TURN_MINUTES.toLocaleString('en-US')}.`)
      return
    }
    void save(body)
  }
  const parts = [tokens != null ? `${integer.format(tokens)} tokens` : '', minutes != null ? `${integer.format(minutes)} min` : ''].filter(Boolean)
  const origin = (key, word) => `${word} from ${limits[key] === 'chat' ? 'this chat' : 'Settings → Agent'}`
  const origins = [tokens != null && origin('token_source', 'Tokens'), minutes != null && origin('time_source', 'time')].filter(Boolean)
  const details = limits.follows_parent ? 'Follows its parent conversation\'s turn limits; its tokens count toward the parent\'s turn.'
    : parts.length ? `${origins.join(', ').replace(/^t/, 'T')}. At a limit the agent gets one last request to summarize what is done and what remains, then the turn ends; Continue starts a new one.`
    : own ? 'No turn limits for this chat.' : 'No turn limits (Settings → Agent sets defaults for every chat).'
  return html`<div class="usage-turn-limits" role="group" aria-label="Turn limits">
    <div class="usage-spending-head"><small>Turn limits</small><strong>${parts.length ? parts.join(' · ') : 'None'}</strong></div>
    <p class="field-help">${details}</p>
    ${limits.follows_parent ? null : html`<form class="usage-spending-form" noValidate onSubmit=${submit}>
      <label>Tokens per turn (0 = none)<input type="number" min="0" max=${MAX_TURN_TOKENS} step="1" inputmode="numeric"
        value=${draft.tokens} onInput=${event => { const text = event.currentTarget.value; setDraft(current => ({ ...current, tokens: text })) }} /></label>
      <label>Minutes per turn (0 = none)<input type="number" min="0" max=${MAX_TURN_MINUTES} step="1" inputmode="numeric"
        value=${draft.minutes} onInput=${event => { const text = event.currentTarget.value; setDraft(current => ({ ...current, minutes: text })) }} /></label>
      <button class="outline" disabled=${saving}>${saving ? 'Saving…' : 'Save turn limits'}</button>
      <button type="button" class="outline" disabled=${saving || !own}
        onClick=${() => void save({ turn_token_limit: null, turn_time_limit_minutes: null })}>Use Settings defaults</button>
    </form>`}
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
  </div>`
}

// The chat's subagents (metrics.subagents, backend planning.usage_rollup): the chat and its subagents together,
// then each subagent with its own calls, tokens and estimated DBU, and Open.
function SubagentUsage({ metrics, onSelectSession }) {
  const rollup = metrics.subagents
  const children = Array.isArray(rollup?.children) ? rollup.children : []
  if (!children.length) return null
  const own = metrics.totals
  const both = key => own[key] == null || rollup.totals[key] == null ? null : own[key] + rollup.totals[key]
  const spent = metrics.spending?.spent_dbu
  const cell = (usage, key) => formatCallTokens(usage ? usage[key] : null)
  return html`<div class="usage-subagents" role="group" aria-label="This chat and its subagents">
    <h3>This chat and its subagents</h3>
    <div class="usage-cards">
      <div><small>Model calls</small><strong>${integer.format(both('calls') ?? own.calls)}</strong></div>
      <div><small>Uncached input</small><strong>${formatTokens(both('input_tokens'))}</strong></div>
      <div><small>Cache read</small><strong>${formatTokens(both('cache_read_input_tokens'))}</strong></div>
      <div><small>Cache write</small><strong>${formatTokens(both('cache_creation_input_tokens'))}</strong></div>
      <div><small>Output tokens</small><strong>${formatTokens(both('output_tokens'))}</strong></div>
      <div><small>Estimated cost</small><strong>${typeof spent === 'number' ? `${formatDbu(spent)} DBU` : 'Unavailable'}</strong></div>
    </div>
    ${rollup.without_usage ? html`<p class="field-help">${rollup.without_usage === 1 ? '1 subagent' : `${rollup.without_usage} subagents`} from before this app kept subagent usage ${rollup.without_usage === 1 ? 'is' : 'are'} not in these totals.</p>` : null}
    <details class="usage-subagent-list"><summary>${children.length === 1 ? '1 subagent' : `${children.length} subagents`}</summary>
      <div class="usage-table-scroll"><table><caption class="sr-only">Usage of each subagent</caption><thead><tr><th>Subagent</th><th>Status</th><th>Calls</th><th>Uncached input</th><th>Cache read</th><th>Cache write</th><th>Output</th><th>Est. DBU</th><th><span class="sr-only">Open</span></th></tr></thead><tbody>
        ${children.map(child => html`<tr key=${child.id}><td><strong>${agentLabel(child.agent_type || 'general')}</strong> <small>${child.task}</small></td>
          <td>${child.deleted ? 'deleted' : String(child.status || '').replace(/_/g, ' ')}</td><td>${cell(child.usage, 'calls')}</td>
          <td>${cell(child.usage, 'input_tokens')}</td><td>${cell(child.usage, 'cache_read_input_tokens')}</td>
          <td>${cell(child.usage, 'cache_creation_input_tokens')}</td><td>${cell(child.usage, 'output_tokens')}</td>
          <td>${child.estimated_dbu == null ? '—' : formatDbu(child.estimated_dbu)}</td>
          <td>${child.deleted || !onSelectSession ? null : html`<button class="outline" aria-label=${`Open subagent: ${child.task}`} onClick=${() => onSelectSession(child.id)}>Open</button>`}</td></tr>`)}
      </tbody></table></div>
    </details>
  </div>`
}

// Agent tools → Usage: this chat's calls, or every chat's (All chats, GET /api/usage).
export default function UsagePanel({ session, onError, onSelectSession }) {
  const [scope, setScope] = useState('chat')
  const switcher = html`<div class="usage-scope" role="group" aria-label="Usage of">
    <button aria-pressed=${scope === 'chat'} onClick=${() => setScope('chat')}>This chat</button>
    <button aria-pressed=${scope === 'all'} onClick=${() => setScope('all')}>All chats</button></div>`
  return scope === 'all' ? html`<${AllUsage} onError=${onError} switcher=${switcher} />`
    : html`<${ChatUsage} session=${session} onError=${onError} onSelectSession=${onSelectSession} switcher=${switcher} />`
}

function ChatUsage({ session, onError, onSelectSession, switcher }) {
  const sessionId = session?.id
  const [metrics, setMetrics] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const alive = useRef(true)
  const latestRequest = useRef(0)
  const report = useRef(onError)
  report.current = onError
  useEffect(() => () => { alive.current = false }, [])
  const refresh = useCallback(async () => {
    if (!sessionId) return
    const request = ++latestRequest.current
    setLoading(true); setError('')
    try {
      const value = await api(`/sessions/${encodeURIComponent(sessionId)}/metrics`)
      if (alive.current && request === latestRequest.current) setMetrics(value)
    } catch (cause) {
      if (alive.current && request === latestRequest.current) { setError(cause.message); report.current(cause.message) }
    } finally { if (alive.current && request === latestRequest.current) setLoading(false) }
  }, [sessionId])
  useEffect(() => { setMetrics(null); void refresh() }, [refresh])
  // A pushed spending_limit change (another tab, the API) updates the chat's own limit: show it.
  const pushedLimit = session?.spending_limit_dbu
  const pushedTurnLimits = `${session?.turn_token_limit}:${session?.turn_time_limit_minutes}`
  const firstLimit = useRef(true)
  useEffect(() => { if (firstLimit.current) { firstLimit.current = false; return } void refresh() }, [pushedLimit, pushedTurnLimits])
  const [exporting, setExporting] = useState(false)
  // withSubagents: the subagent chats' rows too (conversation_id tells them apart).
  const exportCsv = async (withSubagents = false) => {
    if (!sessionId) return
    setExporting(true); setError('')
    try {
      const text = await apiText(`/sessions/${encodeURIComponent(sessionId)}/usage.csv${withSubagents ? '?include_subagents=true' : ''}`)
      const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }))
      const link = document.createElement('a')
      link.href = url
      link.download = `usage-${sessionId.slice(0, 8)}${withSubagents ? '-with-subagents' : ''}.csv`
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
    } catch (cause) {
      if (alive.current) { setError(cause.message); report.current(cause.message) }
    } finally { if (alive.current) setExporting(false) }
  }

  if (!session) return html`<section class="agent-tools-section" aria-label="Usage">${switcher}<h3>Usage and cost</h3><p>Start a conversation to track its model usage.</p></section>`
  return html`<section class="agent-tools-section usage-panel" aria-label="Usage">${switcher}
    <div class="agent-tools-section-heading"><div><h3>Usage and cost</h3><p>Provider-reported model calls for this conversation. Export CSV adds the reply and tools behind each call and the IDs sent to Databricks as request tags.</p></div><div class="agent-tools-actions"><button class="outline" disabled=${exporting} onClick=${() => void exportCsv()}>${exporting ? 'Exporting…' : 'Export CSV'}</button>${metrics?.subagents?.children?.length ? html`<button class="outline" disabled=${exporting} onClick=${() => void exportCsv(true)}>Export with subagents</button>` : null}<button class="outline" disabled=${loading} onClick=${() => void refresh()}>${loading ? 'Refreshing…' : 'Refresh usage'}</button></div></div>
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
    ${!metrics && loading ? html`<p role="status">Loading usage…</p>` : null}
    ${metrics ? html`
      ${metrics.spending ? html`<${SpendingLimit} session=${session} spending=${metrics.spending} onSaved=${refresh} />` : null}
      ${metrics.turn_limits ? html`<${TurnLimits} session=${session} limits=${metrics.turn_limits} onSaved=${refresh} />` : null}
      <${LastInput} report=${metrics.last_input} />
      <div class="usage-cards">
        <div><small>Model calls</small><strong>${integer.format(metrics.totals.calls)}</strong></div>
        <div><small>Uncached input</small><strong>${formatTokens(metrics.totals.input_tokens)}</strong></div>
        <div><small>Cache read</small><strong>${formatTokens(metrics.totals.cache_read_input_tokens)}</strong></div>
        <div><small>Cache write</small><strong>${formatTokens(metrics.totals.cache_creation_input_tokens)}</strong></div>
        <div><small>Output tokens</small><strong>${formatTokens(metrics.totals.output_tokens)}</strong></div>
        <div><small>429 responses</small><strong>${integer.format(metrics.totals.rate_limited)}</strong></div>
        <div><small>Automatic retries</small><strong>${integer.format(metrics.totals.retries ?? 0)}</strong></div>
        <div><small>Estimated cost</small><strong>${metrics.totals.estimated_dbu == null ? 'Unavailable' : `${formatDbu(metrics.totals.estimated_dbu)} DBU`}</strong></div>
      </div>
      <${SubagentUsage} metrics=${metrics} onSelectSession=${onSelectSession} />
      ${!metrics.complete ? html`<p class="usage-warning" role="status">This older conversation has no complete inference ledger. Totals include visible agent calls only; title, compaction, and retry attempts may be missing.</p>` : null}
      ${metrics.calls.length ? html`<${TokenChart} calls=${metrics.calls} />` : html`<p class="muted">No model calls have been recorded for this conversation.</p>`}
      <div class="usage-breakdown">
        <h3>Call breakdown</h3>
        <div class="usage-table-scroll"><table><caption class="sr-only">Model calls for this conversation</caption><thead><tr><th>Purpose</th><th>Model</th><th>Status</th><th>Total input</th><th>Uncached input</th><th>Cache read</th><th>Cache write</th><th>Output</th><th>Est. DBU</th></tr></thead><tbody>
          ${metrics.calls.slice().reverse().map(call => html`<tr key=${call.id}><td>${call.purpose}</td><td>${call.model}${call.effort ? html`<small class="usage-effort"> · ${EFFORT_LABELS[call.effort] || call.effort}</small>` : null}</td><td>${call.status}${call.http_status ? ` · ${call.http_status}` : ''}</td><td>${formatCallTokens(totalInput(call))}</td><td>${formatCallTokens(tokenValue(call, 'input_tokens'))}</td><td>${formatCallTokens(tokenValue(call, 'cache_read_input_tokens'))}</td><td>${formatCallTokens(tokenValue(call, 'cache_creation_input_tokens'))}</td><td>${formatCallTokens(tokenValue(call, 'output_tokens'))}</td><td>${call.estimated_dbu == null ? '—' : formatDbu(call.estimated_dbu)}</td></tr>`)}
        </tbody></table></div>
      </div>
      <p class="field-help usage-cost-note">Cost is a near-real-time estimate from provider-reported tokens and published pay-per-token DBU rates, not an invoice. Databricks billing records can lag and may apply contract pricing, credits, or regional uplifts. Unknown or provisioned endpoints show cost as unavailable. <a href=${metrics.pricing.source_url} target="_blank" rel="noreferrer">Pricing source</a>.</p>` : null}
  </section>`
}

// All chats: every conversation's calls by day, endpoint, purpose and project, from the usage summary each one
// keeps in its header (backend usage_overview.py). Days are local calendar dates.
const PERIODS = [7, 30, 90]
// Validated with the dataviz checks (three categorical slots, all pairs); the aqua step is under 3:1 on the
// surface, so the day table carries every value too.
const DAY_SERIES = [
  { key: 'cache_read_input_tokens', label: 'Cache read', className: 'usage-day-cached' },
  { key: 'uncached', label: 'Uncached + cache write', className: 'usage-day-uncached' },
  { key: 'output_tokens', label: 'Output', className: 'usage-day-output' },
]
const dayValue = (day, key) => key === 'uncached' ? day.input_tokens + day.cache_creation_input_tokens : day[key]
const dayTotal = day => DAY_SERIES.reduce((sum, series) => sum + dayValue(day, series.key), 0)
const inputTotal = row => row.input_tokens + row.cache_read_input_tokens + row.cache_creation_input_tokens
const shortDate = text => new Date(`${text}T12:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
const dbuText = row => row.estimated_dbu ? `${formatDbu(row.estimated_dbu)} DBU` : row.unpriced_calls === row.calls && row.calls ? 'No price data' : '0 DBU'

// A rect with rounded top corners only: the data end, square at the baseline.
function topRounded(x, y, width, height) {
  const r = Math.min(4, width / 2, height)
  return `M${x},${y + height}V${y + r}Q${x},${y} ${x + r},${y}H${x + width - r}Q${x + width},${y} ${x + width},${y + r}V${y + height}Z`
}

// Beside the hovered day, on the side with more room, so it never covers that day's bar.
function tooltipSide(index, count) {
  const center = (index + .5) / count * 100
  return center > 50 ? `right:calc(${100 - center}% + 18px)` : `left:calc(${center}% + 18px)`
}

function DayChart({ days }) {
  const [hover, setHover] = useState(null)
  const width = 640, plot = 150, top = 8
  const maximum = Math.max(1, ...days.map(dayTotal))
  const slot = width / days.length
  const bar = Math.max(2, Math.min(24, slot * .6))
  const shown = hover == null ? null : days[hover]
  return html`<figure class="usage-chart usage-day-chart" aria-label="Tokens by day">
    <div class="usage-legend" aria-hidden="true">${DAY_SERIES.map(series => html`<span key=${series.key}><i class=${series.className} />${series.label}</span>`)}</div>
    <svg role="img" aria-labelledby="usage-days-title usage-days-desc" viewBox="0 0 640 166" preserveAspectRatio="none" onPointerLeave=${() => setHover(null)}>
      <title id="usage-days-title">Tokens by day, all chats</title>
      <desc id="usage-days-desc">Stacked bars of cache reads, uncached input with cache writes, and output tokens for each of the last ${days.length} days; the table below lists every value.</desc>
      ${[0, .5, 1].map(fraction => html`<line key=${fraction} x1="0" x2=${width} y1=${top + plot - plot * fraction} y2=${top + plot - plot * fraction} class="usage-grid" />`)}
      ${days.map((day, index) => {
        let base = top + plot
        const x = slot * index + (slot - bar) / 2
        const parts = DAY_SERIES.map(series => ({ series, height: dayValue(day, series.key) / maximum * plot })).filter(part => part.height > 0)
        const label = `${shortDate(day.date)}: ${integer.format(dayTotal(day))} tokens, ${dbuText(day)}`
        return html`<g key=${day.date} tabindex=${day.calls ? 0 : -1} aria-label=${label}
          onPointerEnter=${() => setHover(index)} onFocus=${() => setHover(index)} onBlur=${() => setHover(null)}>
          <rect class="usage-day-hit" x=${slot * index} y=${top} width=${slot} height=${plot} />
          ${parts.map((part, position) => {
            // A 2px surface gap between stacked segments; the top segment carries the rounded end.
            const height = Math.max(0, part.height - (position ? 2 : 0))
            const y = base - part.height
            base = y
            return position === parts.length - 1
              ? html`<path key=${part.series.key} class=${part.series.className} d=${topRounded(x, y, bar, height)} />`
              : html`<rect key=${part.series.key} class=${part.series.className} x=${x} y=${y} width=${bar} height=${height} />`
          })}
        </g>`
      })}
    </svg>
    <div class="usage-day-axis" aria-hidden="true"><span>${shortDate(days[0].date)}</span><span>${shortDate(days.at(-1).date)}</span></div>
    ${shown ? html`<div class="usage-day-tooltip" aria-hidden="true" style=${tooltipSide(hover, days.length)}>
      <strong>${shortDate(shown.date)}</strong>
      ${DAY_SERIES.map(series => html`<span key=${series.key}><i class=${series.className} /><b>${integer.format(dayValue(shown, series.key))}</b> ${series.label.toLowerCase()}</span>`)}
      <span><b>${dbuText(shown)}</b> · ${integer.format(shown.calls)} calls</span>
    </div>` : null}
  </figure>`
}

function GroupTable({ title, rows, name }) {
  if (!rows.length) return null
  return html`<div class="usage-breakdown"><h3>${title}</h3>
    <div class="usage-table-scroll"><table><caption class="sr-only">${title}, all chats</caption>
      <thead><tr><th>${name}</th><th>Calls</th><th>Total input</th><th>Cache read</th><th>Output</th><th>Est. DBU</th></tr></thead>
      <tbody>${rows.map(row => html`<tr key=${row.key}><td title=${row.key}>${row.label || row.name || row.key}</td><td>${integer.format(row.calls)}</td>
        <td>${integer.format(inputTotal(row))}</td><td>${integer.format(row.cache_read_input_tokens)}</td><td>${integer.format(row.output_tokens)}</td>
        <td>${row.priced === false ? 'No price data' : formatDbu(row.estimated_dbu)}${row.unpriced_calls && row.priced !== false ? html`<small class="usage-effort"> · ${integer.format(row.unpriced_calls)} unpriced</small>` : null}</td></tr>`)}</tbody>
    </table></div></div>`
}

// The list prices the estimates use (backend telemetry.MODEL_DBU_PER_MILLION), with their sources.
function PriceTable({ pricing }) {
  const rate = value => typeof value === 'number' ? dbu.format(value) : '—'
  return html`<details class="usage-prices"><summary>List prices (DBU per million tokens, ${pricing.effective_at})</summary>
    <div class="usage-table-scroll"><table><caption class="sr-only">Databricks pay-per-token list prices</caption>
      <thead><tr><th>Endpoint</th><th>Input</th><th>Output</th><th>Cache read</th><th>Cache write</th></tr></thead>
      <tbody>${Object.entries(pricing.models).map(([model, rates]) => html`<tr key=${model}><td>${model}</td><td>${rate(rates.input_tokens)}</td>
        <td>${rate(rates.output_tokens)}</td><td>${rate(rates.cache_read_input_tokens)}</td><td>${rate(rates.cache_creation_input_tokens)}</td></tr>`)}</tbody>
    </table></div>
    <p class="field-help">Standard pay-per-token list rates from the <a href=${pricing.source_url} target="_blank" rel="noreferrer">proprietary</a> and <a href=${pricing.open_source_url} target="_blank" rel="noreferrer">open model</a> pricing pages. A cache rate shown as — is priced at the input rate. Other endpoints have no estimate.</p>
  </details>`
}

function AllUsage({ onError, switcher }) {
  const [days, setDays] = useState(30)
  const [value, setValue] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const alive = useRef(true)
  const latest = useRef(0)
  const report = useRef(onError)
  report.current = onError
  useEffect(() => () => { alive.current = false }, [])
  const refresh = useCallback(async () => {
    const request = ++latest.current
    setLoading(true); setError('')
    try {
      const result = await api(`/usage?days=${days}`)
      if (alive.current && request === latest.current) setValue(result)
    } catch (cause) {
      if (alive.current && request === latest.current) { setError(cause.message); report.current(cause.message) }
    } finally { if (alive.current && request === latest.current) setLoading(false) }
  }, [days])
  useEffect(() => { void refresh() }, [refresh])
  const totals = value?.totals
  return html`<section class="agent-tools-section usage-panel" aria-label="Usage">${switcher}
    <div class="agent-tools-section-heading"><div><h3>Usage across chats</h3><p>Every conversation's model calls, from the usage each one keeps with it. Days are your local calendar dates.</p></div>
      <div class="agent-tools-actions"><label class="usage-period">Period<select value=${days} onChange=${event => setDays(Number(event.currentTarget.value))}>
        ${PERIODS.map(count => html`<option key=${count} value=${count}>Last ${count} days</option>`)}</select></label>
        <button class="outline" disabled=${loading} onClick=${() => void refresh()}>${loading ? 'Refreshing…' : 'Refresh usage'}</button></div></div>
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
    ${!value && loading ? html`<p role="status">Loading usage…</p>` : null}
    ${value ? html`
      <p class="field-help usage-range">${shortDate(value.since)} – ${shortDate(value.until)}</p>
      ${value.pending_chats ? html`<p class="usage-warning" role="status">${integer.format(value.pending_chats)} older ${value.pending_chats === 1 ? 'chat is' : 'chats are'} not counted yet: Refresh usage counts the next ones.</p>` : null}
      <div class="usage-cards">
        <div><small>Chats with calls</small><strong>${integer.format(totals.chats)}</strong></div>
        <div><small>Model calls</small><strong>${integer.format(totals.calls)}</strong></div>
        <div><small>Uncached input</small><strong>${integer.format(totals.input_tokens)}</strong></div>
        <div><small>Cache read</small><strong>${integer.format(totals.cache_read_input_tokens)}</strong></div>
        <div><small>Cache write</small><strong>${integer.format(totals.cache_creation_input_tokens)}</strong></div>
        <div><small>Output tokens</small><strong>${integer.format(totals.output_tokens)}</strong></div>
        <div><small>Estimated cost</small><strong>${formatDbu(totals.estimated_dbu)} DBU</strong></div>
        <div><small>Calls without price data</small><strong>${integer.format(totals.unpriced_calls)}</strong></div>
      </div>
      ${totals.calls ? html`<${DayChart} days=${value.by_day} />` : html`<p class="muted">No model calls in the last ${value.days} days.</p>`}
      ${totals.calls ? html`<details class="usage-day-table"><summary>Show days as a table</summary><div class="usage-table-scroll"><table><caption class="sr-only">Tokens by day, all chats</caption>
        <thead><tr><th>Date</th><th>Calls</th>${DAY_SERIES.map(series => html`<th key=${series.key}>${series.label}</th>`)}<th>Est. DBU</th></tr></thead>
        <tbody>${value.by_day.filter(day => day.calls).reverse().map(day => html`<tr key=${day.date}><td>${day.date}</td><td>${integer.format(day.calls)}</td>
          ${DAY_SERIES.map(series => html`<td key=${series.key}>${integer.format(dayValue(day, series.key))}</td>`)}<td>${formatDbu(day.estimated_dbu)}</td></tr>`)}</tbody>
      </table></div></details>` : null}
      <${GroupTable} title="By endpoint" rows=${value.by_model} name="Endpoint" />
      <${GroupTable} title="By purpose" rows=${value.by_purpose} name="Purpose" />
      <${GroupTable} title="By project" rows=${value.by_project} name="Project" />
      <p class="field-help usage-cost-note">Cost is an estimate from provider-reported tokens and the list prices below, not an invoice; calls to endpoints without price data are counted in tokens only. Endpoint checks (Settings) are not chats and are not included.</p>
      <${PriceTable} pricing=${value.pricing} />` : null}
  </section>`
}
