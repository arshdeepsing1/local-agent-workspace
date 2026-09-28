import json
import re

import pytest
from playwright.sync_api import expect


def open_dialog(ui, setup="", selected="session()"):
    ui.mount("components/AgentToolsDialog.js", f"dialogProps({selected})", scenario="agent_tools", setup=setup)


def open_tab(ui, page, tab, setup="", selected="session()"):
    open_dialog(ui, setup, selected)
    expect(page.get_by_role("button", name="Preview checkpoint src/main.py")).to_be_visible()
    page.get_by_role("button", name=tab, exact=True).click()


def pending(name, condition):
    """Setup that holds matching requests until the test resolves fake.deferreds[name]."""
    return f"const {name} = fake.defer('{name}'); fake.override = (path, options) => ({condition}) ? {name}.promise : undefined"


def test_restores_only_the_previewed_checkpoint(ui, page):
    open_dialog(ui)
    page.get_by_role("button", name="Preview checkpoint src/main.py").click()
    restore = page.get_by_role("button", name="Restore checkpoint")
    expect(restore).to_be_visible()
    expect(page.locator(".agent-tools-preview pre")).to_have_text("-new\n+original")
    restore.click()
    expect(page.get_by_text("Restored src/main.py.")).to_be_visible()
    assert ui.bodies("/api/checkpoints/checkpoint/restore?session_id=a", "POST") == [{"expected_current_hash": "current-hash"}]
    expect(page.get_by_role("button", name="Restore checkpoint")).to_have_count(0)


def test_unrestorable_checkpoint_stays_disabled_with_explanation(ui, page):
    open_dialog(ui, """fake.override = path => path.includes('/preview') ? fake.json({ ...checkpoint, diff: '', expected_current_hash: '',
      can_restore: false, error: 'The file changed externally.' }) : undefined""")
    page.get_by_role("button", name="Preview checkpoint src/main.py").click()
    expect(page.get_by_role("button", name="Restore checkpoint")).to_be_disabled()
    expect(page.get_by_role("alert")).to_have_text("The file changed externally.")
    assert not ui.js("fake.calls.some(call => call.path.includes('/restore'))")


def test_turn_preview_restores_only_the_selected_file(ui, page):
    setup = """
      const second = { ...checkpoint, id: 'second', path: 'src/helper.py', turn_id: 'turn-1' }
      const first = { ...checkpoint, turn_id: 'turn-1' }
      window.selected = session(); selected.events = [{ id: 'turn-1', type: 'user', text: 'Update both parsers' }]
      fake.override = path => {
        if (path === '/api/checkpoints?session_id=a') return fake.json([first, second])
        if (path === '/api/checkpoint-turns/turn-1/preview?session_id=a&offset=0') return fake.json({ turn_id: 'turn-1', next_offset: null, previews: [
          { ...first, diff: '-new\\n+old', expected_current_hash: 'first-hash', can_restore: true },
          { ...second, diff: '', expected_current_hash: 'second-hash', can_restore: false, error: 'Changed externally.' },
        ] })
      }"""
    open_dialog(ui, setup, "selected")
    page.get_by_role("button", name="Preview turn turn-1").click()
    expect(page.get_by_text("Update both parsers")).to_be_visible()
    first_preview = page.get_by_role("group", name="Recovery preview src/main.py")
    second_preview = page.get_by_role("group", name="Recovery preview src/helper.py")
    expect(first_preview).to_be_visible()
    expect(second_preview.get_by_role("button", name="Restore checkpoint")).to_be_disabled()
    expect(page.get_by_text(re.compile("not a whole-turn undo"))).to_be_visible()
    first_preview.get_by_role("button", name="Restore checkpoint").click()
    expect(page.get_by_text("Restored src/main.py.")).to_be_visible()
    restores = ui.js("fake.calls.filter(call => call.path.includes('/restore'))")
    assert [call["path"] for call in restores] == ["/api/checkpoints/checkpoint/restore?session_id=a"]
    assert json.loads(restores[0]["body"]) == {"expected_current_hash": "first-hash"}


def test_turn_preview_pages_and_clears_on_session_switch(ui, page):
    setup = """fake.override = path => {
      if (path === '/api/checkpoints?session_id=a') return fake.json([{ ...checkpoint, turn_id: 'turn-1' }])
      if (path.startsWith('/api/checkpoint-turns/turn-1/preview?session_id=a')) {
        const offset = new URL(path, 'http://localhost').searchParams.get('offset')
        return fake.json({ turn_id: 'turn-1', next_offset: offset === '0' ? 10 : null, previews: [
          { ...checkpoint, id: `page-${offset}`, path: `page-${offset}.py`, diff: 'diff', expected_current_hash: 'hash', can_restore: true },
        ] })
      }
    }"""
    open_dialog(ui, setup)
    page.get_by_role("button", name="Preview turn turn-1").click()
    page.get_by_role("button", name="More checkpoints in this turn").click()
    expect(page.get_by_role("group", name="Recovery preview page-10.py")).to_be_visible()
    expect(page.get_by_role("group", name="Recovery preview page-0.py")).to_be_visible()
    expect(page.get_by_role("button", name="More checkpoints in this turn")).to_have_count(0)
    ui.rerender("dialogProps(session('b'))")
    expect(page.get_by_role("button", name="Preview checkpoint src/main.py")).to_be_visible()
    expect(page.get_by_role("group", name=re.compile("Recovery preview"))).to_have_count(0)


def test_creates_worktrees_and_opens_their_conversations(ui, page):
    open_tab(ui, page, "Worktrees")
    expect(page.get_by_role("button", name="Open conversation in feature/test")).to_be_visible()
    expect(page.get_by_text(re.compile("committed HEAD.*Uncommitted changes are not copied"))).to_be_visible()
    page.get_by_label("New branch").fill("feature/isolated")
    page.get_by_role("button", name="Create worktree").click()
    expect(page.get_by_role("textbox", name="New branch")).to_have_value("")
    assert ui.bodies("/api/worktrees?session_id=a", "POST") == [{"branch": "feature/isolated"}]
    page.get_by_role("button", name="Open conversation in feature/test").click()
    ui.wait("fake.spies.select.length === 1")
    assert ui.spy("select") == [["worktree-session"]]
    assert ui.spy("close") == [[]]


def test_usage_graph_and_estimated_cost(ui, page):
    open_tab(ui, page, "Usage")
    expect(page.get_by_text("0.2039 DBU")).to_be_visible()
    cards = page.locator(".usage-cards")
    expect(cards.locator("div", has_text="Uncached input")).to_contain_text("1,200")
    expect(cards.locator("div", has_text="Cache read")).to_contain_text("300")
    expect(cards.locator("div", has_text="Cache write")).to_contain_text("100")
    expect(page.get_by_text("Input incl. cache")).to_be_visible()
    expect(page.get_by_role("img", name=re.compile("Tokens by model call"))).to_be_visible()
    expect(page.get_by_role("cell", name="compaction")).to_be_visible()
    expect(page.get_by_text("error · 429")).to_be_visible()
    expect(page.get_by_text(re.compile("near-real-time estimate"))).to_be_visible()
    requests = len(ui.requests("/api/sessions/a/metrics"))
    assert requests >= 1
    ui.rerender("{ ...dialogProps({ ...session(), status: 'running' }) }")
    ui.js("new Promise(done => setTimeout(done, 20))")
    assert len(ui.requests("/api/sessions/a/metrics")) == requests


def test_usage_csv_export_uses_the_local_token(ui, page):
    csv = "n,purpose\n1,agent\n"
    open_tab(ui, page, "Usage", f"""fake.captureDownloads()
      fake.override = path => path === '/api/sessions/a/usage.csv' ? new Response({json.dumps(csv)}, {{ status: 200, headers: {{ 'Content-Type': 'text/csv' }} }}) : undefined""")
    page.get_by_role("button", name="Export CSV").click()
    ui.wait("fake.downloads.length === 1")
    download = ui.js("(async () => { const d = fake.downloads[0]; return { name: d.download, revoked: d.revoked, text: await d.blob.text() } })()")
    assert download == {"name": "usage-a.csv", "revoked": True, "text": csv}
    assert "X-Local-Token" in ui.requests("/api/sessions/a/usage.csv")[0]["headers"]


def test_usage_export_failure_is_shown(ui, page):
    open_tab(ui, page, "Usage", "fake.override = path => path === '/api/sessions/a/usage.csv' ? fake.json({ detail: 'Conversation not found.' }, 404) : undefined")
    page.get_by_role("button", name="Export CSV").click()
    expect(page.get_by_text("Conversation not found.")).to_be_visible()


def test_missing_usage_is_unavailable_not_zero(ui, page):
    open_tab(ui, page, "Usage", """fake.override = path => path === '/api/sessions/a/metrics' ? fake.json({
      ...metrics,
      totals: { ...metrics.totals, input_tokens: null, output_tokens: null, cache_read_input_tokens: null,
        cache_creation_input_tokens: null, reasoning_tokens: null, estimated_dbu: null },
      calls: [{ id: 'missing', purpose: 'agent', model: 'databricks-claude-opus-4-8', created: '2026-09-22T10:00:00Z',
        status: 'error', usage: { input_tokens: 7 }, estimated_dbu: null }],
    }) : undefined""")
    expect(page.get_by_text("Unavailable", exact=True).first).to_be_visible()
    assert page.get_by_text("Unavailable", exact=True).count() >= 3
    table = page.get_by_role("table", name="Model calls for this conversation")
    assert table.get_by_text("—", exact=True).count() >= 3


def test_legacy_usage_is_explained_and_draft_requests_no_metrics(ui, page):
    open_tab(ui, page, "Usage", "fake.override = path => path === '/api/sessions/a/metrics' ? fake.json({ ...metrics, complete: false }) : undefined")
    expect(page.get_by_text(re.compile("older conversation has no complete inference ledger"))).to_be_visible()
    ui.run("unmount()")
    ui.page.evaluate("() => mount('components/AgentToolsDialog.js', () => dialogProps(null))")
    page.get_by_role("button", name="Usage", exact=True).click()
    expect(page.get_by_text("Start a conversation to track its model usage.")).to_be_visible()
    assert ui.requests("/api/sessions/null/metrics") == []


def test_refused_worktree_removal_is_inline(ui, page):
    open_tab(ui, page, "Worktrees", """fake.override = (path, options) => path.includes('/worktrees/worktree') && options.method === 'DELETE'
      ? fake.json({ detail: 'Worktree has uncommitted changes.' }, 409) : undefined""")
    page.get_by_role("button", name="Remove worktree feature/test").click()
    expect(page.get_by_role("alert")).to_have_text("Worktree has uncommitted changes.")
    expect(page.get_by_role("button", name="Open conversation in feature/test")).to_be_visible()
    assert ui.spy("onError") == [["Worktree has uncommitted changes."]]


def test_tasks_dependencies_statuses_and_children(ui, page):
    open_tab(ui, page, "Tasks")
    expect(page.get_by_role("checkbox", name="Complete task Inspect source")).to_be_visible()
    page.get_by_label("Task title").fill("Fix parser")
    page.get_by_label("Task description").fill("Use the inspection findings")
    page.get_by_text("Dependencies").click()
    page.get_by_role("checkbox", name="Inspect source", exact=True).check()
    page.get_by_role("button", name="Add task").click()
    expect(page.get_by_role("checkbox", name="Complete task Fix parser")).to_be_visible()
    assert ui.bodies("/api/sessions/a/tasks", "POST") == [
        {"title": "Fix parser", "description": "Use the inspection findings", "depends_on": ["task"]}]
    page.get_by_role("combobox", name="Status for Inspect source").select_option("in_progress")
    expect(page.get_by_role("combobox", name="Status for Inspect source")).to_have_value("in_progress")
    page.get_by_role("checkbox", name="Complete task Inspect source").click()
    expect(page.get_by_role("checkbox", name="Complete task Inspect source")).to_be_checked()
    assert ui.bodies("/api/sessions/a/tasks/task") == [{"status": "in_progress"}, {"status": "completed"}]
    expect(page.get_by_text("Waiting for approval")).to_be_visible()
    page.get_by_role("button", name="Open subagent Child review").click()
    assert ui.spy("select") == [["child"]]
    assert ui.spy("close") == [[]]


def test_rejected_task_update_keeps_previous_status(ui, page):
    open_tab(ui, page, "Tasks", """fake.override = (path, options) => path.includes('/tasks/task') && options.method === 'PATCH'
      ? fake.json({ detail: 'Complete all dependencies first.' }, 400) : undefined""")
    page.get_by_role("checkbox", name="Complete task Inspect source").click()
    expect(page.get_by_role("alert")).to_have_text("Complete all dependencies first.")
    expect(page.get_by_role("checkbox", name="Complete task Inspect source")).not_to_be_checked()


def test_extensions_save_test_and_skill_toggle(ui, page):
    open_tab(ui, page, "Extensions")
    editor = page.get_by_role("textbox", name="Extension configuration (JSON)")
    expect(editor).to_have_value(re.compile("servers"))
    config = {"servers": [{"id": "example", "name": "Example", "transport": "stdio", "command": "example-server", "args": [], "enabled": True}], "hooks": []}
    editor.fill(json.dumps(config))
    assert ui.requests("/api/extensions", "PUT") == []
    page.get_by_role("button", name="Save configuration").click()
    expect(page.get_by_text("Configuration saved.")).to_be_visible()
    assert ui.bodies("/api/extensions", "PUT") == [config]
    page.get_by_role("button", name="Test saved configuration").click()
    expect(page.get_by_text("example.search")).to_be_visible()
    assert ui.requests("/api/extensions/test")[0].get("body") is None
    page.get_by_role("checkbox", name=re.compile("Review code")).click()
    expect(page.get_by_role("checkbox", name=re.compile("Review code"))).to_be_checked()
    assert ui.bodies("/api/sessions/a/skills") == [{"skill_id": "skill", "enabled": True}]


def test_invalid_extension_json_is_not_sent(ui, page):
    open_tab(ui, page, "Extensions")
    editor = page.get_by_role("textbox", name="Extension configuration (JSON)")
    expect(editor).to_have_value(re.compile("servers"))
    editor.fill("not JSON")
    page.get_by_role("button", name="Save configuration").click()
    expect(page.get_by_role("alert")).to_be_visible()
    assert ui.requests("/api/extensions", "PUT") == []


def test_extension_editor_locked_while_loading_and_saving(ui, page):
    open_tab(ui, page, "Extensions", """const loading = fake.defer('loading'), saving = fake.defer('saving')
      fake.override = (path, options) => path === '/api/extensions' ? (options.method === 'PUT' ? saving.promise : loading.promise) : undefined""")
    editor = page.get_by_role("textbox", name="Extension configuration (JSON)")
    expect(editor).to_be_disabled()
    expect(page.get_by_role("button", name="Save configuration")).to_be_disabled()
    original = {"servers": [], "hooks": [{"id": "old-hook", "event": "before_tool", "command": "old-command", "timeout_seconds": 10, "enabled": True}]}
    ui.run(f"fake.deferreds.loading.resolve(fake.json({json.dumps(original)}))")
    expect(editor).to_be_enabled()
    assert json.loads(editor.input_value()) == original
    editor.fill(json.dumps({"servers": [], "hooks": []}))
    page.get_by_role("button", name="Save configuration").click()
    expect(editor).to_be_disabled()
    assert ui.bodies("/api/extensions", "PUT") == [{"servers": [], "hooks": []}]
    ui.run("fake.deferreds.saving.resolve(fake.json(config))")
    expect(editor).to_be_enabled()
    assert json.loads(editor.input_value()) == {"servers": [], "hooks": []}


@pytest.mark.parametrize("tab,label,button,path", [
    ("Worktrees", "New branch", "Create worktree", "/api/worktrees?session_id=a"),
    ("Tasks", "Task title", "Add task", "/api/sessions/a/tasks"),
])
def test_creation_inputs_locked_while_pending(ui, page, tab, label, button, path):
    open_tab(ui, page, tab, pending("create", f"path === '{path}' && options.method === 'POST'"))
    field = page.get_by_role("textbox", name=label)
    expect(field).to_be_enabled()
    field.fill("codex/new-task")
    page.get_by_role("button", name=button).click()
    expect(field).to_be_disabled()
    if tab == "Tasks":
        expect(page.get_by_role("textbox", name="Task description")).to_be_disabled()
        page.get_by_text("Dependencies").click()
        expect(page.get_by_role("checkbox", name="Inspect source", exact=True)).to_be_disabled()
    ui.run(f"fake.deferreds.create.resolve(fake.json({'{ ...task, id: `created-task` }' if tab == 'Tasks' else 'worktree'}))")
    expect(field).to_be_enabled()
    expect(field).to_have_value("")


def test_skills_come_from_the_selected_conversation_workspace(ui, page):
    open_tab(ui, page, "Extensions", """fake.override = path => path === '/api/skills?session_id=worktree-session'
      ? fake.json([{ ...skill, id: 'worktree-skill', name: 'Worktree skill', path: '/worktree/.agents/skills/SKILL.md' }]) : undefined""",
             "session('worktree-session')")
    expect(page.get_by_role("checkbox", name=re.compile("Worktree skill"))).to_be_visible()
    expect(page.get_by_role("checkbox", name=re.compile("Review code"))).to_have_count(0)
    assert ui.requests("/api/skills?session_id=worktree-session") and not ui.requests("/api/skills")


def test_draft_scope_requires_conversation_for_tasks_and_skills(ui, page):
    open_tab(ui, page, "Tasks", selected="null")
    expect(page.get_by_text("Start a conversation to manage tasks and subagents.")).to_be_visible()
    assert not ui.js("fake.calls.some(call => call.path.includes('/sessions/'))")
    assert ui.requests("/api/checkpoints")
    page.get_by_role("button", name="Extensions", exact=True).click()
    expect(page.get_by_role("checkbox", name=re.compile("Review code"))).to_be_disabled()


def test_previous_scope_preview_and_error_are_ignored(ui, page):
    open_dialog(ui, pending("preview", "path === '/api/checkpoints/checkpoint/preview?session_id=a'"))
    page.get_by_role("button", name="Preview checkpoint src/main.py").click()
    ui.rerender("dialogProps(session('b'))")
    expect(page.get_by_role("button", name="Preview checkpoint src/main.py")).to_be_visible()
    ui.run("fake.deferreds.preview.resolve(fake.json({ detail: 'Old scope error' }, 409))")
    ui.js("new Promise(done => setTimeout(done, 20))")
    expect(page.get_by_role("alert")).to_have_count(0)
    expect(page.get_by_role("button", name="Restore checkpoint")).to_have_count(0)
    assert ui.spy("onError") == []
    assert ui.requests("/api/checkpoints?session_id=b")


def test_late_open_worktree_after_close_does_not_navigate(ui, page):
    open_tab(ui, page, "Worktrees", pending("open", "path === '/api/worktrees/worktree/session?session_id=a'"))
    page.get_by_role("button", name="Open conversation in feature/test").click()
    ui.run("unmount()")
    ui.js("(async () => { fake.deferreds.open.resolve(fake.json(session('late-session'))); await new Promise(done => setTimeout(done, 20)) })()")
    assert ui.spy("select") == []


def test_subagent_ceiling_saved_only_after_server_accepts(ui, page):
    open_tab(ui, page, "Tasks")
    expect(page.get_by_role("button", name="Open subagent Child review")).to_be_visible()
    selector = page.get_by_role("combobox", name="New subagent tool ceiling")
    selector.select_option("read_only")
    expect(page.get_by_text("Tool ceiling saved for new subagents.")).to_be_visible()
    expect(selector).to_have_value("read_only")
    assert ui.bodies("/api/sessions/a/subagent-profile") == [{"tool_profile": "read_only"}]
    ui.run("fake.override = path => path.endsWith('/subagent-profile') ? fake.json({ detail: 'Conversation is running.' }, 409) : undefined")
    selector.select_option("inherit")
    expect(page.get_by_role("alert")).to_be_visible()
    expect(selector).to_have_value("read_only")


def test_child_conversation_cannot_widen_its_profile(ui, page):
    open_tab(ui, page, "Tasks", selected="{ ...session(), is_subagent: true, tool_profile: 'read_only' }")
    expect(page.get_by_text(re.compile("This child cannot widen"))).to_be_visible()
    expect(page.get_by_role("combobox", name="New subagent tool ceiling")).to_have_count(0)


def test_mcp_results_per_server_and_stale_results_cleared(ui, page):
    open_tab(ui, page, "Extensions", """fake.override = path => path === '/api/extensions/test' ? fake.json({ tools: ['mcp__ok__search'], servers: [
      { id: 'ok', name: 'Search server', status: 'connected', tools: ['mcp__ok__search'] },
      { id: 'bad', name: 'Broken server', status: 'failed', tools: [], error: 'Connection refused' },
      { id: 'off', name: 'Unused server', status: 'disabled', tools: [] },
    ] }) : undefined""")
    expect(page.get_by_text("Review code")).to_be_visible()
    page.get_by_role("button", name="Test saved configuration").click()
    expect(page.get_by_text("Connection refused")).to_be_visible()
    expect(page.get_by_text("Connected during test · 1 tools")).to_be_visible()
    expect(page.get_by_text("Disabled · not started")).to_be_visible()
    page.get_by_role("button", name="Save configuration").click()
    expect(page.get_by_text("Configuration saved.")).to_be_visible()
    expect(page.get_by_text("Connection refused")).to_have_count(0)
