// Fake job API for the terminal panel tests.
window.makeJob = (id, overrides = {}) => ({
  id, session_id: 'a', workspace: '/project', command: `command-${id}`, state: 'running',
  created: 1, updated: 1, exit_code: null, output: '', truncated: false,
  timeout_seconds: 60, max_output_bytes: 80000, background: false, ...overrides,
})
fake.jobs = new Map()
fake.handle = (path, options) => {
  const url = new URL(path, 'http://localhost')
  const sessionId = url.searchParams.get('session_id')
  if (url.pathname === '/api/jobs' && options.method === 'POST') {
    const job = makeJob(`job-${fake.jobs.size + 1}`, { ...JSON.parse(options.body), session_id: sessionId })
    fake.jobs.set(job.id, job)
    return fake.json(job)
  }
  if (url.pathname === '/api/jobs') {
    return fake.json([...fake.jobs.values()].filter(job => job.session_id === sessionId).map(({ output, ...summary }) => summary))
  }
  const match = url.pathname.match(/^\/api\/jobs\/([^/]+)(\/stop)?$/)
  const job = match && fake.jobs.get(match[1])
  if (job) {
    if (match[2]) { job.state = 'cancelled'; job.updated++; job.exit_code = -15 }
    return fake.json(job)
  }
}
