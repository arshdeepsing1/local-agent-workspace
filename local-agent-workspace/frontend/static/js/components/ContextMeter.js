import { html, useEffect, useLayoutEffect, useRef, useState } from '../lib.js'

// Custom instructions (Settings), the skill index and selected skills are not files with a folder scope.
const WIDE_SCOPES = ['all projects', 'this workspace', 'this project', 'this chat', 'this message']
// What one loaded instruction source costs: its estimate, marked when the budget cut it (instructions.py).
const sourceCost = source => source.status === 'loaded' ? `~${source.estimated_tokens.toLocaleString()} tokens`
  : source.status === 'trimmed' ? `Trimmed · ~${source.estimated_tokens.toLocaleString()} tokens loaded` : 'Omitted · 0 tokens loaded'
const sourceScope = source => WIDE_SCOPES.includes(source.scope) ? html`${source.path} · applies to ${source.scope}`
  : source.scope.startsWith('files matching ') ? html`<code>${source.path}</code> · applies to ${source.scope}`
  : html`<code>${source.path}</code> · scope: <code>${source.scope}</code> and descendants`
const count = value => typeof value === 'number' ? value.toLocaleString() : 'Not reported'

// The input Databricks reported for one request: total = uncached + cache read + cache write.
function inputParts(report) {
  const parts = [[report.uncached_input_tokens, 'uncached'], [report.cache_read_input_tokens, 'cache read'],
    [report.cache_creation_input_tokens, 'cache write']].filter(([value]) => value > 0)
  const total = report.input_tokens.toLocaleString()
  return parts.length > 1 || (parts.length === 1 && parts[0][1] !== 'uncached')
    ? `${total} = ${parts.map(([value, label]) => `${value.toLocaleString()} ${label}`).join(' + ')}` : total
}

// The request body actually sent, next to the Databricks 4 MB per-request limit.
function requestSize(bytes) {
  return bytes >= 1_000_000 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1000))} KB`
}

function share(percent, whole) {
  return typeof percent === 'number' && typeof whole === 'number' ? `${percent}% of ${whole.toLocaleString()}` : 'Unavailable'
}

function ReportedInput({ report, current, heading }) {
  const gap = report.estimate_gap_percent
  const cached = report.cache_read_input_tokens > 0 || report.cache_creation_input_tokens > 0
  const estimate = typeof report.estimated_tokens === 'number' ? `~${report.estimated_tokens.toLocaleString()}` : 'unavailable'
  // The scale later estimates use, with this report counted; older reports lack it.
  const next = report.next_estimate_scale
  const later = typeof next === 'number' ? `Later estimates are scaled from these reports (×${next.toFixed(2)}).` : 'Later estimates are scaled from these reports.'
  return html`
    <p class="context-heading">${heading} · reported by Databricks</p>
    <dl class="context-breakdown context-reported">
      <div><dt>Input tokens</dt><dd>${inputParts(report)}</dd></div>
      <div><dt>Output tokens</dt><dd>${count(report.output_tokens)}${typeof report.reasoning_tokens === 'number' ? ` (reasoning ${report.reasoning_tokens.toLocaleString()})` : ''}</dd></div>
      <div><dt>Input budget</dt><dd>${share(report.percent_of_input_budget, report.input_budget)}${report.auto_compact_percent ? ` (auto-compaction at ${report.auto_compact_percent}%)` : ''}</dd></div>
      <div><dt>Context budget</dt><dd>${share(report.percent_of_context_budget, report.context_window)}</dd></div>
      <div><dt>Model window</dt><dd>${typeof report.model_window === 'number' ? `${share(report.percent_of_model_window, report.model_window)} for ${report.model}` : 'Not known for this endpoint'}</dd></div>
      <div><dt>Status</dt><dd>${report.status ?? 'unknown'}${report.finish_reason ? ` · ${report.finish_reason}` : ''}${report.partial ? ' · partial (the response ended early; counts may be incomplete)' : ''}</dd></div>
    </dl>
    ${current && typeof gap === 'number' ? html`<p>${gap > 2
      ? `Databricks counted ${gap}% more input tokens than the app's estimate for this request (${estimate}). The app estimates from text size before sending; the provider counts with the model's tokenizer and adds its own formatting for messages and tools. ${later}`
      : gap < -2
        ? `Databricks counted ${Math.abs(gap)}% fewer input tokens than the app's estimate for this request (${estimate}). ${next === 1
          ? `${report.estimate_scale > 1 ? `That estimate was scaled ×${report.estimate_scale.toFixed(2)}; later estimates use the unscaled text-size heuristic (×1.00). ` : ''}The app never scales its estimate below the text-size heuristic, so compaction may start slightly early.` : later}`
        : "The app's estimate matched the reported count within 2%."}${cached ? ' Cached tokens still occupy the context window; they are cheaper, not smaller.' : ''}</p>` : null}`
}

// "Anchored on the last reported count: 52,310 tokens … + ~1,204 tokens added since": the estimate is the input
// Databricks reported for the previous request plus the scaled estimate of what changed since (estimate_anchor).
function anchoredText(info) {
  const anchor = info.estimate_anchor
  if (!anchor || typeof anchor.reported !== 'number') return null
  const change = info.estimated_tokens - anchor.reported
  return `Anchored on the last reported count: ${anchor.reported.toLocaleString()} tokens Databricks reported for the previous request ${change >= 0 ? '+' : '−'} ~${Math.abs(change).toLocaleString()} tokens ${change >= 0 ? 'added' : 'removed'} since. Only what changed is estimated from text size.`
}

// The tool definitions split per MCP server (tool_groups), shown when an MCP server's tools are offered.
function ToolGroups({ groups }) {
  const entries = Object.entries(groups || {})
  if (entries.length < 2) return null
  return html`<ul class="context-tool-groups">${entries.map(([name, tokens]) => html`<li key=${name}>${name} · ~${tokens.toLocaleString()} tokens</li>`)}</ul>`
}

// How the latest compaction fitted its summary within the size limit (summary_adjustment).
const ADJUSTMENTS = {
  condensed: 'The latest conversation summary was over its size limit and was condensed by a short extra request.',
  condensed_trimmed: 'The condensed conversation summary was still over its size limit, so the app shortened its least important sections',
}

// openSignal: a number that /context increases to open the details (a change after mounting only).
export default function ContextMeter({ info, model, busy = false, status, onCompact, openSignal = 0 }) {
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  // The open details float above the meter: a pointer press or focus outside closes them without moving focus.
  const details = useRef(null)
  const [open, setOpen] = useState(false)
  // The summary's click sets the state before the toggle event (input can run ahead of that event), and a layout
  // effect adds the listeners at once.
  useLayoutEffect(() => {
    if (!open) return
    const outside = event => { if (details.current && !details.current.contains(event.target)) details.current.open = false }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('focusin', outside)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('focusin', outside) }
  }, [open])
  // The open details float over the transcript: they close when the chat starts waiting for an answer,
  // so they never cover the new approval card.
  useEffect(() => {
    if (status === 'awaiting_approval' && details.current?.open) details.current.open = false
  }, [status])
  const signalSeen = useRef(openSignal)
  useEffect(() => {
    if (openSignal === signalSeen.current) return
    signalSeen.current = openSignal
    if (!details.current) return
    details.current.open = true
    setOpen(true)
    details.current.querySelector('summary')?.focus()
  }, [openSignal])
  if (!info) return html`<p class="context-empty">Context usage is estimated after the first turn.</p>`
  const legacyEstimate = info.estimate_method === 'conservative_utf8'
  const percent = Math.round(info.estimated_tokens / info.input_budget * 100)
  const safetyReserve = info.context_window - info.input_budget - info.reply_reserve
  const report = info.last_request
  // A report describes the request as sent; after a compaction, in a preview, or after
  // the input budget changed it is history. Until the next request is prepared, the info and the
  // report describe the same request (the model picker does not prepare one), so a model switch
  // alone makes it history only once a request to the new model has been prepared.
  const sameRequest = !!report && report.estimated_tokens === info.estimated_tokens
  const sameSetup = !!report && report.input_budget === info.input_budget
    && (sameRequest || !model || !report.model || report.model === model)
  const reportedIsCurrent = !!report && !info.prepared_for_next_turn && report.compactions === info.compactions
    && sameSetup && typeof report.input_tokens === 'number' && typeof report.percent_of_input_budget === 'number'
  const reportHeading = reportedIsCurrent ? 'Last request' : report && report.compactions !== info.compactions ? 'Before compaction' : 'Earlier request'
  const next = info.next_estimate
  // The report is history only because the budgets or the model changed after it (a model switch or
  // a Settings save): the last request's estimate was scaled for the old model, so the collapsed meter
  // shows the next request's estimate, already rescaled to the new model and budget.
  const setupChanged = !reportedIsCurrent && sameRequest && !info.prepared_for_next_turn && report.compactions === info.compactions
    && (report.input_budget !== info.input_budget || (!!model && !!report.model && report.model !== model))
    && !!next && typeof next.tokens === 'number' && typeof next.percent_of_input_budget === 'number'
  const estimated = setupChanged ? Math.round(next.percent_of_input_budget) : percent
  const value = reportedIsCurrent ? report.percent_of_input_budget : estimated
  const mark = info.auto_compact_percent
  const modelShare = reportedIsCurrent && typeof report.percent_of_model_window === 'number'
    ? `; ${report.percent_of_model_window}% of the model's ${report.model_window.toLocaleString()}-token window` : ''
  const threshold = info.auto_compact_threshold ?? info.input_budget
  // A calibration report replaced the starting ratio the shown estimate used (a cancelled or partial
  // request, or a small one, does not).
  const priorReplaced = reportedIsCurrent && typeof report.next_estimate_scale === 'number' && report.next_estimate_scale !== info.estimate_scale
  // Compact now has earlier turns to summarize (an older saved meter has no flag: allowed, as before).
  const compactable = info.compactable !== false
  return html`<details class="context-meter" ref=${details} onToggle=${event => setOpen(event.currentTarget.open)}
    onKeyDown=${event => {
      if (event.key !== 'Escape' || event.isComposing || !details.current?.open) return
      event.preventDefault()
      details.current.open = false
      details.current.querySelector('summary')?.focus()
    }}>
    <summary onClick=${() => setOpen(!details.current?.open)}>
      <span>${reportedIsCurrent
        ? `Last model input · ${report.input_tokens.toLocaleString()} tokens · ${report.percent_of_input_budget}%${report.partial ? ' (partial)' : ''}`
        : `${info.prepared_for_next_turn ? 'Context preview' : setupChanged ? 'Next request' : 'Last model input'} · ${legacyEstimate ? 'estimate outdated' : `~${estimated}%`}`}</span>
      ${!legacyEstimate ? html`<span class=${`context-bar${typeof mark === 'number' && value >= mark ? ' over' : ''}`}>${reportedIsCurrent
        ? html`<progress aria-label="Reported model input usage" aria-valuetext=${`${report.percent_of_input_budget}% of input budget: ${report.input_tokens.toLocaleString()} of ${report.input_budget.toLocaleString()} tokens reported by Databricks${modelShare}`} value=${Math.min(value, 100)} max=${100} />`
        : html`<progress aria-label=${setupChanged ? 'Estimated next request input usage' : 'Estimated model input usage'} aria-valuetext=${`Approximately ${estimated}% of input budget`} value=${Math.min(estimated, 100)} max=${100} />`}${typeof mark === 'number'
        ? html`<span class="context-bar-mark" aria-hidden="true" style=${`left:${mark}%`} />` : null}</span>` : null}
      ${info.compactions > 0 ? html`<span> · ${info.compactions} ${info.compactions === 1 ? 'compaction' : 'compactions'}</span>` : null}
      ${info.warnings.length > 0 ? html`<span> · ${info.warnings.length} ${info.warnings.length === 1 ? 'warning' : 'warnings'}</span>` : null}
    </summary>
    <div class="context-details">
      ${report && typeof report.input_tokens === 'number' ? html`<${ReportedInput} report=${report} current=${reportedIsCurrent} heading=${reportHeading} />
        <p class="context-heading">App estimate</p>` : null}
      ${legacyEstimate ? html`<p>The saved meter counted bytes as tokens and can overstate context use. It will refresh on the next model request.</p>` : html`
        <p>Approximately ${info.estimated_tokens.toLocaleString()} of ${info.input_budget.toLocaleString()} input tokens used.</p>
        ${anchoredText(info) ? html`<p class="context-anchor">${anchoredText(info)}</p>` : null}
        <p>${info.estimate_scale && info.estimate_prior ? `Text-size estimate scaled ×${info.estimate_scale.toFixed(2)}, this model's starting ratio ${priorReplaced ? `before Databricks reported input tokens for it; later estimates use ×${report.next_estimate_scale.toFixed(2)}` : 'until Databricks reports input tokens for it'}. Not billing usage.` : info.estimate_scale ? `Text-size estimate scaled ×${info.estimate_scale.toFixed(2)} to match the input tokens Databricks reported for recent requests to this model. Not billing usage.` : 'Heuristic text-size estimate, not a provider token count or billing usage. Actual token counts vary by model.'} ${info.prepared_for_next_turn ? 'Preview after compaction using saved tool definitions, or built-in tools when unavailable. Definitions and instructions may change on the next model request.' : 'Estimate for the last request; updates each model call.'}</p>
        ${next && typeof next.tokens === 'number' ? html`<p>Next request: ~${next.tokens.toLocaleString()} tokens (${next.percent_of_input_budget}% of the input budget${next.anchor ? ', anchored on the last reported count' : ''}) before your next message. Automatic compaction runs when it passes ~${threshold.toLocaleString()} tokens.</p>` : null}
        ${info.compaction_deferred ? html`<p>The estimate passed the auto-compact threshold, but summarizing earlier turns would not bring it under the threshold, so the request was sent as it is. Compaction runs when it would, or when the request no longer fits the input budget.</p>` : null}
        ${info.summary_adjustment ? html`<p>${info.summary_adjustment === 'condensed_trimmed'
          ? `${ADJUSTMENTS.condensed_trimmed}${info.handoff_path ? '; the saved handoff has them in full.' : '.'}`
          : ADJUSTMENTS[info.summary_adjustment] ?? 'The latest conversation summary was over its size limit and part of it was omitted.'} Full history is preserved.</p>` : null}
        ${info.summary_fitted_to_budget ? html`<p>The request after the latest compaction was over the input budget (non-ASCII text and escapes count more than plain text), so ${{
          condensed: 'a short extra request condensed the summary to fit',
          condensed_trimmed: 'a short extra request condensed the summary and the app shortened its least important sections to fit',
        }[info.summary_fitted_to_budget] ?? 'part of the summary was omitted to fit'}. Full history is preserved.</p>` : null}`}
      ${!legacyEstimate && info.breakdown ? html`
        <p class="context-heading">Approximate input breakdown (app estimate)</p>
        <dl class="context-breakdown">
          <div><dt>System instructions</dt><dd>~${info.breakdown.system_instructions.toLocaleString()} tokens</dd></div>
          <div><dt>Tool definitions</dt><dd>~${info.breakdown.tool_definitions.toLocaleString()} tokens<${ToolGroups} groups=${info.tool_groups} /></dd></div>
          <div><dt>Messages and tool results</dt><dd>~${info.breakdown.messages_and_results.toLocaleString()} tokens</dd></div>
          <div><dt>Conversation summary</dt><dd>~${info.breakdown.summary.toLocaleString()} tokens</dd></div>
          <div><dt>Request overhead</dt><dd>~${info.breakdown.request_overhead.toLocaleString()} tokens</dd></div>
        </dl>` : null}
      <p>${typeof info.auto_compact_threshold === 'number'
        ? `Automatic compaction at ${info.auto_compact_percent}% of the input budget: about ${info.auto_compact_threshold.toLocaleString()} input tokens (Settings → Auto-compact at).`
        : `Automatic compaction threshold: approximately ${info.input_budget.toLocaleString()} input tokens.`}</p>
      <p>Of the ${info.context_window.toLocaleString()}-token context window, ${info.reply_reserve.toLocaleString()} tokens are reserved for the response and ${safetyReserve.toLocaleString()} for safety.</p>
      <p>The response limit is separate from the context window. Increasing the context budget does not increase the response limit.</p>
      <p>Compactions: ${info.compactions}. Messages summarized: ${info.summarized_messages}.</p>
      ${Number.isInteger(info.payload_bytes) ? html`<p>Request size: ${requestSize(info.payload_bytes)} of Databricks' ${(info.payload_limit_bytes ?? 4_000_000) / 1e6} MB limit.</p>` : null}
      <p class="context-heading">Loaded instructions</p>
      ${info.instruction_sources?.length ? html`
        <ul>${info.instruction_sources.map(source => html`<li key=${source.path}>
          ${sourceScope(source)} · ${sourceCost(source)}
          ${source.reason ? html`<p>${source.reason}</p>` : null}
        </li>`)}</ul>
        <p>File costs are estimates included in system instructions above; shared guidance and framing are counted separately.${info.instruction_sources.some(source => source.scope === 'this message') ? ' A skill your message selected counts in messages and tool results instead.' : ''}</p>`
        : info.instruction_files.length ? html`<ul>${info.instruction_files.map(file => html`<li key=${file}><code>${file}</code></li>`)}</ul>` : html`<p>No project instruction files loaded.</p>`}
      ${info.warnings.length ? html`<p class="context-heading">Warnings</p><ul class="context-warnings">${info.warnings.map((warning, index) => html`<li key=${index}>${warning}</li>`)}</ul>` : null}
      ${onCompact ? html`<form class="context-compact" onSubmit=${event => {
        event.preventDefault()
        if (busy || submitting || !compactable) return
        setSubmitting(true)
        setError('')
        void onCompact(note.trim()).catch(reason => setError(reason.message)).finally(() => setSubmitting(false))
      }}>
        <label>Preservation note (optional)<textarea value=${note} maxLength=${1000} disabled=${busy || submitting || !compactable}
          onInput=${event => setNote(event.currentTarget.value)} placeholder="Decisions or details to preserve in the summary" /></label>
        ${compactable ? html`<p>Summarizes earlier turns using your model and may add inference charges. Full history and the latest turn stay intact. Use Stop to cancel.</p>`
          : html`<p id="context-compact-unavailable">${info.compactions > 0
            ? 'Nothing to compact yet: earlier turns are already summarized and the latest turn is kept whole. Compact now is available again after your next message.'
            : 'Nothing to compact yet: this conversation has one turn, and the latest turn is kept whole. Compact now is available after your next message.'}</p>`}
        <button type="submit" class="outline" disabled=${busy || submitting || !compactable}
          aria-describedby=${compactable ? undefined : 'context-compact-unavailable'}>${submitting ? 'Starting compaction…' : 'Compact now'}</button>
        ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
      </form>` : null}
    </div>
  </details>`
}
