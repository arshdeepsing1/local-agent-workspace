import { html, useEffect, useState } from '../lib.js'
import { Brain, ChevronDown } from '../icons.js'
import { api } from '../api.js'
import { EFFORT_LABELS } from '../types.js'
const FALLBACK = Object.entries(EFFORT_LABELS).map(([value, label]) => ({ value, label, sent: null }))
// The composer's previews by endpoint, format and summaries: an endpoint's levels do not change while the page is open.
const previews = new Map()

// What agent requests to this endpoint carry for each level (POST /settings/reasoning, computed by the server's
// request builder): ReasoningPreview, or null while loading or when the server could not be asked. cache: false
// asks again for every change (Settings shows the current fields, including a rejection since the last look).
export function useReasoningPreview(model, format = 'auto', summaries = true, effort = 'default', enabled = true, cache = true) {
  const key = JSON.stringify([model, format, summaries !== false, effort])
  const [result, setResult] = useState(() => (cache && previews.get(key)) || null)
  useEffect(() => {
    if (!enabled || !model) return
    if (cache && previews.has(key)) { setResult(previews.get(key)); return }
    let current = true
    const timer = setTimeout(() => {
      api('/settings/reasoning', 'POST', { model, reasoning_format: format, thinking_summaries: summaries !== false, reasoning_effort: effort })
        .then(value => { if (cache) previews.set(key, value); if (current) setResult(value) }, () => { if (current) setResult(null) })
    }, 150)
    return () => { current = false; clearTimeout(timer) }
  }, [key, enabled])
  return result && result.model === model && result.format_setting === format && result.effort === effort ? result : null
}

// The option text of a level: "Default (Medium)", "Extra high (sent as High)".
export const levelLabel = level => level.note ? `${level.label} (${level.note})` : level.label

// Props: model, effort (the chat's level), format and summaries (Settings → Reasoning), disabled,
// onChange(level) -> Promise, onError(message). Focus stays on the picker, so arrow keys can step through levels.
export default function EffortPicker(p) {
  const preview = useReasoningPreview(p.model, p.format, p.summaries, 'default', !!p.model)
  const levels = preview?.levels || FALLBACK
  const current = levels.find(level => level.value === p.effort) || levels[0]
  const unverified = preview && !preview.verified
  const title = [`Reasoning effort for this chat: ${levelLabel(current)}. It applies from the agent's next model request.`,
    preview?.format === 'none' ? 'This endpoint gets no reasoning fields (Settings → Reasoning → Request format).' : null,
    unverified && preview?.format !== 'none' ? 'Unverified for this endpoint: if Databricks rejects the fields, the app resends without them.' : null,
    'Higher levels think longer and use more output tokens.'].filter(Boolean).join(' ')
  const change = async value => {
    if (value === p.effort) return
    try { await p.onChange(value) }
    catch (e) { p.onError(e.message) }
  }
  return html`<div class=${`effort-control${unverified ? ' unverified' : ''}`}>
    <${Brain} size=${14} />
    <select aria-label="Reasoning effort" value=${current.value} disabled=${p.disabled} title=${title}
      onChange=${e => void change(e.currentTarget.value)}>
      ${levels.map(level => html`<option key=${level.value} value=${level.value}>${levelLabel(level)}</option>`)}
    </select><${ChevronDown} size=${12} />
  </div>`
}
