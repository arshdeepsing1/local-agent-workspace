// Installed before the UI loads (Playwright init script). Replaces the network
// with an in-page fake backend that tests configure and inspect:
//   fake.handle(path, options)   default responses for a test file (scenario)
//   fake.override(path, options) per-test responses; return undefined to fall through
//   fake.calls                   every request: { path, method, body, headers }
//   fake.sockets                 every WebSocket opened by the UI
//   fake.spy(name, impl)         callback props; arguments land in fake.spies[name]
//   fake.defer(name)             a pending response resolved later from the test
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
      fake.sockets.push(this)
    }
    close() { this.onclose?.() }
    open() { this.onopen?.() }
    emit(data) { this.onmessage?.({ data: JSON.stringify(data) }) }
  }

  const fake = window.fake = {
    json, deferred,
    calls: [],
    sockets: [],
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
      URL.revokeObjectURL = url => { fake.downloads.find(item => item.href === url).revoked = true; revoke(url) }
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

  window.fetch = async (input, options = {}) => {
    const path = String(input)
    fake.calls.push({ path, method: options.method || 'GET', body: options.body, headers: options.headers || {} })
    const response = fake.override(path, options) || fake.handle(path, options)
    if (response) return response
    throw new Error(`Unexpected test request: ${options.method || 'GET'} ${path}`)
  }
  window.WebSocket = TestSocket
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
    writeText: async text => { fake.clipboard.push(text) },
  } })
  window.confirm = message => { fake.confirms.push(message); return fake.confirmAnswer }
})()
