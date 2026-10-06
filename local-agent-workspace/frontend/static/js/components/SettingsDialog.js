import { html, useEffect, useRef, useState } from '../lib.js'
import { X } from '../icons.js'
import { modelLabel } from '../types.js'

export default function SettingsDialog({ settings, connection, onSave, onClose }) {
  const dialog = useRef(null)
  const [form, setForm] = useState(settings)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const inputBudget = form.context_window - form.max_output_tokens - 2048
  const claudeQuotaExceeded = form.model === 'databricks-claude-opus-4-8' && form.max_output_tokens > 20000
  const claudeQuotaConsumed = form.model === 'databricks-claude-opus-4-8' && form.max_output_tokens === 20000
  useEffect(() => { dialog.current?.showModal() }, [])
  const field = (key, value) => setForm(current => ({ ...current, [key]: value }))
  const number = key => event => field(key, Number(event.currentTarget.value))
  return html`<dialog ref=${dialog} class="settings-dialog" onCancel=${onClose} onClick=${e => { if (e.target === dialog.current) onClose() }}>
    <div class="dialog-content"><header><div><h2>Make yourself at home.</h2><p>Configure your local workspace.</p></div><button class="icon-button" onClick=${onClose} aria-label="Close settings"><${X} /></button></header>
      <form onSubmit=${e => { e.preventDefault(); setSaving(true); setError(''); void onSave(form).then(onClose).catch(e => setError(e.message)).finally(() => setSaving(false)) }}>
        <label>Project folder<input value=${form.workspace} required onInput=${e => field('workspace', e.currentTarget.value)} placeholder="/path/to/project" /></label>
        <p class="field-help">A tool-calling agent for your Databricks models. Reads files, edits code, and runs approved commands.</p>
        <label>Databricks model endpoint<input list="settings-models" required value=${form.model} onInput=${e => field('model', e.currentTarget.value)} placeholder="databricks-gpt-oss-120b" /></label>
        <datalist id="settings-models">${connection?.models.map(m => html`<option key=${m} value=${m}>${modelLabel(m)}</option>`)}</datalist>
        <label>Context budget (tokens)<input type="number" required min=${16384} max=${1048576} step=${1}
          value=${form.context_window || ''} onInput=${number('context_window')} /></label>
        <p class="field-help">Set at or below your endpoint's total context limit, not your account usage quota. We reserve the output budget below plus 2,048 tokens for safety; the remainder is the estimated input budget. Applies on the next turn.</p>
        <label>Max output tokens per request<input type="number" required min=${1024} max=${131072} step=${1}
          value=${form.max_output_tokens || ''} onInput=${number('max_output_tokens')} /></label>
        <p class="field-help">The endpoint must support this value. A larger response budget leaves less context for input and can increase usage. Current estimated input budget: ${inputBudget.toLocaleString()} tokens.</p>
        ${inputBudget < 16384 ? html`<p class="form-error" role="alert">This leaves too little room for a working conversation. Lower Max output tokens or increase the context budget; raising output does not fix a full context.</p>` : null}
        ${claudeQuotaExceeded ? html`<p class="form-error" role="alert">Standard Databricks Claude Opus 4.8 pay-per-token quota reserves max_tokens against a 20,000 output-tokens-per-minute limit. Use 20,000 or less unless your workspace quota is higher.</p>` : null}
        ${claudeQuotaConsumed ? html`<p class="settings-note" role="status">A 20,000-token request reserves the full standard Claude Opus 4.8 output-per-minute quota. It is useful for a single long response, but follow-up agent and tool steps can receive 429 responses until earlier output leaves the rolling window. The 8,192 default leaves room for multi-step work.</p>` : null}
        <label>Agent steps per message<input type="number" required min=${1} max=${64} step=${1}
          value=${form.max_agent_steps || ''} onInput=${number('max_agent_steps')} /></label>
        <p class="field-help">Limits model round trips for one message, not individual tool calls. One model response can request several tools. The default is 32.</p>
        <label>Approval wait (minutes)<input type="number" required min=${0} max=${1440} step=${1}
          value=${form.approval_timeout_minutes ?? 0} onInput=${number('approval_timeout_minutes')} /></label>
        <p class="field-help">How long an approval card waits for your answer. 0 waits until you answer or press Stop. With a limit, an unanswered action does not run and the agent is told nobody answered.</p>
        <label class="settings-checkbox"><input type="checkbox" checked=${form.compaction_handoffs !== false}
          onChange=${e => field('compaction_handoffs', e.currentTarget.checked)} />Save a detailed handoff at each compaction</label>
        <p class="field-help">When earlier turns are compacted, the model writes a detailed handoff (up to 16,000 output tokens) that the app saves to handoffs/auto/ in the project folder with an exact log of every command. The model is told where to find it after compaction. Compaction then takes a few minutes longer and uses more output tokens.</p>
        <label>Credential file<input value=${form.env_file} onInput=${e => field('env_file', e.currentTarget.value)} placeholder="/path/to/env_vars.txt" /></label>
        <p class="field-help">Read on the server. Use DBRICKS_URL and DBRICKS_TOKEN assignments. Environment variables also work.</p>
        <p class="settings-note">Project and model changes apply to new conversations. Use Folder access above the chat input to allow additional folders. Commands run according to your selected permission mode.</p>
        ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
        <footer><button type="button" class="outline" onClick=${onClose}>Cancel</button><button class="primary" disabled=${saving}>${saving ? 'Saving…' : 'Save settings'}</button></footer>
      </form>
    </div>
  </dialog>`
}
