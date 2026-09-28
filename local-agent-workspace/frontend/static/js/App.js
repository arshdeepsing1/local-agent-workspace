import { html, resolve, useState } from './lib.js'
import { Folder, Laptop, Menu, PanelRight, Pencil, Terminal, X } from './icons.js'
import { api } from './api.js'
import { useWorkspace } from './useWorkspace.js'
import Sidebar, { Brand } from './components/Sidebar.js'
import Composer from './components/Composer.js'
import Conversation from './components/Conversation.js'
import AgentToolsDialog from './components/AgentToolsDialog.js'
import SettingsDialog from './components/SettingsDialog.js'
import WorkspacePanel from './components/WorkspacePanel.js'
import FolderAccessDialog from './components/FolderAccessDialog.js'
import ContextMeter from './components/ContextMeter.js'

export default function App() {
  const app = useWorkspace()
  const [drafts, setDrafts] = useState({})
  const draft = drafts[app.viewKey] || ''
  const setDraft = value => setDrafts(current => ({ ...current, [app.viewKey]: resolve(value, current[app.viewKey] || '') }))
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [agentToolsOpen, setAgentToolsOpen] = useState(false)
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [foldersOpen, setFoldersOpen] = useState(false)
  const active = app.session
  const workspace = active?.workspace || app.settings?.workspace || ''
  const editorKey = JSON.stringify([app.viewKey, workspace])
  const [editorDrafts, setEditorDrafts] = useState({})
  const setEditorFile = value => setEditorDrafts(current => ({ ...current, [editorKey]: resolve(value, current[editorKey] || null) }))
  const hasMessages = !!active?.events.length
  const busy = !!active && active.status !== 'idle'
  const showError = error => app.setError(error)
  const composer = html`<${Composer} key=${app.viewKey} value=${draft} setValue=${setDraft} model=${active?.model || app.settings?.model || 'databricks-gpt-oss-120b'}
    workspace=${active?.workspace || app.settings?.workspace || ''} mode=${app.permissionMode} onMode=${app.setPermissionMode} onProject=${() => setSettingsOpen(true)}
    onFolders=${() => setFoldersOpen(true)}
    models=${app.connection?.models || []} busy=${busy} modelSaving=${app.modelSaving}
    disabled=${!app.ready || (!!app.activeId && !active) || !app.settings?.configured}
    onModel=${app.changeModel}
    onSend=${app.send} onError=${showError} onAttach=${() => setWorkspaceOpen(true)}
    onStop=${() => { if (active) void api(`/sessions/${active.id}/stop`, 'POST').catch(e => showError(e.message)) }} />`
  return html`<div class=${`app-shell ${workspaceOpen ? 'workspace-visible' : ''}`}>
    <${Sidebar} settings=${app.settings} connection=${app.connection} sessions=${app.sessions} activeId=${app.activeId}
      open=${sidebarOpen} close=${() => setSidebarOpen(false)} onNew=${app.newConversation}
      onSelect=${app.setActiveId} onSettings=${() => setSettingsOpen(true)}
      onRename=${app.renameSession}
      onDelete=${id => void app.deleteSession(id).catch(e => showError(e.message))} />
    <main class="main-area">
      <header class="topbar"><button class="icon-button mobile-only" aria-label="Open sidebar" onClick=${() => setSidebarOpen(true)}><${Menu} size=${20} /></button>
        <${Laptop} size=${18} className="topbar-device" />
        <span class="conversation-title">${active?.title || (app.activeId ? 'Loading conversation…' : 'New conversation')}
          ${app.activeId ? html`<small class="session-id" title=${`Session: ${app.activeId}`}> · ${app.activeId.slice(0, 8)}</small>` : null}</span>
        <button class="outline agent-tools-toggle" onClick=${() => setAgentToolsOpen(true)}>Agent tools</button>
        <button class=${`outline workspace-toggle ${workspaceOpen ? 'active' : ''}`} onClick=${() => setWorkspaceOpen(v => !v)}><${PanelRight} size=${18} /><span>Workspace</span></button>
      </header>
      ${app.error ? html`<div class="app-error" role="alert"><span>${app.error}</span><button class="icon-button" aria-label="Dismiss error" onClick=${() => app.setError('')}><${X} size=${16} /></button></div>` : null}
      ${!app.online && app.activeId ? html`<div class="connection-banner">Reconnecting to your local server… Refresh if the server was restarted.</div>` : null}
      ${app.connection && !app.connection.connected ? html`<div class="connection-banner"><span>${app.connection.error || 'Configure Databricks to start a conversation.'}</span><button onClick=${() => setSettingsOpen(true)}>Open settings</button></div>` : null}
      ${hasMessages && active ? html`<${Conversation} key=${active.id} session=${active} onError=${showError} onSelectSession=${app.setActiveId} />` :
        html`<div class="welcome"><div class="welcome-content"><div class="welcome-heading"><${Brand} /><h1>What’s up next?</h1></div>
          <div class="suggestions"><button class="outline" onClick=${() => setDraft('Explore this project. Read the relevant files and explain its structure and how to run it.')}><${Folder} size=${16} />Explore this project</button>
            <button class="outline" onClick=${() => setDraft('Help me make a change in this project: ')}><${Pencil} size=${16} />Make a change</button>
            <button class="outline" onClick=${() => setDraft('Run a command in this workspace: ')}><${Terminal} size=${16} />Run a command</button></div>
        </div></div>`}
      <div class="chat-composer">${composer}
        ${active?.status === 'awaiting_approval' ? html`<p class="composer-hint" role="status">An action is waiting for your approval above.</p>` : null}
        ${active?.status === 'compacting' ? html`<p class="composer-hint" role="status">Compacting context…</p>` : null}
        ${active?.status === 'naming' ? html`<p class="composer-hint" role="status">Creating conversation title…</p>` : null}
        <${ContextMeter} key=${`context-${app.viewKey}`} info=${active?.context_info} busy=${busy}
          onCompact=${active && app.ready && app.online && app.settings?.configured ? async preservationNote => {
            await api(`/sessions/${encodeURIComponent(active.id)}/compact`, 'POST', { preservation_note: preservationNote })
          } : undefined} />
      </div>
    </main>
    ${workspaceOpen && app.settings && (!app.activeId || active) ? html`<${WorkspacePanel} key=${editorKey} sessionId=${app.activeId}
      workspace=${workspace} file=${editorDrafts[editorKey] || null} setFile=${setEditorFile} onClose=${() => setWorkspaceOpen(false)}
      onAttach=${path => { setDraft(current => `${current}${current ? '\n' : ''}Please read the project file: ${path}`); setWorkspaceOpen(false) }} />` : null}
    ${agentToolsOpen && app.settings && (!app.activeId || active) ? html`<${AgentToolsDialog} key=${editorKey} session=${active} workspace=${workspace} onClose=${() => setAgentToolsOpen(false)} onError=${showError} onSelectSession=${id => { setAgentToolsOpen(false); app.setActiveId(id); void app.refreshSessions().catch(e => showError(e.message)) }} />` : null}
    ${settingsOpen && app.settings ? html`<${SettingsDialog} settings=${app.settings} connection=${app.connection} onSave=${app.saveSettings} onClose=${() => setSettingsOpen(false)} />` : null}
    ${foldersOpen && app.settings ? html`<${FolderAccessDialog} key=${app.viewKey} workspace=${active?.workspace || app.settings.workspace} folders=${active?.allowed_directories || []}
      busy=${busy || (!!app.activeId && !active)} bypass=${app.permissionMode === 'bypassPermissions'} onAllow=${app.allowFolder} onRemove=${app.removeFolder} onClose=${() => setFoldersOpen(false)} />` : null}
  </div>`
}
