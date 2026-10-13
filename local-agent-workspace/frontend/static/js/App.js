import { html, resolve, useEffect, useState } from './lib.js'
import { Folder, Laptop, Menu, PanelRight, Pencil, Terminal, X } from './icons.js'
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
    onSend=${send} onError=${showError} onAttach=${() => setWorkspaceOpen(true)}
    onShell=${app.runShell} onSideQuestion=${app.askSide} loadFiles=${app.loadMentionFiles} pendingContext=${active?.pending_context}
    loadSkills=${() => api(`/skills${app.activeId ? `?session_id=${encodeURIComponent(app.activeId)}` : ''}`)}
    onStop=${() => { if (active) void api(`/sessions/${active.id}/stop`, 'POST').catch(e => showError(e.message)) }} />`
  return html`<div class=${`app-shell ${workspaceOpen ? 'workspace-visible' : ''}`}>
    <${Sidebar} settings=${app.settings} connection=${app.connection} sessions=${app.sessions} activeId=${app.activeId}
      open=${sidebarOpen} close=${() => setSidebarOpen(false)} onNew=${app.newConversation}
      onSelect=${app.setActiveId} onSettings=${() => setSettingsOpen(true)}
      attention=${attention} onRename=${async (id, title) => {
        const before = app.sessions.find(item => item.id === id)?.updated
        attention.renamed(id, before, (await app.renameSession(id, title)).updated)
      }}
      onMeta=${app.setMeta} onSearch=${app.searchConversations} onError=${showError}
      onDelete=${(id, children) => app.deleteSession(id, children).then(deleted => deleted.forEach(removeDraft)).catch(e => showError(e.message))} />
    <main class="main-area">
      <header class="topbar"><button class="icon-button mobile-only" aria-label="Open sidebar" onClick=${() => setSidebarOpen(true)}><${Menu} size=${20} /></button>
        <${Laptop} size=${18} className="topbar-device" />
        <span class="conversation-title">${active?.title || (app.activeId ? 'Loading conversation…' : 'New conversation')}
          ${app.activeId ? html`<small class="session-id" title=${`Session: ${app.activeId}`}> · ${app.activeId.slice(0, 8)}</small>` : null}</span>
        <button class="outline agent-tools-toggle" onClick=${() => setAgentToolsOpen(true)}>Agent tools</button>
        <button class=${`outline workspace-toggle ${workspaceOpen ? 'active' : ''}`} onClick=${() => setWorkspaceOpen(v => !v)}><${PanelRight} size=${18} /><span>Workspace</span>
          ${changedFiles ? html`<span class="change-count" title=${`${plural(changedFiles, 'file')} changed by the agent`}><span aria-hidden="true">${changedFiles}</span><span class="sr-only">(${plural(changedFiles, 'changed file')})</span></span>` : null}</button>
      </header>
      ${app.error ? html`<div class="app-error" role="alert"><span>${app.error}</span>
        <${OpenBlockingChat} blocker=${blocker?.message === app.error ? blocker : null} currentId=${app.activeId} onSelect=${app.setActiveId} /><button class="icon-button" aria-label="Dismiss error" onClick=${() => app.setError('')}><${X} size=${16} /></button></div>` : null}
      <${StorageNotice} problems=${app.storageProblems} />
      ${!app.online && app.activeId ? html`<div class="connection-banner">Reconnecting to your local server… Refresh if the server was restarted.</div>` : null}
      ${app.connection && !app.connection.connected ? html`<div class="connection-banner"><span>${app.connection.error || 'Configure Databricks to start a conversation.'}</span><button onClick=${() => setSettingsOpen(true)}>Open settings</button></div>` : null}
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
        <${ContextMeter} key=${`context-${app.viewKey}`} info=${active?.context_info} model=${active?.model} busy=${busy}
          onCompact=${active && app.ready && app.online && app.settings?.configured ? async preservationNote => {
            await api(`/sessions/${encodeURIComponent(active.id)}/compact`, 'POST', { preservation_note: preservationNote })
          } : undefined} />
      </div>
    </main>
    ${workspaceOpen && app.settings && (!app.activeId || active) ? html`<${WorkspacePanel} key=${editorKey} sessionId=${app.activeId}
      allowedDirectories=${active?.allowed_directories || []} codingDir=${app.settings.coding_dir_status} pythonVenv=${app.settings.python_venv_status}
      workspace=${workspace} file=${editorDrafts[editorKey] || null} setFile=${setEditorFile} onClose=${() => setWorkspaceOpen(false)}
      changes=${app.changes} busy=${busy} review=${review?.viewKey === app.viewKey ? review : null} onReviewOpened=${() => setReview(null)} onReview=${app.reviewChanges} onSelectSession=${app.setActiveId}
      onSendComments=${text => app.send(text)}
      open=${openPath?.viewKey === app.viewKey ? openPath : null} onOpened=${() => setOpenPath(null)}
      onAttach=${path => { setDraft(current => `${current}${current ? '\n' : ''}Please read the ${path.startsWith('/') ? 'file' : 'project file'}: ${path}`); setWorkspaceOpen(false) }} />` : null}
    ${agentToolsOpen && app.settings && (!app.activeId || active) ? html`<${AgentToolsDialog} key=${editorKey} session=${active} onSaveTools=${app.setDisabledTools} workspace=${workspace} onClose=${() => setAgentToolsOpen(false)} onError=${showError} onSelectSession=${id => { setAgentToolsOpen(false); app.setActiveId(id); void app.refreshSessions().catch(e => showError(e.message)) }} />` : null}
    ${settingsOpen && app.settings ? html`<${SettingsDialog} settings=${app.settings} connection=${app.connection} onSave=${app.saveSettings} onClose=${() => setSettingsOpen(false)} />` : null}
    ${foldersOpen && app.settings ? html`<${FolderAccessDialog} key=${app.viewKey} workspace=${active?.workspace || app.settings.workspace} folders=${active?.allowed_directories || []}
      codingDir=${app.settings.coding_dir_status}
      busy=${busy || (!!app.activeId && !active)} bypass=${app.permissionMode === 'bypassPermissions'} onAllow=${app.allowFolder} onRemove=${app.removeFolder} onClose=${() => setFoldersOpen(false)} />` : null}
    ${rulesOpen && app.settings ? html`<${PermissionRulesDialog} key=${app.viewKey} onAdd=${app.addRule} onClose=${() => setRulesOpen(false)} returnFocus=${rulesOpen.done} />` : null}
  </div>`
}
