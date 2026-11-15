import { html, hiddenNote, noteLine, visibleParts, visibleText, useEffect, useState } from '../lib.js'
import { api } from '../api.js'
import DialogShell from './DialogShell.js'

// Settings a project's files may set, by their Settings label (backend config.LAYERABLE).
export const LAYER_LABELS = {
  model: 'Databricks model endpoint', reasoning_effort: 'Default reasoning effort', context_window: 'Context budget',
  max_output_tokens: 'Max output tokens per request', auto_compact_percent: 'Auto-compact at (%)',
  max_agent_steps: 'Agent steps per message', compaction_handoffs: 'Save a detailed handoff at each compaction',
  prompt_caching: 'Prompt caching', output_style: 'Output style', auto_keep_changes: 'Keep changes automatically',
  spending_limit_dbu: 'Default spending limit (DBU)',
}
export const LAYER_FILES = { project: '.agents/settings.json', local: '.agents/settings.local.json' }
export const layerValue = value => typeof value === 'boolean' ? (value ? 'on' : 'off')
  : typeof value === 'number' ? value.toLocaleString('en-US') : String(value)
const query = sessionId => sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''

// Every text from a repository's settings files that the dialog shows (the note covers them all).
const readText = report => [report.workspace, ...report.files.flatMap(file => [file.name, ...Object.entries(file.values).flatMap(([key, value]) => [LAYER_LABELS[key] ? '' : key, layerValue(value)]),
  ...file.problems, ...file.ignored])]

// Asked the first time a project with .agents/settings.json or .agents/settings.local.json is opened, and again when
// a file changed (backend settings_layers.py): what each file would set, what it cannot, and Trust or Ignore. Closing
// it decides nothing: the files stay unused until you choose. report: a ProjectLayers (types.js); sessionId: the chat
// whose project it is (none: the Settings project); onDecided(report) gets the new report.
export default function ProjectTrustDialog({ report, sessionId, onDecided, onClose }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [shown, setShown] = useState(report)
  useEffect(() => { setShown(report) }, [report?.sha256])
  const decide = async decision => {
    if (busy) return
    setBusy(true); setError('')
    try {
      const next = await api(`/settings/project/trust${query(sessionId)}`, 'POST', { decision, sha256: shown.sha256 })
      onDecided(next)
      onClose()
    } catch (e) {
      // The files changed while you read them: show what they hold now.
      if (e.status === 409 && e.detail?.report) { setShown(e.detail.report); onDecided(e.detail.report) }
      setError(e.message)
    } finally { setBusy(false) }
  }
  const names = shown.files.map(file => file.name)
  // Closing decides nothing (Decide later): the Close button has that name, and neither Escape nor a click outside
  // closes it while a decision is being saved.
  return html`<${DialogShell} className="project-trust-dialog" labelledBy="project-trust-title" describedBy="project-trust-description"
    title="Project settings files" titleId="project-trust-title" closeLabel="Decide later" closeTitle="Decide later" closeDisabled=${busy}
    description=${html`<p id="project-trust-description" class="logical-order">${shown.changed ? `${names.join(' and ')} changed since you chose ${shown.decision === 'trust' ? 'Trust' : 'Ignore'}. They are not used until you decide again.`
          : `This project has settings in ${names.join(' and ')}. They apply to its chats only if you trust them.`}</p>`}
    onClose=${onClose} busy=${busy} backdrop=${false}
    footer=${html`<button type="button" class="outline" disabled=${busy} onClick=${() => void decide('ignore')}>Ignore</button>
        <button type="button" class="primary" disabled=${busy} onClick=${() => void decide('trust')}>Trust</button>`}>
      ${noteLine(hiddenNote(readText(shown)))}
      <p class="project-trust-folder logical-order" title=${visibleText(shown.workspace)}>${visibleParts(shown.workspace)}</p>
      ${shown.files.map(file => html`<section key=${file.layer} class="project-trust-file logical-order" aria-label=${visibleText(file.name)}>
        <h3>${visibleParts(file.name)}${file.layer === 'local' ? html` <span class="field-help">(yours, not shared)</span>` : null}</h3>
        ${Object.keys(file.values).length ? html`<dl>${Object.entries(file.values).map(([key, value]) => html`<div key=${key}>
          <dt>${LAYER_LABELS[key] ?? visibleParts(key)}</dt><dd>${visibleParts(layerValue(value))}</dd></div>`)}</dl>`
          : html`<p class="field-help">It sets nothing the app uses.</p>`}
        ${file.problems.map((problem, index) => html`<p key=${index} class="settings-note">Not used: ${visibleParts(problem)}</p>`)}
        ${file.ignored.length ? html`<p class="field-help">Ignored (only Settings can set ${file.ignored.length === 1 ? 'it' : 'them'}): ${visibleParts(file.ignored.join(', '))}</p>` : null}
      </section>`)}
      <p class="field-help">A project file can set only the model (an endpoint you already use), reasoning effort, context budgets, compaction, prompt caching, output style, Agent steps, Automatic Keep and a lower spending limit. Never credentials, permissions, commands, hooks or MCP servers. Settings → Project shows the files later.</p>
      ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
  <//>`
}

// The trust prompt's data: the report of the project the page shows (a chat's, or the Settings project's), read again
// when that project or the Settings project's report changes. dismissed: reports closed with Decide later (by sha256).
export function useProjectLayers(sessionId, project, settingsReport) {
  const [report, setReport] = useState(null)
  const [dismissed, setDismissed] = useState(() => new Set())
  const key = `${sessionId || ''}\n${project || ''}\n${settingsReport?.sha256 || ''}\n${settingsReport?.status || ''}`
  useEffect(() => {
    let current = true
    if (!project) { setReport(null); return undefined }
    api(`/settings/project${query(sessionId)}`).then(value => { if (current) setReport(value) }, () => { if (current) setReport(null) })
    return () => { current = false }
  }, [key])
  const asking = report && report.status === 'pending' && !dismissed.has(`${report.workspace}\n${report.sha256}`) ? report : null
  return {
    report, asking, setReport,
    dismiss: () => { if (report) setDismissed(current => new Set([...current, `${report.workspace}\n${report.sha256}`])) },
  }
}
