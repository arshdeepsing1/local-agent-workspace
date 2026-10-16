import { html } from '../lib.js'
import { EFFORT_LABELS } from '../types.js'

const usageLabels = {
  input_tokens: 'Input tokens',
  output_tokens: 'Output tokens',
  total_tokens: 'Total tokens',
  cache_read_input_tokens: 'Cache read input tokens',
  cache_creation_input_tokens: 'Cache creation input tokens',
  reasoning_tokens: 'Reasoning tokens',
}

// Uncached input plus cache reads and writes: everything the request put in the context window.
function inputWithCache(usage) {
  if (typeof usage?.input_tokens !== 'number') return null
  return usage.input_tokens + (usage.cache_read_input_tokens ?? 0) + (usage.cache_creation_input_tokens ?? 0)
}

// The Databricks rate limit a final 429 named: type, limit, current use and the requested wait.
function rateLimitText(limit) {
  return (limit.limit_type ? String(limit.limit_type).replace(/_/g, ' ') : 'type not named')
    + (typeof limit.limit === 'number' ? `: limit ${limit.limit.toLocaleString()}` : '')
    + (typeof limit.current === 'number' ? `, current ${limit.current.toLocaleString()}` : '')
    + (typeof limit.retry_after === 'number' ? `, retry after ${limit.retry_after.toLocaleString()} s`
      + (typeof limit.retry_after_header === 'string' ? ' (Retry-After header)' : '') : '')
}

// What the 429 body said: "REQUEST_LIMIT_EXCEEDED: Exceeded workspace ..." (code and message, either may be missing).
function rateLimitMessage(limit) {
  const code = typeof limit.code === 'string' ? limit.code : ''
  const message = typeof limit.message === 'string' ? limit.message : ''
  return [code && !message.startsWith(code) ? code : '', message].filter(Boolean).join(': ')
}

// Claude's thinking is counted in output_tokens and never reported on its own; other endpoints may report it.
function usageValue(info, key) {
  const value = info.usage?.[key]
  if (value != null) return value.toLocaleString()
  if (key === 'reasoning_tokens' && info.usage && info.reasoning?.format === 'anthropic') return 'In output tokens (not reported separately)'
  return 'Unavailable'
}

// The reasoning a request asked for (RequestInfo.reasoning): "High (output_config.effort)".
export function effortText(reasoning) {
  const label = EFFORT_LABELS[reasoning.effort] || reasoning.effort
  if (reasoning.skipped === 'rejected') return `${label} (not sent: Databricks rejected ${reasoning.rejected_field}, so the request was sent again without reasoning fields)`
  if (reasoning.skipped === 'rejected_earlier') return `${label} (not sent: this endpoint rejected ${reasoning.rejected_field} earlier)`
  if (reasoning.effort === 'default') return Object.keys(reasoning.fields || {}).length ? `${label} (no effort field; thinking.display)` : `${label} (endpoint default)`
  if (!reasoning.effort_field) return `${label} (not sent: no reasoning fields for this endpoint)`
  const sent = reasoning.sent_effort && reasoning.sent_effort !== reasoning.effort ? ` as ${reasoning.sent_effort}` : ''
  return `${label} (${reasoning.effort_field}${sent})`
}

// reasoningWait: AgentEvent.reasoning_wait_seconds, how long after the request its first thinking arrived;
// reasoningText: the reply has thinking text.
export default function RequestDetails({ info, reasoningWait, reasoningText = false }) {
  const message = info.rate_limit ? rateLimitMessage(info.rate_limit) : ''
  return html`<details class="request-details">
    <summary>Request details · ${info.status}</summary>
    <div class="request-details-body">
      <dl>
        <div><dt>Model endpoint</dt><dd>${info.model}</dd></div>
        ${info.fallback_from ? html`<div><dt>${['completed', 'interrupted'].includes(info.status) ? 'Answered by' : 'Sent to'}</dt><dd>${info.model} (fallback)</dd></div>
          <div><dt>Fallback from</dt><dd>${info.fallback_from}${info.fallback_reason ? ` (${info.fallback_reason})` : ''}</dd></div>` : null}
        <div><dt>Request status</dt><dd>${info.status}</dd></div>
        <div><dt>Finish reason</dt><dd>${info.finish_reason ?? 'Unavailable'}</dd></div>
        <div><dt>HTTP status</dt><dd>${info.http_status ?? 'Unavailable'}</dd></div>
        ${info.error_kind || info.status === 'error' ? html`<div><dt>Error category</dt><dd>${info.error_kind?.replace(/_/g, ' ') ?? 'Unavailable'}</dd></div>` : null}
        ${info.rate_limit ? html`<div><dt>Rate limit</dt><dd>${rateLimitText(info.rate_limit)}</dd></div>` : null}
        ${message ? html`<div><dt>Databricks message</dt><dd>${message}</dd></div>` : null}
        ${info.reasoning ? html`<div><dt>Effort</dt><dd>${effortText(info.reasoning)}</dd></div>` : null}
        ${info.reasoning && Object.keys(info.reasoning.fields || {}).length ? html`<div><dt>Reasoning fields</dt><dd><code class="request-fields">${JSON.stringify(info.reasoning.fields)}</code></dd></div>` : null}
        ${typeof reasoningWait === 'number' ? html`<div><dt>${reasoningText ? 'Thinking text began' : 'Thinking began'}</dt><dd>after ${reasoningWait.toFixed(1)} s</dd></div>` : null}
      </dl>
      <p class="request-details-heading">Token usage · provider-reported</p>
      <dl>${Object.entries(usageLabels).map(([key, label]) => html`<div key=${key}><dt>${label}</dt><dd>${usageValue(info, key)}</dd></div>`)}<div><dt>Input incl. cache</dt><dd>${inputWithCache(info.usage)?.toLocaleString() ?? 'Unavailable'}</dd></div></dl>
      <p>Unavailable means the endpoint has not reported a value.</p>
      ${info.status !== 'completed' && info.usage ? html`<p>Usage received before completion may be partial.</p>` : null}
      <p>Per-request snapshot; excludes title and summary calls. This is not account usage or billing.</p>
    </div>
  </details>`
}
