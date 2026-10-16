// Fake backend for the Agent tools dialog tests.
window.session = (id = 'a') => ({ id, title: `Conversation ${id}`, workspace: '/project', model: 'test-model', status: 'idle', events: [], updated: 1, permission_mode: 'manual', allowed_directories: [] })
window.checkpoint = { id: 'checkpoint', path: 'src/main.py', status: 'available', created: 1 }
window.worktree = { id: 'worktree', path: '/project-worktree', branch: 'feature/test', source_workspace: '/project', created: 1 }
window.task = { id: 'task', title: 'Inspect source', description: 'Find the parser', status: 'pending', depends_on: [] }
window.config = { servers: [], hooks: [] }
window.skill = { id: 'skill', name: 'Review code', description: 'Review changes carefully', path: '/skills/review/SKILL.md' }
// POST /api/extensions/import: a preview (mcp_import.parse), nothing saved.
window.importPreview = { source: 'project', path: '.mcp.json', needs: ['GITHUB_TOKEN'],
  entries: [{ id: 'github', name: 'GitHub', enabled: false, transport: 'stdio', command: 'github-mcp-server', args: ['stdio'],
    env: { GITHUB_PERSONAL_ACCESS_TOKEN: { secret: 'GITHUB_TOKEN' } } }],
  skipped: [{ name: 'legacy', reason: 'SSE servers are not supported; use the server\'s Streamable HTTP URL if it has one.' }],
  ignored: [{ name: 'GitHub', fields: ['cwd'] }] }
window.projectSettings = { workspace: '/project', verify_command: 'pytest -q', verify_timeout_seconds: 600, verify_max_rounds: 2 }
window.toolList = { tools: [
  { name: 'read_file', description: 'Read numbered lines from a text file. Pages follow next_line.' },
  { name: 'write_file', description: 'Create or replace a file.' },
  { name: 'find_files', description: 'Find files by glob pattern.' },
  { name: 'run_command', description: 'Run a command.' },
  { name: 'delegate_task', description: 'Hand a self-contained task to a subagent.' },
  { name: 'list_skills', description: 'List workspace skills.' },
  { name: 'ask_user', description: 'Ask the user short questions.' },
], disabled: ['run_command'] }
// A running explore subagent, as /sessions/<id>/children lists it.
window.child = { ...session('child'), title: 'Child review', status: 'awaiting_approval', parent_session_id: 'a', is_subagent: true,
  agent_type: 'explore', tool_profile: 'read_only', max_steps: 10, created: 1000,
  delegation: { status: 'awaiting_approval', completed_tools: 3, agent_type: 'explore', started: 1000 },
  transcript: 'conversations/child.jsonl',
  subagent: { id: 'child', task: 'Review the parser module', agent_type: 'explore', status: 'running', created: 1000, ended: null,
    transcript: 'conversations/child.jsonl' } }
// GET /api/agents?session_id=: the built-in types with their Settings defaults, one custom agent and a file that cannot be used.
window.agentTypes = {
  built_in: [
    { name: 'explore', built_in: true, profile: 'read_only', description: 'Investigation with read-only tools: find, search, outline and read files.', model: '', effort: 'low' },
    { name: 'general', built_in: true, profile: 'inherit', description: "The chat's tools (within the subagent tool ceiling).", model: '', effort: '' },
    { name: 'editor', built_in: true, profile: 'file_editor', description: 'Reads and edits files, without commands.', model: 'databricks-gpt-oss-120b', effort: 'high' },
  ],
  custom: [{ name: 'security-auditor', built_in: false, description: 'Reviews code for injection and secrets handling.',
    path: '.agents/agents/security-auditor.md', profile: 'read_only', tools: ['read_file', 'search_files'], model: 'databricks-claude-sonnet-4-5',
    effort: 'low', skills: ['review'], max_steps: 8, notes: ['Ignored unknown tools: web_fetch.'], instructions_chars: 120 }],
  errors: [{ path: '.claude/agents/broken.md', error: 'description is required: one line (at most 300 characters) that tells the model when to use this agent.' }],
  folders: ['.agents/agents', '.claude/agents'], index_bytes: 210,
}
window.metrics = {
  scope: 'session', complete: true,
  totals: { calls: 2, successful: 1, errors: 1, rate_limited: 1, retries: 3, input_tokens: 1200, output_tokens: 300, cache_read_input_tokens: 300, cache_creation_input_tokens: 100, reasoning_tokens: 20, estimated_dbu: 0.2039292 },
  calls: [
    { id: 'call-1', purpose: 'agent', model: 'databricks-claude-opus-4-8', created: '2026-09-22T10:00:00Z', status: 'completed', usage: { input_tokens: 1200, output_tokens: 300, cache_read_input_tokens: 300, cache_creation_input_tokens: 100, reasoning_tokens: 20 }, estimated_dbu: 0.2039292 },
    { id: 'call-2', purpose: 'compaction', model: 'databricks-claude-opus-4-8', created: '2026-09-22T10:01:00Z', status: 'error', http_status: 429, estimated_dbu: 0 },
  ],
  by_purpose: [], by_model: [],
  pricing: { currency: 'DBU', unit: 'per_1m_tokens', label: 'Databricks pay-per-token list-price DBU estimate', source_url: 'https://www.databricks.com/product/pricing/proprietary-foundation-model-serving', effective_at: '2026-09-22', estimated: true },
}
window.dialogProps = (selected = session()) => ({
  session: selected, onClose: fake.spy('close'), onSelectSession: fake.spy('select'), onError: fake.spy('onError'),
})
fake.handle = (path, options) => {
  const body = () => JSON.parse(options.body)
  if (/^\/api\/checkpoints(?:\?|$)/.test(path)) return fake.json([checkpoint])
  if (path.includes('/preview')) return fake.json({ ...checkpoint, diff: '-new\n+original', expected_current_hash: 'current-hash', can_restore: true })
  if (path.includes('/restore')) return fake.json({ ok: true })
  if (/^\/api\/worktrees(?:\?|$)/.test(path)) return fake.json(options.method === 'POST' ? worktree : [worktree])
  if (path.includes('/worktrees/worktree/session')) return fake.json(session('worktree-session'))
  if (path.includes('/worktrees/worktree') && options.method === 'DELETE') return fake.json({ ok: true })
  if (path.endsWith('/tasks')) return fake.json(options.method === 'POST' ? { ...task, id: 'new-task', ...body() } : [task])
  if (path.includes('/tasks/')) return fake.json({ ...task, ...body() })
  if (path.endsWith('/children')) return fake.json([child])
  if (path === '/api/agents' || path.startsWith('/api/agents?')) return fake.json(agentTypes)
  if (path.startsWith('/api/tools')) return fake.json(toolList)
  if (path.endsWith('/disabled-tools')) return fake.json({ ...session(), disabled_tools: body().tools })
  if (path.endsWith('/subagent-profile')) return fake.json({ ...session(), subagent_tool_profile: body().tool_profile })
  if (path.endsWith('/metrics')) return fake.json(metrics)
  if (path.startsWith('/api/project-settings')) return fake.json(options.method === 'PUT' ? { ...projectSettings, ...body() } : projectSettings)
  if (path === '/api/extensions') return fake.json(options.method === 'PUT' ? body() : config)
  if (path === '/api/extensions/test') return fake.json({ tools: ['example.search'] })
  if (path.startsWith('/api/extensions/import')) return fake.json({ ...importPreview, source: body().source })
  if (path === '/api/skills' || path.startsWith('/api/skills?')) return fake.json([skill])
  if (path === '/api/commands' || path.startsWith('/api/commands?')) return fake.json({ commands: fake.commands || [], errors: [],
    folders: ['.agents/commands', '.claude/commands', '~/.agents/commands', '~/.claude/commands'] })
  if (path.endsWith('/skills')) return fake.json({ active_skills: body().enabled ? ['skill'] : [] })
}
