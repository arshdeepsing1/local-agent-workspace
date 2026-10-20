import { html, useEffect, useRef, useState } from '../lib.js'
import { Check, X } from '../icons.js'
import { api } from '../api.js'
import { EFFORT_LABELS, modelLabel } from '../types.js'
import { levelLabel, useReasoningPreview } from './EffortPicker.js'
import EndpointChecksDialog, { testResultText } from './EndpointChecksDialog.js'
import ProjectTrustDialog, { LAYER_FILES, layerValue } from './ProjectTrustDialog.js'
import { TEXT_SIZES, applyTextSize, textSize } from '../textSize.js'
import { STYLES, THEMES, applyStyle, applyTheme, style as appStyle, theme } from '../theme.js'
import { groupToolCalls, setGroupToolCalls } from '../viewPrefs.js'

const LIMIT_LEVELS = { error: 'form-error', warning: 'settings-note', note: 'field-help' }
const inRange = (value, low, high) => Number.isInteger(value) && value >= low && value <= high
const LIMIT_KEYS = ['context_window', 'max_output_tokens']
// The dialog's sections, in order: [id, title]. On wide screens a list beside them jumps to each.
const SECTIONS = [['project', 'Project'], ['model', 'Model and context'], ['reasoning', 'Reasoning'], ['agent', 'Agent'],
  ['subagents', 'Subagents'], ['commands', 'Commands and files'], ['web', 'Web access'], ['appearance', 'Appearance'], ['notifications', 'Notifications'],
  ['credentials', 'Credentials']]
// Settings → Web access: web_search providers (backend web_fetch.PROVIDERS) and the credential-file entry each reads
// its API key from when Settings names none (web_fetch.DEFAULT_KEYS); SearXNG takes an address instead.
const SEARCH_PROVIDERS = [['', 'None (no web search)'], ['brave', 'Brave Search'], ['tavily', 'Tavily'], ['searxng', 'SearXNG (your own server)']]
const SEARCH_KEYS = { brave: 'BRAVE_API_KEY', tavily: 'TAVILY_API_KEY' }
const FORMATS = [['auto', 'Automatic (per endpoint)'], ['anthropic', 'Anthropic fields (thinking + output_config.effort)'],
  ['anthropic_budget', 'Anthropic thinking budget (thinking.budget_tokens)'],
  ['openai', 'OpenAI field (reasoning_effort)'], ['none', 'Don\'t send reasoning fields']]
// Settings → Prompt caching (backend config.PROMPT_CACHING): cache markers on agent requests to Claude endpoints;
// 'messages' sends only the forms Databricks documents (prompt_cache.py level "messages").
const CACHING = [['auto', 'Automatic (Claude endpoints)'], ['messages', 'Documented forms only'], ['off', 'Off']]
const OTHER_ENDPOINT = '\u0000other'
// Settings → Output style (backend output_styles.PRESETS): listed until GET /output-styles adds the style files.
const BUILT_IN_STYLES = [{ id: 'default', name: 'Default' }, { id: 'concise', name: 'Concise' }, { id: 'explanatory', name: 'Explanatory' }]
const FALLBACK_LEVELS = Object.entries(EFFORT_LABELS).map(([value, label]) => ({ value, label }))
// Settings → Subagents: each built-in type's endpoint and effort ('' is the chat's own; backend
// config.DEFAULT_SUBAGENT_DEFAULTS).
const SUBAGENT_TYPES = [['explore', 'Explore', 'Read-only investigation: finding, searching and reading files.'],
  ['general', 'General', 'The chat\'s tools, within the subagent tool ceiling.'], ['editor', 'Editor', 'Reads and edits files, without commands.']]
const SUBAGENT_DEFAULTS = { explore: { model: '', effort: 'low' }, general: { model: '', effort: '' }, editor: { model: '', effort: '' } }

// The budgets after those in following take target's values, as the server does on a model change
// (config.follow_limits): a value that would not fit with the other budget is not followed.
function followLimits(following, target, form) {
  let [context, output] = [form.context_window, form.max_output_tokens]
  if (following[1] && target.max_output_tokens < (following[0] ? target.context_window : context) - 2048) output = target.max_output_tokens
  if (following[0] && output < target.context_window - 2048) context = target.context_window
  return { context_window: context, max_output_tokens: output }
}

// The check result of a saved folder (coding_dir_status or python_venv_status); an edited value is checked on save.
function pathStatus(status, saved, current) {
  if ((current || '').trim() !== (saved || '').trim()) return html`<p class="field-help">Checked when you save.</p>`
  if (!status) return null
  if (status.state === 'invalid') return html`<p class="form-error" role="alert">${status.message}</p>`
  if (status.state === 'missing') return html`<p class="settings-note" role="status">${status.message}</p>`
  if (status.state === 'ready') return html`<p class="field-help">${status.message}${status.source === 'env' ? ' From .env.' : ''}</p>`
  return null
}

// Details that most people do not need on every visit.
function More({ topic, children }) {
  return html`<details class="field-more"><summary aria-label=${`More about ${topic}`}>More</summary><p class="field-help">${children}</p></details>`
}

// One built-in subagent type's defaults: its endpoint (Same as the chat, a listed endpoint, or a typed
// name with Other endpoint…) and its effort.
function SubagentDefaults({ label, help, value, choices, onChange }) {
  const model = value.model || ''
  const [other, setOther] = useState(() => !!model && !choices.some(choice => choice.value === model))
  const input = useRef(null)
  const focus = useRef(false)
  useEffect(() => { if (other && focus.current) { focus.current = false; input.current?.focus() } }, [other])
  return html`<div class="settings-subagent">
    <p class="settings-subagent-title"><strong>${label}</strong> <span class="field-help">${help}</span></p>
    <label>${label} model<select value=${other ? OTHER_ENDPOINT : model} onChange=${e => {
      const next = e.currentTarget.value
      if (next === OTHER_ENDPOINT) { focus.current = true; setOther(true); return }
      setOther(false)
      onChange('model', next)
    }}>
      <option value="">Same as the chat</option>
      ${choices.map(choice => html`<option key=${choice.value} value=${choice.value}>${choice.label}</option>`)}
      <option value=${OTHER_ENDPOINT}>Other endpoint…</option>
    </select></label>
    <label>${label} effort<select value=${value.effort || ''} onChange=${e => onChange('effort', e.currentTarget.value)}>
      <option value="">Same as the chat</option>
      ${FALLBACK_LEVELS.map(level => html`<option key=${level.value} value=${level.value}>${level.label}</option>`)}
    </select></label>
    ${other ? html`<label class="settings-subagent-endpoint">${label} endpoint name<input ref=${input} spellcheck=${false} value=${model}
      onInput=${e => onChange('model', e.currentTarget.value.trim())} placeholder="Empty: the chat's model" /></label>` : null}
  </div>`
}

// The endpoints the chooser lists: the app's profiles (label and name), the workspace's endpoints and the saved one.
function endpointChoices(settings, connection) {
  const profiles = Array.isArray(settings.endpoint_profiles) ? settings.endpoint_profiles : []
  const labels = new Map(profiles.map(item => [item.model, item.label]))
  const names = [...new Set([...profiles.map(item => item.model), ...(connection?.models || []), settings.model].filter(Boolean))]
  return names.map(name => ({ value: name, label: `${labels.get(name) || modelLabel(name)} · ${name}` }))
}

// Settings → Fallback endpoint's list: the endpoints the app knows, those Settings → Subagents name and the saved
// one, without the model endpoint unless the form has it (chats on it then have no fallback). The server also
// accepts an endpoint one of your chats uses, typed with Other endpoint… (AgentManager.fallback_allowed).
function fallbackChoices(settings, form, model) {
  const profiles = Array.isArray(settings.endpoint_profiles) ? settings.endpoint_profiles : []
  const labels = new Map(profiles.map(item => [item.model, item.label]))
  const subagents = Object.values(form.subagent_defaults || {}).map(item => item?.model)
  const names = [...new Set([...profiles.map(item => item.model), ...subagents, settings.fallback_model, form.fallback_model]
    .filter(name => typeof name === 'string' && name.trim()))]
  return names.filter(name => name !== model || name === form.fallback_model)
    .map(name => ({ value: name, label: `${labels.get(name) || modelLabel(name)} · ${name}` }))
}

// Settings → Project: the Settings project's settings files (backend settings_layers.py) and what you decided.
function projectFilesText(layers) {
  const names = layers.files.map(file => file.name).join(' and ')
  if (layers.status === 'trusted') return `You trust ${names}: chats in this project use the values ${layers.files.length === 1 ? 'it sets' : 'they set'}, shown under each setting here. Saving changes only your own values.`
  if (layers.status === 'ignored') return `You chose to ignore ${names}: chats in this project use your Settings.`
  return layers.changed ? `${names} changed since you decided: ${layers.files.length === 1 ? 'it is' : 'they are'} not used until you choose again.`
    : `This project has ${names}, not used until you choose Trust or Ignore.`
}

// onProjectDecided(report): a Trust or Ignore chosen from Settings → Project (the app reads Settings again).
export default function SettingsDialog({ settings, connection, onSave, onClose, onProjectDecided }) {
  const dialog = useRef(null)
  const [form, setForm] = useState(settings)
  // Folder states checked again when the dialog opens (a first write creates the default code folder).
  const [statuses, setStatuses] = useState({ coding_dir: settings.coding_dir_status, python_venv: settings.python_venv_status })
  const [limits, setLimits] = useState(null)
  const limitsRequest = useRef(0)
  // Budgets this dialog set to follow the endpoint field; they keep following until edited.
  const followed = useRef(null)
  const contextInput = useRef(null)
  const [saving, setSaving] = useState(false)
  const [scale, setScale] = useState(textSize)
  // What the page shows now (storage may be blocked, so a choice made earlier on this page is not stored).
  const [themeChoice, setThemeChoice] = useState(() => document.documentElement.dataset.theme || theme())
  const [styleChoice, setStyleChoice] = useState(() => document.documentElement.dataset.style || appStyle())
  const [grouping, setGrouping] = useState(groupToolCalls)
  const [error, setError] = useState('')
  // The variable names as typed; the form holds them as a list.
  const [envText, setEnvText] = useState((settings.command_env || []).join(' '))
  const [secretText, setSecretText] = useState((settings.secret_file_patterns || []).join(' '))
  // The limit as typed: a number input reports "2." as "" or "2", so the form keeps the text and its number.
  const [spendingText, setSpendingText] = useState(String(settings.spending_limit_dbu ?? 0))
  const [styles, setStyles] = useState(BUILT_IN_STYLES)
  const choices = endpointChoices(settings, connection)
  // Other endpoint…: a typed name. It stays open while chosen, so typing never switches the list back.
  const [otherEndpoint, setOtherEndpoint] = useState(() => !choices.some(choice => choice.value === settings.model))
  const otherInput = useRef(null)
  const otherFocus = useRef(false)
  // Fallback endpoint → Other endpoint…: a typed name, open while chosen like the endpoint field's.
  const [otherFallback, setOtherFallback] = useState(false)
  const fallbackInput = useRef(null)
  const fallbackFocus = useRef(false)
  // Test model or Check endpoint… (EndpointChecksDialog) for the endpoint in the form, and that endpoint's saved checks.
  const [checking, setChecking] = useState(null)
  const [endpointChecks, setEndpointChecks] = useState(null)
  const sections = useRef({})
  const [activeSection, setActiveSection] = useState(SECTIONS[0][0])
  // The Settings project's settings files: a value a trusted file sets is named under its field.
  const layers = settings.project_layers
  const [reviewingFiles, setReviewingFiles] = useState(false)
  const fromProject = key => {
    const source = layers?.status === 'trusted' ? layers.sources?.[key] : null
    if (!source) return null
    return html`<p class="settings-source">In this project: ${layerValue(layers.values[key])}${(layers.followed || []).includes(key)
      ? `, the default of the endpoint ${LAYER_FILES[source]} sets` : ` from ${LAYER_FILES[source]}`}.</p>`
  }
  const jumping = useRef(0)
  const instructions = form.user_instructions || ''
  const style = form.output_style || 'default'
  const styleChoices = styles.some(item => item.id === style) ? styles : [...styles, { id: style, name: `${style} (no such style file here)` }]
  const inputBudget = form.context_window - form.max_output_tokens - 2048
  const model = (form.model || '').trim()
  const effort = form.reasoning_effort || 'default'
  const preview = useReasoningPreview(model, form.reasoning_format || 'auto', form.thinking_summaries !== false, effort, !!model, false)
  const levels = preview?.levels || FALLBACK_LEVELS
  // Budgets left at the saved model's defaults follow an endpoint change, so the form shows what Save stores:
  // the new endpoint's defaults, or the saved values when the saved endpoint is chosen again.
  const following = result => {
    const saved = settings.default_limits
    if (!saved || !result.defaults) return null
    const marks = LIMIT_KEYS.map(key => form[key] === followed.current?.[key] || (form[key] === settings[key] && settings[key] === saved[key]))
    if (!marks.some(Boolean)) return null
    const target = (form.model || '').trim() === settings.model ? settings : result.defaults
    const next = followLimits(marks, target, form)
    if (!LIMIT_KEYS.some(key => next[key] !== form[key])) return null
    return { next, followed: Object.fromEntries(LIMIT_KEYS.filter((_, index) => marks[index]).map(key => [key, next[key]])) }
  }
  const compactPercent = form.auto_compact_percent ?? 90
  const compactPercentValid = Number.isInteger(compactPercent) && compactPercent >= 50 && compactPercent <= 100
  useEffect(() => { dialog.current?.showModal() }, [])
  useEffect(() => {
    let current = true
    api('/settings').then(fresh => {
      if (current) setStatuses({ coding_dir: fresh.coding_dir_status, python_venv: fresh.python_venv_status })
    }, () => {})
    // The project's and your own style files; the built-in styles stay listed if this fails.
    api('/output-styles').then(result => {
      if (current && Array.isArray(result?.styles) && result.styles.length) setStyles(result.styles)
    }, () => {})
    return () => { current = false }
  }, [])
  useEffect(() => {
    if (otherEndpoint && otherFocus.current) { otherFocus.current = false; otherInput.current?.focus() }
  }, [otherEndpoint])
  // The saved endpoint checks of the endpoint in the form (no model request): the Test model line and what the
  // check found about sending earlier thinking back. Read again when a check dialog closes.
  useEffect(() => {
    let current = true
    setEndpointChecks(null)
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(model) || checking) return
    const timer = setTimeout(() => {
      api(`/endpoints/${encodeURIComponent(model)}/checks`).then(value => { if (current) setEndpointChecks(value) }, () => {})
    }, 250)
    return () => { current = false; clearTimeout(timer) }
  }, [model, checking])
  useEffect(() => {
    if (otherFallback && fallbackFocus.current) { fallbackFocus.current = false; fallbackInput.current?.focus() }
  }, [otherFallback])
  // Warnings for these budgets on this endpoint, from the server's limit table. They never block saving.
  useEffect(() => {
    const { context_window: context, max_output_tokens: output } = form
    if (!model || !inRange(context, 16384, 1048576) || !inRange(output, 1024, 131072) || output >= context - 2048) {
      limitsRequest.current++
      setLimits(null)
      return
    }
    const request = ++limitsRequest.current
    const timer = setTimeout(() => {
      api('/settings/limits', 'POST', { model, context_window: context, max_output_tokens: output }).then(
        result => {
          if (request !== limitsRequest.current) return
          const follow = following(result)
          if (!follow) { setLimits({ ...result, query: { model, context, output } }); return }
          followed.current = follow.followed
          setLimits(null)  // The new budgets are checked next.
          setForm(current => ({ ...current, ...follow.next }))
        },
        () => { if (request === limitsRequest.current) setLimits(null) })
    }, 250)
    return () => clearTimeout(timer)
  }, [form.model, form.context_window, form.max_output_tokens])
  // The endpoint's recommended budgets stay offered; once the form holds them the button says so instead.
  const recommended = limits?.recommended && limits.profile ? limits.recommended : null
  const usingRecommended = !!recommended && recommended.context_window === form.context_window
    && recommended.max_output_tokens === form.max_output_tokens
  const recommendedText = recommended ? `${limits.profile.label} defaults (${recommended.context_window.toLocaleString('en-US')} context, ${recommended.max_output_tokens.toLocaleString('en-US')} output)` : ''
  const field = (key, value) => setForm(current => ({ ...current, [key]: value }))
  const searchProvider = form.web_search_provider || ''
  // The saved provider's state (configured, or which key is missing); an edited choice is checked on save.
  const searchStatus = searchProvider && searchProvider === (settings.web_search_provider || '') && (form.web_search_key || '') === (settings.web_search_key || '')
    ? settings.web_search_status : null
  // The check's answer for the endpoint and budgets now in the form only: after an edit an older answer
  // would name another budget (and reason) until the next check answers.
  const query = limits?.query
  const effective = query && query.model === model && query.context === form.context_window
    && query.output === form.max_output_tokens ? limits.effective : null
  // The input budget requests to this endpoint use, when it differs: its maxima (clamped) and, near its
  // window, the margin for estimate error (input_budget; computed here for a check without it).
  const requestBudget = effective ? effective.input_budget ?? effective.context_window - effective.max_output_tokens - 2048 : null
  const capped = requestBudget !== null && requestBudget !== inputBudget ? requestBudget : null
  const cappedReason = effective?.clamped ? 'which accepts less' : 'keeping 5% of its window for estimate error'
  const number = key => event => field(key, Number(event.currentTarget.value))
  // A focused field must not end under the sticky footer. For a text box the browser scrolls only its
  // caret into view, so on a phone the lower part of Custom instructions could stay hidden.
  const keepAboveFooter = event => {
    const target = event.target
    const footer = event.currentTarget.querySelector(':scope > footer')
    if (!footer || footer.contains(target) || !target.getBoundingClientRect) return
    const box = target.getBoundingClientRect()
    const top = footer.getBoundingClientRect().top
    // Before the browser's own focus scroll, which then finds the caret already in view.
    if (box.bottom > top + .5) target.scrollIntoView({ block: box.height < top - 60 ? 'nearest' : 'start' })
  }
  const jump = id => {
    const section = sections.current[id]
    if (!section) return
    jumping.current = Date.now()
    setActiveSection(id)
    section.scrollIntoView({ block: 'start' })
    section.focus({ preventScroll: true })
  }
  // The section list follows scrolling (not the scroll a jump itself causes).
  const scrolled = () => {
    const box = dialog.current
    if (!box || Date.now() - jumping.current < 300) return
    const top = box.getBoundingClientRect().top
    let current = SECTIONS[0][0]
    for (const [id] of SECTIONS) {
      const section = sections.current[id]
      if (section && section.getBoundingClientRect().top - top <= 120) current = id
    }
    if (box.scrollTop + box.clientHeight >= box.scrollHeight - 2) current = SECTIONS[SECTIONS.length - 1][0]
    setActiveSection(current)
  }
  const group = (id, children) => html`<fieldset class="settings-group" id=${`settings-${id}`} tabIndex=${-1}
    ref=${element => { sections.current[id] = element }}><legend>${SECTIONS.find(item => item[0] === id)[1]}</legend>${children}</fieldset>`
  const fallback = (form.fallback_model || '').trim()
  const fallbacks = fallbackChoices(settings, form, model)
  const fields = preview?.fields || {}
  const currentLevel = levels.find(level => level.value === effort) || levels[0]
  const subagents = { ...SUBAGENT_DEFAULTS, ...(form.subagent_defaults || {}) }
  const subagentField = (kind, key, value) => setForm(current => {
    const all = { ...SUBAGENT_DEFAULTS, ...(current.subagent_defaults || {}) }
    return { ...current, subagent_defaults: { ...all, [kind]: { ...all[kind], [key]: value } } }
  })
  return html`<dialog ref=${dialog} class="settings-dialog app-settings" aria-label="Settings" aria-describedby="settings-description" onCancel=${onClose} onScroll=${scrolled} onClick=${e => { if (e.target === dialog.current) onClose() }}>
    <div class="dialog-content"><header><div><h2>Settings</h2><p id="settings-description">Configure your local workspace.</p></div><button class="icon-button" onClick=${onClose} aria-label="Close settings"><${X} /></button></header>
      <form onfocusin=${keepAboveFooter} onSubmit=${e => {
        e.preventDefault()
        // The server checks this too; saying it here avoids its raw validation error.
        if ((form.command_env || []).length > 32) { setError('List at most 32 environment variable names.'); return }
        if ((form.secret_file_patterns || []).length > 32) { setError('List at most 32 secret file name patterns.'); return }
        setSaving(true); setError(''); void onSave(form).then(onClose).catch(e => setError(e.message)).finally(() => setSaving(false))
      }}>
        <div class="settings-layout">
        <nav class="settings-nav" aria-label="Settings sections"><ul>
          ${SECTIONS.map(([id, title]) => html`<li key=${id}><button type="button" aria-current=${activeSection === id ? 'true' : null}
            onClick=${() => jump(id)}>${title}</button></li>`)}
        </ul></nav>
        <div class="settings-sections">
        ${group('project', html`
        <label>Project folder<input value=${form.workspace} required onInput=${e => field('workspace', e.currentTarget.value)} placeholder="/path/to/project" /></label>
        <p class="field-help">New conversations work in this folder: the agent reads files, edits code and runs approved commands here.</p>
        <label>Default folder for new code<input value=${form.coding_dir || ''} spellcheck=${false} placeholder="~/code (optional)"
          onInput=${e => field('coding_dir', e.currentTarget.value)} /></label>
        <p class="field-help">When you ask for a new standalone script, tool or project without saying where, the agent creates it here; work on the open project stays in the project.</p>
        <${More} topic="Default folder for new code">Every conversation can use this folder without a Folder access card; edits still follow the permission mode and rules. A value saved here overrides LOCAL_AGENT_CODING_DIR in .env; left unchanged, it follows .env.<//>
        ${pathStatus(statuses.coding_dir, settings.coding_dir, form.coding_dir)}
        <label>Python virtual environment<input value=${form.python_venv || ''} spellcheck=${false} placeholder="~/.venvs/project (optional)"
          onInput=${e => field('python_venv', e.currentTarget.value)} /></label>
        <p class="field-help">The agent's commands, Verify, hooks and the Terminal run with this environment active: VIRTUAL_ENV is set and its bin/ comes first on PATH.</p>
        <${More} topic="Python virtual environment">The read-only commands Auto runs without a card are unaffected, and agent edits inside the environment always ask. A value saved here overrides LOCAL_AGENT_PYTHON_VENV in .env; left unchanged, it follows .env.<//>
        ${pathStatus(statuses.python_venv, settings.python_venv, form.python_venv)}
        ${layers && layers.status !== 'none' ? html`<div class="settings-project-files">
          <p class=${layers.status === 'pending' ? 'settings-note' : 'field-help'}>${projectFilesText(layers)}</p>
          <button type="button" class="outline" onClick=${() => setReviewingFiles(true)}>Review settings files…</button></div>` : null}`)}
        ${group('model', html`
        <label>Databricks model endpoint<select value=${otherEndpoint ? OTHER_ENDPOINT : form.model} onChange=${e => {
          const value = e.currentTarget.value
          if (value === OTHER_ENDPOINT) { otherFocus.current = true; setOtherEndpoint(true); return }
          setOtherEndpoint(false)
          field('model', value)
        }}>
          ${choices.map(choice => html`<option key=${choice.value} value=${choice.value}>${choice.label}</option>`)}
          <option value=${OTHER_ENDPOINT}>Other endpoint…</option>
        </select></label>
        ${otherEndpoint ? html`<label>Endpoint name<input ref=${otherInput} required spellcheck=${false} value=${form.model}
          onInput=${e => field('model', e.currentTarget.value)} placeholder=${settings.default_model || 'databricks-claude-opus-5-5'} /></label>` : null}
        ${fromProject('model')}
        <p class="field-help">Endpoints with built-in limits and the ones your Databricks workspace lists. Choose Other endpoint… to type a serving endpoint name.</p>
        <div class="settings-endpoint-checks">
          <button type="button" class="outline" disabled=${!model} onClick=${() => setChecking('test')}>Test model</button>
          <button type="button" class="outline" disabled=${!model} onClick=${() => setChecking('check')}>Check endpoint capabilities…</button>
        </div>
        ${testResultText(endpointChecks?.saved) ? html`<p class="field-help endpoint-test-line">Test model: ${testResultText(endpointChecks.saved)}</p>` : null}
        <p class="field-help">Test model sends one short request; Check endpoint capabilities… sends about ten small ones and shows what the endpoint accepts. Each shows its estimated cost first and sends nothing until you press Run.</p>
        ${settings.model_migrated_from ? html`<p class="settings-note" role="status">The default model changed from ${modelLabel(settings.model_migrated_from)} to ${modelLabel(settings.model)}, the app's new default. ${modelLabel(settings.model)} costs more per token. To switch back, choose ${settings.model_migrated_from} here and save, or in the model picker of a new conversation (in an open conversation the picker changes only that conversation).</p>` : null}
        <label>Context budget (tokens)<input ref=${contextInput} type="number" required min=${16384} max=${1048576} step=${1}
          value=${form.context_window || ''} onInput=${number('context_window')} /></label>
        ${fromProject('context_window')}
        <p class="field-help">Set at or below your endpoint's total context limit, not your account usage quota. We reserve the output budget below plus 2,048 tokens for safety; the remainder is the estimated input budget.</p>
        <label>Max output tokens per request<input type="number" required min=${1024} max=${131072} step=${1}
          value=${form.max_output_tokens || ''} onInput=${number('max_output_tokens')} /></label>
        ${fromProject('max_output_tokens')}
        <p class="field-help">Current estimated input budget: ${inputBudget.toLocaleString()} tokens${capped ? ` (${capped.toLocaleString()} for requests to this endpoint, ${cappedReason})` : ''}. The endpoint must support this value; a larger response budget leaves less room for input and can increase usage.</p>
        ${inputBudget < 16384 ? html`<p class="form-error" role="alert">This leaves too little room for a working conversation. Lower Max output tokens or increase the context budget; raising output does not fix a full context.</p>` : null}
        ${limits ? html`<div class="settings-limits">
          ${limits.warnings.map(warning => html`<p key=${warning.code} class=${LIMIT_LEVELS[warning.level] || 'field-help'}
            role=${warning.level === 'error' ? 'alert' : warning.level === 'warning' ? 'status' : null}>${warning.text}</p>`)}
          ${recommended ? html`<button type="button" class=${`outline settings-recommended${usingRecommended ? ' applied' : ''}`}
            aria-disabled=${usingRecommended ? 'true' : null} onClick=${() => { if (!usingRecommended) setForm(current => ({ ...current, ...recommended })) }}>
            ${usingRecommended ? html`<${Check} size=${14} />Using ${recommendedText}` : `Use ${recommendedText}`}</button>` : null}
          <p class="field-help">${limits.note}</p>
        </div>` : null}
        <label>Auto-compact at (% of input budget)<input type="number" required min=${50} max=${100} step=${1}
          value=${form.auto_compact_percent === undefined ? 90 : form.auto_compact_percent || ''} onInput=${number('auto_compact_percent')} /></label>
        ${fromProject('auto_compact_percent')}
        <p class="field-help">Earlier turns are summarized automatically when the next request's estimated input passes this share of the input budget (default 90). ${compactPercentValid ? `With these settings that is about ${Math.floor(inputBudget * compactPercent / 100).toLocaleString()} tokens${capped ? ` (${Math.floor(capped * compactPercent / 100).toLocaleString()} for requests to this endpoint)` : ''}.` : 'Enter a whole number from 50 to 100.'} Lower values compact sooner and keep requests smaller; 100 waits until the input budget is full.</p>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.compaction_handoffs !== false}
          onChange=${e => field('compaction_handoffs', e.currentTarget.checked)} />Save a detailed handoff at each compaction</label>
        ${fromProject('compaction_handoffs')}
        <p class="field-help">The model writes a detailed handoff (up to 16,000 output tokens, plus up to 8,192 for thinking on models that always think, such as Claude Opus 5.5) that the app saves to handoffs/auto/ in the project folder.</p>
        <${More} topic="compaction handoffs">The handoff includes an exact log of every command, and the model is told where to find it after compaction. Compaction then takes a few minutes longer and uses more output tokens.<//>
        <label>Prompt caching<select value=${form.prompt_caching || 'auto'} onChange=${e => field('prompt_caching', e.currentTarget.value)}>
          ${CACHING.map(([value, label]) => html`<option key=${value} value=${value}>${label}</option>`)}
        </select></label>
        ${fromProject('prompt_caching')}
        <p class="field-help">Marks the stable start of each agent request so Databricks can bill repeated input at the cache-read rate (about 5% of the input price). Writes cost 25% more; entries expire after 5 minutes. Documented forms only marks just your last message or the last tool call.</p>
        <label>Fallback endpoint<select value=${otherFallback ? OTHER_ENDPOINT : fallback} onChange=${e => {
          const value = e.currentTarget.value
          if (value === OTHER_ENDPOINT) { fallbackFocus.current = true; setOtherFallback(true); return }
          setOtherFallback(false)
          field('fallback_model', value)
        }}>
          <option value="">None</option>
          ${fallbacks.map(choice => html`<option key=${choice.value} value=${choice.value}>${choice.label}</option>`)}
          <option value=${OTHER_ENDPOINT}>Other endpoint…</option>
        </select></label>
        ${otherFallback ? html`<label>Fallback endpoint name<input ref=${fallbackInput} spellcheck=${false} value=${form.fallback_model || ''}
          onInput=${e => field('fallback_model', e.currentTarget.value)} placeholder="An endpoint one of your chats uses" /></label>` : null}
        ${fallback && fallback === model ? html`<p class="settings-note" role="status">This is the model endpoint: chats on it have no fallback.</p>` : null}
        <p class="field-help">When the model endpoint fails before a reply starts (no connection, HTTP 502, 503 or 504, or a rate limit) and the automatic retries do not help, that one request goes to this endpoint. The next request tries the model endpoint again.</p>
        <${More} topic="fallback endpoint">The request uses the fallback's own limits, reasoning fields and prompt caching, and is not sent when the conversation does not fit its context window (or, when the app does not know that window, its default context budget). Titles, /btw and compaction never fall back. Only endpoints the app knows or ones you already use (here, in Subagents or in a chat) are accepted, because each request carries the conversation. Request details then show Answered by with the fallback, and its usage is priced at that endpoint's rates.<//>`)}
        ${group('reasoning', html`
        <label>Default effort for new chats<select value=${effort} onChange=${e => field('reasoning_effort', e.currentTarget.value)}>
          ${levels.map(level => html`<option key=${level.value} value=${level.value}>${levelLabel(level)}</option>`)}
        </select></label>
        ${fromProject('reasoning_effort')}
        <p class="field-help">Each chat can change its own effort beside the model picker. Default sends no effort field, so the endpoint uses its own; higher levels think longer and use more output tokens.</p>
        <label>Request format<select value=${form.reasoning_format || 'auto'} onChange=${e => field('reasoning_format', e.currentTarget.value)}>
          ${FORMATS.map(([value, label]) => html`<option key=${value} value=${value}>${label}</option>`)}
        </select></label>
        <p class="field-help">Automatic sends what each endpoint is documented to take. If Databricks rejects a reasoning field, the app sends that request again once without reasoning fields and skips them for the endpoint until it restarts or you save Settings.</p>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.thinking_summaries !== false}
          onChange=${e => field('thinking_summaries', e.currentTarget.checked)} />Show thinking summaries</label>
        <p class="field-help">Asks endpoints with adaptive thinking (Claude Opus 5.5 and 4.8) for a summary of their thinking (thinking.display: summarized), shown in a Thinking block above each reply. It changes what is shown, not how much the model thinks or costs.</p>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.thinking_replay === true}
          onChange=${e => field('thinking_replay', e.currentTarget.checked)} />Send earlier thinking back (experimental)</label>
        <p class="field-help">During tool use, each request also carries the signed thinking of this turn's earlier replies, back to the endpoint that wrote it. It adds input tokens; another endpoint (a fallback or a model change) never gets it, and an endpoint that rejects it is skipped.</p>
        ${(() => {
          const row = (endpointChecks?.capabilities || []).find(item => item.id === 'replay')
          return row && row.status !== 'unknown' ? html`<p class="field-help endpoint-replay-line">Endpoint check for ${model}: ${row.result} (${row.source}).</p>`
            : html`<p class="field-help endpoint-replay-line">Databricks does not document it for this route: run Check endpoint capabilities… first to see whether ${model || 'the endpoint'} accepts it.</p>`
        })()}
        <div class="settings-preview">
          <p class="settings-preview-title">Added to agent requests to ${preview?.label || model || 'this endpoint'} at ${levelLabel(currentLevel)}</p>
          ${!preview ? html`<p class="field-help">${model ? 'Checking…' : 'Choose an endpoint.'}</p>`
            : Object.keys(fields).length ? html`<pre class="settings-preview-json"><code>${JSON.stringify(fields, null, 2)}</code></pre>`
            : html`<p class="settings-preview-empty">No reasoning fields.</p>`}
          ${(preview?.notes || []).map((note, index) => html`<p key=${index} class="field-help">${note}</p>`)}
        </div>`)}
        ${group('agent', html`
        <label>Agent steps per message<input type="number" required min=${1} max=${64} step=${1}
          value=${form.max_agent_steps || ''} onInput=${number('max_agent_steps')} /></label>
        ${fromProject('max_agent_steps')}
        <p class="field-help">Limits model round trips for one message, not individual tool calls. One model response can request several tools. The default is 32.</p>
        <label>Spending limit per conversation (DBU)<input type="number" required min=${0} max=${100000} step="any" inputmode="decimal"
          value=${spendingText} onInput=${e => { const text = e.currentTarget.value; setSpendingText(text); field('spending_limit_dbu', text.trim() === '' ? NaN : Number(text)) }} /></label>
        ${fromProject('spending_limit_dbu')}
        <p class="field-help">The agent stops before its next model request once a conversation and its subagents have used this much (estimated from list prices, as in Agent tools → Usage), and warns at 80%. 0 means no limit. A chat can set its own limit in Usage.</p>
        <${More} topic="spending limit">It is a soft cap: one response can pass it before the next request is stopped. Calls to endpoints without price data are not counted, and title and compaction requests are not stopped.<//>
        <label>Token limit per turn<input type="number" required min=${0} max=${100000000} step=${1}
          value=${form.turn_token_limit ?? 0} onInput=${number('turn_token_limit')} /></label>
        <label>Time limit per turn (minutes)<input type="number" required min=${0} max=${1440} step=${1}
          value=${form.turn_time_limit_minutes ?? 0} onInput=${number('turn_time_limit_minutes')} /></label>
        <p class="field-help">Once one message's turn has processed this many tokens (cache reads included, its subagents' too) or run this long, the agent gets one last request to summarize what is done and what remains, then the turn ends; Continue starts a new one. 0 means no limit. A chat can set its own in Usage.</p>
        <label>Approval wait (minutes)<input type="number" required min=${0} max=${1440} step=${1}
          value=${form.approval_timeout_minutes ?? 60} onInput=${number('approval_timeout_minutes')} /></label>
        <p class="field-help">How long an approval card waits for your answer (default 60); after that the action does not run and the agent is told nobody answered. 0 waits until you answer or press Stop.</p>
        <${More} topic="approval wait">A waiting card keeps the chat busy, which blocks Undo and Recovery in this project, so 0 can hold them up.<//>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.auto_keep_changes === true}
          onChange=${e => field('auto_keep_changes', e.currentTarget.checked)} />Keep agent changes automatically when a reply finishes</label>
        ${fromProject('auto_keep_changes')}
        <p class="field-help">The changes bar clears when a reply completes; a reply you stop, that fails or that reaches the step limit keeps its list.</p>
        <${More} topic="automatic Keep">Edits made with the agent's file tools can still be undone from Agent tools → Checkpoints; edits made by commands can no longer be undone from the app.<//>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.rate_limit_auto_continue !== false}
          onChange=${e => field('rate_limit_auto_continue', e.currentTarget.checked)} />Continue automatically after rate limits</label>
        <p class="field-help">After three quick retries, wait for Databricks's rate limit to reset (up to two minutes, at most twice per message) instead of stopping. Stop cancels the wait.</p>
        <label>Custom instructions<textarea class="settings-instructions" rows=${5} maxLength=${8000} spellcheck=${false} value=${instructions} aria-describedby="settings-instructions-count"
          onInput=${e => field('user_instructions', e.currentTarget.value)} placeholder="How you want the agent to work in every project" /></label>
        <p class="field-help"><span class="field-counter" id="settings-instructions-count">${instructions.length.toLocaleString('en-US')} / 8,000</span> Added to every conversation in every project, before project instructions (AGENTS.md, CLAUDE.md), which cannot override them. Their size is counted in the context meter.</p>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.user_agents_file !== false}
          onChange=${e => field('user_agents_file', e.currentTarget.checked)} />Load my instructions file ~/.agents/AGENTS.md</label>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.user_claude_file === true}
          onChange=${e => field('user_claude_file', e.currentTarget.checked)} />Also load ~/.claude/CLAUDE.md</label>
        <p class="field-help">Your own instruction files for every project, added after Custom instructions with the same precedence, up to 8,000 bytes each. The app only reads them.</p>
        <${More} topic="instruction files">A file can import another file in your home folder by naming it as @path, for example @~/notes/style.md; credential files are never read. Changing a file does not hold back a running reply. For one project, AGENTS.local.md or CLAUDE.local.md in the project hold personal notes; list them in .gitignore.<//>
        <label>Output style<select value=${style} onChange=${e => field('output_style', e.currentTarget.value)}>
          ${styleChoices.map(item => html`<option key=${item.id} value=${item.id}>${item.name}</option>`)}
        </select></label>
        ${fromProject('output_style')}
        <p class="field-help">How the agent writes its replies: Concise keeps them short, Explanatory explains the reasons behind its work. A project can add styles in .agents/output-styles/, and you in ~/.agents/output-styles/ (yours win for the same name).</p>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.project_memory !== false}
          onChange=${e => field('project_memory', e.currentTarget.checked)} />Project memory</label>
        <p class="field-help">The agent can save short notes about a project, each with your approval, and later chats in that project see them. Review, edit and delete them in Agent tools → Memory.</p>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.git_context !== false}
          onChange=${e => field('git_context', e.currentTarget.checked)} />Git status in messages</label>
        <p class="field-help">The first message of each reply carries a short Git snapshot of the project (branch, upstream, change counts and the last five commits, at most 1,200 bytes), so the agent needs no command to find them; messages sent while the agent works carry none. Credential files are left out.</p>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.schedules_enabled === true}
          onChange=${e => field('schedules_enabled', e.currentTarget.checked)} />Scheduled runs at their times</label>
        <p class="field-help">While the app is open it checks the schedules of Agent tools → Schedules every 30 seconds. A schedule that asks before each run waits for your Run; one set to run automatically starts on its own and makes model calls. Off (the default): schedules run only with Run now.</p>`)}
        ${group('subagents', html`
        <p class="field-help">The agent hands self-contained work to subagents of three built-in types. Each type can use its own endpoint and reasoning effort; Same as the chat follows the chat that starts it.</p>
        ${SUBAGENT_TYPES.map(([kind, label, help]) => html`<${SubagentDefaults} key=${kind} label=${label} help=${help}
          value=${subagents[kind] || {}} choices=${choices} onChange=${(key, value) => subagentField(kind, key, value)} />`)}
        <p class="field-help">Explore starts at Fast: reading and searching rarely needs long thinking. Default sends no effort field, so the subagent's endpoint uses its own.</p>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.background_subagent_wake === true}
          onChange=${e => field('background_subagent_wake', e.currentTarget.checked)} />Continue the chat when a background subagent finishes</label>
        <p class="field-help">The agent can start a subagent in the background and keep working. When one finishes while the chat is idle, the chat gets a notice; with this on, the app also sends it a short message so the agent reads the report and goes on. Off by default: each such turn is a paid model request.</p>
        <${More} topic="subagent defaults">A subagent on another endpoint than its chat keeps its requests within that endpoint's default budgets when the Model and context budgets are larger. Custom agents (a project's Markdown agent files) set their own model and effort; Agent tools → Subagents lists them and where their files go. A running subagent keeps the model it started with, and one whose type sets an effort keeps it when you change the chat's effort.<//>`)}
        ${group('commands', html`
        <label class="settings-checkbox"><input type="checkbox" checked=${form.slash_commands !== false}
          onChange=${e => field('slash_commands', e.currentTarget.checked)} />Run commands typed with /</label>
        <p class="field-help">/name at the start of a message runs a built-in or custom command, and /skill-id sends that skill with the message. Off: such text is sent to the agent as typed; built-in commands still run when you choose them in the / menu.</p>
        <label>File write limit (KB)<input type="number" required min=${16} max=${1000} step=${1}
          value=${form.write_limit_kb === undefined ? 80 : form.write_limit_kb || ''} onInput=${number('write_limit_kb')} /></label>
        <p class="field-help">Largest file the agent can edit or create and the editor can open (default 80). One write_file call carries at most about 500 KB, so larger files are built with edits.</p>
        <${More} topic="file write limit">Larger files cost more tokens to rewrite. Recovery keeps copies of edited files, up to 256 MB in total; change review keeps one copy per changed file until you Keep or Undo.<//>
        <label>Environment variables for commands<input value=${envText} spellcheck=${false} placeholder="JAVA_HOME NVM_DIR CONDA_PREFIX"
          onInput=${e => { const text = e.currentTarget.value; setEnvText(text); field('command_env', [...new Set(text.split(/[\s,]+/).filter(Boolean))]) }} /></label>
        <p class="field-help">Commands get a minimal environment; names listed here are passed from the environment the server was started with. Never list secrets you do not want commands to see; the Databricks token and variables that change how programs start (LD_*, BASH_ENV, NODE_OPTIONS and similar) are never passed.</p>
        <label>Secret file names for commands<input value=${secretText} spellcheck=${false} placeholder="*.tfvars prod-*.yaml"
          onInput=${e => { const text = e.currentTarget.value; setSecretText(text); field('secret_file_patterns', [...new Set(text.split(/[\s,]+/).filter(Boolean))]) }} /></label>
        <p class="field-help">A command that names a file matching one of these patterns, or a built-in credential name (.env, *.pem, id_rsa, .databrickscfg and similar), always shows an approval card that warns about it, in every mode, and read-only commands checked by the app never read it. File tools keep their fixed credential exclusions.</p>`)}
        ${group('web', html`
        <label class="settings-checkbox"><input type="checkbox" checked=${form.web_fetch !== false}
          onChange=${e => field('web_fetch', e.currentTarget.checked)} />Let the agent fetch web pages (web_fetch)</label>
        <p class="field-help">The agent reads one public page at a time, as text. Each new domain asks first: Allow once, allow the domain in this chat, or always with a rule. This computer, private networks and cloud metadata addresses are never fetched, and page text reaches the agent as untrusted data.</p>
        <label>Web search provider<select value=${searchProvider} onChange=${e => field('web_search_provider', e.currentTarget.value)}>
          ${SEARCH_PROVIDERS.map(([value, label]) => html`<option key=${value} value=${value}>${label}</option>`)}
        </select></label>
        ${searchProvider === 'searxng' ? html`<label>SearXNG address<input value=${form.web_search_url || ''} spellcheck=${false} required
          placeholder="http://127.0.0.1:8888" onInput=${e => field('web_search_url', e.currentTarget.value)} /></label>` : null}
        ${SEARCH_KEYS[searchProvider] ? html`<label>API key entry in your credential file<input value=${form.web_search_key || ''} spellcheck=${false}
          placeholder=${SEARCH_KEYS[searchProvider]} onInput=${e => field('web_search_key', e.currentTarget.value.trim())} /></label>
          <p class="field-help">The name of the entry, for example ${form.web_search_key || SEARCH_KEYS[searchProvider]}=… in your credential file. The key itself stays there: it is read for each search and never saved in settings.</p>` : null}
        ${searchStatus ? html`<p class=${searchStatus.ready ? 'field-help' : 'settings-note'} role="status">${searchStatus.ready ? `${searchStatus.provider} is ready.` : searchStatus.problem}</p>` : null}
        <p class="field-help">With a provider, the agent gets a web_search tool. Each query goes to that provider, which may charge for it and sees what is searched; check your organization's rules first. The first search in each chat shows its query for your approval.</p>`)}
        ${group('appearance', html`
        <label>Style<select value=${styleChoice} onChange=${e => setStyleChoice(applyStyle(e.currentTarget.value))}>
          ${STYLES.map(([value, label]) => html`<option key=${value} value=${value}>${label}</option>`)}
        </select></label>
        <p class="field-help">The Claude style has warm ivory, clay and rounded cards; the VS Code style has the editor's default colours, fonts and square corners. Either comes in light and dark (Theme below). In this browser only (not saved with Settings).</p>
        <label>Theme<select value=${themeChoice} onChange=${e => setThemeChoice(applyTheme(e.currentTarget.value))}>
          ${THEMES.map(([value, label]) => html`<option key=${value} value=${value}>${label}</option>`)}
        </select></label>
        <p class="field-help">Light or dark colours, or Match system to follow your computer's light or dark mode. In this browser only (not saved with Settings).</p>
        <label>Text size<select value=${String(scale)} onChange=${e => setScale(applyTextSize(Number(e.currentTarget.value)))}>
          ${TEXT_SIZES.map(size => html`<option value=${String(size)}>${size === 100 ? '100% (default)' : `${size}%`}</option>`)}
        </select></label>
        <p class="field-help">Changes the size of all text at once, in this browser only (not saved with Settings). Browser zoom (Ctrl or Cmd and +) enlarges the whole page instead.</p>
        <label class="settings-checkbox"><input type="checkbox" checked=${grouping}
          onChange=${e => { setGroupToolCalls(e.currentTarget.checked); setGrouping(e.currentTarget.checked) }} />Group tool calls in the transcript</label>
        <p class="field-help">Two or more finished read-only actions in a row (commands, file reads, searches, job checks, web lookups) fold under one line such as "Ran 3 commands"; click it to see them. Approvals, questions, running or failed actions and edits never fold. The subagents of a multi-agent run share one head that folds them (a run that ended before you opened the chat starts folded); one that waits for you, failed, was stopped or was not run stays shown. Turned off, every card shows on its own, without these lines and heads. In this browser only (not saved with Settings).</p>`)}
        ${group('notifications', html`
        <label class="settings-checkbox"><input type="checkbox" checked=${form.system_notifications === true}
          onChange=${e => field('system_notifications', e.currentTarget.checked)} />Desktop notification when no browser tab is open</label>
        <p class="field-help">When no page of this app is open, the server shows a system notification when a conversation needs approval, finishes, or stops with an error.</p>
        <${More} topic="desktop notifications">It uses notify-send on Linux and osascript on macOS, and names only the status and the conversation title.<//>`)}
        ${group('credentials', html`
        <label>Credential file<input value=${form.env_file} onInput=${e => field('env_file', e.currentTarget.value)} placeholder="/path/to/env_vars.txt" /></label>
        <p class="field-help">Read on the server. Use DBRICKS_URL and DBRICKS_TOKEN assignments. Environment variables also work.</p>
        <label>Token expires on<input type="date" value=${form.token_expires_on || ''} onInput=${e => field('token_expires_on', e.currentTarget.value)} /></label>
        <p class="field-help">Optional: the day your Databricks personal access token expires. From 7 days before, a banner and Agent tools → Schedules remind you to renew it. The app never reads the token's date from Databricks.</p>`)}
        <p class="settings-note">Project, model and effort changes apply to new conversations; each chat keeps its own model and effort. Use Folder access above the chat input to allow additional folders. Commands run according to your selected permission mode.</p>
        </div>
        </div>
        <footer>${error ? html`<p class="form-error" role="alert">${error}</p>` : null}<button type="button" class="outline" onClick=${onClose}>Cancel</button><button class="primary" disabled=${saving}>${saving ? 'Saving…' : 'Save settings'}</button></footer>
      </form>
    </div>
    ${checking ? html`<${EndpointChecksDialog} model=${model} mode=${checking} onClose=${() => setChecking(null)} />` : null}
    ${reviewingFiles && layers?.files?.length ? html`<${ProjectTrustDialog} report=${layers} onDecided=${report => onProjectDecided?.(report)}
      onClose=${() => setReviewingFiles(false)} />` : null}
  </dialog>`
}
