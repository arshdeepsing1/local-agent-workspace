import re

from playwright.sync_api import expect


def open_terminal(ui, setup="", session="'a'", workspace="/project"):
    ui.mount("components/TerminalPanel.js", f"{{ sessionId: {session}, workspace: '{workspace}' }}",
             scenario="terminal", setup=setup, clock=True)


def run(page, command="print progress"):
    page.get_by_role("textbox", name="Shell command").fill(command)
    page.get_by_role("button", name="Run", exact=True).click()


def count(ui, path):
    return ui.js(f"fake.requests('{path}').length")


def test_launches_and_shows_live_output_before_completion(ui, page):
    open_terminal(ui)
    run(page)
    expect(page.get_by_role("button", name="Run", exact=True)).to_be_disabled()
    assert ui.bodies("/api/jobs?session_id=a", "POST") == [
        {"command": "print progress", "timeout_seconds": 60, "max_output_bytes": 80000, "background": False}]
    ui.run("fake.jobs.set('job-1', { ...fake.jobs.get('job-1'), output: 'first line\\n', updated: 2 })")
    ui.tick(500)
    expect(page.get_by_label("Job output")).to_contain_text("first line")
    expect(page.get_by_role("status")).to_have_text("running")
    expect(page.get_by_role("button", name="Stop job print progress")).to_be_visible()
    ui.run("fake.jobs.set('job-1', { ...fake.jobs.get('job-1'), output: 'first line\\nfinished\\n', updated: 3, state: 'completed', exit_code: 0, truncated: true })")
    ui.tick(500)
    expect(page.get_by_label("Job output")).to_contain_text("finished")
    expect(page.get_by_role("status")).to_have_text("completed · exit 0 · output truncated")
    expect(page.get_by_role("button", name="Run", exact=True)).to_be_enabled()
    ui.tick(1000)
    calls = count(ui, "/api/jobs/job-1?session_id=a")
    ui.tick(3000)
    assert count(ui, "/api/jobs/job-1?session_id=a") == calls


def test_stop_ignores_a_late_pre_cancellation_poll(ui, page):
    open_terminal(ui, "fake.jobs.set('one', makeJob('one', { output: 'started' }))")
    expect(page.get_by_role("button", name="Stop job command-one")).to_be_visible()
    ui.run("const pending = fake.defer('poll'); fake.override = path => path === '/api/jobs/one?session_id=a' ? pending.promise : undefined")
    ui.tick(500)
    page.get_by_role("button", name="Stop job command-one").click()
    expect(page.get_by_role("status")).to_have_text("cancelled · exit -15")
    ui.run("fake.deferreds.poll.resolve(fake.json(makeJob('one', { output: 'stale running output' })))")
    ui.js("new Promise(done => queueMicrotask(done))")
    expect(page.get_by_role("status")).to_have_text("cancelled · exit -15")
    expect(page.get_by_label("Job output")).not_to_contain_text("stale running output")


def test_background_jobs_send_limits_and_history_survives_reopening(ui, page):
    open_terminal(ui)
    page.get_by_label("Timeout (seconds)").fill("300")
    page.get_by_label("Output limit (bytes)").fill("120000")
    page.get_by_label("Run in background").check()
    run(page, "background-one")
    expect(page.get_by_role("button", name=re.compile("^Stop job background-one"))).to_be_visible()
    expect(page.get_by_role("button", name="Run", exact=True)).to_be_enabled()
    run(page, "background-two")
    expect(page.get_by_role("button", name=re.compile("^Stop job "))).to_have_count(2)
    job = ui.js("fake.jobs.get('job-1')")
    assert (job["background"], job["timeout_seconds"], job["max_output_bytes"]) == (True, 300, 120000)
    ui.run("unmount()")
    calls = ui.js("fake.calls.length")
    ui.tick(5000)
    assert ui.js("fake.calls.length") == calls
    ui.page.evaluate("() => mount('components/TerminalPanel.js', () => ({ sessionId: 'a', workspace: '/project' }))")
    expect(page.get_by_role("listitem")).to_have_count(2)
    page.get_by_role("button", name=re.compile("Show job background-two · running")).click()
    expect(page.get_by_label("Job output")).to_contain_text("$ background-two")
    expect(page.get_by_text(re.compile("Background jobs continue after Stop response"))).to_be_visible()


def test_scope_change_ignores_old_polls_and_clears_state(ui, page):
    open_terminal(ui, """fake.jobs.set('one', makeJob('one', { output: 'session A output' }))
      fake.jobs.set('two', makeJob('two', { session_id: 'b', output: 'session B output', state: 'completed', exit_code: 0 }))""")
    expect(page.get_by_label("Job output")).to_contain_text("session A output")
    ui.run("const pending = fake.defer('poll'); fake.override = path => path === '/api/jobs/one?session_id=a' ? pending.promise : undefined")
    ui.tick(500)
    ui.rerender("{ sessionId: 'b', workspace: '/other' }")
    expect(page.get_by_label("Job output")).to_contain_text("session B output")
    ui.run("fake.deferreds.poll.resolve(fake.json(makeJob('one', { output: 'late A output', updated: 2 })))")
    ui.js("new Promise(done => queueMicrotask(done))")
    expect(page.get_by_label("Job output")).to_contain_text("session B output")
    expect(page.get_by_text(re.compile("late A output"))).to_have_count(0)
    ui.rerender("{ sessionId: null, workspace: '/global' }")
    expect(page.get_by_label("Job output")).to_have_text("Command output will appear here.")
    run(page, "global command")
    ui.wait("fake.requests('/api/jobs', 'POST').length === 1")


def test_old_selected_job_response_is_ignored_after_selecting_another(ui, page):
    open_terminal(ui, """fake.jobs.set('one', makeJob('one', { background: true, output: 'one output' }))
      fake.jobs.set('two', makeJob('two', { background: true, output: 'two output' }))""")
    expect(page.get_by_label("Job output")).to_contain_text("one output")
    ui.run("const pending = fake.defer('poll'); fake.override = path => path === '/api/jobs/one?session_id=a' ? pending.promise : undefined")
    ui.tick(500)
    page.get_by_role("button", name=re.compile("Show job command-two · running")).click()
    expect(page.get_by_label("Job output")).to_contain_text("two output")
    ui.run("fake.deferreds.poll.resolve(fake.json(makeJob('one', { output: 'late first job', updated: 2 })))")
    ui.js("new Promise(done => queueMicrotask(done))")
    expect(page.get_by_label("Job output")).to_contain_text("two output")
    expect(page.get_by_label("Job output")).not_to_contain_text("late first job")


def test_late_launch_does_not_move_into_another_scope(ui, page):
    open_terminal(ui, """const pending = fake.defer('launch')
      fake.override = (path, options) => path === '/api/jobs?session_id=a' && options.method === 'POST' ? pending.promise : undefined""")
    run(page, "slow launch")
    expect(page.get_by_role("button", name="Starting…")).to_be_visible()
    ui.rerender("{ sessionId: 'b', workspace: '/other' }")
    ui.run("fake.deferreds.launch.resolve(fake.json(makeJob('late', { command: 'slow launch', output: 'old scope output' })))")
    ui.js("new Promise(done => queueMicrotask(done))")
    expect(page.get_by_role("listitem")).to_have_count(0)
    expect(page.get_by_label("Job output")).to_have_text("Command output will appear here.")
    expect(page.get_by_role("textbox", name="Shell command")).to_have_value("")


def test_discovers_a_model_launched_job_while_idle(ui, page):
    open_terminal(ui)
    expect(page.get_by_text("No jobs in this workspace scope yet.")).to_be_visible()
    ui.run("fake.jobs.set('model-job', makeJob('model-job', { background: true, output: 'model command output' }))")
    ui.tick(3000)
    expect(page.get_by_role("button", name=re.compile("Show job command-model-job · running"))).to_be_visible()
    expect(page.get_by_label("Job output")).to_contain_text("model command output")
    calls = count(ui, "/api/jobs?session_id=a")
    ui.tick(1000)
    assert count(ui, "/api/jobs?session_id=a") == calls + 1


def test_polling_stops_on_error_until_refresh(ui, page):
    open_terminal(ui, "fake.override = () => fake.json({ detail: 'Server unavailable' }, 500)")
    expect(page.get_by_role("alert")).to_have_text("Server unavailable")
    calls = ui.js("fake.calls.length")
    ui.tick(3000)
    assert ui.js("fake.calls.length") == calls
    ui.run("fake.override = () => undefined")
    page.get_by_role("button", name="Refresh jobs").click()
    expect(page.get_by_role("alert")).to_have_count(0)
    assert ui.js("fake.calls.length") == calls + 1
