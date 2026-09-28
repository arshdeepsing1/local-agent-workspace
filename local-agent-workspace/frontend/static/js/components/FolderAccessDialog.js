import { html, useEffect, useRef, useState } from '../lib.js'
import { Check, Folder, LoaderCircle, X } from '../icons.js'
import { api } from '../api.js'

// Check result: { accessible, path, error, python_executable? }
export default function FolderAccessDialog({ workspace, folders, busy, bypass, onAllow, onRemove, onClose }) {
  const dialog = useRef(null)
  const [path, setPath] = useState('~/Downloads')
  const [result, setResult] = useState(null)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { dialog.current?.showModal() }, [])
  const check = async () => {
    setWorking(true); setError(''); setResult(null)
    try { setResult(await api('/folder-access/check', 'POST', { path })) }
    catch (e) { setError(e.message) }
    finally { setWorking(false) }
  }
  const change = async action => {
    setWorking(true); setError('')
    try { await action() }
    catch (e) { setError(e.message) }
    finally { setWorking(false) }
  }
  const granted = result && (folders.includes(result.path) || result.path === workspace)
  return html`<dialog ref=${dialog} class="settings-dialog folder-access-dialog" aria-labelledby="folder-access-title"
    onCancel=${onClose} onClick=${e => { if (e.target === dialog.current) onClose() }}>
    <div class="dialog-content">
      <header><div><h2 id="folder-access-title">Folder access</h2><p>Choose folders this conversation can work with.</p></div>
        <button class="icon-button" aria-label="Close folder access" onClick=${onClose}><${X} size=${20} /></button></header>
      <div class="folder-grants"><div class="folder-grant"><${Folder} size=${16} /><span title=${workspace}>${workspace}<small>Project folder</small></span></div>
        ${folders.map(folder => html`<div class="folder-grant" key=${folder}><${Folder} size=${16} /><span title=${folder}>${folder}<small>Allowed in this conversation</small></span>
          <button class="icon-button" aria-label=${`Remove access to ${folder}`} disabled=${working || busy || bypass} onClick=${() => void change(() => onRemove(folder))}><${X} size=${15} /></button></div>`)}
      </div>
      ${bypass ? html`<p class="settings-note">Bypass permissions allows all folders readable by the server. Select another permission mode to restrict access to the folders listed here.</p>` : null}
      <form onSubmit=${e => { e.preventDefault(); void check() }}>
        <label>Folder path<input value=${path} disabled=${working} required placeholder="~/Downloads" onInput=${e => { setPath(e.currentTarget.value); setResult(null); setError('') }} /></label>
        <p class="field-help">Checking access does not read file contents or grant access to the agent.</p>
        <div class="folder-check-actions"><button class="outline" disabled=${working || !path.trim()}>${working ? html`<${LoaderCircle} size=${15} className="spin" />` : null}Check access</button></div>
      </form>
      ${result ? html`<div class=${result.accessible ? 'folder-access-success' : 'form-error'} role="status">
        ${result.accessible ? html`<${Check} size=${16} /><span>${granted ? 'Allowed for this conversation.' : 'The Python server can read this folder.'}</span>` :
          html`<p>${result.error}</p>${result.python_executable ? html`<details><summary>Python used by this server</summary><code>${result.python_executable}</code></details>` : null}`}
      </div>` : null}
      <p class="field-help">Allowing a folder lets the agent read its files and send relevant content to your model. File edits follow your selected permission mode. Browser approval cannot change macOS privacy settings.</p>
      ${busy ? html`<p class="field-help">Stop the current response before changing folder access.</p>` : null}
      ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
      <footer><button class="outline" onClick=${onClose}>Done</button><button class="primary" disabled=${working || busy || !result?.accessible || !!granted}
        onClick=${() => { if (result?.accessible) void change(() => onAllow(result.path)) }}>${granted ? 'Allowed' : 'Allow folder'}</button></footer>
    </div>
  </dialog>`
}
