// A turn's result record (backend turns.py turn_result, types.js TurnResult) as one line under its last reply,
// for example "8 steps · 14 tools · 2 min 10 s · 182k in (160k cached) / 3.1k out · ~1.4 DBU". A token count no
// call reported is "—", never 0; there is no DBU part without price data. backend transcript.turn_summary_text
// writes the same line.

const plural = (count, word) => `${(Number.isInteger(count) ? count : 0).toLocaleString('en-US')} ${word}${count === 1 ? '' : 's'}`

export function tokenText(value) {
  if (!Number.isInteger(value)) return '—'
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`
  if (value >= 1e4) return `${Math.round(value / 1000)}k`
  return value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(value)
}

export function durationText(seconds) {
  const whole = Math.round(Number.isFinite(seconds) ? seconds : 0)
  if (whole < 60) return `${whole} s`
  if (whole < 3600) return whole % 60 ? `${Math.floor(whole / 60)} min ${whole % 60} s` : `${whole / 60} min`
  const minutes = Math.floor(whole % 3600 / 60)
  return minutes ? `${Math.floor(whole / 3600)} h ${minutes} min` : `${Math.floor(whole / 3600)} h`
}

const REASONS = {
  completed: 'completed', tool_error: 'completed with a failed tool', step_limit: 'reached the step limit',
  spending_limit: 'reached the spending limit', budget_limit: 'reached the turn\'s token or time limit',
  stopped: 'stopped', interrupted: 'interrupted', error: 'ended with an error', inference_error: 'ended with an error',
  hook_blocked: 'blocked by a hook', approval_expired: 'stopped at its scheduled run\'s approval limit',
}

export function turnSummary(result) {
  if (!result || typeof result !== 'object') return null
  const usage = result.usage || {}
  const uncached = usage.input_tokens
  const totalIn = Number.isInteger(uncached) ? uncached + (usage.cache_read_input_tokens || 0) + (usage.cache_creation_input_tokens || 0) : null
  const cached = usage.cache_read_input_tokens
  const parts = [plural(result.steps, 'step'), plural(result.tool_calls, 'tool'), durationText(result.duration_s),
    `${tokenText(totalIn)} in${Number.isInteger(cached) && cached ? ` (${tokenText(cached)} cached)` : ''} / ${tokenText(usage.output_tokens)} out`]
  if (typeof result.estimated_dbu === 'number') parts.push(`~${result.estimated_dbu >= 1 ? result.estimated_dbu.toFixed(1) : result.estimated_dbu.toFixed(3)} DBU`)
  const agents = result.subagents?.count
  if (Number.isInteger(agents) && agents) parts.push(plural(agents, 'subagent'))
  const files = result.changed_files || []
  const details = [
    `This turn ${REASONS[result.terminal_reason] || String(result.terminal_reason || 'ended').replace(/_/g, ' ')}.`,
    Number.isInteger(result.tokens) ? `${result.tokens.toLocaleString('en-US')} tokens processed${agents ? ', its subagents included' : ''}.` : '',
    result.unpriced_calls ? `${plural(result.unpriced_calls, 'call')} without price data not in the DBU estimate.` : '',
    files.length ? `Changed: ${files.join(', ')}${result.changed_files_total > files.length ? ` and ${result.changed_files_total - files.length} more` : ''}.` : '',
  ].filter(Boolean)
  return { text: parts.join(' · '), details: details.join(' ') }
}
