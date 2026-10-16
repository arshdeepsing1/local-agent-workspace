import { html, useEffect, useRef, useState } from '../lib.js'
import { api, apiFile, apiJsonText } from '../api.js'

const MAX_BYTES = 16 * 1024 * 1024

function saveFile(text, name, type) {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const link = document.createElement('a')
  link.href = url; link.download = name
  document.body.appendChild(link)
  try { link.click() } finally { link.remove(); URL.revokeObjectURL(url) }
}

// Copy text that is still loading. Safari allows a clipboard write only during
// the click, so the write starts at once and receives the text when it arrives.
async function copyLoaded(load) {
  if (window.ClipboardItem && navigator.clipboard?.write) {
    const blob = load().then(text => new Blob([text], { type: 'text/plain' }))
    await navigator.clipboard.write([new ClipboardItem({ 'text/plain': blob })])
  } else await navigator.clipboard.writeText(await load())
}

export default function ConversationTransfer({ sessionId, workspace, busy = false, onSelectSession, onError }) {
  const [destination, setDestination] = useState(workspace)
  const [file, setFile] = useState(null)
  const [includeChildren, setIncludeChildren] = useState(false)
  const [withReasoning, setWithReasoning] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  // Which actions the message belongs to: it is shown right under them.
  const [noticeFor, setNoticeFor] = useState('transfer')
  const inFlight = useRef(false)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const disabled = busy || pending

  // A transcript can be read during a response; the other actions wait for it.
  async function run(operation, needsIdle = true, area = 'transfer') {
    if ((needsIdle && busy) || inFlight.current) return
    inFlight.current = true
    setPending(true); setError(''); setNotice(''); setNoticeFor(area)
    try { await operation() } catch (e) {
      if (alive.current) { setError(e.message); onError(e.message) }
    } finally { inFlight.current = false; if (alive.current) setPending(false) }
  }

  const messages = html`${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
    ${notice ? html`<p role="status">${notice}</p>` : null}`
  const transcriptPath = () => `/sessions/${encodeURIComponent(sessionId)}/transcript.md`
  return html`<section class="agent-tools-section" aria-label="Conversation transfer">
    <h3>Markdown transcript</h3>
    <p>A readable transcript of this conversation: messages, replies, notices and errors, and each tool call's main input, with command output shortened to 600 characters. Common secret shapes and the configured Databricks token are replaced with [REDACTED]; review it before sharing.</p>
    <div class="agent-tools-actions">
      <button class="outline" disabled=${pending || !sessionId} onClick=${() => void run(async () => {
        const file = await apiFile(transcriptPath())
        if (!alive.current) return
        saveFile(file.text, file.name || `transcript-${sessionId.slice(0, 8)}.md`, 'text/markdown')
        setNotice('Markdown transcript exported.')
      }, false, 'transcript')}>Export Markdown</button>
      <button class="outline" disabled=${pending || !sessionId} onClick=${() => void run(async () => {
        await copyLoaded(async () => (await apiFile(transcriptPath())).text)
        if (alive.current) setNotice('Conversation copied as Markdown.')
      }, false, 'transcript')}>Copy conversation</button>
    </div>
    ${noticeFor === 'transcript' ? messages : null}
    <h3>Export, import, and fork</h3>
    <p>Conversation files include messages, tool output, summaries, and reported usage, which may contain private data or pasted secrets. Review before sharing. Credential/settings files, file checkpoints, task-board records, and managed jobs are not copied.</p>
    <div class="agent-tools-actions">
      <button class="outline" disabled=${disabled || !sessionId} onClick=${() => void run(async () => {
        const bundle = await api(`/sessions/${encodeURIComponent(sessionId)}/export?include_children=${includeChildren}${withReasoning ? '&with_reasoning=true' : ''}`)
        if (!alive.current) return
        saveFile(JSON.stringify(bundle), `conversation-${sessionId}.json`, 'application/json')
        setNotice('Conversation file exported.')
      })}>Export conversation</button>
      <button class="outline" disabled=${disabled || !sessionId} onClick=${() => void run(async () => {
        const created = await api(`/sessions/${encodeURIComponent(sessionId)}/fork`, 'POST')
        if (alive.current) onSelectSession(created.id)
      })}>Fork completed conversation</button>
    </div>
    <label class="agent-tools-task"><input type="checkbox" disabled=${disabled || !sessionId} checked=${includeChildren} onChange=${e => setIncludeChildren(e.currentTarget.checked)} />Include subagent conversations in export (up to 32 total)</label>
    <label class="agent-tools-task"><input type="checkbox" disabled=${disabled || !sessionId} checked=${withReasoning} onChange=${e => setWithReasoning(e.currentTarget.checked)} />Export with reasoning (the latest turn's signed thinking, kept only while Send earlier thinking back is on)</label>
    <p class="field-help">Fork copies the full completed conversation into the same workspace. It does not copy child conversations or modify the source. Stop any active response before exporting or forking.</p>
    <form class="agent-tools-form" onSubmit=${e => { e.preventDefault(); void run(async () => {
      if (!file) throw new Error('Choose a conversation JSON file.')
      if (file.size > MAX_BYTES) throw new Error('Conversation file exceeds the 16 MiB limit.')
      const text = await file.text()
      const result = await apiJsonText(`/sessions/import?workspace=${encodeURIComponent(destination.trim())}`, text)
      if (alive.current) onSelectSession(result.session.id)
    }) }}>
      <label>Conversation JSON file<input type="file" accept=".json,application/json" disabled=${disabled} onChange=${e => setFile(e.currentTarget.files?.[0] || null)} /></label>
      <label>Import destination workspace<input required disabled=${disabled} value=${destination} onInput=${e => setDestination(e.currentTarget.value)} placeholder="/absolute/path/to/project" /></label>
      <p class="field-help">Each import creates a new copy in this selected folder, never overwrites an existing conversation, and starts with manual permissions, no folder grants, and no active skills. Imported paths and tool requests are historical data; nothing is executed during import.</p>
      <button class="primary" disabled=${disabled || !file || !destination.trim()}>Import as new conversation</button>
    </form>
    ${noticeFor === 'transfer' ? messages : null}
  </section>`
}
