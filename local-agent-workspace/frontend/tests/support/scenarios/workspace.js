// Fake backend for whole-app tests (App.js and useWorkspace.js).
window.baseSettings = {
  workspace: '/project', model: 'test-model', env_file: '', context_window: 131072,
  max_output_tokens: 8192, max_agent_steps: 32, host: '', configured: true,
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
fake.sessions = new Map([['a', makeSession('a', 'Other conversation')]])
fake.settings = { ...baseSettings }
fake.handle = (path, options) => {
  const body = () => JSON.parse(options.body)
  if (path === '/api/bootstrap') return fake.json({ token: 'test-token', settings: fake.settings })
  if (path === '/api/connection') return fake.json({ connected: true, models: ['test-model', 'other-model', 'latest-model'], error: null })
  if (path === '/api/settings' && options.method === 'PUT') {
    fake.settings = { ...fake.settings, ...body() }
    return fake.json(fake.settings)
  }
  if (path === '/api/sessions' && options.method === 'POST') {
    const created = { ...makeSession('created', 'Created conversation'), model: fake.settings.model }
    fake.sessions.set(created.id, created)
    return fake.json(created)
  }
  if (path === '/api/sessions') return fake.json([...fake.sessions.values()])
  if (path.startsWith('/api/files?')) return fake.json([{ path: 'note.txt', name: 'note.txt', directory: false }])
  if (path.startsWith('/api/file?')) return fake.json({ content: 'original' })
  if (path === '/api/file' || path.endsWith('/messages')) return fake.json({ ok: true })
  const titleId = path.match(/^\/api\/sessions\/([^/]+)\/title$/)?.[1]
  if (titleId && options.method === 'PUT') {
    const updated = { ...fake.sessions.get(titleId), title: body().title.trim().replace(/\s+/g, ' ') }
    fake.sessions.set(titleId, updated)
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
  const id = path.match(/^\/api\/sessions\/([^/]+)$/)?.[1]
  if (id) {
    if (options.method === 'DELETE') { fake.sessions.delete(id); return fake.json({ ok: true }) }
    return fake.sessions.has(id) ? fake.json(fake.sessions.get(id)) : fake.json({ detail: 'Conversation not found.' }, 404)
  }
}
