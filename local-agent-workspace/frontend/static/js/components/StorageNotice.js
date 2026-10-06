import { html, useRef, useState } from '../lib.js'
import { X } from '../icons.js'

const SHOWN = 5

// problems: StorageProblem[] from /api/bootstrap, conversation files the server
// skipped because they could not be read. Dismissing hides the notice until reload
// or until the list of files changes.
export default function StorageNotice({ problems }) {
  const [dismissed, setDismissed] = useState(null)
  const notice = useRef(null)
  const signature = (problems || []).map(problem => `${problem.file}\n${problem.reason}`).join('\n')
  if (!problems?.length || dismissed === signature) return null
  const dismiss = () => {
    // Keep keyboard focus in the page: the message box, or the main region if it is disabled.
    const main = notice.current?.closest('main')
    const target = main?.querySelector('textarea[aria-label="Message"]:not(:disabled)') || main
    if (target === main && main && !main.hasAttribute('tabindex')) main.setAttribute('tabindex', '-1')
    target?.focus()
    setDismissed(signature)
  }
  const count = problems.length
  const item = problem => html`<code>${problem.file}</code> — ${problem.reason}`
  return html`<div class="storage-notice" role="alert" ref=${notice}>
    ${count === 1
      ? html`<p>1 conversation file could not be read and is hidden: ${item(problems[0])}. The file was left unchanged in the state folder.</p>`
      : html`<div><p>${count} conversation files could not be read and are hidden. The files were left unchanged in the state folder.</p>
          <ul>${problems.slice(0, SHOWN).map(problem => html`<li key=${problem.file}>${item(problem)}</li>`)}
            ${count > SHOWN ? html`<li>and ${count - SHOWN} more</li>` : null}</ul></div>`}
    <button class="icon-button" aria-label="Dismiss storage notice" onClick=${dismiss}><${X} size=${16} /></button>
  </div>`
}
