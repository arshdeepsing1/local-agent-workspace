// Fake backend for whole-app tests (App.js and useWorkspace.js).
window.baseSettings = {
  workspace: '/project', model: 'test-model', env_file: '', context_window: 131072,
  max_output_tokens: 8192, max_agent_steps: 32, host: '', configured: true, coding_dir: '', python_venv: '',
}
window.contextInfo = {
  estimated_tokens: 60380, input_budget: 120832, context_window: 131072, reply_reserve: 8192,
  compactions: 1, summarized_messages: 8, estimate_method: 'weighted_utf8',
  instruction_files: ['AGENTS.md', 'src/AGENTS.md'], warnings: ['Project instructions were truncated.'],
}
window.makeSession = (id, title = id) => ({
  id, title, workspace: '/project', model: 'test-model', status: 'idle',
  events: [], updated: 1, permission_mode: 'manual', allowed_directories: [],
})
window.skills = [
  { id: 'review', name: 'Code review', description: 'Review the current diff for correctness bugs.', path: '.agents/skills/review/SKILL.md' },
  { id: 'handoff', name: 'handoff', description: 'Write a detailed cold-start handoff.', path: '/app/skills/handoff/SKILL.md' },
]
// Agent file changes pushed over the socket ({ type: 'changes', changes }) and their review.
window.changedFile = (path, added = 1, removed = 1, status = 'modified') => ({ path, status, added, removed, current_hash: `hash-${path}` })
window.makeChanges = (...files) => ({ files, added: files.reduce((sum, item) => sum + item.added, 0), removed: files.reduce((sum, item) => sum + item.removed, 0) })
window.noteDiff = {
  path: 'note.txt', status: 'modified', added: 2, removed: 1, baseline_hash: 'base', current_hash: 'hash-note.txt',
  hunks: [{ index: 0, line: 1, removed: 1, added: 1 }, { index: 1, line: 4, removed: 0, added: 1 }],
  lines: [
    { kind: 'context', text: 'one', old: 1, new: 1 }, { kind: 'removed', text: 'two', old: 2, hunk: 0 },
    { kind: 'added', text: 'TWO', new: 2, hunk: 0 }, { kind: 'context', text: 'three', old: 3, new: 3 },
    { kind: 'added', text: 'four', new: 4, hunk: 1, newline: false },
  ],
}
fake.reviewResult = makeChanges()
fake.sessions = new Map([['a', makeSession('a', 'Other conversation')]])
// The M2 user settings at their defaults (the server always returns them).
fake.settings = { ...baseSettings, user_instructions: '', write_limit_kb: 80, command_env: [], rate_limit_auto_continue: true,
  coding_dir_status: { path: null, state: 'off', message: 'Not set. New code goes where you ask, or into the project folder.', source: 'none' },
  python_venv_status: { path: null, state: 'off', message: 'Not set. Commands use the Python on the server\'s PATH.', python: null, version: null, source: 'none' },
  default_model: 'databricks-claude-opus-5-5', default_limits: { context_window: 131072, max_output_tokens: 8192 },
  reasoning_effort: 'default', reasoning_format: 'auto', thinking_summaries: true,
  endpoint_profiles: [{ model: 'databricks-claude-opus-5-5', label: 'Claude Opus 5.5' }, { model: 'databricks-claude-opus-4-8', label: 'Claude Opus 4.8' },
    { model: 'databricks-gpt-oss-120b', label: 'GPT OSS 120B' }, { model: 'databricks-gpt-oss-20b', label: 'GPT OSS 20B' }] }
// POST /api/settings/limits: canned answers like the server's for Claude Opus 5.5; other endpoints have no profile.
window.opusProfile = { label: 'Claude Opus 5.5', context_max: 1000000, output_max: 128000, itpm: 1000000, otpm: 100000,
  recommended_context_window: 200000, recommended_max_output_tokens: 16384, thinking_reserve: 4096,
  model: 'databricks-claude-opus-5-5', output_concurrency_limit: 20000, context_rate_limit: 250000 }
window.limitsCheck = ({ model, context_window, max_output_tokens }) => {
  const effective = { context_window, max_output_tokens, clamped: false }
  if (model.trim().toLowerCase() !== 'databricks-claude-opus-5-5') {
    return { profile: null, recommended: null, defaults: { context_window: 131072, max_output_tokens: 8192 }, effective, warnings: [],
      note: 'No built-in limits for this endpoint. Set the context budget at or below its context window.' }
  }
  const warnings = []
  effective.context_window = Math.min(context_window, 1000000)
  effective.max_output_tokens = Math.min(max_output_tokens, 128000, effective.context_window - 2049)
  effective.clamped = effective.context_window !== context_window || effective.max_output_tokens !== max_output_tokens
  if (max_output_tokens > 20000) warnings.push({ code: 'output_concurrency', level: 'warning', text: 'Above 20,000, four requests at once can be rejected with HTTP 429 before they start.' })
  if (context_window > 250000) warnings.push({ code: 'context_itpm', level: 'note', text: 'Above 250,000 tokens, four full steps in one minute reach the input limit.' })
  return { profile: opusProfile, recommended: { context_window: 200000, max_output_tokens: 16384 },
    defaults: { context_window: 200000, max_output_tokens: 16384 }, effective, warnings,
    note: 'Limits for Claude Opus 5.5 are Databricks\'s documented pay-per-token defaults (Enterprise tier, checked 2026-10-13); your workspace\'s limits may differ.' }
}
// Saved project and user permission rules, returned by GET /api/permission-rules with the chat's rules.
fake.projectRules = []
fake.userRules = []
fake.rulesProblem = null
window.ruleSets = id => ({ chat: fake.sessions.get(id)?.allow_rules || [], project: fake.projectRules, user: fake.userRules,
  project_path: '/project', problem: fake.rulesProblem })
fake.handle = (path, options) => {
  const body = () => JSON.parse(options.body)
  if (path.startsWith('/api/permission-rules') && (options.method || 'GET') === 'GET') {
    return fake.json(ruleSets(new URLSearchParams(path.split('?')[1] || '').get('session_id')))
  }
  if (path === '/api/bootstrap') return fake.json({ token: 'test-token', settings: fake.settings })
  if (path === '/api/skills' || path.startsWith('/api/skills?')) return fake.json(skills)
  if (path === '/api/connection') return fake.json({ connected: true, models: ['test-model', 'other-model', 'latest-model'], error: null })
  if (path === '/api/settings' && options.method === 'PUT') {
    fake.settings = { ...fake.settings, ...body() }
    return fake.json(fake.settings)
  }
  if (path === '/api/settings' && (options.method || 'GET') === 'GET') return fake.json(fake.settings)
  if (path === '/api/settings/limits' && options.method === 'POST') return fake.json(limitsCheck(body()))
  if (path === '/api/sessions' && options.method === 'POST') {
    const requested = options.body ? body().reasoning_effort : undefined
    const created = { ...makeSession('created', 'Created conversation'), model: fake.settings.model,
      reasoning_effort: requested || fake.settings.reasoning_effort || 'default' }
    fake.sessions.set(created.id, created)
    return fake.json(created)
  }
  if (path === '/api/sessions') return fake.json([...fake.sessions.values()])
  if (/^\/api\/sessions\/[^/]+\/changes\/diff\?/.test(path)) return fake.json(noteDiff)
  if (/^\/api\/sessions\/[^/]+\/changes\/(keep|undo)$/.test(path)) return fake.json(fake.reviewResult)
  if (path.startsWith('/api/files?')) return fake.json([{ path: 'note.txt', name: 'note.txt', directory: false }])
  if (path.startsWith('/api/file?')) return fake.json({ content: 'original' })
  if (path === '/api/file' || path.endsWith('/messages')) return fake.json({ ok: true })
  const titleId = path.match(/^\/api\/sessions\/([^/]+)\/title$/)?.[1]
  if (titleId && options.method === 'PUT') {
    const updated = { ...fake.sessions.get(titleId), title: body().title.trim().replace(/\s+/g, ' ') }
    fake.sessions.set(titleId, updated)
    return fake.json(updated)
  }
  const effortId = path.match(/^\/api\/sessions\/([^/]+)\/reasoning-effort$/)?.[1]
  if (effortId && options.method === 'PUT') {
    const updated = { ...fake.sessions.get(effortId), reasoning_effort: body().reasoning_effort }
    fake.sessions.set(effortId, updated)
    return fake.json(updated)
  }
  const modelId = path.match(/^\/api\/sessions\/([^/]+)\/model$/)?.[1]
  if (modelId && options.method === 'PUT') {
    const updated = { ...fake.sessions.get(modelId), model: body().model }
    fake.sessions.set(modelId, updated)
    return fake.json(updated)
  }
  const folderId = path.match(/^\/api\/sessions\/([^/]+)\/folders$/)?.[1]
  if (folderId && options.method === 'POST') {
    const updated = { ...fake.sessions.get(folderId), allowed_directories: [body().path] }
    fake.sessions.set(folderId, updated)
    return fake.json(updated)
  }
  const modeId = path.match(/^\/api\/sessions\/([^/]+)\/permissions$/)?.[1]
  if (modeId && options.method === 'PUT') {
    const updated = { ...fake.sessions.get(modeId), permission_mode: body().permission_mode }
    fake.sessions.set(modeId, updated)
    return fake.json(updated)
  }
  // The existence check before a chat's stream opens: its header, without events.
  const infoId = path.match(/^\/api\/sessions\/([^/]+)\/info$/)?.[1]
  if (infoId && (options.method || 'GET') === 'GET') {
    if (!fake.sessions.has(infoId)) return fake.json({ detail: 'Conversation not found.' }, 404)
    const { events, ...header } = fake.sessions.get(infoId)
    return fake.json(header)
  }
  // Earlier pages of a chat's events (tests that page override this).
  const eventsId = path.match(/^\/api\/sessions\/([^/]+)\/events\?/)?.[1]
  if (eventsId) return fake.json({ events: [], start: 0, total: fake.sessions.get(eventsId)?.events.length || 0 })
  const metaId = path.match(/^\/api\/sessions\/([^/]+)\/meta$/)?.[1]
  if (metaId && options.method === 'PUT') {
    const updated = { ...fake.sessions.get(metaId), ...body() }
    fake.sessions.set(metaId, updated)
    return fake.json(updated)
  }
  if (path.startsWith('/api/session-search?')) return fake.json({ results: [], truncated: false })
  const id = path.split('?')[0].match(/^\/api\/sessions\/([^/]+)$/)?.[1]
  if (id) {
    if (options.method === 'DELETE') { fake.sessions.delete(id); return fake.json({ ok: true, deleted: [id] }) }
    return fake.sessions.has(id) ? fake.json(fake.sessions.get(id)) : fake.json({ detail: 'Conversation not found.' }, 404)
  }
}
