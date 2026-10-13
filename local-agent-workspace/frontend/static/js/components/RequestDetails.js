import { html } from '../lib.js'

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
  return String(limit.limit_type ?? '').replace(/_/g, ' ')
    + (typeof limit.limit === 'number' ? `: limit ${limit.limit.toLocaleString()}` : '')
    + (typeof limit.current === 'number' ? `, current ${limit.current.toLocaleString()}` : '')
    + (typeof limit.retry_after === 'number' ? `, retry after ${limit.retry_after.toLocaleString()} s` : '')
}

export default function RequestDetails({ info }) {
  return html`<details class="request-details">
    <summary>Request details · ${info.status}</summary>
    <div class="request-details-body">
      <dl>
        <div><dt>Model endpoint</dt><dd>${info.model}</dd></div>
        <div><dt>Request status</dt><dd>${info.status}</dd></div>
        <div><dt>Finish reason</dt><dd>${info.finish_reason ?? 'Unavailable'}</dd></div>
        <div><dt>HTTP status</dt><dd>${info.http_status ?? 'Unavailable'}</dd></div>
        ${info.error_kind || info.status === 'error' ? html`<div><dt>Error category</dt><dd>${info.error_kind?.replace(/_/g, ' ') ?? 'Unavailable'}</dd></div>` : null}
        ${info.rate_limit ? html`<div><dt>Rate limit</dt><dd>${rateLimitText(info.rate_limit)}</dd></div>` : null}
      </dl>
      <p class="request-details-heading">Token usage · provider-reported</p>
      <dl>${Object.entries(usageLabels).map(([key, label]) => {
        const value = info.usage?.[key]
        return html`<div key=${key}><dt>${label}</dt><dd>${value?.toLocaleString() ?? 'Unavailable'}</dd></div>`
      })}<div><dt>Input incl. cache</dt><dd>${inputWithCache(info.usage)?.toLocaleString() ?? 'Unavailable'}</dd></div></dl>
      <p>Unavailable means the endpoint has not reported a value.</p>
      ${info.status !== 'completed' && info.usage ? html`<p>Usage received before completion may be partial.</p>` : null}
      <p>Per-request snapshot; excludes title and summary calls. This is not account usage or billing.</p>
    </div>
  </details>`
}
