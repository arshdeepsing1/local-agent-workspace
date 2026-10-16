import { useCallback, useEffect, useRef, useState } from './lib.js'
import { api, ApiError, OFFLINE_MESSAGE, refreshToken, setToken, streamProtocols, streamUrl } from './api.js'

// Selection: { id: string | null, key: string, creating?: Promise<Session> }
const sessionInUrl = () => new URL(window.location.href).searchParams.get('session')

export function useWorkspace() {
  const [settings, setSettings] = useState(null)
  const [connection, setConnection] = useState(null)
  // The last check, for the chat socket (a turn that ends while it failed checks again).
  const connectionRef = useRef(null)
  connectionRef.current = connection
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
  // false while the session poll cannot reach the local server (no chat socket is needed to notice).
  const [reachable, setReachable] = useState(true)
  const [ready, setReady] = useState(false)
  const [storageProblems, setStorageProblems] = useState([])
  const [draftMode, setDraftMode] = useState('manual')
  // The reasoning effort chosen in a new conversation before it exists (null: the Settings default).
  const [draftEffort, setDraftEffort] = useState(null)
  // Unreviewed agent file changes of one conversation: { id, value: Changes }.
  const [changes, setChanges] = useState({ id: null, value: null })
  const changesPushed = useRef(0)
  const childStates = useRef(new Map())  // Card event id -> its subagent and status, as last seen.
  // The selected conversation's tasks, pushed as { type: 'tasks', tasks }: { id, value: Task[] }.
  const [tasks, setTasks] = useState({ id: null, value: [] })
  // The saved project and user permission rules of one selection: { key, project, user, project_path, problem }.
  const [savedRules, setSavedRules] = useState({ key: null, project: [], user: [], project_path: null, problem: null })

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
    if (!id) { setDraftMode('manual'); setDraftEffort(null) }
    if (!fromHistory) updateUrl(id)
  }, [updateUrl])
  // message: '' when another notice (the storage notice) already explains why.
  const clearMissingSession = useCallback((message = 'That conversation no longer exists. Start a new conversation below.') => {
    selectSession(null, true)
    updateUrl(null, true)
    setError(message)
  }, [selectSession, updateUrl])

  const refreshSessions = useCallback(async () => {
    const request = ++sessionsRequest.current
    const atStart = new Map(contextUpdates.current)
    let list
    try { list = await api('/sessions') }
    catch (e) {
      // The reconnect banner says it; the poll keeps trying.
      if (e instanceof ApiError && e.status === 0) { setReachable(false); return }
      // Any HTTP answer means the server is there, even when it reports a problem (the caller shows it).
      if (e instanceof ApiError && e.status > 0) setReachable(true)
      throw e
    }
    setReachable(true)
    // Older summaries must not undo a title event and its newer refresh.
    if (request !== sessionsRequest.current) return
    // Preserve context streamed after this summary request began.
    setSessions(list.map(item => {
      const context = contextUpdates.current.get(item.id)
      return context && context !== atStart.get(item.id) ? { ...item, context_info: context } : item
    }))
  }, [])
  // keep: show the last result while checking again (Check again), so its banner and button stay.
  const [connectionChecking, setConnectionChecking] = useState(false)
  const checkConnection = useCallback(async ({ keep = false } = {}) => {
    if (!keep) setConnection(null)
    setConnectionChecking(true)
    let result
    try { result = await api('/connection') }
    catch (e) {
      // An HTTP error from /api/connection itself (for example the 400 "macOS denied access" an unreadable credential
      // file produces through the global OSError handler) is a credentials problem; offline (status 0) stays an error.
      if (!(e instanceof ApiError) || !(e.status >= 400)) throw e
      result = { connected: false, models: [], error: e.message, error_kind: 'credentials' }
    } finally { setConnectionChecking(false) }
    setConnection(result)
    return result
  }, [])
  const hasActiveSessions = sessions.some(item => item.status !== 'idle')

  useEffect(() => {
    // Every 1.5 s while a chat is active; every 10 s otherwise, to notice chats started
    // elsewhere; every 2 s while the local server cannot be reached, to notice it is back.
    // A failed slow poll stays quiet: the server may just be restarting. An HTTP error while unreachable is
    // reported: the server is back but has a problem.
    if (!ready) return
    const timer = !reachable ? setInterval(() => void refreshSessions().catch(e => setError(e.message)), 2000)
      : hasActiveSessions ? setInterval(() => void refreshSessions().catch(e => setError(e.message)), 1500)
      : setInterval(() => void refreshSessions().catch(() => {}), 10000)
    return () => clearInterval(timer)
  }, [ready, reachable, hasActiveSessions, refreshSessions])

  // Back online: the offline error is out of date, and Databricks may have changed meanwhile.
  const wasReachable = useRef(true)
  useEffect(() => {
    const back = reachable && !wasReachable.current
    wasReachable.current = reachable
    if (!back) return
    setError(current => current === OFFLINE_MESSAGE ? '' : current)
    void checkConnection().catch(() => {})
  }, [reachable, checkConnection])
  const wasOnline = useRef(true)
  useEffect(() => {
    const back = online && !wasOnline.current
    wasOnline.current = online
    if (back) setError(current => current === OFFLINE_MESSAGE ? '' : current)
  }, [online])

  useEffect(() => {
    let mounted = true
    void (async () => {
      const bootstrap = await api('/bootstrap')
      if (!mounted) return
      setToken(bootstrap.token)
      setSettings(bootstrap.settings)
      setStorageProblems(bootstrap.storage_problems || [])
      const list = await api('/sessions')
      if (!mounted) return
      setSessions(list)
      // Only an explicit conversation URL resumes history. The root page is new.
      if (selected.current.id && !list.some(s => s.id === selected.current.id)) {
        const unreadable = (bootstrap.storage_problems || []).some(problem => problem.file === `${selected.current.id}.jsonl`)
        clearMissingSession(unreadable ? '' : undefined)
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
        // from an expired token. Check existence (the header only) before opening or retrying it.
        await api(`/sessions/${activeId}/info`)
      } catch (error) {
        if (!isCurrent()) return
        if (error instanceof ApiError && error.status === 404) {
          clearMissingSession()
          void refreshSessions().catch(e => setError(e.message))
        } else if (error instanceof ApiError && error.status === 400) {
          // The server could not read this conversation's file: retrying cannot help.
          clearMissingSession(error.message)
          void refreshSessions().then(() => api('/bootstrap'))
            .then(bootstrap => setStorageProblems(bootstrap.storage_problems || [])).catch(e => setError(e.message))
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
          // The first snapshot holds only the latest events (events_start > 0 when earlier ones exist);
          // a snapshot without events_start is the complete history.
          setSession({ ...data.session, events_start: data.session.events_start || 0,
            events_total: data.session.events_total ?? data.session.events.length })
          void refreshSessions().catch(e => setError(e.message))
        }
        if (data.type === 'permissions') {
          setSession(current => current?.id === activeId ? {
            ...current, permission_mode: data.permission_mode, allowed_directories: data.allowed_directories,
            allow_rules: data.allow_rules || current.allow_rules,
            ...(Array.isArray(data.disabled_tools) ? { disabled_tools: data.disabled_tools } : {}),
          } : current)
          if (Array.isArray(data.project_rules) || Array.isArray(data.user_rules)) setSavedRules(current => ({
            key: target.key, project: data.project_rules || [], user: data.user_rules || [],
            project_path: current.key === target.key ? current.project_path : null, problem: data.rules_problem || null,
          }))
        }
        if (data.type === 'context') {
          contextUpdates.current.set(activeId, data.context_info)
          setSession(current => current?.id === activeId ? { ...current, context_info: data.context_info } : current)
          setSessions(current => current.map(item => item.id === activeId ? { ...item, context_info: data.context_info } : item))
        }
        if (data.type === 'changes') { changesPushed.current++; setChanges({ id: activeId, value: data.changes }) }
        if (data.type === 'tasks') setTasks({ id: activeId, value: data.tasks })
        if (data.type === 'event' && data.event.child_session_id) {
          // Parallel subagents update their cards with every action: refresh the sidebar only when a
          // subagent appears or its status changes, not for each completed action.
          const state = `${data.event.child_session_id}:${data.event.delegation?.status ?? ''}`
          if (childStates.current.get(data.event.id) !== state) {
            childStates.current.set(data.event.id, state)
            void refreshSessions().catch(e => setError(e.message))
          }
        }
        if (data.type === 'event') setSession(current => {
          if (!current || current.id !== activeId) return current
          const event = data.event
          const known = current.events.find(e => e.id === event.id)
          // A reply that waited (rate limit, retry) comes back with a new creation time: it moves below the wait's notices.
          if (known && known.created !== event.created) return { ...current, events: [...current.events.filter(e => e.id !== event.id), event] }
          if (known) return { ...current, events: current.events.map(e => e.id === event.id ? event : e) }
          // An update to an event on a page that is not loaded yet: it arrives with that page.
          if (current.events_start > 0 && current.events.length && event.created < current.events[0].created) return current
          return { ...current, events: [...current.events, event], events_total: (current.events_total ?? current.events.length) + 1 }
        })
        if (data.type === 'reasoning_effort') setSession(current => current?.id === activeId ? { ...current, reasoning_effort: data.reasoning_effort } : current)
        if (data.type === 'spending_limit') setSession(current => {
          if (current?.id !== activeId) return current
          const { spending_limit_dbu: _, ...rest } = current
          return typeof data.spending_limit_dbu === 'number' ? { ...rest, spending_limit_dbu: data.spending_limit_dbu } : rest
        })
        if (data.type === 'meta') {
          const meta = { pinned: data.pinned, archived: data.archived, tags: data.tags }
          setSession(current => current?.id === activeId ? { ...current, ...meta } : current)
          setSessions(current => current.map(item => item.id === activeId ? { ...item, ...meta } : item))
        }
        if (data.type === 'delta') setSession(current => current?.id === activeId ? {
          ...current, events: current.events.map(e => e.id === data.id ? { ...e, text: (e.text || '') + data.text } : e),
        } : current)
        if (data.type === 'status') {
          // Keep the sidebar current and prevent an older list response from
          // replacing this newer streamed status.
          sessionsRequest.current++
          setSession(current => current?.id === activeId ? {
            ...current, status: data.status, ...('terminal_reason' in data ? { terminal_reason: data.terminal_reason } : {}),
            ...(data.turn_ended ? { turn_ended: data.turn_ended } : {}),
          } : current)
          setSessions(current => current.map(item => item.id === activeId ? {
            ...item, status: data.status, ...(data.terminal_reason ? { terminal_reason: data.terminal_reason } : {}),
            ...(data.turn_ended ? { turn_ended: data.turn_ended, turn_delegated: !!data.turn_delegated } : {}),
          } : item))
          if (data.status === 'idle') void refreshSessions().catch(e => setError(e.message))
          // A turn ended while the last check failed (for example a passing Databricks outage): check once more, so
          // the banner and the sidebar do not keep reporting a problem that has gone.
          if (data.status === 'idle' && connectionRef.current?.connected === false) void checkConnection({ keep: true }).catch(() => {})
        }
        if (data.type === 'queue') setSession(current => current?.id === activeId ? { ...current, queued_messages: data.queued_messages || [], queue_error: data.error || null } : current)
        if (data.type === 'pending_context') setSession(current => current?.id === activeId ? { ...current, pending_context: data.pending_context || [] } : current)
        if (data.type === 'mcp_status') setSession(current => current?.id === activeId ? { ...current, mcp_status: data.mcp_status || [] } : current)
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
  }, [activeId, selection.key, ready, refreshSessions, clearMissingSession, checkConnection])

  const newConversation = () => selectSession(null)
  const ensureSession = async target => {
    if (!target.id) {
      // Folder access can create a chat before its first message. Wait for a
      // pending default-model choice before capturing the creation settings.
      if (defaultModelSave.current) await defaultModelSave.current
      // Concurrent first actions in one draft share its creation request.
      target.creating ||= api('/sessions', 'POST', { permission_mode: draftMode, ...(draftEffort ? { reasoning_effort: draftEffort } : {}) })
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
    return id
  }
  const saveSettings = async next => {
    const { workspace, model, env_file, context_window, max_output_tokens, max_agent_steps, compaction_handoffs, approval_timeout_minutes,
      system_notifications, user_instructions, write_limit_kb, command_env, rate_limit_auto_continue, auto_keep_changes,
      coding_dir, python_venv, auto_compact_percent, reasoning_effort, reasoning_format, thinking_summaries, prompt_caching, subagent_defaults,
      spending_limit_dbu, fallback_model, user_agents_file, user_claude_file, project_memory, output_style } = next
    const saved = await api('/settings', 'PUT', {
      workspace, model, env_file, context_window, max_output_tokens, max_agent_steps,
      ...(typeof compaction_handoffs === 'boolean' ? { compaction_handoffs } : {}),
      ...(Number.isInteger(auto_compact_percent) ? { auto_compact_percent } : {}),
      ...(Number.isInteger(approval_timeout_minutes) ? { approval_timeout_minutes } : {}),
      ...(typeof system_notifications === 'boolean' ? { system_notifications } : {}),
      ...(typeof user_instructions === 'string' ? { user_instructions } : {}),
      ...(Number.isInteger(write_limit_kb) ? { write_limit_kb } : {}),
      ...(Array.isArray(command_env) ? { command_env } : {}),
      ...(typeof rate_limit_auto_continue === 'boolean' ? { rate_limit_auto_continue } : {}),
      ...(typeof auto_keep_changes === 'boolean' ? { auto_keep_changes } : {}),
      ...(typeof coding_dir === 'string' ? { coding_dir } : {}),
      ...(typeof python_venv === 'string' ? { python_venv } : {}),
      ...(typeof reasoning_effort === 'string' ? { reasoning_effort } : {}),
      ...(Number.isFinite(spending_limit_dbu) ? { spending_limit_dbu } : {}),
      ...(typeof reasoning_format === 'string' ? { reasoning_format } : {}),
      ...(typeof thinking_summaries === 'boolean' ? { thinking_summaries } : {}),
      ...(typeof prompt_caching === 'string' ? { prompt_caching } : {}),
      ...(typeof fallback_model === 'string' ? { fallback_model: fallback_model.trim() } : {}),
      ...(subagent_defaults && typeof subagent_defaults === 'object' ? { subagent_defaults } : {}),
      ...(typeof user_agents_file === 'boolean' ? { user_agents_file } : {}),
      ...(typeof user_claude_file === 'boolean' ? { user_claude_file } : {}),
      ...(typeof project_memory === 'boolean' ? { project_memory } : {}),
      ...(typeof output_style === 'string' && output_style ? { output_style } : {}),
    })
    setSettings(saved)
    void checkConnection().catch(e => setError(e.message))
  }
  // Settings again from the server: the folder states can change without a save (a first write creates
  // the default code folder; a removed environment becomes invalid).
  const refreshSettings = async () => {
    const fresh = await api('/settings')
    setSettings(fresh)
    return fresh
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
  // children: 'delete' also deletes its subagent chats; otherwise they are kept as separate chats.
  const deleteSession = async (id, children) => {
    const result = await api(`/sessions/${id}${children === 'delete' ? '?children=delete' : ''}`, 'DELETE')
    const deleted = Array.isArray(result?.deleted) ? result.deleted : [id]
    if (deleted.includes(selected.current.id)) newConversation()
    await refreshSessions()
    return deleted
  }
  // Loads the page of up to 100 events before the first loaded one; one request at a time.
  const earlierRequest = useRef(null)
  const loadEarlier = async () => {
    const target = selected.current
    const current = session?.id === target.id ? session : null
    const first = current?.events[0]
    if (!first || !(current.events_start > 0) || earlierRequest.current === target) return
    earlierRequest.current = target
    try {
      const page = await api(`/sessions/${encodeURIComponent(current.id)}/events?before=${encodeURIComponent(first.id)}&limit=100`)
      if (selected.current !== target) return
      setSession(value => {
        // A newer snapshot (a reconnect, a complete history) replaced what this page extends.
        if (value?.id !== current.id || !(value.events_start > 0) || value.events[0]?.id !== first.id) return value
        const known = new Set(value.events.map(event => event.id))
        return { ...value, events: [...page.events.filter(event => !known.has(event.id)), ...value.events],
          events_start: page.start, events_total: page.total }
      })
    } finally { if (earlierRequest.current === target) earlierRequest.current = null }
  }
  // changes: { pinned?, archived?, tags? }; saved without changing the chat's order or unread state.
  const setMeta = async (id, changes) => {
    const updated = await api(`/sessions/${encodeURIComponent(id)}/meta`, 'PUT', changes)
    const meta = { pinned: updated.pinned, archived: updated.archived, tags: updated.tags }
    sessionsRequest.current++  // An older list response must not undo it.
    setSessions(current => current.map(item => item.id === id ? { ...item, ...meta } : item))
    setSession(current => current?.id === id ? { ...current, ...meta } : current)
    return updated
  }
  // Full-text search of titles and messages: { results: SearchResult[], truncated }.
  const searchConversations = (q, workspace) =>
    api(`/session-search?${new URLSearchParams({ q, ...(workspace ? { workspace } : {}) })}`)
  const renameSession = async (id, title) => {
    const updated = await api(`/sessions/${encodeURIComponent(id)}/title`, 'PUT', { title })
    // A summary captured before the rename must not restore the old title.
    sessionsRequest.current++
    setSessions(current => current.map(item => item.id === id ? { ...item, title: updated.title } : item))
    // Renaming never replaces live events/status or changes the selected chat.
    setSession(current => current?.id === id ? { ...current, title: updated.title } : current)
    return updated
  }
  // A chat's reasoning effort, also while it is busy: its next model request uses it. In a new conversation it is
  // held here and sent when the chat is created. The picker shows a choice at once; choices made while one is
  // saved are sent in turn, the latest last, and a failed save shows the chat's saved level again.
  const effortSaves = useRef(new Map())  // selection key -> the latest level waiting to be sent, or null
  const changeEffort = async reasoning_effort => {
    const target = selected.current
    const scope = target.key
    if (!target.id && !target.creating) { setDraftEffort(reasoning_effort); return }
    const show = (id, value) => {
      if (selected.current === target) setSession(current => current?.id === id ? { ...current, reasoning_effort: value } : current)
      setSessions(current => current.map(item => item.id === id ? { ...item, reasoning_effort: value } : item))
    }
    const id = target.id || (await target.creating).id
    // The level the server has: before the first save, then each saved one, so a failed later save never undoes an earlier one.
    let saved = session?.id === id ? session.reasoning_effort : undefined
    show(id, reasoning_effort)
    if (effortSaves.current.has(scope)) { effortSaves.current.set(scope, reasoning_effort); return }
    effortSaves.current.set(scope, null)
    let next = reasoning_effort
    try {
      while (next) {
        const updated = await api(`/sessions/${encodeURIComponent(id)}/reasoning-effort`, 'PUT', { reasoning_effort: next })
        saved = updated.reasoning_effort
        next = effortSaves.current.get(scope)
        effortSaves.current.set(scope, null)
        if (!next) show(id, updated.reasoning_effort)
      }
    } catch (error) {
      show(id, saved)
      throw error
    } finally { effortSaves.current.delete(scope) }
  }
  // options.confirm: the user confirmed switching to Bypass permissions (the server requires it).
  const setPermissionMode = async (permission_mode, options = {}) => {
    const target = selected.current
    if (activeId) {
      const updated = await api(`/sessions/${activeId}/permissions`, 'PUT', { permission_mode, ...(options.confirm ? { confirm: true } : {}) })
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
  // A permission-rules response: { chat, project, user, project_path, problem }.
  const applyRules = (target, result, chat = false) => {
    if (selected.current !== target) return
    setSavedRules({ key: target.key, project: result.project || [], user: result.user || [], project_path: result.project_path || null, problem: result.problem || null })
    if (chat && target.id && Array.isArray(result.chat)) setSession(current => current?.id === target.id ? { ...current, allow_rules: result.chat } : current)
  }
  const loadRules = async () => {
    const target = selected.current
    const result = await api(`/permission-rules${target.id ? `?session_id=${encodeURIComponent(target.id)}` : ''}`)
    applyRules(target, result)
    return result
  }
  // rule: a canonical Rule or a command draft { tool: 'run_command', behavior, text, match, options? }.
  // Chat and project rules need the chat, so a draft chat is created first (like a folder grant).
  const addRule = async (scope, rule) => {
    const target = selected.current
    const id = scope === 'user' ? target.id : await ensureSession(target)
    const result = await api('/permission-rules', 'POST', { scope, ...(id ? { session_id: id } : {}), rule })
    applyRules(target, result, scope === 'chat')
    return result
  }
  // An M1 chat rule (no behavior) uses the M1 route, which returns the saved Session.
  const removeRule = async (scope, rule) => {
    const target = selected.current
    if (scope === 'chat' && !rule.behavior) {
      if (!target.id) return
      const updated = await api(`/sessions/${target.id}/allow-rules`, 'DELETE', rule)
      if (selected.current === target) setSession(current => current?.id === updated.id ? {
        ...current, allow_rules: updated.allow_rules,
      } : current)
      return updated
    }
    // Like addRule: chat and project rules need the chat. A new chat lists the project's rules too.
    const id = scope === 'user' ? target.id : await ensureSession(target)
    const result = await api('/permission-rules', 'DELETE', { scope, ...(id ? { session_id: id } : {}), rule })
    applyRules(target, result, scope === 'chat')
    return result
  }
  // Composer: ! commands, /btw side questions, the message queue and the @ file menu.
  const [sideAnswer, setSideAnswer] = useState({ key: null, value: null })
  const sidePending = useRef(new Set())  // Chats with a side question being answered (one at a time).
  const runShell = async command => {
    const id = await ensureSession(selected.current)
    await api(`/sessions/${encodeURIComponent(id)}/shell`, 'POST', { command })
  }
  const askSide = async question => {
    const target = selected.current
    if (!target.id) throw new Error('Start a conversation first.')
    if (sidePending.current.has(target.id)) throw new Error('A side question is already being answered in this conversation.')
    sidePending.current.add(target.id)
    setSideAnswer({ key: target.key, value: { question, answer: null } })
    try {
      const result = await api(`/sessions/${encodeURIComponent(target.id)}/side-question`, 'POST', { question })
      setSideAnswer(current => current.key === target.key && current.value?.question === question
        ? { key: target.key, value: { question, answer: result.answer } } : current)
    } catch (error) {
      setSideAnswer(current => current.key === target.key && current.value?.question === question ? { key: null, value: null } : current)
      throw error
    } finally { sidePending.current.delete(target.id) }
  }
  const dismissSide = () => setSideAnswer({ key: null, value: null })
  const removeQueued = async itemId => {
    const id = selected.current.id
    if (!id) return
    const result = await api(`/sessions/${encodeURIComponent(id)}/queue/${encodeURIComponent(itemId)}`, 'DELETE')
    setSession(current => current?.id === id ? { ...current, queued_messages: result.queued_messages, queue_error: null } : current)
  }
  const sendQueued = async () => {
    const id = selected.current.id
    if (!id) return
    // The old error goes before the request: an error the server pushes while it is answered (the
    // queue could not start) must stay, and a refused request shows its own error.
    setSession(current => current?.id === id ? { ...current, queue_error: null } : current)
    await api(`/sessions/${encodeURIComponent(id)}/queue/send`, 'POST')
  }
  const loadMentionFiles = q => {
    const id = selected.current.id
    const query = `q=${encodeURIComponent(q.slice(0, 200))}`
    return api(id ? `/sessions/${encodeURIComponent(id)}/mention-files?${query}` : `/mention-files?${query}`)
  }
  const resetRules = async () => {
    const target = selected.current
    const result = await api(`/permission-rules/reset${target.id ? `?session_id=${encodeURIComponent(target.id)}` : ''}`, 'POST')
    applyRules(target, result)
    return result
  }
  // Turned-off tools of the selected chat; returns the saved Session.
  const setDisabledTools = async tools => {
    const target = selected.current
    if (!target.id) return null
    const updated = await api(`/sessions/${target.id}/disabled-tools`, 'PUT', { tools })
    if (selected.current === target) setSession(current => current?.id === updated.id ? { ...current, disabled_tools: updated.disabled_tools || [] } : current)
    return updated
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
  const rulesHere = savedRules.key === selection.key ? savedRules : null
  return { settings, connection, connectionChecking, sessions, session: activeSession, activeId, viewKey: selection.key, error,
    online: online && reachable, offlineError: error === OFFLINE_MESSAGE, ready,
    setError: reportError, setActiveId: selectSession, newConversation, send, saveSettings, refreshSettings, deleteSession, renameSession, changeModel,
    runShell, askSide, removeQueued, sendQueued, loadMentionFiles, sideAnswer: sideAnswer.key === selection.key ? sideAnswer.value : null, dismissSide,
    modelSaving: modelSaves.has(selection.key) || (!activeId && defaultModelSave.current !== null),
    checkConnection, refreshSessions, permissionMode: activeSession?.permission_mode || draftMode, setPermissionMode, allowFolder, removeFolder,
    // A chat saved without an effort, and a new conversation, use the Settings default.
    reasoningEffort: (activeId ? activeSession?.reasoning_effort : draftEffort) || settings?.reasoning_effort || 'default',
    changeEffort,
    loadEarlier, setMeta, searchConversations,
    rules: { chat: activeSession?.allow_rules || [], project: rulesHere?.project || [], user: rulesHere?.user || [],
      project_path: rulesHere?.project_path || null, problem: rulesHere?.problem || null },
    loadRules, addRule, removeRule, resetRules, setDisabledTools,
    changes: activeId && changes.id === activeId ? changes.value : null, reviewChanges, storageProblems,
    tasks: activeId && tasks.id === activeId ? tasks.value : [] }
}
