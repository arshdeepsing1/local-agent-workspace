import { html, resolve, useEffect, useRef, useState } from './lib.js'
import { Folder, Laptop, Menu, PanelRight, Pencil, Terminal, Wrench, X } from './icons.js'
import { api } from './api.js'
import { useWorkspace } from './useWorkspace.js'
import { useAttention } from './attention.js'
import Sidebar, { Brand } from './components/Sidebar.js'
import Composer from './components/Composer.js'
import Conversation from './components/Conversation.js'
import AgentToolsDialog from './components/AgentToolsDialog.js'
import SettingsDialog from './components/SettingsDialog.js'
import WorkspacePanel from './components/WorkspacePanel.js'
import FolderAccessDialog from './components/FolderAccessDialog.js'
import { inside } from './components/FileRoots.js'
import PermissionRulesDialog from './components/PermissionRulesDialog.js'
import ContextMeter from './components/ContextMeter.js'
import ChangesBar, { plural } from './components/ChangesBar.js'
import StorageNotice from './components/StorageNotice.js'
import { blockerOf, OpenBlockingChat } from './components/BlockingChat.js'
import TaskChecklist from './components/TaskChecklist.js'
import ContinueButton from './components/ContinueButton.js'
import QueueBar from './components/QueueBar.js'
import SideAnswer from './components/SideAnswer.js'
import { removeDraft, useSavedDrafts } from './drafts.js'
import { RewindNotice } from './rewindNotice.js'
import WorktreeChat from './components/WorktreeChat.js'

export default function App() {
  const app = useWorkspace()
  const attention = useAttention({ sessions: app.sessions, activeId: app.activeId, ready: app.ready, onSelect: app.setActiveId })
  // Message drafts are saved in localStorage per conversation ('new' before it exists).
  const draftKey = app.activeId || 'new'
  const [draft, setDraft, forgetDraft, releaseDraft] = useSavedDrafts(app.viewKey, draftKey)
  const send = async text => {
    const restore = draftKey === 'new' ? releaseDraft('new') : null
    let id
    try { id = await app.send(text) }
    catch (error) { restore?.(); throw error }
    forgetDraft(text, draftKey, id)
  }
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [agentToolsOpen, setAgentToolsOpen] = useState(false)
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [foldersOpen, setFoldersOpen] = useState(false)
  // The add-rule dialog: false, or { done } that returns focus when it closes.
  const [rulesOpen, setRulesOpen] = useState(false)
  // A changed file chosen in the changes bar, to open in the Workspace panel's review.
  const [review, setReview] = useState(null)
  // A file a reply or tool card names, to open in the Workspace editor: { path, line?, viewKey }.
  const [openPath, setOpenPath] = useState(null)
  const active = app.session
  const workspace = active?.workspace || app.settings?.workspace || ''
  const editorKey = JSON.stringify([app.viewKey, workspace])
  const [editorDrafts, setEditorDrafts] = useState({})
  const setEditorFile = value => setEditorDrafts(current => ({ ...current, [editorKey]: resolve(value, current[editorKey] || null) }))
  const hasMessages = !!active?.events.length
  const busy = !!active && active.status !== 'idle'
  // Below 1,024 px the Workspace panel is a drawer over the chat (and full width on phones): focus moves into it,
  // Escape closes it, focus returns to the Workspace toggle, and the chat column is inert while it is open.
  const [drawerTier, setDrawerTier] = useState(() => matchMedia('(width < 1024px)').matches)
  useEffect(() => {
    const query = matchMedia('(width < 1024px)')
    const update = () => setDrawerTier(query.matches)
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])
  const drawerOpen = drawerTier && workspaceOpen && !!app.settings && (!app.activeId || !!active)
  // Focus moves in when the drawer is asked for (opened, or the window narrowed with it open), not when it only
  // comes back after a chat chosen in the sidebar has loaded.
  const drawerWanted = drawerTier && workspaceOpen
  const workspaceToggle = useRef(null)
  const drawerWasOpen = useRef(false)
  const drawerWasWanted = useRef(false)
  const drawerFocusPending = useRef(false)
  useEffect(() => {
    const was = drawerWasOpen.current
    drawerWasOpen.current = drawerOpen
    if (drawerWanted !== drawerWasWanted.current) drawerFocusPending.current = drawerWanted
    drawerWasWanted.current = drawerWanted
    if (drawerOpen && drawerFocusPending.current) {
      drawerFocusPending.current = false
      const panel = document.querySelector('.workspace-panel')
      if (panel && !panel.contains(document.activeElement)) panel.querySelector('[aria-label="Close workspace"]')?.focus()
    } else if (drawerOpen && !was) {
      // Back after that chat loaded: its message box took focus and is inert now, so focus returns to its row.
      const focused = document.activeElement
      if (!focused || focused === document.body || document.querySelector('.main-area')?.contains(focused)) document.querySelector('.sidebar [aria-current="page"]')?.focus()
    } else if (was && !workspaceOpen && (!document.activeElement || document.activeElement === document.body)) {
      workspaceToggle.current?.focus()
    }
  }, [drawerOpen, drawerWanted, workspaceOpen])
  useEffect(() => {
    if (!drawerOpen) return
    const onKeyDown = event => {
      if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing || document.querySelector('dialog[open]')) return
      // From the drawer, the sidebar beside it or its error; the chat column is inert.
      if (event.target !== document.body && !event.target.closest?.('.workspace-panel, .sidebar, .drawer-alert')) return
      event.preventDefault()
      setWorkspaceOpen(false)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [drawerOpen])
  // An error can name another chat that blocks the action: { message, session_id, title }.
  const [blocker, setBlocker] = useState(null)
  const showError = error => {
    const message = typeof error === 'string' ? error : error?.message || String(error)
    setBlocker(blockerOf(error))
    app.setError(message)
  }
  const changedFiles = app.changes?.files.length || 0
  // A missing default code folder is created by the agent's first write (a file tool or a command): check it
  // again when a change lands inside it, when a reply ends and when the Workspace panel opens, so Files keeps it
  // browsable after the change list empties.
  const missingCodingDir = app.settings?.coding_dir_status?.state === 'missing' ? app.settings.coding_dir_status.path : null
  const changedInMissing = !!missingCodingDir && (app.changes?.files || []).some(item => inside(item.path, missingCodingDir))
  useEffect(() => {
    if (missingCodingDir && (changedInMissing || !busy)) void app.refreshSettings().catch(() => {})
  }, [missingCodingDir, changedInMissing, busy, workspaceOpen])
  const openReview = path => { setReview({ path, viewKey: app.viewKey }); setWorkspaceOpen(true) }
  const openFileAt = ({ path, line }) => { setOpenPath({ path, line, viewKey: app.viewKey }); setWorkspaceOpen(true) }
  // Removing a rule saves the chat (a newer 'updated'); like a rename, your own change is not news.
  const removeRule = async (scope, rule) => {
    const id = app.activeId
    const before = app.sessions.find(item => item.id === id)?.updated
    const updated = await app.removeRule(scope, rule)
    if (scope === 'chat' && typeof updated?.updated === 'number') attention.renamed(id, before, updated.updated)
  }
  const composer = html`<${Composer} key=${app.viewKey} value=${draft} setValue=${setDraft} model=${active?.model || app.settings?.model || app.settings?.default_model || 'databricks-claude-opus-5-5'}
    workspace=${active?.workspace || app.settings?.workspace || ''} worktree=${active?.worktree} mode=${app.permissionMode} onMode=${app.setPermissionMode} onProject=${() => setSettingsOpen(true)}
    onFolders=${() => { setFoldersOpen(true); void app.refreshSettings().catch(() => {}) }} rules=${app.rules.chat} onRemoveRule=${removeRule}
    projectRules=${app.rules.project} userRules=${app.rules.user} rulesProblem=${app.rules.problem} onLoadRules=${app.loadRules}
    onAddRule=${done => setRulesOpen({ done })} onResetRules=${app.resetRules}
    models=${app.connection?.models || []} busy=${busy} modelSaving=${app.modelSaving}
    disabled=${!app.ready || (!!app.activeId && !active) || !app.settings?.configured}
    onModel=${app.changeModel}
    effort=${app.reasoningEffort} onEffort=${app.changeEffort}
    reasoningFormat=${app.settings?.reasoning_format} thinkingSummaries=${app.settings?.thinking_summaries}
    onSend=${send} onError=${showError} onAttach=${() => setWorkspaceOpen(true)}
    onShell=${app.runShell} onSideQuestion=${app.askSide} loadFiles=${app.loadMentionFiles} pendingContext=${active?.pending_context}
    loadSkills=${() => api(`/skills${app.activeId ? `?session_id=${encodeURIComponent(app.activeId)}` : ''}`)}
    onStop=${() => { if (active) void api(`/sessions/${active.id}/stop`, 'POST').catch(e => showError(e.message)) }} />`
  return html`<div class=${`app-shell ${workspaceOpen ? 'workspace-visible' : ''}`}>
    <${Sidebar} settings=${app.settings} connection=${app.connection} online=${app.online} sessions=${app.sessions} activeId=${app.activeId}
      open=${sidebarOpen} close=${() => setSidebarOpen(false)} onNew=${app.newConversation}
      onSelect=${app.setActiveId} onSettings=${() => setSettingsOpen(true)}
      attention=${attention} onRename=${async (id, title) => {
        const before = app.sessions.find(item => item.id === id)?.updated
        attention.renamed(id, before, (await app.renameSession(id, title)).updated)
      }}
      onMeta=${app.setMeta} onSearch=${app.searchConversations} onError=${showError}
      onDelete=${(id, children) => app.deleteSession(id, children).then(deleted => deleted.forEach(removeDraft)).catch(e => showError(e.message))} />
    <main class="main-area" inert=${drawerOpen}>
      <header class="topbar"><button class="icon-button mobile-only" aria-label="Open sidebar" onClick=${() => setSidebarOpen(true)}><${Menu} size=${20} /></button>
        <${Laptop} size=${18} className="topbar-device" />
        <span class="conversation-title" title=${active ? `${active.title}\nSession: ${active.id}` : undefined}>${active?.title || (app.activeId ? 'Loading conversation…' : 'New conversation')}
          ${app.activeId ? html`<small class="session-id" title=${`Session: ${app.activeId}`}> · ${app.activeId.slice(0, 8)}</small>` : null}</span>
        <button class="outline agent-tools-toggle" title="Agent tools" onClick=${() => setAgentToolsOpen(true)}><${Wrench} size=${16} /><span class="toggle-label">Agent tools</span></button>
        <button ref=${workspaceToggle} class=${`outline workspace-toggle ${workspaceOpen ? 'active' : ''}`} title="Workspace" onClick=${() => setWorkspaceOpen(v => !v)}><${PanelRight} size=${18} /><span class="toggle-label">Workspace</span>
          ${changedFiles ? html`<span class="change-count" title=${`${plural(changedFiles, 'file')} changed by the agent`}><span aria-hidden="true">${changedFiles}</span><span class="sr-only">(${plural(changedFiles, 'changed file')})</span></span>` : null}</button>
      </header>
      ${app.error && (app.online || !app.offlineError) ? html`<div class="app-error" role="alert"><span>${app.error}</span>
        <${OpenBlockingChat} blocker=${blocker?.message === app.error ? blocker : null} currentId=${app.activeId} onSelect=${app.setActiveId} /><button class="icon-button" aria-label="Dismiss error" onClick=${() => app.setError('')}><${X} size=${16} /></button></div>` : null}
      <${StorageNotice} problems=${app.storageProblems} />
      ${!app.online ? html`<div class="connection-banner" role="status">Reconnecting to your local server… Refresh if the server was restarted.</div>` : null}
      ${app.online && app.connection && !app.connection.connected ? html`<${ConnectionBanner} app=${app} onSettings=${() => setSettingsOpen(true)} onError=${showError} />` : null}
      ${hasMessages && active ? html`<${Conversation} key=${active.id} session=${active} onError=${showError} onSelectSession=${app.setActiveId} onOpenPath=${openFileAt}
          busy=${busy} onLoadEarlier=${app.loadEarlier} onPrefill=${text => { if (draft.trim()) return false; setDraft(text); return true }} />` :
        html`<div class="welcome"><div class="welcome-content">
          <div class="welcome-heading"><${Brand} /><h1>What’s up next?</h1></div>
          <div class="suggestions"><button class="outline" onClick=${() => setDraft('Explore this project. Read the relevant files and explain its structure and how to run it.')}><${Folder} size=${16} />Explore this project</button>
            <button class="outline" onClick=${() => setDraft('Help me make a change in this project: ')}><${Pencil} size=${16} />Make a change</button>
            <button class="outline" onClick=${() => setDraft('Run a command in this workspace: ')}><${Terminal} size=${16} />Run a command</button>
            <${WorktreeChat} key=${app.viewKey} sessionId=${app.activeId} onSelectSession=${app.setActiveId} onError=${showError} /></div>
        </div></div>`}
      <div class="chat-composer"><div class="composer-bars"><${RewindNotice} sessionId=${app.activeId} onSelectSession=${app.setActiveId} />
        <${ChangesBar} key=${`changes-${app.viewKey}`} changes=${app.changes} busy=${busy} onOpen=${openReview} onAction=${app.reviewChanges} onError=${showError} />
        <${TaskChecklist} key=${`tasks-${app.viewKey}`} tasks=${app.tasks} />
        <${ContinueButton} session=${active} disabled=${!app.ready || !app.online || app.modelSaving || !app.settings?.configured} onSend=${app.send} onError=${showError} />
        <${QueueBar} session=${active} onRemove=${app.removeQueued} onSend=${app.sendQueued} onError=${showError}
          onEdit=${async item => { await app.removeQueued(item.id); setDraft(current => current ? `${current}\n${item.text}` : item.text) }} />
        <${SideAnswer} side=${app.sideAnswer} onDismiss=${app.dismissSide} onError=${showError} /></div>
        ${composer}
        ${active?.status === 'awaiting_approval' ? html`<p class="composer-hint" role="status">${(name => name === 'ask_user' ? 'The agent is waiting for your answer above.' : name === 'submit_plan' ? 'A plan is waiting for your review above.' : 'An action is waiting for your approval above.')(active.events?.findLast(event => event.state === 'pending')?.name)}</p>` : null}
        ${active?.status === 'compacting' ? html`<p class="composer-hint" role="status">Compacting context…</p>` : null}
        ${active?.status === 'naming' ? html`<p class="composer-hint" role="status">Creating conversation title…</p>` : null}
        <${ContextMeter} key=${`context-${app.viewKey}`} info=${active?.context_info} model=${active?.model} busy=${busy} status=${active?.status}
          onCompact=${active && app.ready && app.online && app.settings?.configured ? async preservationNote => {
            await api(`/sessions/${encodeURIComponent(active.id)}/compact`, 'POST', { preservation_note: preservationNote })
          } : undefined} />
      </div>
    </main>
    ${drawerOpen && app.error && (app.online || !app.offlineError) ? html`<div class="drawer-alert" role="alert"><span>${app.error}</span>
      <${OpenBlockingChat} blocker=${blocker?.message === app.error ? blocker : null} currentId=${app.activeId} onSelect=${app.setActiveId} /><button class="icon-button" aria-label="Dismiss error" onClick=${() => { app.setError(''); document.querySelector('.workspace-panel [aria-label="Close workspace"]')?.focus() }}><${X} size=${16} /></button></div>` : null}
    ${workspaceOpen && app.settings && (!app.activeId || active) ? html`<div class="workspace-scrim" aria-hidden="true" onClick=${() => setWorkspaceOpen(false)} />` : null}
    ${workspaceOpen && app.settings && (!app.activeId || active) ? html`<${WorkspacePanel} key=${editorKey} sessionId=${app.activeId}
      worktree=${active?.worktree || null} allowedDirectories=${active?.allowed_directories || []} codingDir=${app.settings.coding_dir_status} pythonVenv=${app.settings.python_venv_status}
      workspace=${workspace} file=${editorDrafts[editorKey] || null} setFile=${setEditorFile} onClose=${() => setWorkspaceOpen(false)}
      changes=${app.changes} busy=${busy} review=${review?.viewKey === app.viewKey ? review : null} onReviewOpened=${() => setReview(null)} onReview=${app.reviewChanges} onSelectSession=${app.setActiveId}
      onSendComments=${text => app.send(text)}
      open=${openPath?.viewKey === app.viewKey ? openPath : null} onOpened=${() => setOpenPath(null)}
      onAttach=${path => { setDraft(current => `${current}${current ? '\n' : ''}Please read the ${path.startsWith('/') ? 'file' : 'project file'}: ${path}`); setWorkspaceOpen(false) }} />` : null}
    ${agentToolsOpen && app.settings && (!app.activeId || active) ? html`<${AgentToolsDialog} key=${editorKey} session=${active} onSaveTools=${app.setDisabledTools} workspace=${workspace} changedFiles=${active && app.changes ? changedFiles : null} onClose=${() => setAgentToolsOpen(false)} onError=${showError} onSelectSession=${id => { setAgentToolsOpen(false); app.setActiveId(id); void app.refreshSessions().catch(e => showError(e.message)) }} />` : null}
    ${settingsOpen && app.settings ? html`<${SettingsDialog} settings=${app.settings} connection=${app.connection} onSave=${app.saveSettings} onClose=${() => setSettingsOpen(false)} />` : null}
    ${foldersOpen && app.settings ? html`<${FolderAccessDialog} key=${app.viewKey} workspace=${active?.workspace || app.settings.workspace} folders=${active?.allowed_directories || []}
      codingDir=${app.settings.coding_dir_status}
      busy=${busy || (!!app.activeId && !active)} bypass=${app.permissionMode === 'bypassPermissions'} onAllow=${app.allowFolder} onRemove=${app.removeFolder} onClose=${() => setFoldersOpen(false)} />` : null}
    ${rulesOpen && app.settings ? html`<${PermissionRulesDialog} key=${app.viewKey} onAdd=${app.addRule} onClose=${() => setRulesOpen(false)} returnFocus=${rulesOpen.done} />` : null}
  </div>`
}

// Why Databricks cannot be used, with Check again (keeps focus while it checks and says what it found) and Open settings.
function ConnectionBanner({ app, onSettings, onError }) {
  const connection = app.connection
  const network = connection.error_kind === 'network'
  // A network failure, a Databricks server problem or rate limit, or an unreadable answer may pass: offer to check again.
  const passing = connection.error_kind === 'invalid_response'
    || (connection.error_kind === 'http' && (connection.http_status >= 500 || connection.http_status === 429))
  const recheckable = network || passing || (!connection.error && app.settings?.configured)
  // { connection, time } of the last Check again that did not connect; shown while that result is current.
  const [checked, setChecked] = useState(null)
  const recheck = async event => {
    if (app.connectionChecking) return
    const banner = event.currentTarget.closest('.connection-banner')
    try {
      const result = await app.checkConnection({ keep: true })
      if (!result.connected) setChecked({ connection: result, time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) })
    } catch (e) { onError(e) }
    // Check again goes away with the banner (connected) or when the new problem cannot be checked again: keep
    // keyboard focus on the banner's remaining button, or continue in the message box.
    if (document.activeElement && document.activeElement !== document.body) return
    const next = banner?.isConnected ? banner.querySelector('button') : null
    ;(next || document.querySelector('.chat-composer textarea'))?.focus()
  }
  const text = network ? html`Can't reach Databricks${app.settings?.host ? ` at ${app.settings.host}` : ''}. Check your network or VPN connection.
      <details class="banner-details"><summary>Details</summary>${connection.error}</details>`
    : connection.error || (!app.settings?.configured ? 'Configure Databricks to start a conversation.' : 'Could not check the Databricks connection.')
  const outcome = app.connectionChecking ? 'Checking…'
    : checked?.connection === connection ? `Still not connected (checked ${checked.time}).` : ''
  return html`<div class="connection-banner"><span class="banner-text">${text}
      ${recheckable ? html`<span class="banner-checked" role="status">${outcome}</span>` : null}</span>
    <div class="banner-actions">${recheckable ? html`<button aria-busy=${app.connectionChecking ? 'true' : 'false'} aria-disabled=${app.connectionChecking ? 'true' : 'false'} onClick=${recheck}>Check again</button>` : null}
      <button onClick=${onSettings}>Open settings</button></div></div>`
}
