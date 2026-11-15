import { html, useEffect, useRef } from '../lib.js'
import { X } from '../icons.js'

// The one frame every dialog uses (Settings, Agent tools, Folder access, Add a permission rule, Project settings
// files, Delete conversation, Fork from here and Rewind). A native modal <dialog> that holds:
//   header   the title (an h2), an optional description, and the Close button (aria-label closeLabel, "Close" unless
//            the dialog already had another name that people or tests rely on);
//   nav      optional: a section list. Beside the body on wide screens, a scrolling row above it on a phone;
//   body     the only part that scrolls;
//   footer   optional: the button row. The primary action is the last (right-most) button, the secondary ones come
//            before it, and a destructive action carries the `destructive` class.
// Behaviour the shell owns, so the dialogs need no copies of it:
//   - Opens with showModal(). Focus goes to initialFocus (a ref), else to the first control, which the browser picks.
//   - Tab and Shift+Tab stay inside the dialog (the page behind is inert as well).
//   - Escape calls onClose, unless busy (a save is running). The Close button is disabled while busy too, and a dialog
//     whose footer has a Cancel disables that as well while busy. Closing never happens by accident while saving.
//   - A click on the backdrop calls onClose when backdrop is true (each dialog keeps what it did before) and not busy.
//   - On close focus goes back to the control that opened the dialog. returnFocus() runs after that, for an opener
//     that is gone by then (a menu item).
// Props:
//   title, description (a vnode, usually a <p>), label or labelledBy (the dialog's accessible name), describedBy,
//   className (on the <dialog>, with settings-dialog), frameClass (on the frame), bodyClass, bodyRef, onBodyScroll,
//   closeLabel, closeTitle, closeDisabled, onClose, busy, backdrop, nav, footer, onSubmit (the frame is a <form>),
//   plain (no .dialog-content form styling: for a dialog whose body has its own), dialogRef, initialFocus, returnFocus, after (nested dialogs, rendered inside the <dialog> after the frame).
const FOCUSABLE = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[tabindex]'

function tabStops(root) {
  return [...root.querySelectorAll(FOCUSABLE)].filter(item => item.closest('dialog') === root && !item.disabled
    && item.tabIndex >= 0 && !item.closest('[inert]') && item.checkVisibility?.({ visibilityProperty: true }) !== false)
}

export default function DialogShell({ title, description, label, labelledBy, describedBy, titleId, className = '', frameClass = '', bodyClass = '',
  bodyRef, onBodyScroll, closeLabel = 'Close', closeTitle, closeDisabled = false, onClose, busy = false, backdrop = true, nav, footer,
  onSubmit, plain = false, dialogRef, initialFocus, returnFocus, after, children }) {
  const dialog = useRef(null)
  const opener = useRef(null)
  useEffect(() => {
    const node = dialog.current
    opener.current = document.activeElement
    if (dialogRef) dialogRef.current = node
    node?.showModal()
    initialFocus?.current?.focus()
    return () => {
      if (node?.open) node.close()
      const back = opener.current
      // The browser returns focus on close(); this covers one that does not (the dialog left the page first).
      if (back?.isConnected && back !== document.body && (!document.activeElement || document.activeElement === document.body || node?.contains(document.activeElement))) back.focus()
      returnFocus?.()
    }
  }, [])
  const trap = event => {
    if (event.key !== 'Tab' || event.defaultPrevented || event.target.closest?.('dialog') !== dialog.current) return
    const stops = tabStops(dialog.current)
    if (!stops.length) return
    const first = stops[0]
    const last = stops[stops.length - 1]
    if (event.shiftKey && (event.target === first || event.target === dialog.current)) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && event.target === last) { event.preventDefault(); first.focus() }
  }
  const Frame = onSubmit ? 'form' : 'div'
  return html`<dialog ref=${dialog} class=${`settings-dialog ${className}`} aria-label=${label} aria-labelledby=${labelledBy} aria-describedby=${describedBy}
    onCancel=${event => { event.preventDefault(); if (!busy) onClose() }} onKeyDown=${trap}
    onClick=${event => { if (backdrop && !busy && event.target === dialog.current) onClose() }}>
    <${Frame} class=${`dialog-frame ${plain ? '' : 'dialog-content'} ${frameClass}`} onSubmit=${onSubmit}>
      <header class="dialog-header"><div class="dialog-heading"><h2 id=${titleId}>${title}</h2>${description}</div>
        <button type="button" class="icon-button dialog-close" aria-label=${closeLabel} title=${closeTitle} disabled=${closeDisabled || busy} onClick=${onClose}><${X} size=${20} /></button></header>
      <div class="dialog-main">
        ${nav ? html`<div class="dialog-nav">${nav}</div>` : null}
        <div class=${`dialog-body ${bodyClass}`} ref=${bodyRef} onScroll=${onBodyScroll}>${children}</div>
      </div>
      ${footer ? html`<footer class="dialog-footer">${footer}</footer>` : null}
    <//>
    ${after}
  </dialog>`
}
