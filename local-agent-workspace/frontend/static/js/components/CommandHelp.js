import { html, useEffect, useRef } from '../lib.js'
import { X } from '../icons.js'
import { BUILT_IN } from './SlashMenu.js'

// Above the message box: what /help lists, or the outcome of a command that has nothing else to show
// (/copy). notice: { help: true } | { text }. onDismiss closes it; focus returns to the message box.
export default function CommandHelp({ notice, onDismiss }) {
  const close = useRef(null)
  useEffect(() => { if (notice?.help) close.current?.focus() }, [notice])
  if (!notice) return null
  const dismiss = () => { onDismiss(); document.querySelector('.chat-composer textarea[aria-label="Message"]')?.focus() }
  if (!notice.help) {
    return html`<div class="command-notice" role="status"><span>${notice.text}</span>
      <button class="icon-button" aria-label="Dismiss" onClick=${dismiss}><${X} size=${14} /></button></div>`
  }
  return html`<section class="command-help" aria-label="Commands" onKeyDown=${event => { if (event.key === 'Escape') { event.preventDefault(); dismiss() } }}>
    <header><h2>Commands</h2><button ref=${close} class="icon-button" aria-label="Close commands" onClick=${dismiss}><${X} size=${16} /></button></header>
    <dl>${BUILT_IN.map(command => html`<div key=${command.id}><dt><code>/${command.id}${command.hint ? ` ${command.hint}` : ''}</code></dt>
      <dd>${command.description}</dd></div>`)}</dl>
    <p>These run here and are never sent to the agent. Type <code>/</code> at the start of the message box to list them with
      your custom commands (<code>.agents/commands/&lt;name&gt;.md</code>) and skills; <code>/&lt;skill-id&gt;</code> in a message
      sends that skill with it. To send text such as <code>/clear</code> to the agent as it is, turn on
      <strong>Send as plain text</strong> (the T button below the box) or press Shift+Alt+Enter.</p>
  </section>`
}
