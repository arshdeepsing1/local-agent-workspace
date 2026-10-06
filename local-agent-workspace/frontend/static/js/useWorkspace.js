import { useCallback, useEffect, useRef, useState } from './lib.js'
import { api, ApiError, refreshToken, setToken, streamProtocols, streamUrl } from './api.js'

// Selection: { id: string | null, key: string, creating?: Promise<Session> }
const sessionInUrl = () => new URL(window.location.href).searchParams.get('session')

export function useWorkspace() {
  const [settings, setSettings] = useState(null)
  const [connection, setConnection] = useState(null)
  const [sessions, setSessions] = useState([])
  const [session, setSession] = useState(null)
  const [selection, setSelection] = useState(() => {
    const id = sessionInUrl()
    return { id, key: id || crypto.randomUUID() }
  })
  const selected = useRef({ ...selection })
  const draftKeys = useRef(new Map())
  const contextUpdates = useRef(new Map())
  const modelVersions = useRef(new Map())
  const defaultModelSave = useRef(null)
  const sessionsRequest = useRef(0)
  const [modelSaves, setModelSaves] = useState(() => new Set())
  const activeId = selection.id
  const [error, setError] = useState('')
  const [online, setOnline] = useState(true)
  const [ready, setReady] = useState(false)
  const [draftMode, setDraftMode] = useState('manual')
  // Unreviewed agent file changes of one conversation: { id, value: Changes }.
  const [changes, setChanges] = useState({ id: null, value: null })
  const changesPushed = useRef(0)

  const updateUrl = useCallback((id, replace = false) => {
    const url = new URL(window.location.href)
    if (id) url.searchParams.set('session', id)
    else url.searchParams.delete('session')
    if (url.href !== window.location.href) window.history[replace ? 'replaceState' : 'pushState'](null, '', url)
  }, [])
  const selectSession = useCallback((id, fromHistory = false) => {
    if (id && id === selected.current.id) return
    const next = { id, key: id ? draftKeys.current.get(id) || id : crypto.randomUUID() }
    selected.current = next
    setSelection({ ...next }); setSession(null); setError(''); setOnline(true)
    if (!id) setDraftMode('manual')
    if (!fromHistory) updateUrl(id)
  }, [updateUrl])
  const clearMissingSession = useCallback(() => {
    selectSession(null, true)
    updateUrl(null, true)
    setError('That conversation no longer exists. Start a new conversation below.')
  }, [selectSession, updateUrl])

  const refreshSessions = useCallback(async () => {
    const request = ++sessionsRequest.current
    const atStart = new Map(contextUpdates.current)
    const list = await api('/sessions')
    // Older summaries must not undo a title event and its newer refresh.
    if (request !== sessionsRequest.current) return
    // Preserve context streamed after this summary request began.
    setSessions(list.map(item => {
      const context = contextUpdates.current.get(item.id)
      return context && context !== atStart.get(item.id) ? { ...item, context_info: context } : item
    }))
  }, [])
  const checkConnection = useCallback(async () => {
    setConnection(null)
    const result = await api('/connection')
    setConnection(result)
    return result
  }, [])
  const hasActiveSessions = sessions.some(item => item.status !== 'idle')

  useEffect(() => {
    if (!hasActiveSessions) return
    const timer = setInterval(() => void refreshSessions().catch(e => setError(e.message)), 1500)
    return () => clearInterval(timer)
  }, [hasActiveSessions, refreshSessions])

  useEffect(() => {
    let mounted = true
    void (async () => {
      const bootstrap = await api('/bootstrap')
      if (!mounted) return
      setToken(bootstrap.token)
      setSettings(bootstrap.settings)
      const list = await api('/sessions')
      if (!mounted) return
      setSessions(list)
      // Only an explicit conversation URL resumes history. The root page is new.
      if (selected.current.id && !list.some(s => s.id === selected.current.id)) {
        clearMissingSession()
      }
      setReady(true)
      await checkConnection()
    })().catch(e => setError(e.message))
    return () => { mounted = false }
  }, [checkConnection, clearMissingSession])

  useEffect(() => {
    const onPopState = () => selectSession(sessionInUrl(), true)
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [selectSession])

  useEffect(() => {
    if (!activeId || !ready) { setSession(null); return }
    const target = selected.current
    let socket
    let timer
    let closed = false
    const isCurrent = () => !closed && selected.current === target
    setSession(null)
    let reconnect = false
    const connect = async () => {
      try {
        if (reconnect) await refreshToken()
        // A rejected WebSocket handshake cannot distinguish a missing chat
        // from an expired token. Check existence before opening or retrying it.
        await api(`/sessions/${activeId}`)
      } catch (error) {
        if (!isCurrent()) return
        if (error instanceof ApiError && error.status === 404) {
          clearMissingSession()
          void refreshSessions().catch(e => setError(e.message))
        } else {
          reconnect = true; setOnline(false); timer = setTimeout(() => void connect(), 2000)
        }
        return
      }
      if (!isCurrent()) return
      socket = new WebSocket(streamUrl(activeId), streamProtocols())
      socket.onopen = () => { if (isCurrent()) setOnline(true) }
      socket.onmessage = message => {
        if (!isCurrent()) return
        const data = JSON.parse(message.data)
        if (data.type === 'snapshot' && data.session.id === activeId) {
          // A reconnect can reveal a model update missed while disconnected.
          if (reconnect) modelVersions.current.set(activeId, (modelVersions.current.get(activeId) || 0) + 1)
          if (data.session.context_info) contextUpdates.current.set(activeId, data.session.context_info)
          setSession(data.session)
          void refreshSessions().catch(e => setError(e.message))
        }
        if (data.type === 'permissions') setSession(current => current?.id === activeId ? {
          ...current, permission_mode: data.permission_mode, allowed_directories: data.allowed_directories,
        } : current)
        if (data.type === 'context') {
          contextUpdates.current.set(activeId, data.context_info)
          setSession(current => current?.id === activeId ? { ...current, context_info: data.context_info } : current)
          setSessions(current => current.map(item => item.id === activeId ? { ...item, context_info: data.context_info } : item))
        }
        if (data.type === 'changes') { changesPushed.current++; setChanges({ id: activeId, value: data.changes }) }
        if (data.type === 'event' && data.event.child_session_id) void refreshSessions().catch(e => setError(e.message))
        if (data.type === 'event') setSession(current => {
          if (!current || current.id !== activeId) return current
          const event = data.event
          return { ...current, events: current.events.some(e => e.id === event.id)
            ? current.events.map(e => e.id === event.id ? event : e) : [...current.events, event] }
        })
        if (data.type === 'delta') setSession(current => current?.id === activeId ? {
          ...current, events: current.events.map(e => e.id === data.id ? { ...e, text: (e.text || '') + data.text } : e),
        } : current)
        if (data.type === 'status') {
          // Keep the sidebar current and prevent an older list response from
          // replacing this newer streamed status.
          sessionsRequest.current++
          setSession(current => current?.id === activeId ? { ...current, status: data.status } : current)
          setSessions(current => current.map(item => item.id === activeId ? { ...item, status: data.status } : item))
          if (data.status === 'idle') void refreshSessions().catch(e => setError(e.message))
        }
        if (data.type === 'title') {
          setSession(current => current?.id === activeId ? { ...current, title: data.title } : current)
          setSessions(current => current.map(item => item.id === activeId ? { ...item, title: data.title } : item))
          void refreshSessions().catch(e => setError(e.message))
        }
        if (data.type === 'model') {
          modelVersions.current.set(activeId, (modelVersions.current.get(activeId) || 0) + 1)
          setSession(current => current?.id === activeId ? { ...current, model: data.model } : current)
          setSessions(current => current.map(item => item.id === activeId ? { ...item, model: data.model } : item))
          void refreshSessions().catch(e => setError(e.message))
        }
      }
      socket.onclose = () => {
        if (isCurrent()) { reconnect = true; setOnline(false); timer = setTimeout(() => void connect(), 2000) }
      }
    }
    void connect()
    return () => { closed = true; clearTimeout(timer); socket?.close() }
  }, [activeId, selection.key, ready, refreshSessions, clearMissingSession])

  const newConversation = () => selectSession(null)
  const ensureSession = async target => {
    if (!target.id) {
      // Folder access can create a chat before its first message. Wait for a
      // pending default-model choice before capturing the creation settings.
      if (defaultModelSave.current) await defaultModelSave.current
      // Concurrent first actions in one draft share its creation request.
      target.creating ||= api('/sessions', 'POST', { permission_mode: draftMode })
      let created
      try { created = await target.creating }
      catch (error) { target.creating = undefined; throw error }
      target.id = created.id
      draftKeys.current.set(created.id, target.key)
      // A slow creation must not reopen a chat the user has already left.
      if (selected.current === target) {
        setSelection({ id: created.id, key: target.key })
        updateUrl(created.id)
      }
      void refreshSessions().catch(e => setError(e.message))
    }
    return target.id
  }
  const send = async text => {
    const id = await ensureSession(selected.current)
    await api(`/sessions/${id}/messages`, 'POST', { text })
  }
  const saveSettings = async next => {
    const { workspace, model, env_file, context_window, max_output_tokens, max_agent_steps, compaction_handoffs, approval_timeout_minutes } = next
    const saved = await api('/settings', 'PUT', {
      workspace, model, env_file, context_window, max_output_tokens, max_agent_steps,
      ...(typeof compaction_handoffs === 'boolean' ? { compaction_handoffs } : {}),
      ...(Number.isInteger(approval_timeout_minutes) ? { approval_timeout_minutes } : {}),
    })
    setSettings(saved)
    void checkConnection().catch(e => setError(e.message))
  }
  const changeModel = async model => {
    const target = selected.current
    const scope = target.key
    const creating = target.creating
    if (modelSaves.has(scope) || (!target.id && defaultModelSave.current) || !settings) return
    setModelSaves(current => new Set(current).add(scope))
    try {
      if (!target.id && !creating) {
        const saving = saveSettings({ ...settings, model })
        defaultModelSave.current = saving
        try { await saving }
        finally { if (defaultModelSave.current === saving) defaultModelSave.current = null }
      }
      else {
        // A first-session request already in flight has captured its default.
        // Change that conversation once it exists instead of changing defaults.
        const id = target.id || (await creating).id
        const version = modelVersions.current.get(id)
        const updated = await api(`/sessions/${id}/model`, 'PUT', { model })
        if (selected.current === target && modelVersions.current.get(id) === version) {
          modelVersions.current.set(id, (version || 0) + 1)
          setSession(current => current?.id === id ? { ...current, model: updated.model } : current)
          setSessions(current => current.map(item => item.id === id ? { ...item, model: updated.model } : item))
          void refreshSessions().catch(e => setError(e.message))
        }
      }
    } finally { setModelSaves(current => { const next = new Set(current); next.delete(scope); return next }) }
  }
  const deleteSession = async id => {
    await api(`/sessions/${id}`, 'DELETE')
    if (selected.current.id === id) newConversation()
    await refreshSessions()
  }
  const renameSession = async (id, title) => {
    const updated = await api(`/sessions/${encodeURIComponent(id)}/title`, 'PUT', { title })
    // A summary captured before the rename must not restore the old title.
    sessionsRequest.current++
    setSessions(current => current.map(item => item.id === id ? { ...item, title: updated.title } : item))
    // Renaming never replaces live events/status or changes the selected chat.
    setSession(current => current?.id === id ? { ...current, title: updated.title } : current)
  }
  const setPermissionMode = async permission_mode => {
    const target = selected.current
    if (activeId) {
      const updated = await api(`/sessions/${activeId}/permissions`, 'PUT', { permission_mode })
      if (selected.current === target) setSession(current => current?.id === updated.id ? {
        ...current, permission_mode: updated.permission_mode,
      } : current)
    } else setDraftMode(permission_mode)
  }
  const allowFolder = async path => {
    const target = selected.current
    const id = await ensureSession(target)
    const updated = await api(`/sessions/${id}/folders`, 'POST', { path })
    if (selected.current === target) setSession(current => current?.id === updated.id ? {
      ...current, allowed_directories: updated.allowed_directories,
    } : current)
  }
  const removeFolder = async path => {
    const target = selected.current
    if (activeId) {
      const updated = await api(`/sessions/${activeId}/folders`, 'DELETE', { path })
      if (selected.current === target) setSession(current => current?.id === updated.id ? {
        ...current, allowed_directories: updated.allowed_directories,
      } : current)
    }
  }
  const reviewChanges = async (action, files) => {
    const id = selected.current.id
    if (!id) return null
    const pushed = changesPushed.current
    const result = await api(`/sessions/${encodeURIComponent(id)}/changes/${action}`, 'POST', { files })
    // The server also pushes the outcome; a pushed list can be newer than this response.
    if (selected.current.id === id && changesPushed.current === pushed) setChanges({ id, value: result })
    return result
  }
  const activeSession = session?.id === activeId ? session : null
  const reportError = message => { if (selected.current.key === selection.key) setError(message) }
  return { settings, connection, sessions, session: activeSession, activeId, viewKey: selection.key, error, online, ready,
    setError: reportError, setActiveId: selectSession, newConversation, send, saveSettings, deleteSession, renameSession, changeModel,
    modelSaving: modelSaves.has(selection.key) || (!activeId && defaultModelSave.current !== null),
    checkConnection, refreshSessions, permissionMode: activeSession?.permission_mode || draftMode, setPermissionMode, allowFolder, removeFolder,
    changes: activeId && changes.id === activeId ? changes.value : null, reviewChanges }
}
