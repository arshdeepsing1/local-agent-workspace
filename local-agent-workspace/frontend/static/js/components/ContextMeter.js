import { html, useState } from '../lib.js'

export default function ContextMeter({ info, busy = false, onCompact }) {
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  if (!info) return html`<p class="context-empty">Context usage is estimated after the first turn.</p>`
  const legacyEstimate = info.estimate_method === 'conservative_utf8'
  const percent = Math.round(info.estimated_tokens / info.input_budget * 100)
  const safetyReserve = info.context_window - info.input_budget - info.reply_reserve
  return html`<details class="context-meter">
    <summary>
      <span>${info.prepared_for_next_turn ? 'Context preview' : 'Last model input'} · ${legacyEstimate ? 'estimate outdated' : `~${percent}%`}</span>
      ${!legacyEstimate ? html`<progress aria-label="Estimated model input usage" aria-valuetext=${`Approximately ${percent}% of input budget`} value=${Math.min(percent, 100)} max=${100} />` : null}
      ${info.compactions > 0 ? html`<span> · ${info.compactions} ${info.compactions === 1 ? 'compaction' : 'compactions'}</span>` : null}
      ${info.warnings.length > 0 ? html`<span> · ${info.warnings.length} ${info.warnings.length === 1 ? 'warning' : 'warnings'}</span>` : null}
    </summary>
    <div class="context-details">
      ${legacyEstimate ? html`<p>The saved meter counted bytes as tokens and can overstate context use. It will refresh on the next model request.</p>` : html`
        <p>Approximately ${info.estimated_tokens.toLocaleString()} of ${info.input_budget.toLocaleString()} input tokens used.</p>
        <p>${info.estimate_scale ? `Text-size estimate scaled ×${info.estimate_scale.toFixed(2)} to match the input tokens Databricks reported for recent requests to this model. Not billing usage.` : 'Heuristic text-size estimate, not a provider token count or billing usage. Actual token counts vary by model.'} ${info.prepared_for_next_turn ? 'Preview after compaction using saved tool definitions, or built-in tools when unavailable. Definitions and instructions may change on the next model request.' : 'Estimate for the last request; updates each model call.'}</p>
        ${info.summary_adjustment ? html`<p>${info.summary_adjustment === 'condensed' ? 'The latest conversation summary was over its size limit and was condensed by a short extra request.' : 'The latest conversation summary was over its size limit and part of it was omitted.'} Full history is preserved.</p>` : null}`}
      ${!legacyEstimate && info.breakdown ? html`
        <p class="context-heading">Approximate input breakdown</p>
        <dl class="context-breakdown">
          <div><dt>System instructions</dt><dd>~${info.breakdown.system_instructions.toLocaleString()} tokens</dd></div>
          <div><dt>Tool definitions</dt><dd>~${info.breakdown.tool_definitions.toLocaleString()} tokens</dd></div>
          <div><dt>Messages and tool results</dt><dd>~${info.breakdown.messages_and_results.toLocaleString()} tokens</dd></div>
          <div><dt>Conversation summary</dt><dd>~${info.breakdown.summary.toLocaleString()} tokens</dd></div>
          <div><dt>Request overhead</dt><dd>~${info.breakdown.request_overhead.toLocaleString()} tokens</dd></div>
        </dl>` : null}
      <p>Automatic compaction threshold: approximately ${info.input_budget.toLocaleString()} input tokens.</p>
      <p>Of the ${info.context_window.toLocaleString()}-token context window, ${info.reply_reserve.toLocaleString()} tokens are reserved for the response and ${safetyReserve.toLocaleString()} for safety.</p>
      <p>The response limit is separate from the context window. Increasing the context budget does not increase the response limit.</p>
      <p>Compactions: ${info.compactions}. Messages summarized: ${info.summarized_messages}.</p>
      <p class="context-heading">Loaded project instructions</p>
      ${info.instruction_sources?.length ? html`
        <ul>${info.instruction_sources.map(source => html`<li key=${source.path}>
          <code>${source.path}</code> · scope: <code>${source.scope}</code> and descendants · ${source.status === 'loaded' ? `~${source.estimated_tokens.toLocaleString()} tokens` : 'Omitted · 0 tokens loaded'}
          ${source.reason ? html`<p>${source.reason}</p>` : null}
        </li>`)}</ul>
        <p>File costs are estimates included in system instructions above; shared guidance and framing are counted separately.</p>`
        : info.instruction_files.length ? html`<ul>${info.instruction_files.map(file => html`<li key=${file}><code>${file}</code></li>`)}</ul>` : html`<p>No project instruction files loaded.</p>`}
      ${info.warnings.length ? html`<p class="context-heading">Warnings</p><ul class="context-warnings">${info.warnings.map((warning, index) => html`<li key=${index}>${warning}</li>`)}</ul>` : null}
      ${onCompact ? html`<form class="context-compact" onSubmit=${event => {
        event.preventDefault()
        if (busy || submitting) return
        setSubmitting(true)
        setError('')
        void onCompact(note.trim()).catch(reason => setError(reason.message)).finally(() => setSubmitting(false))
      }}>
        <label>Preservation note (optional)<textarea value=${note} maxLength=${1000} disabled=${busy || submitting}
          onInput=${event => setNote(event.currentTarget.value)} placeholder="Decisions or details to preserve in the summary" /></label>
        <p>Summarizes earlier turns using your model and may add inference charges. Full history and the latest turn stay intact. Use Stop to cancel.</p>
        <button type="submit" class="outline" disabled=${busy || submitting}>${submitting ? 'Starting compaction…' : 'Compact now'}</button>
        ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
      </form>` : null}
    </div>
  </details>`
}
