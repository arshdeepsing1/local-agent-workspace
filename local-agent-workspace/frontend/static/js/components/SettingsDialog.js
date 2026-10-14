import { html, useEffect, useRef, useState } from '../lib.js'
import { X } from '../icons.js'
import { api } from '../api.js'
import { modelLabel } from '../types.js'

const LIMIT_LEVELS = { error: 'form-error', warning: 'settings-note', note: 'field-help' }
const inRange = (value, low, high) => Number.isInteger(value) && value >= low && value <= high
const LIMIT_KEYS = ['context_window', 'max_output_tokens']

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

export default function SettingsDialog({ settings, connection, onSave, onClose }) {
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
  const [error, setError] = useState('')
  // The variable names as typed; the form holds them as a list.
  const [envText, setEnvText] = useState((settings.command_env || []).join(' '))
  const instructions = form.user_instructions || ''
  const inputBudget = form.context_window - form.max_output_tokens - 2048
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
    return () => { current = false }
  }, [])
  // Warnings for these budgets on this endpoint, from the server's limit table. They never block saving.
  useEffect(() => {
    const model = (form.model || '').trim()
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
          if (!follow) { setLimits(result); return }
          followed.current = follow.followed
          setLimits(null)  // The new budgets are checked next.
          setForm(current => ({ ...current, ...follow.next }))
        },
        () => { if (request === limitsRequest.current) setLimits(null) })
    }, 250)
    return () => clearTimeout(timer)
  }, [form.model, form.context_window, form.max_output_tokens])
  const recommended = limits?.recommended && limits.profile && (limits.recommended.context_window !== form.context_window
    || limits.recommended.max_output_tokens !== form.max_output_tokens) ? limits.recommended : null
  const field = (key, value) => setForm(current => ({ ...current, [key]: value }))
  const effective = limits?.effective
  const capped = effective?.clamped ? effective.context_window - effective.max_output_tokens - 2048 : null
  const number = key => event => field(key, Number(event.currentTarget.value))
  return html`<dialog ref=${dialog} class="settings-dialog app-settings" aria-label="Settings" aria-describedby="settings-description" onCancel=${onClose} onClick=${e => { if (e.target === dialog.current) onClose() }}>
    <div class="dialog-content"><header><div><h2>Make yourself at home.</h2><p id="settings-description">Configure your local workspace.</p></div><button class="icon-button" onClick=${onClose} aria-label="Close settings"><${X} /></button></header>
      <form onSubmit=${e => {
        e.preventDefault()
        // The server checks this too; saying it here avoids its raw validation error.
        if ((form.command_env || []).length > 32) { setError('List at most 32 environment variable names.'); return }
        setSaving(true); setError(''); void onSave(form).then(onClose).catch(e => setError(e.message)).finally(() => setSaving(false))
      }}>
        <fieldset class="settings-group"><legend>Project</legend>
        <label>Project folder<input value=${form.workspace} required onInput=${e => field('workspace', e.currentTarget.value)} placeholder="/path/to/project" /></label>
        <p class="field-help">A tool-calling agent for your Databricks models. Reads files, edits code, and runs approved commands.</p>
        <label>Default folder for new code<input value=${form.coding_dir || ''} spellcheck=${false} placeholder="~/code (optional)"
          onInput=${e => field('coding_dir', e.currentTarget.value)} /></label>
        <p class="field-help">When you ask for a new standalone script, tool or project without saying where, the agent creates it here; work on the open project stays in the project. Every conversation can use this folder without a Folder access card; edits still follow the permission mode and rules. A value saved here overrides LOCAL_AGENT_CODING_DIR in .env; left unchanged, it follows .env.</p>
        ${pathStatus(statuses.coding_dir, settings.coding_dir, form.coding_dir)}
        <label>Python virtual environment<input value=${form.python_venv || ''} spellcheck=${false} placeholder="~/.venvs/project (optional)"
          onInput=${e => field('python_venv', e.currentTarget.value)} /></label>
        <p class="field-help">The agent's commands, Verify, hooks and the Terminal run with this environment active: VIRTUAL_ENV is set and its bin/ comes first on PATH. The read-only commands Auto runs without a card are unaffected, and agent edits inside the environment always ask. A value saved here overrides LOCAL_AGENT_PYTHON_VENV in .env; left unchanged, it follows .env.</p>
        ${pathStatus(statuses.python_venv, settings.python_venv, form.python_venv)}
        </fieldset>
        <fieldset class="settings-group"><legend>Model and context</legend>
        <label>Databricks model endpoint<input list="settings-models" required value=${form.model} onInput=${e => field('model', e.currentTarget.value)} placeholder=${settings.default_model || 'databricks-claude-opus-5-5'} /></label>
        ${settings.model_migrated_from ? html`<p class="settings-note" role="status">The default model changed from ${modelLabel(settings.model_migrated_from)} to ${modelLabel(settings.model)}, the app's new default. ${modelLabel(settings.model)} costs more per token. To switch back, choose ${settings.model_migrated_from} here and save, or in the model picker of a new conversation (in an open conversation the picker changes only that conversation).</p>` : null}
        <datalist id="settings-models">${connection?.models.map(m => html`<option key=${m} value=${m}>${modelLabel(m)}</option>`)}</datalist>
        <label>Context budget (tokens)<input ref=${contextInput} type="number" required min=${16384} max=${1048576} step=${1}
          value=${form.context_window || ''} onInput=${number('context_window')} /></label>
        <p class="field-help">Set at or below your endpoint's total context limit, not your account usage quota. We reserve the output budget below plus 2,048 tokens for safety; the remainder is the estimated input budget. Applies on the next turn.</p>
        <label>Max output tokens per request<input type="number" required min=${1024} max=${131072} step=${1}
          value=${form.max_output_tokens || ''} onInput=${number('max_output_tokens')} /></label>
        <p class="field-help">The endpoint must support this value. A larger response budget leaves less context for input and can increase usage. Current estimated input budget: ${inputBudget.toLocaleString()} tokens${capped ? ` (${capped.toLocaleString()} for requests to this endpoint, which accepts less)` : ''}.</p>
        ${inputBudget < 16384 ? html`<p class="form-error" role="alert">This leaves too little room for a working conversation. Lower Max output tokens or increase the context budget; raising output does not fix a full context.</p>` : null}
        ${limits ? html`<div class="settings-limits">
          ${limits.warnings.map(warning => html`<p key=${warning.code} class=${LIMIT_LEVELS[warning.level] || 'field-help'}
            role=${warning.level === 'error' ? 'alert' : warning.level === 'warning' ? 'status' : null}>${warning.text}</p>`)}
          ${recommended ? html`<button type="button" class="outline settings-recommended"
            onClick=${() => { setForm(current => ({ ...current, ...recommended })); contextInput.current?.focus() }}>Use ${limits.profile.label} defaults (${recommended.context_window.toLocaleString('en-US')} context, ${recommended.max_output_tokens.toLocaleString('en-US')} output)</button>` : null}
          <p class="field-help">${limits.note}</p>
        </div>` : null}
        <label>Auto-compact at (% of input budget)<input type="number" required min=${50} max=${100} step=${1}
          value=${form.auto_compact_percent === undefined ? 90 : form.auto_compact_percent || ''} onInput=${number('auto_compact_percent')} /></label>
        <p class="field-help">Earlier turns are summarized automatically when the next request's estimated input passes this share of the input budget (default 90). ${compactPercentValid ? `With these settings that is about ${Math.floor(inputBudget * compactPercent / 100).toLocaleString()} tokens${capped ? ` (${Math.floor(capped * compactPercent / 100).toLocaleString()} for requests to this endpoint)` : ''}.` : 'Enter a whole number from 50 to 100.'} Lower values compact sooner and keep requests smaller; 100 waits until the input budget is full.</p>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.compaction_handoffs !== false}
          onChange=${e => field('compaction_handoffs', e.currentTarget.checked)} />Save a detailed handoff at each compaction</label>
        <p class="field-help">When earlier turns are compacted, the model writes a detailed handoff (up to 16,000 output tokens, plus up to 8,192 for thinking on models that always think, such as Claude Opus 5.5) that the app saves to handoffs/auto/ in the project folder with an exact log of every command. The model is told where to find it after compaction. Compaction then takes a few minutes longer and uses more output tokens.</p>
        </fieldset>
        <fieldset class="settings-group"><legend>Agent</legend>
        <label>Agent steps per message<input type="number" required min=${1} max=${64} step=${1}
          value=${form.max_agent_steps || ''} onInput=${number('max_agent_steps')} /></label>
        <p class="field-help">Limits model round trips for one message, not individual tool calls. One model response can request several tools. The default is 32.</p>
        <label>Approval wait (minutes)<input type="number" required min=${0} max=${1440} step=${1}
          value=${form.approval_timeout_minutes ?? 60} onInput=${number('approval_timeout_minutes')} /></label>
        <p class="field-help">How long an approval card waits for your answer (default 60). After that the action does not run and the agent is told nobody answered. 0 waits until you answer or press Stop, but a waiting card keeps the chat busy, which blocks Undo and Recovery in this project.</p>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.auto_keep_changes === true}
          onChange=${e => field('auto_keep_changes', e.currentTarget.checked)} />Keep agent changes automatically when a reply finishes</label>
        <p class="field-help">The changes bar clears when a reply completes; a reply you stop, that fails or that reaches the step limit keeps its list. Edits made with the agent's file tools can still be undone from Agent tools → Recovery; edits made by commands can no longer be undone from the app.</p>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.rate_limit_auto_continue !== false}
          onChange=${e => field('rate_limit_auto_continue', e.currentTarget.checked)} />Continue automatically after rate limits</label>
        <p class="field-help">After three quick retries, wait for Databricks's rate limit to reset (up to two minutes, at most twice per message) instead of stopping. Stop cancels the wait.</p>
        <label>Custom instructions<textarea class="settings-instructions" rows=${5} maxLength=${8000} spellcheck=${false} value=${instructions} aria-describedby="settings-instructions-count"
          onInput=${e => field('user_instructions', e.currentTarget.value)} placeholder="How you want the agent to work in every project" /></label>
        <p class="field-help"><span class="field-counter" id="settings-instructions-count">${instructions.length.toLocaleString('en-US')} / 8,000</span> Added to every conversation in every project, before project instructions (AGENTS.md, CLAUDE.md), which cannot override them. Their size is counted in the context meter.</p>
        </fieldset>
        <fieldset class="settings-group"><legend>Commands and files</legend>
        <label>File write limit (KB)<input type="number" required min=${16} max=${1000} step=${1}
          value=${form.write_limit_kb === undefined ? 80 : form.write_limit_kb || ''} onInput=${number('write_limit_kb')} /></label>
        <p class="field-help">Largest file the agent can edit or create and the editor can open (default 80). One write_file call carries at most about 500 KB, so larger files are built with edits. Larger files cost more tokens to rewrite. Recovery keeps copies of edited files, up to 256 MB in total; change review keeps one copy per changed file until you Keep or Undo.</p>
        <label>Environment variables for commands<input value=${envText} spellcheck=${false} placeholder="JAVA_HOME NVM_DIR CONDA_PREFIX"
          onInput=${e => { const text = e.currentTarget.value; setEnvText(text); field('command_env', [...new Set(text.split(/[\s,]+/).filter(Boolean))]) }} /></label>
        <p class="field-help">Commands get a minimal environment. Names listed here are passed from the environment the server was started with. Never list secrets you do not want commands to see; the Databricks token and variables that change how programs start (LD_*, BASH_ENV, NODE_OPTIONS and similar) are never passed.</p>
        </fieldset>
        <fieldset class="settings-group"><legend>Notifications</legend>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.system_notifications === true}
          onChange=${e => field('system_notifications', e.currentTarget.checked)} />Desktop notification when no browser tab is open</label>
        <p class="field-help">When no page of this app is open, the server shows a system notification when a conversation needs approval, finishes, or stops with an error (notify-send on Linux, osascript on macOS). It names only the status and the conversation title.</p>
        </fieldset>
        <fieldset class="settings-group"><legend>Credentials</legend>
        <label>Credential file<input value=${form.env_file} onInput=${e => field('env_file', e.currentTarget.value)} placeholder="/path/to/env_vars.txt" /></label>
        <p class="field-help">Read on the server. Use DBRICKS_URL and DBRICKS_TOKEN assignments. Environment variables also work.</p>
        </fieldset>
        <p class="settings-note">Project and model changes apply to new conversations. Use Folder access above the chat input to allow additional folders. Commands run according to your selected permission mode.</p>
        <footer>${error ? html`<p class="form-error" role="alert">${error}</p>` : null}<button type="button" class="outline" onClick=${onClose}>Cancel</button><button class="primary" disabled=${saving}>${saving ? 'Saving…' : 'Save settings'}</button></footer>
      </form>
    </div>
  </dialog>`
}
