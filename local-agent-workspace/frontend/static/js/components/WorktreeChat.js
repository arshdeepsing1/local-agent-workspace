import { html, useEffect, useRef, useState } from '../lib.js'
import { GitBranch } from '../icons.js'
import { api } from '../api.js'
import { stampedBranch } from './ForkDialog.js'

// The welcome screen's 'New chat in worktree': a new Git worktree on a new branch, then a chat in it.
// Props: sessionId (the selected chat whose project is used; none before any chat exists, so the
// default workspace is used), onSelectSession(new chat id), onError(message).
export default function WorktreeChat({ sessionId, onSelectSession, onError }) {
  const [open, setOpen] = useState(false)
  const [branch, setBranch] = useState('')
  const [saving, setSaving] = useState(false)
  const input = useRef(null)
  const toggle = useRef(null)
  const opened = useRef(false)
  useEffect(() => {
    if (open) input.current?.focus()
    else if (opened.current) toggle.current?.focus()  // Back to the button after Cancel or Esc.
    opened.current = open
  }, [open])
  const scope = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  if (!open) {
    return html`<button ref=${toggle} class="outline" onClick=${() => { setBranch(stampedBranch('chat')); setOpen(true) }}><${GitBranch} size=${16} />New chat in worktree</button>`
  }
  const submit = async e => {
    e.preventDefault()
    if (saving || !branch.trim()) return
    setSaving(true)
    try {
      const record = await api(`/worktrees${scope}`, 'POST', { branch: branch.trim() })
      const created = await api(`/worktrees/${encodeURIComponent(record.id)}/session${scope}`, 'POST')
      setOpen(false)
      onSelectSession(created.id)
    } catch (error) {
      onError(error)
      setTimeout(() => input.current?.focus())  // Once enabled again: fix the name and retry.
    } finally { setSaving(false) }
  }
  const onKeyDown = e => {
    if (e.key === 'Escape' && !saving) { e.preventDefault(); setOpen(false) }
  }
  return html`<form class="worktree-chat" aria-label="New chat in worktree" onSubmit=${submit} onKeyDown=${onKeyDown}>
    <label>Branch name<input ref=${input} maxLength=${200} spellcheck=${false} value=${branch} disabled=${saving}
      onInput=${e => setBranch(e.currentTarget.value)} /></label>
    <button type="submit" class="primary" disabled=${saving || !branch.trim()}>Create</button>
    <button type="button" class="outline" disabled=${saving} onClick=${() => setOpen(false)}>Cancel</button>
  </form>`
}
