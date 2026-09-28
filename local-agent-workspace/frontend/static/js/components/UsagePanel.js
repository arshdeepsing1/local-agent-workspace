import { html, useCallback, useEffect, useMemo, useRef, useState } from '../lib.js'
import { api, apiText } from '../api.js'

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

export default function UsagePanel({ session, onError }) {
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
  const [exporting, setExporting] = useState(false)
  const exportCsv = async () => {
    if (!sessionId) return
    setExporting(true); setError('')
    try {
      const text = await apiText(`/sessions/${encodeURIComponent(sessionId)}/usage.csv`)
      const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }))
      const link = document.createElement('a')
      link.href = url
      link.download = `usage-${sessionId.slice(0, 8)}.csv`
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
    <div class="agent-tools-section-heading"><div><h3>Usage and cost</h3><p>Provider-reported model calls for this conversation. Export CSV adds the reply and tools behind each call and the IDs sent to Databricks as request tags.</p></div><div class="agent-tools-actions"><button class="outline" disabled=${exporting} onClick=${() => void exportCsv()}>${exporting ? 'Exporting…' : 'Export CSV'}</button><button class="outline" disabled=${loading} onClick=${() => void refresh()}>${loading ? 'Refreshing…' : 'Refresh usage'}</button></div></div>
    ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
    ${!metrics && loading ? html`<p role="status">Loading usage…</p>` : null}
    ${metrics ? html`
      <div class="usage-cards">
        <div><small>Model calls</small><strong>${integer.format(metrics.totals.calls)}</strong></div>
        <div><small>Uncached input</small><strong>${formatTokens(metrics.totals.input_tokens)}</strong></div>
        <div><small>Cache read</small><strong>${formatTokens(metrics.totals.cache_read_input_tokens)}</strong></div>
        <div><small>Cache write</small><strong>${formatTokens(metrics.totals.cache_creation_input_tokens)}</strong></div>
        <div><small>Output tokens</small><strong>${formatTokens(metrics.totals.output_tokens)}</strong></div>
        <div><small>429 responses</small><strong>${integer.format(metrics.totals.rate_limited)}</strong></div>
        <div><small>Estimated cost</small><strong>${metrics.totals.estimated_dbu == null ? 'Unavailable' : `${formatDbu(metrics.totals.estimated_dbu)} DBU`}</strong></div>
      </div>
      ${!metrics.complete ? html`<p class="usage-warning" role="status">This older conversation has no complete inference ledger. Totals include visible agent calls only; title, compaction, and retry attempts may be missing.</p>` : null}
      ${metrics.calls.length ? html`<${TokenChart} calls=${metrics.calls} />` : html`<p class="muted">No model calls have been recorded for this conversation.</p>`}
      <div class="usage-breakdown">
        <h3>Call breakdown</h3>
        <div class="usage-table-scroll"><table><caption class="sr-only">Model calls for this conversation</caption><thead><tr><th>Purpose</th><th>Model</th><th>Status</th><th>Uncached input</th><th>Cache read</th><th>Cache write</th><th>Output</th><th>Est. DBU</th></tr></thead><tbody>
          ${metrics.calls.slice().reverse().map(call => html`<tr key=${call.id}><td>${call.purpose}</td><td>${call.model}</td><td>${call.status}${call.http_status ? ` · ${call.http_status}` : ''}</td><td>${formatCallTokens(tokenValue(call, 'input_tokens'))}</td><td>${formatCallTokens(tokenValue(call, 'cache_read_input_tokens'))}</td><td>${formatCallTokens(tokenValue(call, 'cache_creation_input_tokens'))}</td><td>${formatCallTokens(tokenValue(call, 'output_tokens'))}</td><td>${call.estimated_dbu == null ? '—' : formatDbu(call.estimated_dbu)}</td></tr>`)}
        </tbody></table></div>
      </div>
      <p class="field-help usage-cost-note">Cost is a near-real-time estimate from provider-reported tokens and published pay-per-token DBU rates, not an invoice. Databricks billing records can lag and may apply contract pricing, credits, or regional uplifts. Unknown or provisioned endpoints show cost as unavailable. <a href=${metrics.pricing.source_url} target="_blank" rel="noreferrer">Pricing source</a>.</p>` : null}
  </section>`
}
