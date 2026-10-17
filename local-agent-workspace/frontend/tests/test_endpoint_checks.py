"""Endpoint checks: the Test model and Check endpoint capabilities… dialog (the estimated cost, nothing sent before Run,
progress, Stop and the capability table), the Settings buttons and the Send earlier thinking back setting, the Agent
tools Overview entry, an export with reasoning and the Request details line of a request that sent thinking back."""
import re

from playwright.sync_api import expect

OPUS = "databricks-claude-opus-5-5"
DIALOG = "components/EndpointChecksDialog.js"

# A plan like the server's (endpoint_probes.plan), for two checks; a run and the saved results it ends with.
FAKE = """
window.planFor = body => {
  const probes = [
    { id: 'basic', title: 'Smoke test and usage fields', question: 'Does a small streamed request work?', requests: [
      { id: 'basic', label: 'One short streamed request', estimated_input_tokens: 381, max_tokens: 2048, estimated_dbu: 0.607 }] },
    { id: 'cache', title: 'Prompt-cache markers and a cache read', question: 'Are cache_control markers accepted?', requests: [
      { id: 'cache_write', label: 'About 6,000 tokens of fixed text with cache markers', estimated_input_tokens: 8276, max_tokens: 2048, estimated_dbu: 1.176 },
      { id: 'cache_read', label: 'The same request again', estimated_input_tokens: 8276, max_tokens: 2048, estimated_dbu: 1.176 }] }]
  const chosen = probes.filter(probe => !body.probes || body.probes.includes(probe.id))
  const total = chosen.flatMap(probe => probe.requests).reduce((sum, request) => sum + request.estimated_dbu, 0)
  return { model: body.model, probes: chosen, total_dbu: Math.round(total * 1000) / 1000, priced: body.model !== 'databricks-gpt-oss-120b',
    max_output_tokens: 2048 * chosen.length, pricing_effective_at: '2026-10-13', plan_id: `plan-${(body.probes || ['all']).join('-')}` }
}
window.runOf = (status, states) => ({ id: 'run', model: 'databricks-claude-opus-5-5', kind: 'check', status, error: null, waiting: null,
  reported_dbu: status === 'running' ? 0.012 : 0.031, approved_dbu: 2.959,
  requests: [['basic', 'One short streamed request'], ['cache_write', 'About 6,000 tokens'], ['cache_read', 'The same request again']]
    .map(([id, label], index) => ({ id, probe: id.split('_')[0], label, state: states[index], http_status: states[index] === 'done' ? 200 : null,
      summary: states[index] === 'done' ? 'Finish reason stop' : null })) })
window.capabilityRows = [
  { id: 'streaming', capability: 'Streamed chat request', result: 'Answered “ready” in 1.2 s', source: 'probed 2026-10-16', status: 'accepted' },
  { id: 'cache_control', capability: 'Prompt-cache markers', result: 'All markers accepted', source: 'probed 2026-10-16', status: 'accepted' },
  { id: 'vision', capability: 'Image input (image_url)', result: 'Documented', source: 'documented', status: 'documented' }]
window.savedChecks = { checked: '2026-10-16T08:00:00Z', plan_id: 'plan-all', calls: [], results: {
  basic: { status: 'accepted', summary: 'Answered “ready” in 1.2 s (first byte after 0.4 s), finish reason stop.', checked: '2026-10-16T08:00:00Z' },
  cache: { status: 'accepted', summary: 'All cache markers were accepted.', checked: '2026-10-16T08:00:00Z' } } }
fake.checks = { model: 'databricks-claude-opus-5-5', saved: null, run: null, capabilities: [] }
fake.handle = (path, options) => {
  const method = options.method || 'GET'
  if (path === '/api/endpoints/plan' && method === 'POST') return fake.json(planFor(JSON.parse(options.body)))
  if (/^\\/api\\/endpoints\\/[^/]+\\/checks$/.test(path)) return fake.json(fake.checks)
  if (/^\\/api\\/endpoints\\/[^/]+\\/state$/.test(path)) return fake.json({ ready: 'READY', task: 'llm/v1/chat', error: null })
  if ((path === '/api/endpoints/run' || path === '/api/endpoints/test') && method === 'POST') {
    fake.checks = { ...fake.checks, run: runOf('running', ['done', 'running', 'pending']) }
    return fake.json(fake.checks.run, 202)
  }
  if (path === '/api/endpoints/run/stop' && method === 'POST') {
    fake.checks = { ...fake.checks, run: runOf('stopped', ['done', 'stopped', 'not_sent']) }
    return fake.json({ run: fake.checks.run })
  }
}
"""


def open_dialog(ui, mode="check", model=OPUS, setup=""):
    ui.mount(DIALOG, f"{{ model: '{model}', mode: '{mode}', onClose: fake.spy('onClose') }}", setup=FAKE + setup)
    return ui.page.get_by_role("dialog")


def run_calls(ui):
    return [call for call in ui.js("fake.calls") if call["path"] in ("/api/endpoints/run", "/api/endpoints/test")]


def test_the_dialog_lists_each_request_and_the_total_and_cancel_sends_nothing(ui, page):
    dialog = open_dialog(ui)
    expect(dialog).to_have_accessible_name("Check endpoint capabilities")
    requests = dialog.get_by_role("list", name="Requests")
    expect(requests).to_contain_text("One short streamed request")
    expect(requests).to_contain_text("max 2,048 output tokens · at most 0.61 DBU")
    expect(requests.locator(".endpoint-requests li")).to_have_count(3)
    expect(dialog.get_by_text("At most 3.0 DBU at list prices (an estimate; a rejected request is not billed).")).to_be_visible()
    expect(dialog.get_by_text("Serving state: READY (llm/v1/chat)")).to_be_visible()
    expect(dialog.get_by_role("button", name="Run (about 3.0 DBU)")).to_be_enabled()
    dialog.get_by_role("button", name="Cancel").click()
    assert len(ui.spy("onClose")) == 1 and run_calls(ui) == []


def test_unchecking_a_check_asks_for_a_new_plan_and_run_posts_its_plan_id(ui, page):
    dialog = open_dialog(ui)
    expect(dialog.get_by_role("button", name="Run (about 3.0 DBU)")).to_be_visible()
    dialog.get_by_role("checkbox", name="Prompt-cache markers and a cache read").uncheck()
    expect(dialog.get_by_role("button", name="Run (about 0.61 DBU)")).to_be_visible()
    assert ui.bodies("/api/endpoints/plan")[-1] == {"model": OPUS, "probes": ["basic"]}
    expect(dialog.get_by_role("checkbox", name="Smoke test and usage fields")).to_be_checked()
    dialog.get_by_role("checkbox", name="Smoke test and usage fields").click()  # The last check stays chosen.
    expect(dialog.get_by_role("checkbox", name="Smoke test and usage fields")).to_be_checked()
    dialog.get_by_role("button", name="Run (about 0.61 DBU)").click()
    expect(dialog.get_by_role("button", name="Stop")).to_be_visible()
    assert ui.bodies("/api/endpoints/run") == [{"model": OPUS, "plan_id": "plan-basic", "approved_dbu": 0.607, "probes": ["basic"]}]


def test_run_waits_for_the_estimate_of_a_changed_choice(ui, page):
    dialog = open_dialog(ui)
    expect(dialog.get_by_role("button", name="Run (about 3.0 DBU)")).to_be_enabled()
    ui.run("fake.override = (path, options) => path === '/api/endpoints/plan' && JSON.parse(options.body).probes"
           " ? fake.defer('plan').promise : undefined")
    dialog.get_by_role("checkbox", name="Prompt-cache markers and a cache read").uncheck()
    # The shown estimate is for the earlier choice: Run is not offered until the new one comes.
    expect(dialog.get_by_role("button", name="Run (updating the estimate…)")).to_be_disabled()
    ui.run("fake.deferreds.plan.resolve(fake.json(planFor({ model: 'databricks-claude-opus-5-5', probes: ['basic'] })))")
    dialog.get_by_role("button", name="Run (about 0.61 DBU)").click()
    expect(dialog.get_by_role("button", name="Stop")).to_be_visible()
    assert ui.bodies("/api/endpoints/run") == [{"model": OPUS, "plan_id": "plan-basic", "approved_dbu": 0.607, "probes": ["basic"]}]


def test_progress_polls_until_the_run_ends_and_the_table_renders(ui, page):
    dialog = open_dialog(ui)
    dialog.get_by_role("button", name="Run (about 3.0 DBU)").click()
    progress = dialog.get_by_label("Progress")
    expect(progress).to_contain_text("Running · 0.012 DBU reported so far")
    expect(progress.locator("li[data-state=running]")).to_contain_text("Sending…")
    assert ui.bodies("/api/endpoints/run") == [{"model": OPUS, "plan_id": "plan-all", "approved_dbu": 2.959}]
    ui.run("fake.checks = { ...fake.checks, saved: savedChecks, capabilities: capabilityRows, run: runOf('completed', ['done', 'done', 'done']) }")
    expect(progress).to_contain_text("Finished · 0.031 DBU reported so far")
    table = dialog.get_by_role("table", name="Endpoint capabilities")
    expect(table.locator("tbody tr")).to_have_count(3)
    expect(table.locator("tbody tr").first).to_contain_text("Streamed chat request")
    expect(table.locator("tbody tr").first).to_contain_text("probed 2026-10-16")
    expect(dialog.get_by_text("Results (last check 2026-10-16)")).to_be_visible()
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_visible()
    expect(dialog.get_by_role("button", name="Run (about 3.0 DBU)")).to_be_visible()  # Another run asks again.


def test_stop_ends_a_running_check(ui, page):
    dialog = open_dialog(ui)
    dialog.get_by_role("button", name="Run (about 3.0 DBU)").click()
    dialog.get_by_role("button", name="Stop").click()
    expect(dialog.get_by_label("Progress")).to_contain_text("Stopped")
    expect(dialog.get_by_label("Progress").locator("li[data-state=not_sent]")).to_contain_text("Not sent")
    assert ui.requests("/api/endpoints/run/stop", "POST")


def test_test_model_sends_only_the_smoke_check_and_shows_its_result_line(ui, page):
    dialog = open_dialog(ui, mode="test")
    expect(dialog).to_have_accessible_name("Test model")
    expect(dialog.get_by_role("list", name="Requests").locator(".endpoint-requests li")).to_have_count(1)
    expect(dialog.get_by_role("checkbox")).to_have_count(0)
    dialog.get_by_role("button", name="Run (about 0.61 DBU)").click()
    assert ui.bodies("/api/endpoints/test") == [{"model": OPUS, "plan_id": "plan-basic", "approved_dbu": 0.607}]
    ui.run("fake.checks = { ...fake.checks, saved: savedChecks, run: { ...runOf('completed', ['done']), kind: 'test', requests: [runOf('completed', ['done']).requests[0]] } }")
    expect(dialog.get_by_text(re.compile(r"^Works: Answered “ready” in 1\.2 s .* \(2026-10-16\)$"))).to_be_visible()


def test_an_unpriced_endpoint_names_its_token_caps(ui, page):
    dialog = open_dialog(ui, model="databricks-gpt-oss-120b")
    expect(dialog.get_by_text("No price data for this endpoint: up to 4,096 output tokens in all.")).to_be_visible()
    expect(dialog.get_by_role("button", name="Run (no price data)")).to_be_visible()


def test_a_refused_run_shows_the_reason(ui, page):
    dialog = open_dialog(ui, setup="""fake.override = (path, options) => path === '/api/endpoints/run'
      ? fake.json({ detail: 'The checks changed since you saw their estimate. Review the new estimate; nothing was sent.' }, 409) : undefined""")
    dialog.get_by_role("button", name="Run (about 3.0 DBU)").click()
    expect(dialog.get_by_role("alert")).to_contain_text("nothing was sent")
    expect(dialog.get_by_role("button", name="Run (about 3.0 DBU)")).to_be_enabled()


# Settings, Overview, export and Request details

def open_settings(ui, page, setup=""):
    ui.app(scenario="workspace", setup=f"fake.settings = {{ ...fake.settings, model: '{OPUS}' }};" + setup)
    expect(page.get_by_role("combobox", name="Model")).to_be_visible()
    page.get_by_role("button", name="Settings", exact=True).click()
    return page.get_by_role("dialog", name="Settings")


def test_settings_opens_the_checks_for_the_endpoint_in_the_form(ui, page):
    checks = """fake.override = (path, options) => path === '/api/endpoints/plan'
      ? fake.json({ model: JSON.parse(options.body).model, probes: [{ id: 'basic', title: 'Smoke test and usage fields', question: 'Works?',
          requests: [{ id: 'basic', label: 'One short streamed request', estimated_input_tokens: 381, max_tokens: 2048, estimated_dbu: 0.607 }] }],
          total_dbu: 0.607, priced: true, max_output_tokens: 2048, pricing_effective_at: '2026-10-13', plan_id: 'p' })
      : path === '/api/endpoints/databricks-claude-opus-5-5/checks' ? fake.json({ model: 'databricks-claude-opus-5-5', run: null,
          saved: { checked: '2026-10-16T08:00:00Z', results: { basic: { status: 'accepted', summary: 'Answered “ready” in 1.2 s.', checked: '2026-10-16T08:00:00Z' } } },
          capabilities: [{ id: 'replay', capability: 'Signed thinking sent back', result: 'Rejected', source: 'probed 2026-10-16', status: 'rejected' }] })
      : undefined"""
    settings = open_settings(ui, page, checks)
    group = settings.get_by_role("group", name="Model and context")
    expect(group.get_by_text("Test model: Works: Answered “ready” in 1.2 s. (2026-10-16)")).to_be_visible()
    group.get_by_role("button", name="Test model").click()
    checks_dialog = page.get_by_role("dialog", name="Test model")
    expect(checks_dialog.get_by_role("button", name="Run (about 0.61 DBU)")).to_be_visible()
    assert ui.bodies("/api/endpoints/plan")[-1] == {"model": OPUS, "probes": ["basic"]}
    checks_dialog.get_by_role("button", name="Cancel").click()
    expect(page.get_by_role("dialog", name="Test model")).to_have_count(0)
    expect(settings).to_be_visible()  # Settings stays open with the form as it was.
    group.get_by_role("button", name="Check endpoint capabilities…").click()
    expect(page.get_by_role("dialog", name="Check endpoint capabilities")).to_be_visible()
    page.get_by_role("dialog", name="Check endpoint capabilities").get_by_role("button", name="Cancel").click()
    reasoning = settings.get_by_role("group", name="Reasoning", exact=True)
    expect(reasoning.get_by_text(f"Endpoint check for {OPUS}: Rejected (probed 2026-10-16).")).to_be_visible()
    assert ui.requests("/api/endpoints/run") == [] and ui.requests("/api/endpoints/test") == []


def test_send_earlier_thinking_back_is_off_by_default_and_saved(ui, page):
    settings = open_settings(ui, page)
    reasoning = settings.get_by_role("group", name="Reasoning", exact=True)
    toggle = reasoning.get_by_role("checkbox", name="Send earlier thinking back (experimental)")
    expect(toggle).not_to_be_checked()
    expect(reasoning.get_by_text(re.compile("run Check endpoint capabilities… first"))).to_be_visible()
    toggle.check()
    reasoning.get_by_role("combobox", name="Request format").select_option("anthropic_budget")
    settings.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    body = ui.bodies("/api/settings", "PUT")[0]
    assert body["thinking_replay"] is True and body["reasoning_format"] == "anthropic_budget"


def test_the_overview_names_the_chats_endpoint_checks(ui, page):
    setup = """fake.override = path => path === '/api/endpoints/test-model/checks' ? fake.json({ model: 'test-model', run: null,
        saved: { checked: '2026-10-16T08:00:00Z', results: { basic: { status: 'accepted' }, image: { status: 'rejected' } } },
        capabilities: [{ id: 'vision', capability: 'Image input (image_url)', result: 'Rejected', source: 'probed 2026-10-16', status: 'rejected' }] })
      : undefined"""
    ui.mount("components/AgentToolsDialog.js", "{ session: session(), onClose: fake.spy('onClose'), onSelectSession: fake.spy('onSelectSession'), onError: fake.spy('onError') }",
             scenario="agent_tools", setup="localStorage.removeItem('agent-tools-tab');" + setup)
    overview = page.get_by_role("region", name="Overview")
    row = overview.locator(".overview-row").filter(has=page.locator("dt", has_text=re.compile("^Endpoint checks$")))
    expect(row.locator(".overview-value")).to_have_text("Checked 2026-10-16: 1 of 2 checks accepted")
    overview.get_by_text("Endpoint capabilities · test-model").click()
    expect(overview.get_by_role("table", name="Endpoint capabilities of test-model")).to_contain_text("Image input (image_url)")


def test_an_unchecked_endpoint_says_where_to_check_it(ui, page):
    ui.mount("components/AgentToolsDialog.js", "{ session: session(), onClose: fake.spy('onClose'), onSelectSession: fake.spy('onSelectSession'), onError: fake.spy('onError') }",
             scenario="agent_tools", setup="localStorage.removeItem('agent-tools-tab')")
    row = page.locator(".overview-row").filter(has=page.locator("dt", has_text=re.compile("^Endpoint checks$")))
    expect(row.locator(".overview-value")).to_have_text("Not checked yet (Settings → Model and context → Check endpoint capabilities…)")
    expect(page.locator(".overview-capabilities")).to_have_count(0)


def test_an_export_with_reasoning_asks_for_it(ui, page):
    ui.mount("components/ConversationTransfer.js", "{ sessionId: 'source', workspace: '/project', onSelectSession: fake.spy('select'), onError: fake.spy('error') }",
             setup="""fake.captureDownloads(); fake.handle = path => path.startsWith('/api/sessions/source/export')
               ? fake.json({ format: 'local-agent-workspace.conversations', version: 1, root_session_id: 'source', exported_at: 1, sessions: [] }) : undefined""")
    page.get_by_role("checkbox", name=re.compile("^Export with reasoning")).check()
    page.get_by_role("button", name="Export conversation").click()
    expect(page.get_by_role("status")).to_have_text("Conversation file exported.")
    assert ui.js("fake.calls.map(call => call.path)") == ["/api/sessions/source/export?include_children=false&with_reasoning=true"]


def test_request_details_name_the_earlier_thinking_a_request_sent(ui, page):
    ui.mount("components/RequestDetails.js", """{ info: { model: 'databricks-claude-sonnet-4-5', status: 'completed', finish_reason: 'stop',
      http_status: 200, reasoning_replayed: 2, usage: { input_tokens: 10, output_tokens: 5 },
      reasoning: { effort: 'high', format: 'anthropic_budget', fields: { thinking: { type: 'enabled', budget_tokens: 8192 } },
        effort_field: 'thinking.budget_tokens', sent_effort: 'high' } } }""")
    page.get_by_text("Request details · completed").click()
    details = page.locator(".request-details")
    expect(details).to_contain_text("Earlier thinking sent2 earlier replies of this turn")
    expect(details).to_contain_text("High (thinking.budget_tokens)")
    expect(details).to_contain_text("In output tokens (not reported separately)")
