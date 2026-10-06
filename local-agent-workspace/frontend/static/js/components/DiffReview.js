import { html, useEffect, useMemo, useRef, useState } from '../lib.js'
import { ChevronDown, ChevronUp } from '../icons.js'
import { api } from '../api.js'
import { DiffStat, STATUS_LABELS } from './ChangesBar.js'

// The agent's changes to one file, inline: removed lines in red, added lines in
// green, Keep/Undo on each change, and a bar at the bottom right that moves between
// changes and keeps or undoes the whole file.
// Diff: { path, status, added, removed, baseline_hash, current_hash,
//   hunks: [{ index, line, added, removed }],
//   lines: [{ kind: 'context'|'removed'|'added', text, old?, new?, hunk?, newline? }] }
// Props: sessionId, path, version (changes when the file or its review changes),
//   busy (a response is running; Undo waits for it), onAction(action, files) -> Promise
export default function DiffReview({ sessionId, path, version, busy, onAction }) {
  const [diff, setDiff] = useState(null)
  const [loadError, setLoadError] = useState('')
  const [error, setError] = useState('')  // The last Keep/Undo failure; a refresh keeps it visible.
  const [active, setActive] = useState(0)
  const [working, setWorking] = useState(false)
  const [revision, setRevision] = useState(0)
  const scroller = useRef(null)
  useEffect(() => {
    let current = true
    api(`/sessions/${encodeURIComponent(sessionId)}/changes/diff?path=${encodeURIComponent(path)}`)
      .then(result => {
        if (!current) return
        setDiff(result); setLoadError('')
        setActive(index => Math.min(index, Math.max(result.hunks.length - 1, 0)))
      })
      .catch(e => { if (current) setLoadError(e.message) })
    return () => { current = false }
  }, [sessionId, path, version, revision])
  const starts = useMemo(() => new Map((diff?.hunks || []).map(hunk => [hunk.line, hunk.index])), [diff])
  const count = diff?.hunks.length || 0
  const go = index => {
    setActive(index)
    scroller.current?.querySelector(`[data-hunk="${index}"]`)?.scrollIntoView({ block: 'center' })
  }
  const run = async (action, hunk) => {
    if (!diff || working) return
    setWorking(true); setError('')
    const file = { path, current_hash: diff.current_hash }
    try { await onAction(action, [hunk === undefined ? file : { ...file, baseline_hash: diff.baseline_hash, hunk }]) }
    catch (e) { setError(e.message) }
    finally { setWorking(false); setRevision(value => value + 1) }
  }
  const undoTitle = busy ? 'Stop the response before undoing changes.' : undefined
  if (!diff) return html`<div class="diff-review">${loadError ? html`<div class="panel-error" role="alert">${loadError}</div>` : html`<p class="panel-empty">Loading changes…</p>`}</div>`
  return html`<div class="diff-review" role="region" aria-label=${`Agent changes in ${path}`}>
    <div class="diff-review-summary"><span>${STATUS_LABELS[diff.status] || diff.status}</span><${DiffStat} added=${diff.added} removed=${diff.removed} />
      <span class="diff-review-note">${count ? `${count} ${count === 1 ? 'change' : 'changes'} by the agent` : diff.status === 'created' ? 'Empty new file' : 'No line changes'}</span></div>
    ${error || loadError ? html`<div class="panel-error" role="alert">${error || loadError}</div>` : null}
    <div class="diff-review-lines" ref=${scroller} tabindex="0">
      ${diff.lines.map((line, index) => {
        const hunk = starts.get(index)
        return html`${hunk === undefined ? null : html`<div key=${`hunk-${hunk}`} class=${`hunk-actions ${hunk === active ? 'active' : ''}`} data-hunk=${hunk}>
            <span>Change ${hunk + 1} of ${count}</span>
            <button aria-label=${`Keep change ${hunk + 1}`} disabled=${working} onClick=${() => { setActive(hunk); void run('keep', hunk) }}>Keep</button>
            <button aria-label=${`Undo change ${hunk + 1}`} title=${undoTitle} disabled=${working || busy} onClick=${() => { setActive(hunk); void run('undo', hunk) }}>Undo</button>
          </div>`}
          <div key=${index} class=${`diff-line ${line.kind}`}>
            <span class="line-number">${line.old ?? ''}</span><span class="line-number">${line.new ?? ''}</span>
            <span class="line-marker" aria-hidden="true">${line.kind === 'added' ? '+' : line.kind === 'removed' ? '−' : ''}</span>
            <span class="line-text"><span class="sr-only">${line.kind === 'added' ? 'Added: ' : line.kind === 'removed' ? 'Removed: ' : ''}</span>${line.text}${line.newline === false ? html`<small class="no-newline">No newline at end of file</small>` : null}</span>
          </div>`
      })}
    </div>
    <div class="review-bar" role="toolbar" aria-label="Review this file">
      <button class="icon-button" aria-label="Previous change" disabled=${count < 2} onClick=${() => go((active - 1 + count) % count)}><${ChevronUp} size=${16} /></button>
      <span class="review-position">${count ? `${active + 1} of ${count}` : '0 of 0'}</span>
      <button class="icon-button" aria-label="Next change" disabled=${count < 2} onClick=${() => go((active + 1) % count)}><${ChevronDown} size=${16} /></button>
      <button class="primary" aria-label="Keep this file" title="Accept every change in this file" disabled=${working} onClick=${() => void run('keep')}>Keep</button>
      <button class="outline" aria-label="Undo this file" title=${undoTitle || 'Restore this file to how it was before the agent changed it'} disabled=${working || busy} onClick=${() => void run('undo')}>Undo</button>
    </div>
  </div>`
}
