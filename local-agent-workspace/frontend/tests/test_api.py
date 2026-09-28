"""The API client's token refresh and no-replay rules (static/js/api.js)."""

LOAD = "window.client = await import('/static/js/api.js')"


def run(ui, script: str):
    ui.start()
    return ui.js(f"(async () => {{ {LOAD}; {script} }})()")


def test_stale_token_refreshes_once_and_action_runs_once(ui):
    result = run(ui, """
      let executions = 0
      fake.handle = (url, options) => {
        if (url === '/api/bootstrap') return fake.json({ token: 'fresh' })
        if (options.headers['X-Local-Token'] === 'old') return fake.json({ code: 'reconnect_required', detail: 'Reconnect' }, 403)
        executions++
        if (options.headers['X-Local-Token'] !== 'fresh' || options.body !== JSON.stringify({ text: 'hello' })) throw new Error('bad retry')
        return fake.json({ ok: true })
      }
      client.setToken('old')
      const value = await client.api('/sessions/test/messages', 'POST', { text: 'hello' })
      return { value, executions, calls: fake.calls.map(call => call.path) }""")
    assert result == {"value": {"ok": True}, "executions": 1,
                      "calls": ["/api/sessions/test/messages", "/api/bootstrap", "/api/sessions/test/messages"]}


def test_rejections_and_server_failures_are_not_replayed(ui):
    result = run(ui, """
      const outcomes = []
      for (const status of [403, 500]) {
        fake.calls.length = 0
        fake.handle = () => fake.json({ detail: 'Rejected' }, status)
        const error = await client.api('/command', 'POST', { text: 'do something' }).catch(error => error.message)
        outcomes.push([error, fake.calls.length])
      }
      return outcomes""")
    assert result == [["Rejected", 1], ["Rejected", 1]]


def test_connection_loss_after_sending_is_not_replayed(ui):
    result = run(ui, """
      fake.handle = () => { throw new TypeError('Connection lost') }
      const error = await client.api('/command', 'POST', { text: 'do something' }).catch(error => error.message)
      return [error, fake.calls.length]""")
    assert result == ["Connection lost", 1]


def test_raw_import_json_is_sent_unchanged_through_token_refresh(ui):
    result = run(ui, """
      const body = '{"version":1,"version":2,"large":9007199254740993,"invalid":NaN}'
      const sent = []
      fake.handle = (url, options) => {
        if (url === '/api/bootstrap') return fake.json({ token: 'fresh-import' })
        sent.push(options.body)
        if (options.headers['X-Local-Token'] === 'old-import') return fake.json({ code: 'reconnect_required' }, 403)
        return fake.json({ detail: options.headers['X-Local-Token'] === 'fresh-import' ? 'Invalid bundle' : 'wrong token' }, 400)
      }
      client.setToken('old-import')
      const error = await client.apiJsonText('/sessions/import?workspace=%2Fproject', body).catch(error => error.message)
      return { error, same: sent.length === 2 && sent.every(text => text === body) }""")
    assert result == {"error": "Invalid bundle", "same": True}
