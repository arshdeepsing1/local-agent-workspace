import { html } from '../lib.js'

const usageLabels = {
  input_tokens: 'Input tokens',
  output_tokens: 'Output tokens',
  total_tokens: 'Total tokens',
  cache_read_input_tokens: 'Cache read input tokens',
  cache_creation_input_tokens: 'Cache creation input tokens',
  reasoning_tokens: 'Reasoning tokens',
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
      </dl>
      <p class="request-details-heading">Token usage · provider-reported</p>
      <dl>${Object.entries(usageLabels).map(([key, label]) => {
        const value = info.usage?.[key]
        return html`<div key=${key}><dt>${label}</dt><dd>${value?.toLocaleString() ?? 'Unavailable'}</dd></div>`
      })}</dl>
      <p>Unavailable means the endpoint has not reported a value.</p>
      ${info.status !== 'completed' && info.usage ? html`<p>Usage received before completion may be partial.</p>` : null}
      <p>Per-request snapshot; excludes title and summary calls. This is not account usage or billing.</p>
    </div>
  </details>`
}
