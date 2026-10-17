// Installed before the UI loads (Playwright init script). Replaces the network
// with an in-page fake backend that tests configure and inspect:
//   fake.handle(path, options)   default responses for a test file (scenario)
//   fake.override(path, options) per-test responses; return undefined to fall through
//   fake.calls                   every request: { path, method, body, headers }
//   fake.sockets                 every conversation WebSocket opened by the UI
//   fake.presence                every presence WebSocket (one per open page)
//   fake.spy(name, impl)         callback props; arguments land in fake.spies[name]
//   fake.defer(name)             a pending response resolved later from the test
//   fake.reasoningPreview(body)  the answer to POST /api/settings/reasoning unless a test overrides it
//                                (a small copy of backend reasoning.preview for the endpoints tests use)
//   GET /api/endpoints/<model>/checks and /state answer "nothing saved" unless a test overrides them
//   GET /api/git/pr answers "gh is not installed" unless a test overrides it
//   POST /api/sessions/<id>/uploads answers fake.uploadRecord(name, file) (a file attached in the composer; uploads.py)
//                                unless a test overrides it; fake.uploads lists [{ session, name, file }]
(() => {
  const json = (body, status = 200) => new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  })
  const deferred = () => {
    let resolve, reject
    const promise = new Promise((done, fail) => { resolve = done; reject = fail })
    return { promise, resolve, reject }
  }

  class TestSocket {
    constructor(url, protocols) {
      this.url = url
      this.protocols = protocols
      this.onopen = null
      this.onmessage = null
      this.onclose = null
      ;(url.endsWith('/api/presence') ? fake.presence : fake.sockets).push(this)
    }
    close() { this.onclose?.() }
    open() { this.onopen?.() }
    emit(data) { this.onmessage?.({ data: JSON.stringify(data) }) }
  }

  const fake = window.fake = {
    json, deferred,
    calls: [],
    sockets: [],
    presence: [],
    spies: {},
    deferreds: {},
    clipboard: [],
    confirms: [],
    confirmAnswer: true,
    downloads: [],
    handle: () => undefined,
    override: () => undefined,
    defer(name) { return (this.deferreds[name] = deferred()) },
    spy(name, impl) {
      const calls = this.spies[name] = []
      return (...args) => { calls.push(args); return impl ? impl(...args) : undefined }
    },
    // Requests matching an exact path (or RegExp) and optional method.
    requests(path, method) {
      return this.calls.filter(call => (path instanceof RegExp ? path.test(call.path) : call.path === path)
        && (!method || call.method === method))
    },
    bodies(path, method) { return this.requests(path, method).map(call => JSON.parse(call.body)) },
    socketsFor(id) { return this.sockets.filter(socket => socket.url.endsWith(`/${id}/stream`)) },
    socketFor(id) { return this.socketsFor(id).at(-1) },
    // Record downloads instead of saving files.
    captureDownloads() {
      const blobs = new Map()
      const create = URL.createObjectURL.bind(URL)
      URL.createObjectURL = blob => { const url = create(blob); blobs.set(url, blob); return url }
      const revoke = URL.revokeObjectURL.bind(URL)
      URL.revokeObjectURL = url => {  // Also blob URLs that were not downloads (a preview's image).
        const item = fake.downloads.find(entry => entry.href === url)
        if (item) item.revoked = true
        revoke(url)
      }
      HTMLAnchorElement.prototype.click = function () {
        fake.downloads.push({ download: this.download, href: this.href, blob: blobs.get(this.href), connected: this.isConnected, revoked: false })
      }
    },
    // Select a file in an <input type="file">; `size` can simulate a large file.
    chooseFile(input, text, name = 'conversation.json', size) {
      const file = new File([text], name, { type: 'application/json' })
      const read = file.text.bind(file)
      file.reads = 0
      file.text = () => { file.reads++; return read() }
      if (size !== undefined) Object.defineProperty(file, 'size', { value: size })
      Object.defineProperty(input, 'files', { configurable: true, value: [file] })
      input.dispatchEvent(new Event('change', { bubbles: true }))
      return file
    },
  }

  const LABELS = { default: 'Default', low: 'Fast', medium: 'Medium', high: 'High', xhigh: 'Extra high', max: 'Max' }
  const CLAUDE = ['low', 'medium', 'high', 'xhigh', 'max']
  const BUDGETS = { low: 1024, medium: 4096, high: 8192, xhigh: 16384, max: 32768 }  // reasoning.BUDGET_TOKENS
  const PROFILES = {
    'databricks-claude-opus-5-5': { label: 'Claude Opus 5.5', format: 'anthropic', levels: CLAUDE, default: 'medium', thinking: 'always' },
    'databricks-claude-opus-4-8': { label: 'Claude Opus 4.8', format: 'anthropic', levels: CLAUDE, default: null, thinking: 'adaptive' },
    'databricks-gpt-oss-120b': { label: 'GPT OSS 120B', format: 'openai', levels: ['low', 'medium', 'high'], default: 'medium', map: { xhigh: 'high', max: 'high' } },
  }
  fake.reasoningPreview = ({ model, reasoning_effort: effort = 'default', reasoning_format: setting = 'auto', thinking_summaries: summaries = true }) => {
    const profile = PROFILES[model]
    const endpoint = profile || { label: null, format: /claude/.test(model) ? 'anthropic' : /gpt-oss/.test(model) ? 'openai' : null,
      levels: CLAUDE, default: null, thinking: /claude/.test(model) ? 'adaptive' : null }
    const format = setting !== 'auto' ? setting : endpoint.format || 'none'
    const sentFor = value => value === 'default' || format === 'none' ? null : (endpoint.map || {})[value] || value
    const sent = sentFor(effort)
    const display = summaries ? { display: 'summarized' } : {}
    let fields = {}
    if (format === 'anthropic' && sent) fields = { thinking: { type: 'adaptive', ...display }, output_config: { effort: sent } }
    else if (format === 'anthropic' && summaries && endpoint.thinking === 'always') fields = { thinking: { type: 'adaptive', ...display } }
    else if (format === 'anthropic_budget' && sent) fields = { thinking: { type: 'enabled', budget_tokens: BUDGETS[sent] } }
    else if (format === 'openai' && sent) fields = { reasoning_effort: sent }
    const meaning = endpoint.default ? LABELS[endpoint.default] : profile && endpoint.thinking === 'adaptive' ? 'no thinking' : null
    const levels = Object.keys(LABELS).map(value => {
      if (value === 'default') return { value, label: meaning ? `Default (${meaning})` : 'Default', sent: null }
      const level = { value, label: LABELS[value], sent: sentFor(value) }
      if (level.sent && level.sent !== value) level.note = `sent as ${LABELS[level.sent]}`
      return level
    })
    const notes = []
    if (!profile) notes.push(format === 'none' ? `No reasoning profile for ${model}, and its name does not say which fields it takes, so requests carry no reasoning fields. Choose a Request format to send them (unverified).`
      : `No reasoning profile for ${model}: every level is offered and these fields are unverified.`)
    if (effort === 'default' && format !== 'none') notes.push(`Default sends no effort field: ${endpoint.label || model} uses its own default${meaning ? ` (${meaning})` : ''}.`)
    return { model, label: endpoint.label, verified: !!profile, format, format_setting: setting, endpoint_format: endpoint.format,
      default_effort: endpoint.default, default_label: meaning, levels, effort,
      ...(Object.keys(fields).length && sent ? { sent_effort: sent, effort_field: format === 'openai' ? 'reasoning_effort'
        : format === 'anthropic_budget' ? 'thinking.budget_tokens' : 'output_config.effort' } : {}),
      fields, notes }
  }
  // Answers every scenario shares unless a test overrides them: the reasoning preview, the built-in output styles
  // (Settings → Output style), an empty project memory (Agent tools → Memory and its Overview row) and no project
  // settings files (the trust prompt).
  const STYLES = [{ id: 'default', name: 'Default', description: "The app's usual replies.", source: 'built-in' },
    { id: 'concise', name: 'Concise', description: 'Short, direct replies that lead with the result.', source: 'built-in' },
    { id: 'explanatory', name: 'Explanatory', description: 'Explains the reasoning and concepts behind the work.', source: 'built-in' }]
  fake.memory = { project: '/project', notes: [], bytes: 0, max_bytes: 4000, max_notes: 50, max_note_chars: 500, enabled: true, problem: null }
  // GET /api/settings/project (settings_layers.py): no project settings files unless a test sets some.
  fake.projectLayers = { workspace: '/project', status: 'none', changed: false, decision: null, sha256: '', files: [],
    values: {}, sources: {}, followed: [], notes: [] }
  // An uploaded file's record: an image is 640×480, a PDF has 3 pages, the id counts the uploads (64 hex digits).
  fake.uploads = []
  fake.uploadRecord = (name, file) => {
    const id = fake.uploads.length.toString(16).padStart(64, '0')
    const format = (/\.([^.]+)$/.exec(name)?.[1] || '').toLowerCase()
    if (['png', 'jpg', 'jpeg', 'gif', 'webp'].includes(format)) {
      const type = format === 'jpg' ? 'jpeg' : format
      return { id, name, kind: 'image', format: type === 'jpeg' ? 'jpg' : type, media_type: `image/${type}`, width: 640, height: 480,
        bytes: file?.size || 0, estimated_tokens: 410 }
    }
    return { id, name, kind: 'document', format, media_type: 'application/octet-stream', bytes: file?.size || 0, chars: 1200,
      truncated: false, notes: [], estimated_tokens: 300, ...(format === 'pdf' ? { units: 'page', count: 3 } : {}) }
  }
  // Endpoint checks (Settings → Check endpoint capabilities…, Agent tools → Overview) default to none saved and no
  // serving state.
  const defaults = (path, options) => path === '/api/settings/reasoning' && options.method === 'POST'
    ? json(fake.reasoningPreview(JSON.parse(options.body)))
    : /^\/api\/output-styles(?:\?|$)/.test(path) ? json({ styles: STYLES, errors: [] })
    : /^\/api\/memory(?:\?|$)/.test(path) && (options.method || 'GET') === 'GET' ? json(fake.memory)
    : /^\/api\/settings\/project(?:\?|$)/.test(path) && (options.method || 'GET') === 'GET' ? json(fake.projectLayers)
    : /^\/api\/endpoints\/[^/]+\/checks$/.test(path) && (options.method || 'GET') === 'GET'
      ? json({ model: decodeURIComponent(path.split('/')[3]), saved: null, run: null, capabilities: [] })
    : /^\/api\/endpoints\/[^/]+\/state$/.test(path) ? json({ ready: null, task: null, error: 'Not in tests.' })
    // The Changes view's pull request chip: gh is not installed unless a test says otherwise.
    : /^\/api\/git\/pr(?:\?|$)/.test(path) ? json({ available: false, code: 'no_gh', reason: 'GitHub CLI (gh) is not installed or not on PATH.' })
    : /^\/api\/sessions\/[^/]+\/uploads$/.test(path) && options.method === 'POST' ? (() => {
      const name = decodeURIComponent(options.headers?.['X-File-Name'] || '')
      fake.uploads.push({ session: path.split('/')[3], name, file: options.body })
      return json(fake.uploadRecord(name, options.body))
    })() : undefined

  window.fetch = async (input, options = {}) => {
    const path = String(input)
    fake.calls.push({ path, method: options.method || 'GET', body: options.body, headers: options.headers || {} })
    const response = fake.override(path, options) || fake.handle(path, options) || defaults(path, options)
    if (response) return response
    throw new Error(`Unexpected test request: ${options.method || 'GET'} ${path}`)
  }
  window.WebSocket = TestSocket
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
    writeText: async text => { fake.clipboard.push(text) },
  } })
  window.confirm = message => { fake.confirms.push(message); return fake.confirmAnswer }
})()
