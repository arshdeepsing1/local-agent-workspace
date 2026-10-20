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

// The settings file that sets a chat's auto-compact threshold (auto_compact_source; settings_layers.py).
const COMPACT_FILES = { project: '.agents/settings.json', local: '.agents/settings.local.json' }

// What the collapsed meter shows for a chat's context info: the meter and Workspace → Progress both read it from
// here, so the two can never disagree. model: the chat's model. Returns { legacyEstimate (a byte-counted saved meter:
// no bar), reportedIsCurrent (Databricks' report describes the request the meter shows), setupChanged (the budgets or
// model changed after it: the next request's estimate is shown), estimated (the app's estimate, % of the input budget),
// value (the bar's %), mark (the auto-compact %), label (the one-line text), ariaValueText (the bar's spoken value),
// report (info.last_request) }.
export function meterReading(info, model) {
  const legacyEstimate = info.estimate_method === 'conservative_utf8'
  const percent = Math.round(info.estimated_tokens / info.input_budget * 100)
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
  const next = info.next_estimate
  // The report is history only because the budgets or the model changed after it (a model switch or
  // a Settings save): the last request's estimate was scaled for the old model, so the collapsed meter
  // shows the next request's estimate, already rescaled to the new model and budget.
  const setupChanged = !reportedIsCurrent && sameRequest && !info.prepared_for_next_turn && report.compactions === info.compactions
    && (report.input_budget !== info.input_budget || (!!model && !!report.model && report.model !== model))
    && !!next && typeof next.tokens === 'number' && typeof next.percent_of_input_budget === 'number'
  const estimated = setupChanged ? Math.round(next.percent_of_input_budget) : percent
  const value = reportedIsCurrent ? report.percent_of_input_budget : estimated
  const modelShare = reportedIsCurrent && typeof report.percent_of_model_window === 'number'
    ? `; ${report.percent_of_model_window}% of the model's ${report.model_window.toLocaleString()}-token window` : ''
  const label = reportedIsCurrent
    ? `Last model input · ${report.input_tokens.toLocaleString()} tokens · ${report.percent_of_input_budget}%${report.partial ? ' (partial)' : ''}`
    : `${info.prepared_for_next_turn ? 'Context preview' : setupChanged ? 'Next request' : 'Last model input'} · ${legacyEstimate ? 'estimate outdated' : `~${estimated}%`}`
  const ariaValueText = reportedIsCurrent
    ? `${report.percent_of_input_budget}% of input budget: ${report.input_tokens.toLocaleString()} of ${report.input_budget.toLocaleString()} tokens reported by Databricks${modelShare}`
    : `Approximately ${estimated}% of input budget`
  return { legacyEstimate, reportedIsCurrent, setupChanged, estimated, value, mark: info.auto_compact_percent, label, ariaValueText, report }
}

// Tokens as Claude shows them: 950, 12.3k, 1.2M.
export function shortTokens(value) {
  if (value < 1000) return String(Math.round(value))
  if (value < 1_000_000) return `${(value / 1000).toFixed(value < 99_950 ? 1 : 0)}k`
  return `${(value / 1_000_000).toFixed(value % 1_000_000 ? 1 : 0)}M`
}

const shareOf = (part, whole) => {
  const percent = part / whole * 100
  return `${percent > 0 && percent < 0.1 ? '<0.1' : percent.toFixed(1)}%`
}

// Where a chat's context window goes, like Claude's context panel: what the request holds (the app's breakdown,
// scaled to the count Databricks reported when that report is current), then the free space up to the auto-compact
// threshold, the buffer between it and the input budget, and what is kept for the reply and safety. The parts add up
// to the context budget. Returns null without a breakdown (an older or byte-counted meter).
export function contextUsage(info, reading) {
  const breakdown = info.breakdown
  if (!breakdown || reading.legacyEstimate || !(info.context_window > 0)) return null
  const reported = reading.reportedIsCurrent ? reading.report.input_tokens : null
  const used = reported ?? (reading.setupChanged ? info.next_estimate.tokens : info.estimated_tokens)
  const estimated = Object.values(breakdown).reduce((sum, value) => sum + (value > 0 ? value : 0), 0)
  const factor = estimated > 0 ? used / estimated : 0
  // Instruction files, memory, custom instructions and skills are part of the system message (a skill the latest
  // message selected counts in messages). Their costs are text-size estimates: scaled like the breakdown.
  const scale = info.estimate_scale > 0 ? info.estimate_scale : 1
  const loaded = (info.instruction_sources ?? []).filter(source => source.status !== 'omitted' && source.scope !== 'this message')
  const instructions = Math.min(breakdown.system_instructions, Math.round(loaded.reduce((sum, source) => sum + (source.estimated_tokens || 0), 0) * scale))
  const groups = Object.entries(info.tool_groups ?? {})
  const builtIn = groups.length ? groups.filter(([name]) => name === 'Built-in tools').reduce((sum, [, tokens]) => sum + tokens, 0) : breakdown.tool_definitions
  const parts = [
    { key: 'messages', label: 'Messages and tool results', tokens: breakdown.messages_and_results },
    { key: 'summary', label: 'Conversation summary', tokens: breakdown.summary },
    { key: 'system', label: 'System instructions', tokens: breakdown.system_instructions - instructions },
    { key: 'instructions', label: 'Instruction files and memory', tokens: instructions, items: loaded.length },
    { key: 'tools', label: groups.length > 1 ? 'Built-in tools' : 'Tool definitions', tokens: builtIn },
    { key: 'mcp', label: 'MCP tools', tokens: breakdown.tool_definitions - builtIn, items: Math.max(0, groups.length - 1) },
    { key: 'overhead', label: 'Request overhead', tokens: breakdown.request_overhead },
  ].map(part => ({ ...part, tokens: Math.max(0, Math.round(part.tokens * factor)) })).filter(part => part.tokens > 0)
  const window = info.context_window
  const threshold = Math.min(info.auto_compact_threshold ?? info.input_budget, info.input_budget)
  const room = [
    { key: 'free', label: 'Free space', tokens: Math.max(0, threshold - used) },
    { key: 'buffer', label: 'Autocompact buffer', tokens: Math.max(0, info.input_budget - Math.max(used, threshold)) },
    { key: 'reserved', label: 'Reserved for the reply', tokens: Math.max(0, window - Math.max(used, info.input_budget)) },
  ]
  return { used, window, reported: reported !== null, parts, room, percent: used / window * 100,
    budgetPercent: reading.reportedIsCurrent ? reading.report.percent_of_input_budget : reading.estimated,
    mark: typeof info.auto_compact_threshold === 'number' ? info.auto_compact_threshold / window * 100 : null,
    modelWindow: reading.reportedIsCurrent ? reading.report.model_window : null }
}

// The usage card at the top of the context details (and, without the bar, in Workspace → Progress).
export function ContextUsage({ usage, autoCompact, bar = true }) {
  if (!usage) return null
  const { used, window, parts, room, percent, mark } = usage
  const rows = [...parts, ...room.filter(part => part.key === 'free' || part.tokens > 0)]
  const described = parts.map(part => `${part.label} ${shortTokens(part.tokens)}`).join(', ')
  return html`<section class="context-usage" aria-label="Context usage">
    <div class="context-usage-head">
      <strong>${percent >= 10 ? Math.round(percent) : percent.toFixed(1)}% used</strong>
      <span>${usage.reported ? '' : '~'}${shortTokens(used)} / ${shortTokens(window)}</span>
    </div>
    ${bar ? html`<div class="context-stack" role="img" aria-label=${`${shortTokens(used)} of the ${shortTokens(window)}-token context budget used: ${described}`}>
      ${parts.map(part => html`<span key=${part.key} class=${`context-part context-${part.key}`} style=${`width:${Math.min(100, part.tokens / window * 100)}%`} />`)}
      ${typeof mark === 'number' && mark < 100 ? html`<span class="context-stack-mark" aria-hidden="true" style=${`left:${mark}%`} />` : null}
    </div>` : null}
    <ul class="context-legend">${rows.map(part => html`<li key=${part.key}>
      <span class=${`context-swatch context-${part.key}`} aria-hidden="true" />
      <span class="context-legend-name">${part.label}${part.items ? html` <span class="context-legend-count">· ${part.items}</span>` : null}</span>
      <span class="context-legend-tokens">${shortTokens(part.tokens)}</span>
      <span class="context-legend-share">${shareOf(part.tokens, window)}</span>
    </li>`)}</ul>
    <p class="context-usage-note">${usage.reported ? 'Reported by Databricks for the last request' : 'Estimated by the app'}${typeof usage.budgetPercent === 'number'
      ? ` · ${usage.budgetPercent}% of the input budget` : ''}${autoCompact ? ` · compacts at ${autoCompact}%` : ''}${usage.reported ? '; parts are the app\'s estimate' : ''}.${typeof usage.modelWindow === 'number' && usage.modelWindow > window
      ? ` The model's window is ${shortTokens(usage.modelWindow)}; this chat uses a ${shortTokens(window)} context budget (Settings).` : ''}</p>
  </section>`
}

// The instruction files and skills a chat's requests load, with their costs (the details' Loaded instructions).
// note: false leaves out the line on how the costs are counted (it refers to the details' breakdown).
export function LoadedInstructions({ info, note = true }) {
  return info.instruction_sources?.length ? html`
    <ul>${info.instruction_sources.map(source => html`<li key=${source.path}>
      ${sourceScope(source)} · ${sourceCost(source)}
      ${source.reason ? html`<p>${source.reason}</p>` : null}
    </li>`)}</ul>
    ${note ? html`<p>File costs are estimates included in system instructions above; shared guidance and framing are counted separately.${info.instruction_sources.some(source => source.scope === 'this message') ? ' A skill your message selected counts in messages and tool results instead.' : ''}</p>` : null}`
    : info.instruction_files?.length ? html`<ul>${info.instruction_files.map(file => html`<li key=${file}><code>${file}</code></li>`)}</ul>` : html`<p>No project instruction files loaded.</p>`
}

// The meter's text before a chat's first turn (Workspace → Progress shows it too).
export const CONTEXT_EMPTY = 'Context usage is estimated after the first turn.'

// Whether the details' Details section is open: kept while the page is open, for every chat's meter.
let moreOpen = false

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
  if (!info) return html`<p class="context-empty">${CONTEXT_EMPTY}</p>`
  const { legacyEstimate, reportedIsCurrent, setupChanged, estimated, value, mark, label, ariaValueText, report } = meterReading(info, model)
  const safetyReserve = info.context_window - info.input_budget - info.reply_reserve
  const reportHeading = reportedIsCurrent ? 'Last request' : report && report.compactions !== info.compactions ? 'Before compaction' : 'Earlier request'
  const next = info.next_estimate
  const threshold = info.auto_compact_threshold ?? info.input_budget
  // A calibration report replaced the starting ratio the shown estimate used (a cancelled or partial
  // request, or a small one, does not).
  const priorReplaced = reportedIsCurrent && typeof report.next_estimate_scale === 'number' && report.next_estimate_scale !== info.estimate_scale
  // Compact now has earlier turns to summarize (an older saved meter has no flag: allowed, as before).
  const compactable = info.compactable !== false
  const usage = contextUsage(info, { legacyEstimate, reportedIsCurrent, setupChanged, estimated, report })
  const warnings = info.warnings.length ? html`<p class="context-heading">Warnings</p><ul class="context-warnings">${info.warnings.map((warning, index) => html`<li key=${index}>${warning}</li>`)}</ul>` : null
  const compactForm = onCompact ? html`<form class="context-compact" onSubmit=${event => {
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
      </form>` : null
  const explanation = html`
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
        ? `Automatic compaction at ${info.auto_compact_percent}% of the input budget: about ${info.auto_compact_threshold.toLocaleString()} input tokens (${COMPACT_FILES[info.auto_compact_source] ? `set by this project's ${COMPACT_FILES[info.auto_compact_source]}` : 'Settings → Auto-compact at'}).`
        : `Automatic compaction threshold: approximately ${info.input_budget.toLocaleString()} input tokens.`}</p>
      <p>Of the ${info.context_window.toLocaleString()}-token context window, ${info.reply_reserve.toLocaleString()} tokens are reserved for the response and ${safetyReserve.toLocaleString()} for safety.</p>
      <p>The response limit is separate from the context window. Increasing the context budget does not increase the response limit.</p>
      <p>Compactions: ${info.compactions}. Messages summarized: ${info.summarized_messages}.</p>
      ${Number.isInteger(info.payload_bytes) ? html`<p>Request size: ${requestSize(info.payload_bytes)} of Databricks' ${(info.payload_limit_bytes ?? 4_000_000) / 1e6} MB limit.</p>` : null}
      <p class="context-heading">Loaded instructions</p>
      <${LoadedInstructions} info=${info} />`
  return html`<details class="context-meter" ref=${details} onToggle=${event => setOpen(event.currentTarget.open)}
    onKeyDown=${event => {
      if (event.key !== 'Escape' || event.isComposing || !details.current?.open) return
      event.preventDefault()
      details.current.open = false
      details.current.querySelector('summary')?.focus()
    }}>
    <summary onClick=${() => setOpen(!details.current?.open)}>
      <span>${label}</span>
      ${!legacyEstimate ? html`<span class=${`context-bar${typeof mark === 'number' && value >= mark ? ' over' : ''}`}>${reportedIsCurrent
        ? html`<progress aria-label="Reported model input usage" aria-valuetext=${ariaValueText} value=${Math.min(value, 100)} max=${100} />`
        : html`<progress aria-label=${setupChanged ? 'Estimated next request input usage' : 'Estimated model input usage'} aria-valuetext=${ariaValueText} value=${Math.min(estimated, 100)} max=${100} />`}${typeof mark === 'number'
        ? html`<span class="context-bar-mark" aria-hidden="true" style=${`left:${mark}%`} />` : null}</span>` : null}
      ${info.compactions > 0 ? html`<span> · ${info.compactions} ${info.compactions === 1 ? 'compaction' : 'compactions'}</span>` : null}
      ${info.warnings.length > 0 ? html`<span> · ${info.warnings.length} ${info.warnings.length === 1 ? 'warning' : 'warnings'}</span>` : null}
    </summary>
    <div class="context-details">
      <${ContextUsage} usage=${usage} autoCompact=${info.auto_compact_percent} />
      ${usage ? html`${warnings}${compactForm}<details class="context-more" open=${moreOpen} onToggle=${event => { moreOpen = event.currentTarget.open }}>
        <summary>Details</summary>${explanation}</details>` : html`${explanation}${warnings}${compactForm}`}
    </div>
  </details>`
}
