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
  useEffect(() => { setDraft(limit == null ? '0' : String(limit)) }, [limit, spending.source])
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
      : own ? 'This chat\'s own limit.' : 'The default from Settings → Agent.',
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

export default function UsagePanel({ session, onError, onSelectSession }) {
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
  const firstLimit = useRef(true)
  useEffect(() => { if (firstLimit.current) { firstLimit.current = false; return } void refresh() }, [pushedLimit])
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

  if (!session) return html`<section class="agent-tools-section" aria-label="Usage"><h3>Usage and cost</h3><p>Start a conversation to track its model usage.</p></section>`
  return html`<section class="agent-tools-section usage-panel" aria-label="Usage">
    <div class="agent-tools-section-heading"><div><h3>Usage and cost</h3><p>Provider-reported model calls for this conversation. Export CSV adds the reply and tools behind each call and the IDs sent to Databricks as request tags.</p></div><div class="agent-tools-actions"><button class="outline" disabled=${exporting} onClick=${() => void exportCsv()}>${exporting ? 'Exporting…' : 'Export CSV'}</button>${metrics?.subagents?.children?.length ? html`<button class="outline" disabled=${exporting} onClick=${() => void exportCsv(true)}>Export with subagents</button>` : null}<button class="outline" disabled=${loading} onClick=${() => void refresh()}>${loading ? 'Refreshing…' : 'Refresh usage'}</button></div></div>
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
    ${!metrics && loading ? html`<p role="status">Loading usage…</p>` : null}
    ${metrics ? html`
      ${metrics.spending ? html`<${SpendingLimit} session=${session} spending=${metrics.spending} onSaved=${refresh} />` : null}
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
