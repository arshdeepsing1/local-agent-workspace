// Fake backend for the Agent tools dialog tests.
window.session = (id = 'a') => ({ id, title: `Conversation ${id}`, workspace: '/project', model: 'test-model', status: 'idle', events: [], updated: 1, permission_mode: 'manual', allowed_directories: [] })
window.checkpoint = { id: 'checkpoint', path: 'src/main.py', status: 'available', created: 1 }
window.worktree = { id: 'worktree', path: '/project-worktree', branch: 'feature/test', source_workspace: '/project', created: 1 }
window.task = { id: 'task', title: 'Inspect source', description: 'Find the parser', status: 'pending', depends_on: [] }
window.config = { servers: [], hooks: [] }
window.skill = { id: 'skill', name: 'Review code', description: 'Review changes carefully', path: '/skills/review/SKILL.md' }
window.metrics = {
  scope: 'session', complete: true,
  totals: { calls: 2, successful: 1, errors: 1, rate_limited: 1, input_tokens: 1200, output_tokens: 300, cache_read_input_tokens: 300, cache_creation_input_tokens: 100, reasoning_tokens: 20, estimated_dbu: 0.2039292 },
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
  if (path.endsWith('/children')) return fake.json([{ ...session('child'), title: 'Child review', status: 'awaiting_approval' }])
  if (path.endsWith('/subagent-profile')) return fake.json({ ...session(), subagent_tool_profile: body().tool_profile })
  if (path.endsWith('/metrics')) return fake.json(metrics)
  if (path === '/api/extensions') return fake.json(options.method === 'PUT' ? body() : config)
  if (path === '/api/extensions/test') return fake.json({ tools: ['example.search'] })
  if (path === '/api/skills' || path.startsWith('/api/skills?')) return fake.json([skill])
  if (path.endsWith('/skills')) return fake.json({ active_skills: body().enabled ? ['skill'] : [] })
}
