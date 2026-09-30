import json
import re

import pytest
from playwright.sync_api import expect

SETUP = """
window.makeSession = (events, overrides = {}) => ({
  id: 'parent', title: 'Parent task', workspace: '/project', model: 'test-model', status: 'idle',
  events, updated: 1, permission_mode: 'manual', allowed_directories: [], ...overrides,
})
"""


def show(ui, events: str, extra: str = "", setup: str = ""):
    ui.mount("components/Conversation.js", f"{{ session: makeSession({events}), onError: fake.spy('onError'){extra} }}",
             setup=SETUP + setup)


def details_for(page, summary_text):
    return page.locator("details").filter(has=page.get_by_text(summary_text, exact=True)).last


def definition(scope, label):
    return scope.locator(f'xpath=.//dt[normalize-space()="{label}"]/following-sibling::dd[1]')


def test_delegation_progress_updates_without_expanding(ui, page):
    event = """{ id: 'delegate', type: 'tool', name: 'delegate_task', input: { task: 'Review the parser' },
      state: 'running', child_session_id: 'child-1', delegation: { status: 'running', completed_tools: 0 } }"""
    show(ui, f"[{event}]", ", onSelectSession: fake.spy('select')")
    heading = page.get_by_role("button", name="delegate task Review the parser")
    expect(heading).to_have_attribute("aria-expanded", "false")
    expect(page.get_by_label("Subagent progress")).to_contain_text("Subagent: running")
    ui.rerender(f"""{{ session: makeSession([{{ ...{event}, delegation: {{ status: 'awaiting_approval', completed_tools: 1,
      last_tool: 'run_command' }} }}]), onError: fake.spy('onError'), onSelectSession: fake.spy('select') }}""")
    expect(page.get_by_text("Subagent: Waiting for approval")).to_be_visible()
    expect(page.get_by_text("1 completed action", exact=True)).to_be_visible()
    expect(page.get_by_text("Last tool: run command")).to_be_visible()
    expect(page.get_by_label("Input")).to_have_count(0)
    page.get_by_role("button", name="Open subagent").click()
    assert ui.spy("select") == [["child-1"]]
    ui.rerender(f"""{{ session: makeSession([{{ ...{event}, state: 'completed', delegation: {{ status: 'failed', completed_tools: 2,
      last_tool: 'read_file', terminal_reason: 'step_limit' }} }}]), onError: fake.spy('onError'), onSelectSession: fake.spy('select') }}""")
    expect(page.get_by_text("Subagent: failed")).to_be_visible()
    expect(page.get_by_text("Reason: step limit")).to_be_visible()
    expect(page.get_by_text("2 completed actions")).to_be_visible()
    expect(heading).to_have_attribute("aria-expanded", "false")


def test_only_the_delegated_prompt_shows_provenance(ui, page):
    ui.mount("components/Conversation.js", """{ onError: fake.spy('onError'), session: makeSession([
      { id: 'delegated', type: 'user', text: 'Review source', origin: {
        kind: 'delegated', parent_session_id: 'parent', parent_event_id: 'delegate-1', parent_call_id: 'call-1' } },
      { id: 'human', type: 'user', text: 'Check one more thing' },
    ], { id: 'child', parent_session_id: 'parent', is_subagent: true }) }""", setup=SETUP)
    expect(page.get_by_text("Delegated by parent conversation")).to_have_count(1)
    expect(page.locator(".user-message").nth(1)).to_have_text("Check one more thing")


def test_copies_raw_user_and_assistant_text(ui, page):
    show(ui, """[
      { id: 'user', type: 'user', text: 'Review this path:\\n/project/file.ts', child_session_id: 'child' },
      { id: 'empty-user', type: 'user', text: '' },
      { id: 'reply', type: 'assistant', text: 'I reviewed **the file**.' },
    ]""", ", onSelectSession: fake.spy('select')")
    expect(page.get_by_role("button", name="Copy message")).to_have_count(1)
    page.get_by_role("button", name="Copy message").click()
    ui.wait("fake.clipboard.length === 1")
    page.get_by_role("button", name="Copy response").click()
    ui.wait("fake.clipboard.length === 2")
    assert ui.js("fake.clipboard") == ["Review this path:\n/project/file.ts", "I reviewed **the file**."]
    page.get_by_role("button", name="Open subagent").click()
    assert ui.spy("select") == [["child"]]


def test_reasoning_is_collapsed_and_separate_from_the_answer(ui, page):
    show(ui, """[{ id: 'reply', type: 'assistant', text: 'The answer is ready.', reasoning_summary: 'Provider considered **two approaches**.' }]""")
    summary = page.get_by_text("Provider reasoning summary")
    details = page.locator("details.reasoning-summary")
    expect(details).not_to_have_attribute("open", "")
    expect(details.get_by_text("The answer is ready.")).to_have_count(0)
    summary.click()
    expect(details).to_have_attribute("open", "")
    expect(details.get_by_text("Supplied by the model endpoint.")).to_be_visible()
    expect(details.locator("strong", has_text="two approaches")).to_be_visible()
    expect(page.get_by_text("The answer is ready.")).to_have_count(1)
    summary.click()
    expect(details).not_to_have_attribute("open", "")
    expect(page.get_by_text("The answer is ready.")).to_be_visible()


def test_ordinary_messages_have_no_invented_reasoning_or_activity(ui, page):
    show(ui, """[{ id: 'user', type: 'user', text: 'Please explain the parser.' },
      { id: 'reply', type: 'assistant', text: 'I considered two approaches. Here is the answer.' }]""")
    expect(page.get_by_text("I considered two approaches. Here is the answer.")).to_be_visible()
    expect(page.get_by_text("Provider reasoning summary")).to_have_count(0)
    expect(page.get_by_text(re.compile("Activity ·"))).to_have_count(0)
    expect(page.get_by_text("Summary display limit reached.")).to_have_count(0)
    expect(page.get_by_text(re.compile("Request details"))).to_have_count(0)


def test_markdown_renders_gfm_and_shows_raw_html_as_text(ui, page):
    show(ui, """[{ id: 'reply', type: 'assistant', text: '| a | b |\\n|---|---|\\n| 1 | ~~2~~ |\\n\\n<img src=x onerror="window.pwned=1"> [ok](https://example.com) [bad](javascript:alert(1))' }]""")
    expect(page.locator(".markdown table td del")).to_have_text("2")
    expect(page.locator(".markdown img")).to_have_count(0)
    expect(page.locator(".markdown")).to_contain_text('<img src=x onerror="window.pwned=1">')
    expect(page.get_by_role("link", name="ok")).to_have_attribute("href", "https://example.com")
    expect(page.locator(".markdown a", has_text="bad")).not_to_have_attribute("href", re.compile("javascript"))
    assert ui.js("window.pwned === undefined")


def test_usage_snapshot_for_event_without_text(ui, page):
    show(ui, """[{ id: 'request', type: 'assistant', request_info: {
      model: 'databricks-test-model', status: 'completed', finish_reason: 'tool_calls', http_status: 200,
      usage: { input_tokens: 1000, output_tokens: 200, total_tokens: 1200, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, reasoning_tokens: 50 } } }]""")
    summary = page.get_by_text("Request details · completed")
    details = page.locator("details.request-details")
    expect(details).not_to_have_attribute("open", "")
    expect(page.get_by_role("button", name="Copy response")).to_have_count(0)
    summary.click()
    expect(details).to_have_attribute("open", "")
    expect(definition(details, "Model endpoint")).to_have_text("databricks-test-model")
    expect(definition(details, "Finish reason")).to_have_text("tool_calls")
    expect(definition(details, "HTTP status")).to_have_text("200")
    for label, value in [("Input tokens", "1,000"), ("Output tokens", "200"), ("Total tokens", "1,200"),
                         ("Cache read input tokens", "0"), ("Cache creation input tokens", "0"), ("Reasoning tokens", "50")]:
        expect(definition(details, label)).to_have_text(value)
    expect(details.get_by_text(re.compile("excludes title and summary calls"))).to_be_visible()
    expect(details.get_by_text("Usage received before completion may be partial.")).to_have_count(0)
    expect(details).not_to_contain_text("%")
    summary.click()
    expect(details).not_to_have_attribute("open", "")


def test_request_metadata_updates_in_place(ui, page):
    running = "{ id: 'request', type: 'assistant', request_info: { model: 'model-a', status: 'running' } }"
    show(ui, f"[{running}]")
    page.get_by_text("Request details · running").click()
    details = page.locator("details.request-details")
    expect(definition(details, "Input tokens")).to_have_text("Unavailable")
    ui.rerender(f"""{{ onError: fake.spy('onError'), session: makeSession([{{ ...{running}, request_info: {{ model: 'model-a',
      status: 'completed', usage: {{ input_tokens: 12, output_tokens: 0 }} }} }}]) }}""")
    expect(details).to_have_attribute("open", "")
    expect(page.get_by_text("Request details · completed")).to_be_visible()
    expect(definition(details, "Input tokens")).to_have_text("12")
    expect(definition(details, "Output tokens")).to_have_text("0")
    expect(definition(details, "Total tokens")).to_have_text("Unavailable")
    expect(definition(details, "Finish reason")).to_have_text("Unavailable")
    expect(definition(details, "HTTP status")).to_have_text("Unavailable")
    expect(details.get_by_text("Error category")).to_have_count(0)


@pytest.mark.parametrize("error_kind", ["authentication", "permission", "rate_limit", "invalid_request", "server", "network",
                                        "output_limit", "invalid_tool_arguments", "incomplete_response", "invalid_response", "unknown"])
def test_request_error_category(ui, page, error_kind):
    show(ui, f"""[{{ id: 'failed', type: 'assistant', request_info: {{ model: 'model-b', status: 'error', error_kind: '{error_kind}', http_status: 429 }} }}]""")
    page.get_by_text("Request details · error").click()
    expect(definition(page, "Error category")).to_have_text(error_kind.replace("_", " "))
    expect(definition(page, "HTTP status")).to_have_text("429")
    expect(definition(page, "Input tokens")).to_have_text("Unavailable")
    expect(page.get_by_role("button", name="Copy response")).to_have_count(0)


def test_cancelled_and_interrupted_snapshots(ui, page):
    show(ui, """[
      { id: 'cancelled', type: 'assistant', request_info: { model: 'model-a', status: 'cancelled', usage: { total_tokens: 15 } } },
      { id: 'interrupted', type: 'assistant', request_info: { model: 'model-b', status: 'interrupted' } },
      { id: 'legacy-empty', type: 'assistant' },
    ]""")
    expect(page.get_by_text(re.compile("Request details ·"))).to_have_count(2)
    cancelled = details_for(page, "Request details · cancelled")
    interrupted = details_for(page, "Request details · interrupted")
    cancelled.locator("summary").click()
    interrupted.locator("summary").click()
    expect(definition(cancelled, "Model endpoint")).to_have_text("model-a")
    expect(definition(cancelled, "Total tokens")).to_have_text("15")
    expect(cancelled.get_by_text("Usage received before completion may be partial.")).to_be_visible()
    expect(definition(interrupted, "Model endpoint")).to_have_text("model-b")
    expect(definition(interrupted, "Total tokens")).to_have_text("Unavailable")
    expect(interrupted.get_by_text("Usage received before completion may be partial.")).to_have_count(0)


def test_usage_before_failure_is_marked_partial(ui, page):
    show(ui, """[{ id: 'failed-stream', type: 'assistant', request_info: { model: 'model-a', status: 'error',
      error_kind: 'incomplete_response', usage: { output_tokens: 0 } } }]""")
    page.get_by_text("Request details · error").click()
    expect(page.get_by_text("Usage received before completion may be partial.")).to_be_visible()
    expect(definition(page, "Output tokens")).to_have_text("0")


@pytest.mark.parametrize("truncated", [True, False])
def test_reasoning_display_limit_notice(ui, page, truncated):
    show(ui, f"""[{{ id: 'reply', type: 'assistant', reasoning_summary: 'A provider-supplied partial summary.',
      reasoning_truncated: {json.dumps(truncated)} }}]""")
    summary = page.get_by_text("Provider reasoning summary")
    expect(page.locator("details.reasoning-summary")).not_to_have_attribute("open", "")
    summary.click()
    expect(page.get_by_text("A provider-supplied partial summary.")).to_be_visible()
    expect(page.get_by_text("Summary display limit reached.")).to_have_count(1 if truncated else 0)


def test_activity_summary_keeps_states_and_approval(ui, page):
    states = ["running", "pending", "completed", "rejected", "cancelled", "error"]
    events = "[" + ",".join(f"{{ id: 'tool-{i}', type: 'tool', name: 'action_{i}', state: '{state}', input: {{ path: 'file-{i}' }} }}"
                            for i, state in enumerate(states)) + "]"
    show(ui, events)
    details = page.locator("details.activity-summary")
    expect(details).not_to_have_attribute("open", "")
    expect(page.get_by_role("button", name="Approve")).to_be_visible()
    page.get_by_text("Activity · 6 actions").click()
    expect(details).to_have_attribute("open", "")
    rows = details.get_by_role("listitem")
    expect(rows).to_have_count(len(states))
    for index, state in enumerate(states):
        expect(rows.nth(index).get_by_text(f"action {index}", exact=True)).to_be_visible()
        expect(rows.nth(index).get_by_text(f"Action: {state}")).to_be_visible()
    ui.rerender(f"{{ onError: fake.spy('onError'), session: makeSession({events}.map(event => ({{ ...event, state: 'completed' }}))) }}")
    expect(details.get_by_text("Action: completed")).to_have_count(6)
    expect(page.get_by_role("button", name="Approve")).to_have_count(0)
    page.get_by_text("Activity · 6 actions").click()
    expect(details).not_to_have_attribute("open", "")


def test_distinct_failed_tool_diagnostics(ui, page):
    show(ui, """[
      { id: 'file', type: 'tool', name: 'search_files', state: 'error', input: { path: 'run.log' }, output: 'Search path must be a regular file or directory.' },
      { id: 'timeout', type: 'tool', name: 'search_files', state: 'error', input: { path: '/large' }, output: 'File query time limit reached; narrow the path, glob, or line range.' },
      { id: 'regex', type: 'tool', name: 'search_files', state: 'error', input: { query: 'connect(' }, output: 'Invalid regular expression: missing ).' },
    ]""")
    expect(page.get_by_text("Search path must be a regular file or directory.")).to_be_visible()
    expect(page.get_by_text("File query time limit reached; narrow the path, glob, or line range.")).to_be_visible()
    expect(page.get_by_text("Invalid regular expression: missing ).")).to_be_visible()
    expect(page.get_by_text("Tool returned an error")).to_have_count(0)


def test_task_actions_are_named_with_task_status(ui, page):
    show(ui, """[
      { id: 'create', type: 'tool', name: 'create_task', state: 'completed', input: { title: 'Write exporter script' },
        output: JSON.stringify({ id: 'task-1', title: 'Write exporter script', status: 'pending' }) },
      { id: 'create-other', type: 'tool', name: 'create_task', state: 'completed', input: { title: 'Render portable HTML' },
        output: JSON.stringify({ id: 'task-2', title: 'Render portable HTML', status: 'pending' }) },
      { id: 'update', type: 'tool', name: 'update_task', state: 'completed', input: { task_id: 'task-1', status: 'in_progress' },
        output: JSON.stringify({ id: 'task-1', title: 'Write exporter script', status: 'in_progress' }) },
    ]""")
    create = page.get_by_role("button", name="create task Write exporter script Task: pending", exact=True)
    update = page.get_by_role("button", name="update task Write exporter script Task: in progress", exact=True)
    expect(create).to_have_attribute("aria-expanded", "false")
    expect(update).to_have_attribute("aria-expanded", "false")
    expect(page.get_by_role("button", name="create task Render portable HTML Task: pending", exact=True)).to_be_visible()
    page.get_by_text("Activity · 3 actions").click()
    row = page.get_by_role("listitem").nth(2)
    expect(row).to_contain_text("Write exporter script")
    expect(row.get_by_text("Task: in progress")).to_be_visible()
    expect(row.get_by_text("Action: completed")).to_be_visible()


def test_task_updates_do_not_claim_requested_status(ui, page):
    create = """{ id: 'create', type: 'tool', name: 'create_task', state: 'completed', input: { title: 'Verify exported HTML' },
      output: JSON.stringify({ id: 'task-1', status: 'pending' }) }"""
    update = "{ id: 'update', type: 'tool', name: 'update_task', input: { task_id: 'task-1', status: 'completed' } }"
    show(ui, f"[{create}, {{ ...{update}, state: 'running', output: '' }}]")
    expect(page.get_by_role("button", name="update task Verify exported HTML", exact=True)).to_be_visible()
    expect(page.get_by_text("Task: completed")).to_have_count(0)
    ui.rerender(f"{{ onError: fake.spy('onError'), session: makeSession([{create}, {{ ...{update}, state: 'error', output: 'Complete dependencies first.' }}]) }}")
    page.get_by_role("button", name="update task Verify exported HTML", exact=True).click()
    expect(page.get_by_label("Output")).to_have_text("Complete dependencies first.")
    expect(page.get_by_text("Task: completed")).to_have_count(0)


def test_command_and_options_show_only_after_expansion(ui, page):
    command = "python - <<'PY'\n# " + "script content " * 50 + "\nprint('Export ready')\nPY"
    show(ui, f"""[{{ id: 'python', type: 'tool', name: 'run_command', state: 'completed',
      input: {{ command: {json.dumps(command)}, timeout_seconds: 90 }}, output: 'Export ready' }}]""")
    expect(page.get_by_label("Command", exact=True)).to_have_count(0)
    expect(page.get_by_label("Output")).to_have_count(0)
    heading = page.get_by_role("button", name=re.compile("run command"))
    assert len(heading.text_content()) < 270
    heading.click()
    assert page.get_by_label("Command", exact=True).text_content() == command
    expect(page.get_by_label("Options")).to_contain_text('"timeout_seconds": 90')
    expect(page.get_by_label("Output")).to_have_text("Export ready")
    heading.click()
    expect(page.get_by_label("Command", exact=True)).to_have_count(0)
    expect(page.get_by_label("Output")).to_have_count(0)


def test_completed_tool_keeps_input_without_output(ui, page):
    show(ui, """[{ id: 'write', type: 'tool', name: 'write_file', state: 'completed',
      input: { path: 'result.txt', content: 'Report contents' }, output: '' }]""")
    page.get_by_role("button", name="write file result.txt").click()
    expect(page.get_by_label("Input")).to_contain_text("Report contents")
    expect(page.get_by_label("Output")).to_have_text("No output.")


def test_approval_keeps_details_and_sends_decision(ui, page):
    command = "python -c \"print('ready')\""
    preview = f"{command}\n\nTimeout: 60 seconds · Output limit: 80000 bytes\nForeground command: Stop response also stops this job."
    show(ui, f"""[{{ id: 'approval', type: 'tool', name: 'run_command', state: 'pending',
      input: {{ command: {json.dumps(command)} }}, preview: {json.dumps(preview)} }}]""",
         setup="fake.handle = () => new Response('{}', { status: 200 })")
    assert page.get_by_label("Command", exact=True).text_content() == preview
    expect(page.get_by_label("Output")).to_have_count(0)
    page.get_by_role("button", name="Approve").click()
    ui.wait("fake.calls.length === 1")
    assert ui.requests("/api/sessions/parent/approvals/approval", "POST")[0]["body"] == '{"allowed":true}'


@pytest.mark.parametrize("kind", ["notice", "user"])
def test_child_link_navigation_and_back(ui, page, kind):
    setup = SETUP + f"""
    window.parent = makeSession([{{ id: 'link', type: '{kind}', text: 'Delegated parser review.', child_session_id: 'child' }}])
    window.child = makeSession([{{ id: 'child-reply', type: 'assistant', text: 'Child findings.' }}],
      {{ id: 'child', title: 'Child task', parent_session_id: 'parent', is_subagent: true }})
    window.navigation = id => {{
      const session = id === 'child' ? child : parent
      rerender(() => ({{ key: session.id, session, onError: fake.spy('onError'), onSelectSession: navigation }}))
    }}"""
    ui.mount("components/Conversation.js", "{ key: 'parent', session: parent, onError: fake.spy('onError'), onSelectSession: navigation }", setup=setup)
    expect(page.get_by_role("button", name="Back to parent conversation")).to_have_count(0)
    page.get_by_role("button", name="Open subagent").click()
    expect(page.get_by_text("Child findings.")).to_be_visible()
    expect(page.get_by_text("Delegated parser review.")).to_have_count(0)
    page.get_by_role("button", name="Back to parent conversation").click()
    expect(page.get_by_text("Delegated parser review.")).to_be_visible()
    expect(page.get_by_text("Child findings.")).to_have_count(0)
    expect(page.get_by_role("button", name="Open subagent")).to_have_count(1)


def test_pages_of_one_file_show_the_lines_each_read_covered(ui, page):
    # Reported: consecutive pages of a long file looked like the same read repeated.
    def read(event_id, start, end=None, state="completed", output=None):
        page_json = json.dumps({"path": "notes/v7.md", "start_line": start, "end_line": end, "content": "…"})
        return (f"{{ id: '{event_id}', type: 'tool', name: 'read_file', state: '{state}', "
                f"input: {{ path: 'notes/v7.md'{f', start_line: {start}' if start > 1 else ''} }}, "
                f"output: {json.dumps(output if output is not None else page_json)} }}")
    show(ui, "[" + ", ".join([read("p1", 1, 108), read("p2", 109, 243), read("p3", 244, state="running", output=""),
                               read("empty", 1, 0), read("failed", 500, state="error", output="Not a file.")]) + "]")
    for name in ("read file notes/v7.md · lines 1–108", "read file notes/v7.md · lines 109–243",
                 "read file notes/v7.md · from line 244", "read file notes/v7.md · from line 500"):
        expect(page.get_by_role("button", name=name, exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="read file notes/v7.md", exact=True)).to_be_visible()
    page.get_by_text("Activity · 5 actions").click()
    expect(page.locator(".activity-subject").nth(1)).to_have_text(" · notes/v7.md · lines 109–243")
