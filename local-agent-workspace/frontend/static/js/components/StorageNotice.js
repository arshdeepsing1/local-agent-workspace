import { html, useState } from '../lib.js'
import { X } from '../icons.js'

const SHOWN = 5

// problems: StorageProblem[] from /api/bootstrap, conversation files the server
// skipped because they could not be read. Dismissing hides the notice until reload.
export default function StorageNotice({ problems }) {
  const [dismissed, setDismissed] = useState(false)
  if (dismissed || !problems?.length) return null
  const count = problems.length
  const item = problem => html`<code>${problem.file}</code> — ${problem.reason}`
  return html`<div class="storage-notice" role="alert">
    ${count === 1
      ? html`<p>1 conversation file could not be read and is hidden: ${item(problems[0])}. The file was left unchanged in the state folder.</p>`
      : html`<div><p>${count} conversation files could not be read and are hidden. The files were left unchanged in the state folder.</p>
          <ul>${problems.slice(0, SHOWN).map(problem => html`<li key=${problem.file}>${item(problem)}</li>`)}
            ${count > SHOWN ? html`<li>and ${count - SHOWN} more</li>` : null}</ul></div>`}
    <button class="icon-button" aria-label="Dismiss storage notice" onClick=${() => setDismissed(true)}><${X} size=${16} /></button>
  </div>`
}
