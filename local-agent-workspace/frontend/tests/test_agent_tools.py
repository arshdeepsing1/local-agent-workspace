import json
import re

import pytest
from playwright.sync_api import expect


def open_dialog(ui, setup="", selected="session()", props=""):
    ui.mount("components/AgentToolsDialog.js", f"{{ ...dialogProps({selected}){props} }}", scenario="agent_tools", setup=setup)
    expect(ui.page.get_by_role("region", name="Overview")).to_be_visible()  # The first section.


def open_tab(ui, page, tab, setup="", selected="session()"):
    open_dialog(ui, setup, selected)
    page.get_by_role("button", name=tab, exact=True).click()
    expect(page.get_by_role("button", name=tab, exact=True)).to_have_attribute("aria-pressed", "true")


def pending(name, condition):
    """Setup that holds matching requests until the test resolves fake.deferreds[name]."""
    return f"const {name} = fake.defer('{name}'); fake.override = (path, options) => ({condition}) ? {name}.promise : undefined"


def test_restores_only_the_previewed_checkpoint(ui, page):
    open_tab(ui, page, "Checkpoints")
    page.get_by_role("button", name="Preview checkpoint src/main.py").click()
    restore = page.get_by_role("button", name="Restore checkpoint")
    expect(restore).to_be_visible()
    expect(page.locator(".agent-tools-preview pre")).to_have_text("-new\n+original")
    restore.click()
    expect(page.get_by_text("Restored src/main.py.")).to_be_visible()
    assert ui.bodies("/api/checkpoints/checkpoint/restore?session_id=a", "POST") == [{"expected_current_hash": "current-hash"}]
    expect(page.get_by_role("button", name="Restore checkpoint")).to_have_count(0)


def test_unrestorable_checkpoint_stays_disabled_with_explanation(ui, page):
    open_tab(ui, page, "Checkpoints", """fake.override = path => path.includes('/preview') ? fake.json({ ...checkpoint, diff: '', expected_current_hash: '',
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
    open_tab(ui, page, "Checkpoints", setup, "selected")
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
    open_tab(ui, page, "Checkpoints", setup)
    page.get_by_role("button", name="Preview turn turn-1").click()
    page.get_by_role("button", name="More checkpoints in this turn").click()
    expect(page.get_by_role("group", name="Recovery preview page-10.py")).to_be_visible()
    expect(page.get_by_role("group", name="Recovery preview page-0.py")).to_be_visible()
    expect(page.get_by_role("button", name="More checkpoints in this turn")).to_have_count(0)
    ui.rerender("dialogProps(session('b'))")  # Another chat: the remembered section, without the old previews.
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
    expect(cards.locator("div", has_text="Automatic retries")).to_contain_text("3")
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


def test_usage_shows_the_last_reported_input_and_total_input_per_call(ui, page):
    open_tab(ui, page, "Usage")
    last = page.locator(".usage-last-input")
    expect(last).to_contain_text("Last model input")
    expect(last).to_contain_text("No reported input yet.")
    header = page.get_by_role("columnheader")
    expect(header).to_have_text(["Purpose", "Model", "Status", "Total input", "Uncached input", "Cache read",
                                 "Cache write", "Output", "Est. DBU"])
    first = page.get_by_role("row").filter(has_text="agent")
    expect(first.get_by_role("cell").nth(3)).to_have_text("1,600")
    expect(page.get_by_role("row").filter(has_text="compaction").get_by_role("cell").nth(3)).to_have_text("—")


def test_usage_last_model_input_row(ui, page):
    open_tab(ui, page, "Usage", """fake.override = path => path === '/api/sessions/a/metrics' ? fake.json({ ...metrics,
      last_input: { input_tokens: 10000, percent_of_input_budget: 8.3, percent_of_model_window: 1, estimated_tokens: 8000,
        partial: false } }) : undefined""")
    last = page.get_by_role("group", name="Last model input")
    expect(last).to_contain_text("10,000 tokens")
    expect(last).to_contain_text("8.3% of input budget")
    expect(last).to_contain_text("1% of model window")
    expect(last).to_contain_text("App estimate ~8,000")
    page.get_by_role("button", name="Refresh usage").click()
    ui.run("""fake.override = path => path === '/api/sessions/a/metrics' ? fake.json({ ...metrics,
      last_input: { input_tokens: 500, percent_of_input_budget: 0.4, percent_of_model_window: null, estimated_tokens: 480,
        partial: true } }) : undefined""")
    page.get_by_role("button", name="Refresh usage").click()
    expect(last).to_contain_text("Model window unknown")
    expect(last).to_contain_text("Last model input (partial)")


def test_verify_tab_loads_and_saves_the_project_command(ui, page):
    open_tab(ui, page, "Verify")
    section = page.get_by_role("region", name="Verify")
    expect(section.get_by_role("heading", name="Verify command")).to_be_visible()
    expect(section.get_by_text(re.compile(r"changed files in this project \(/project\)"))).to_be_visible()
    command = section.get_by_role("textbox", name="Command")
    expect(command).to_have_value("pytest -q")
    expect(command).to_have_attribute("placeholder", "pytest -q")
    expect(section.get_by_role("spinbutton", name="Time limit (seconds)")).to_have_value("600")
    command.fill("  make check  ")
    section.get_by_role("spinbutton", name="Time limit (seconds)").fill("120")
    section.get_by_role("spinbutton", name="Fix rounds").fill("3")
    section.get_by_role("button", name="Save", exact=True).click()
    expect(section.get_by_role("status")).to_have_text("Verify command saved for this project.")
    assert ui.bodies("/api/project-settings?session_id=a", "PUT") == [
        {"verify_command": "make check", "verify_timeout_seconds": 120, "verify_max_rounds": 3}]
    expect(command).to_have_value("make check")
    command.fill("")
    section.get_by_role("button", name="Save", exact=True).click()
    expect(section.get_by_role("status")).to_have_text("Verify is off for this project.")
    assert ui.bodies("/api/project-settings?session_id=a", "PUT")[-1]["verify_command"] == ""


def test_verify_tab_reports_a_rejected_value(ui, page):
    open_tab(ui, page, "Verify", """fake.override = (path, options) => path.startsWith('/api/project-settings') && options.method === 'PUT'
      ? fake.json({ detail: 'The verify time limit must be an integer from 10 to 3,600 seconds.' }, 400) : undefined""")
    section = page.get_by_role("region", name="Verify")
    expect(section.get_by_role("textbox", name="Command")).to_have_value("pytest -q")
    section.get_by_role("spinbutton", name="Time limit (seconds)").fill("5")
    section.get_by_role("button", name="Save", exact=True).click()  # The field's own range check stops this one.
    assert ui.bodies("/api/project-settings?session_id=a", "PUT") == []
    section.get_by_role("spinbutton", name="Time limit (seconds)").fill("20")
    section.get_by_role("button", name="Save", exact=True).click()
    expect(section.get_by_role("alert")).to_have_text("The verify time limit must be an integer from 10 to 3,600 seconds.")
    expect(section.get_by_role("status")).to_have_count(0)


def test_saving_verify_keeps_keyboard_focus_and_ignores_repeat_submits(ui, page):
    open_tab(ui, page, "Verify", pending("saving", "path.startsWith('/api/project-settings') && options.method === 'PUT'"))
    section = page.get_by_role("region", name="Verify")
    command = section.get_by_role("textbox", name="Command")
    expect(command).to_have_value("pytest -q")
    command.fill("make check")
    command.press("Enter")
    ui.wait("fake.calls.some(call => call.path.startsWith('/api/project-settings') && call.method === 'PUT')")
    # The fields are read-only while saving, not disabled, so focus stays where it was.
    expect(command).to_have_attribute("readonly", "")
    expect(command).to_be_focused()
    save = section.get_by_role("button", name="Save", exact=True)
    expect(save).to_have_attribute("aria-disabled", "true")
    command.press("Enter")
    save.click(force=True)
    assert len(ui.bodies("/api/project-settings?session_id=a", "PUT")) == 1
    ui.run("fake.deferreds.saving.resolve(fake.json({ workspace: '/project', verify_command: 'make check', "
           "verify_timeout_seconds: 600, verify_max_rounds: 2 }))")
    expect(section.get_by_role("status")).to_have_text("Verify command saved for this project.")
    expect(save).to_be_focused()  # The repeat click moved focus to Save, and it stays there.
    expect(command).not_to_have_attribute("readonly", "")
    expect(save).to_have_attribute("aria-disabled", "false")


def test_verify_tab_without_a_conversation_uses_the_default_workspace(ui, page):
    open_dialog(ui, selected="null")
    page.get_by_role("button", name="Verify", exact=True).click()
    expect(page.get_by_role("textbox", name="Command")).to_have_value("pytest -q")
    assert ui.requests("/api/project-settings")


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


def test_unreported_reasoning_tokens_leave_the_other_totals_shown(ui, page):
    # Claude reports no reasoning_tokens: the metrics total is null, not 0, and the cards show the rest.
    open_tab(ui, page, "Usage", """fake.override = path => path === '/api/sessions/a/metrics' ? fake.json({
      ...metrics, totals: { ...metrics.totals, reasoning_tokens: null } }) : undefined""")
    cards = page.locator(".usage-cards")
    expect(cards).to_contain_text("1,200")
    expect(cards.get_by_text("Unavailable", exact=True)).to_have_count(0)


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


def test_tasks_dependencies_and_statuses(ui, page):
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
    expect(page.get_by_role("region", name="Tasks")).to_contain_text("1 of 2 done.")
    expect(page.get_by_role("button", name="Open subagent Child review")).to_have_count(0)  # Subagents have their own section.


def test_rejected_task_update_keeps_previous_status(ui, page):
    open_tab(ui, page, "Tasks", """fake.override = (path, options) => path.includes('/tasks/task') && options.method === 'PATCH'
      ? fake.json({ detail: 'Complete all dependencies first.' }, 400) : undefined""")
    page.get_by_role("checkbox", name="Complete task Inspect source").click()
    expect(page.get_by_role("alert")).to_have_text("Complete all dependencies first.")
    expect(page.get_by_role("checkbox", name="Complete task Inspect source")).not_to_be_checked()


def json_editor(page):
    """The Extensions editor's JSON view (the form is the default)."""
    page.get_by_role("button", name="Edit as JSON").click()
    return page.get_by_role("textbox", name="Extension configuration (JSON)")


def test_extensions_save_test_and_skill_toggle(ui, page):
    open_tab(ui, page, "Extensions")
    editor = json_editor(page)
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
    editor = json_editor(page)
    expect(editor).to_have_value(re.compile("servers"))
    editor.fill("not JSON")
    page.get_by_role("button", name="Save configuration").click()
    expect(page.get_by_role("alert")).to_be_visible()
    assert ui.requests("/api/extensions", "PUT") == []


def test_extension_editor_locked_while_loading_and_saving(ui, page):
    open_tab(ui, page, "Extensions", """const loading = fake.defer('loading'), saving = fake.defer('saving')
      fake.override = (path, options) => path === '/api/extensions' ? (options.method === 'PUT' ? saving.promise
        : loading.promise.then(response => response.clone())) : undefined  // The Overview reads it too.""")
    editor = json_editor(page)
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
    expect(page.get_by_text("Start a conversation to manage its tasks.")).to_be_visible()
    page.get_by_role("button", name="Subagents", exact=True).click()
    expect(page.get_by_text("Start a conversation to see its subagents.")).to_be_visible()
    assert not ui.js("fake.calls.some(call => call.path.includes('/sessions/'))")
    assert ui.requests("/api/checkpoints")
    page.get_by_role("button", name="Extensions", exact=True).click()
    expect(page.get_by_role("checkbox", name=re.compile("Review code"))).to_be_disabled()


def test_previous_scope_preview_and_error_are_ignored(ui, page):
    open_tab(ui, page, "Checkpoints", pending("preview", "path === '/api/checkpoints/checkpoint/preview?session_id=a'"))
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
    open_tab(ui, page, "Subagents")
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
    open_tab(ui, page, "Subagents", selected="{ ...session(), is_subagent: true, tool_profile: 'read_only' }")
    expect(page.get_by_text(re.compile("This child cannot widen"))).to_be_visible()
    expect(page.get_by_role("combobox", name="New subagent tool ceiling")).to_have_count(0)


def test_mcp_results_per_server_and_stale_results_cleared(ui, page):
    open_tab(ui, page, "Extensions", """fake.override = path => path === '/api/extensions/test' ? fake.json({ tools: ['mcp__ok__search'], servers: [
      { id: 'ok', name: 'Search server', status: 'connected', tools: ['mcp__ok__search'], server_info: { name: 'search-mcp', version: '1.4.0' },
        instructions: 'Search before you read.', tool_annotations: { mcp__ok__search: { read_only: true } } },
      { id: 'bad', name: 'Broken server', status: 'failed', tools: [], error: 'Connection refused', stderr_tail: 'fatal: no config [REDACTED]' },
      { id: 'off', name: 'Unused server', status: 'disabled', tools: [] },
    ] }) : undefined""")
    expect(page.get_by_text("Review code")).to_be_visible()
    page.get_by_role("button", name="Test saved configuration").click()
    results = page.get_by_label("MCP server test results")
    expect(results.get_by_text("Connection refused")).to_be_visible()
    expect(results.get_by_text("Connected during test · 1 tool · server: search-mcp 1.4.0")).to_be_visible()
    expect(results.get_by_text("Server says: read-only")).to_be_visible()
    expect(results.get_by_text("Disabled · not started")).to_be_visible()
    results.get_by_text("Server stderr (end)").click()
    expect(results.get_by_text("fatal: no config [REDACTED]")).to_be_visible()
    expect(results.get_by_text("Server instructions (shown here only; never sent to the agent)")).to_be_visible()
    page.get_by_role("button", name="Save configuration").click()
    expect(page.get_by_text("Configuration saved.")).to_be_visible()
    expect(page.get_by_text("Connection refused")).to_have_count(0)


def test_last_turn_mcp_status_in_extensions_and_overview(ui, page):
    setup = """window.selected = { ...session(), mcp_status: [
      { id: 'docs', name: 'Docs', status: 'connected', tool_count: 3, server_info: { name: 'docs-mcp', version: '2.0' },
        capped: ['mcp__docs__extra'] },
      { id: 'broken', name: 'Broken', status: 'failed', tool_count: 0, error: 'could not connect or list tools: No such file',
        stderr_tail: 'Traceback: missing module', retry_seconds: 42 },
      { id: 'quiet', name: 'Quiet', status: 'off', tool_count: 0 }] }"""
    open_dialog(ui, setup, "selected")
    expect(overview_row(page, "MCP servers").locator(".overview-value")).to_have_text("Last turn: 1 connected · 1 failed · 1 off in this chat")
    overview_row(page, "MCP servers").get_by_role("button", name="Open Extensions").click()
    status = page.get_by_label("MCP servers in the last turn")
    expect(status.get_by_text("Connected · 3 tools · server: docs-mcp 2.0")).to_be_visible()
    expect(status.get_by_text("Not offered (Databricks takes at most 32 tools per request): mcp__docs__extra")).to_be_visible()
    expect(status.get_by_text("Failed · tried again in about 42 s")).to_be_visible()
    expect(status.get_by_text("could not connect or list tools: No such file")).to_be_visible()
    expect(status.get_by_text("Turned off in this chat · not started")).to_be_visible()
    # A push from the running chat updates it.
    ui.rerender("dialogProps({ ...selected, mcp_status: [{ id: 'docs', name: 'Docs', status: 'connected', tool_count: 4 }] })")
    expect(status.get_by_text("Connected · 4 tools")).to_be_visible()
    expect(status.get_by_text("Failed · tried again in about 42 s")).to_have_count(0)


# The dialog keeps one size: the tabs stay put and only the tab content scrolls

TABS = ["Overview", "Tasks", "Subagents", "Tools", "Usage", "Export", "Checkpoints", "Verify", "Worktrees", "Extensions"]


def tabs_top(ui):
    return ui.js("document.querySelector('.agent-tools-tabs').getBoundingClientRect().top")


@pytest.mark.parametrize("width,height", [(1280, 900), (390, 844)])
def test_tabs_stay_put(ui, page, width, height):
    page.set_viewport_size({"width": width, "height": height})
    open_dialog(ui)
    top = tabs_top(ui)
    size = ui.js("document.querySelector('.agent-tools-dialog').getBoundingClientRect().toJSON()")
    for tab in TABS:
        page.get_by_role("button", name=tab, exact=True).click()
        expect(page.get_by_role("button", name=tab, exact=True)).to_have_attribute("aria-pressed", "true")
        assert abs(tabs_top(ui) - top) <= 1, tab
        box = ui.js("document.querySelector('.agent-tools-dialog').getBoundingClientRect().toJSON()")
        assert all(abs(box[key] - size[key]) <= 1 for key in ("width", "height", "top", "left")), tab
        if tab in ("Usage", "Export", "Tools"):
            if tab == "Usage":
                expect(page.get_by_role("button", name="Refresh usage")).to_be_visible()
            ui.run("const body = document.querySelector('.agent-tools-body'); body.scrollTop = body.scrollHeight")
            assert abs(tabs_top(ui) - top) <= 1, tab
            expect(page.get_by_role("button", name="Close agent tools")).to_be_in_viewport()
            assert ui.js("(d => d.scrollHeight <= d.clientHeight + 1)(document.querySelector('.agent-tools-dialog'))"), tab


@pytest.mark.parametrize("width", [360, 600, 639])
def test_narrow_navigation_is_one_scrolling_row(ui, page, width):
    page.set_viewport_size({"width": width, "height": 740})
    open_dialog(ui)
    nav = ui.js("(n => ({ box: n.getBoundingClientRect().toJSON(), scroll: n.scrollWidth, client: n.clientWidth }))"
                "(document.querySelector('.agent-tools-tabs'))")
    content = ui.js("document.querySelector('.agent-tools-content').getBoundingClientRect().toJSON()")
    assert nav["box"]["left"] >= content["left"] - 0.5 and nav["box"]["right"] <= content["right"] + 0.5
    assert nav["scroll"] > nav["client"]  # More sections than fit: the row scrolls instead of wrapping.
    tops = {round(page.get_by_role("button", name=tab, exact=True).bounding_box()["y"]) for tab in TABS}
    assert len(tops) == 1, tops
    assert ui.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    page.get_by_role("button", name="Extensions", exact=True).click()  # The last one scrolls into view.
    expect(page.get_by_role("region", name="Extensions")).to_be_visible()
    ui.js("new Promise(done => setTimeout(done, 400))")  # A smooth scroll settles.
    button = page.get_by_role("button", name="Extensions", exact=True).bounding_box()
    nav_box = ui.js("document.querySelector('.agent-tools-tabs').getBoundingClientRect().toJSON()")
    assert button["x"] >= nav_box["left"] - 1 and button["x"] + button["width"] <= nav_box["right"] + 1


def test_wide_navigation_is_a_grouped_column(ui, page):
    open_dialog(ui)
    nav = page.get_by_role("navigation", name="Agent tools sections")
    expect(nav.get_by_role("group")).to_have_count(2)
    expect(nav.get_by_role("group", name="This chat").get_by_role("button")).to_have_text(TABS[:6])
    expect(nav.get_by_role("group", name="Project").get_by_role("button")).to_have_text(TABS[6:])
    expect(nav.locator(".agent-tools-group-label")).to_have_text(["This chat", "Project"])
    boxes = [page.get_by_role("button", name=tab, exact=True).bounding_box() for tab in TABS]
    assert len({round(box["x"]) for box in boxes}) == 1 and all(a["y"] < b["y"] for a, b in zip(boxes, boxes[1:]))
    body = ui.js("document.querySelector('.agent-tools-body').getBoundingClientRect().toJSON()")
    assert boxes[0]["x"] + boxes[0]["width"] <= body["left"] + 1  # The navigation is left of the content.


@pytest.mark.parametrize("width,height", [(360, 740), (390, 844), (1280, 900)])
def test_usage_heading_fits(ui, page, width, height):
    page.set_viewport_size({"width": width, "height": height})
    open_tab(ui, page, "Usage")
    expect(page.get_by_role("button", name="Refresh usage")).to_be_visible()
    result = ui.js("""(() => {
      const heading = document.querySelector('.usage-panel .agent-tools-section-heading')
      const content = document.querySelector('.agent-tools-content')
      const right = content.getBoundingClientRect().right - parseFloat(getComputedStyle(content).paddingRight)
      const buttons = [...heading.querySelectorAll('button')].map(b => b.getBoundingClientRect())
      const text = heading.querySelector(':scope > div:first-child').getBoundingClientRect()
      return { fits: buttons.every(b => b.right <= right + 1), scroll: heading.scrollWidth <= heading.clientWidth,
               beside: buttons.every(b => b.top < text.bottom) }
    })()""")
    assert result["fits"] and result["scroll"], result
    if width >= 1280:
        assert result["beside"], result


def test_a_long_task_title_keeps_a_square_checkbox_beside_its_first_line(ui, page):
    # V-T13. Mutation: restore align-items:center on .agent-tools-task (the box shrinks to 10 px and moves 14 px down).
    title = "Inspect the source of the parser and its tests, then list every place that reads the configuration file directly"
    open_tab(ui, page, "Tasks", f"window.task = {{ ...task, title: {json.dumps(title + ' before startup')} }}")
    checkbox = page.get_by_role("checkbox", name=re.compile("^Complete task Inspect the source"))
    expect(checkbox).to_be_visible()
    box = checkbox.bounding_box()
    text = page.locator(".agent-tools-task strong").first
    assert text.evaluate("e => e.getClientRects().length > 1 || e.getBoundingClientRect().height > 30")  # it wraps
    assert round(box["width"]) == 13 and round(box["height"]) == 13, box
    assert abs(box["y"] - text.bounding_box()["y"]) <= 6, (box, text.bounding_box())


# Overview: this chat and its project at a glance, with a way into each section

def overview_row(page, label):
    return page.get_by_role("region", name="Overview").locator(".overview-row").filter(
        has=page.locator("dt", has_text=re.compile(f"^{re.escape(label)}$")))


def test_overview_is_the_first_section_and_sums_up_the_chat_and_project(ui, page):
    open_dialog(ui, props=", changedFiles: 3")
    expect(page.get_by_role("button", name="Overview", exact=True)).to_have_attribute("aria-pressed", "true")
    expected = {"Model": "Test model · test-model", "Permission mode": "Manual", "Tools": "6 of 7 on", "Subagents": "1 running · 0 finished",
                "Tasks": "0 of 1 done", "Unreviewed changes": "3 files", "Checkpoints": "1 file checkpoint", "Verify": "pytest -q",
                "Worktrees": "1 managed worktree", "Extensions": "None configured"}
    for label, value in expected.items():
        expect(overview_row(page, label).locator(".overview-value")).to_have_text(value)
    expect(page.get_by_role("region", name="Overview")).to_contain_text("/project")
    assert ui.spy("onError") == []


def test_overview_names_the_chats_reasoning_effort(ui, page):
    open_dialog(ui, selected="{ ...session(), reasoning_effort: 'xhigh' }")
    expect(overview_row(page, "Reasoning effort").locator(".overview-value")).to_have_text("Extra high")


def test_overview_buttons_open_their_sections(ui, page):
    open_dialog(ui)
    regions = {"Tools": "Tools", "Subagents": "Subagents", "Tasks": "Tasks", "Usage": "Usage", "Export": "Conversation transfer",
               "Checkpoints": "Checkpoints", "Verify": "Verify", "Worktrees": "Worktrees", "Extensions": "Extensions"}
    for tab, region in regions.items():
        page.get_by_role("button", name=f"Open {tab}", exact=True).click()
        expect(page.get_by_role("button", name=tab, exact=True)).to_have_attribute("aria-pressed", "true")
        expect(page.get_by_role("region", name=region, exact=True)).to_be_visible()
        page.get_by_role("button", name="Overview", exact=True).click()
        expect(page.get_by_role("region", name="Overview")).to_be_visible()
    # Model and mode are shown, not sections of this dialog.
    expect(overview_row(page, "Model").get_by_role("button")).to_have_count(0)


def test_overview_reports_what_it_cannot_load_without_an_error_banner(ui, page):
    open_dialog(ui, """fake.override = path => path === '/api/extensions' ? fake.json({ detail: 'Broken' }, 500)
      : path === '/api/worktrees?session_id=a' ? fake.json([]) : path === '/api/project-settings?session_id=a'
      ? fake.json({ ...projectSettings, verify_command: '' }) : undefined""")
    expect(overview_row(page, "Extensions").locator(".overview-value")).to_have_text("Could not load")
    expect(overview_row(page, "Worktrees").locator(".overview-value")).to_have_text("None")
    expect(overview_row(page, "Verify").locator(".overview-value")).to_have_text("Off")
    expect(overview_row(page, "Unreviewed changes")).to_have_count(0)  # Not known without the changes bar.
    assert ui.spy("onError") == []


def test_overview_without_a_conversation_shows_the_project(ui, page):
    open_dialog(ui, selected="null")
    overview = page.get_by_role("region", name="Overview")
    expect(overview).to_contain_text("Start a conversation to see its tools, tasks, subagents and usage here.")
    expect(overview_row(page, "Checkpoints").locator(".overview-value")).to_have_text("1 file checkpoint")
    expect(overview_row(page, "Tools")).to_have_count(0)
    assert not ui.js("fake.calls.some(call => call.path.includes('/sessions/') || call.path.startsWith('/api/tools'))")


# Subagents: each child chat with its type, status, work and transcript

def test_subagents_lists_each_child_and_opens_it(ui, page):
    open_tab(ui, page, "Subagents")
    section = page.get_by_role("region", name="Subagents")
    row = section.locator(".subagent-row")
    expect(row).to_have_count(1)
    expect(row.locator("strong")).to_have_text("Review the parser module")
    expect(row).to_contain_text("Explore · Waiting for approval · Read-only files")
    expect(row).to_contain_text("up to 10 steps · 3 completed actions")
    expect(row).to_contain_text("Started ")
    expect(row.locator("code")).to_have_text("conversations/child.jsonl")
    section.get_by_role("button", name="Open subagent Child review").click()
    assert ui.spy("select") == [["child"]] and ui.spy("close") == [[]]


def test_a_finished_subagent_shows_steps_tool_calls_and_how_long_it_ran(ui, page):
    open_tab(ui, page, "Subagents", """window.child = { ...child, status: 'idle',
      delegation: { ...child.delegation, status: 'completed', terminal_reason: 'completed', ended: 1125 },
      subagent: { ...child.subagent, status: 'completed', ended: 1125, steps_used: 4, tool_calls: 6 } }""")
    row = page.locator(".subagent-row")
    expect(row).to_contain_text("Explore · completed · Read-only files")
    expect(row).to_contain_text("4 of 10 steps · 6 tool calls")
    expect(row).to_contain_text("(2 min 05 s)")


def test_a_subagent_from_before_the_index_falls_back_to_its_header(ui, page):
    open_tab(ui, page, "Subagents", """window.child = { ...session('old'), title: 'Old child', status: 'idle', tool_profile: 'inherit',
      created: 1000, delegation: { status: 'completed', completed_tools: 2, terminal_reason: 'completed' } }""")
    row = page.locator(".subagent-row")
    expect(row.locator("strong")).to_have_text("Old child")
    expect(row).to_contain_text("completed · Inherit permitted tools")
    expect(row.locator("code")).to_have_text("conversations/old.jsonl")


# Tools: grouped by category, with a filter and per-group switches

def test_tools_are_grouped_and_each_group_can_be_turned_off(ui, page):
    open_tab(ui, page, "Tools")
    tools = page.get_by_role("region", name="Tools")
    groups = tools.get_by_role("group")
    expect(groups).to_have_count(6)
    assert [groups.nth(index).get_attribute("aria-label") for index in range(6)] == [
        "Files", "Search", "Commands and jobs", "Tasks and subagents", "Skills", "Other"]
    files = tools.get_by_role("group", name="Files")
    expect(files.get_by_role("checkbox")).to_have_count(2)
    expect(files.locator("h4")).to_contain_text("2 of 2 on")
    expect(tools.locator(".tools-count")).to_have_text("6 of 7 on")
    read = files.get_by_role("checkbox", name="read_file", exact=True)
    expect(read).to_have_accessible_description("Read numbered lines from a text file.")  # The first sentence.
    expect(files.locator("label.tools-toggle").first).to_have_attribute(
        "title", "Read numbered lines from a text file. Pages follow next_line.")
    tools.get_by_role("button", name="Turn off all Files tools").click()
    expect(files.get_by_role("checkbox", checked=True)).to_have_count(0)
    expect(tools.get_by_role("button", name="Turn off all Files tools")).to_be_disabled()
    tools.get_by_role("button", name="Turn on all Commands and jobs tools").click()
    expect(tools.get_by_role("checkbox", name="run_command", exact=True)).to_be_checked()
    tools.get_by_role("button", name="Save tools").click()
    expect(tools.get_by_role("status")).to_have_text("Tools saved for this chat.")
    assert ui.bodies("/api/sessions/a/disabled-tools", "PUT") == [{"tools": ["read_file", "write_file"]}]


def test_the_tool_filter_matches_names_groups_and_descriptions(ui, page):
    open_tab(ui, page, "Tools")
    tools = page.get_by_role("region", name="Tools")
    search = tools.get_by_role("searchbox", name="Filter tools")
    search.fill("subagent")
    expect(tools.get_by_role("checkbox")).to_have_count(1)
    expect(tools.get_by_role("checkbox", name="delegate_task", exact=True)).to_be_visible()
    search.fill("COMMANDS")
    expect(tools.get_by_role("group")).to_have_count(1)
    expect(tools.get_by_role("group", name="Commands and jobs")).to_be_visible()
    # A group switch acts on the tools it shows.
    search.fill("file")
    expect(tools.get_by_role("group", name="Files").get_by_role("checkbox")).to_have_count(2)
    expect(tools.get_by_role("group", name="Search").get_by_role("checkbox")).to_have_count(1)
    tools.get_by_role("button", name="Turn off all Search tools").click()
    search.fill("nothing like this")
    expect(tools.get_by_text("No tools match “nothing like this”.")).to_be_visible()
    search.fill("")
    expect(tools.get_by_role("checkbox", name="find_files", exact=True)).not_to_be_checked()
    expect(tools.get_by_role("checkbox")).to_have_count(7)


# The last section is remembered in this browser

def test_the_last_section_is_remembered(ui, page):
    open_tab(ui, page, "Verify")
    expect(page.get_by_role("region", name="Verify")).to_be_visible()
    ui.run("unmount()")
    ui.page.evaluate("() => mount('components/AgentToolsDialog.js', () => dialogProps(session('b')))")
    expect(page.get_by_role("button", name="Verify", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_role("region", name="Verify")).to_be_visible()
    ui.run("localStorage.setItem('agent-tools-tab', 'Recovery'); unmount()")  # An unknown name: Overview.
    ui.page.evaluate("() => mount('components/AgentToolsDialog.js', () => dialogProps())")
    expect(page.get_by_role("button", name="Overview", exact=True)).to_have_attribute("aria-pressed", "true")


def test_blocked_storage_falls_back_to_the_overview(ui, page):
    open_dialog(ui, "Object.defineProperty(window, 'localStorage', { configurable: true, get() { throw new Error('blocked') } })")
    page.get_by_role("button", name="Subagents", exact=True).click()
    expect(page.get_by_role("region", name="Subagents")).to_be_visible()


def test_every_section_is_readable(ui, page):
    from test_visual_tokens import CONTRAST_JS
    page.add_init_script(script=CONTRAST_JS)
    open_dialog(ui, props=", changedFiles: 1")
    expect(overview_row(page, "Tools").locator(".overview-value")).to_have_text("6 of 7 on")
    for tab in TABS:
        page.get_by_role("button", name=tab, exact=True).click()
        expect(page.get_by_role("button", name=tab, exact=True)).to_have_attribute("aria-pressed", "true")
        ui.js("new Promise(done => setTimeout(done, 50))")
        problems = page.evaluate("() => [...visual.failures(document.querySelector('.agent-tools-dialog')), "
                                 "...visual.iconFailures(document.querySelector('.agent-tools-dialog'))]")
        assert problems == [], (tab, problems)
        assert page.evaluate("() => visual.smallText(document.querySelector('.agent-tools-dialog'))") == [], tab


# Extensions: the form editor, its JSON round trip, and the import preview

FULL_CONFIG = {
    "servers": [
        {"id": "github", "name": "GitHub", "transport": "stdio", "command": "github-mcp-server", "args": ["stdio", ""],
         "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": {"secret": "GITHUB_TOKEN"}, "LOG_LEVEL": "debug"}, "enabled": True},
        {"id": "sql", "name": "Workspace SQL", "transport": "http", "url": "https://workspace.example.com/api/2.0/mcp/sql",
         "headers": {"X-Api-Key": {"secret": "API_KEY", "prefix": "Key "}}, "workspace_credentials": True, "enabled": False},
    ],
    "hooks": [{"id": "check", "event": "before_tool", "tools": ["write_file"], "command": "check-edit", "timeout_seconds": 5,
               "enabled": True},
              {"id": "stop", "event": "agent_stop", "command": "stop-check", "timeout_seconds": 10, "enabled": False}],
}


def open_extensions(ui, page, config=FULL_CONFIG):
    open_tab(ui, page, "Extensions", f"window.config = {json.dumps(config)}")
    expect(page.get_by_role("textbox", name="Server 1 ID")).to_have_value("github") if config["servers"] else None


def test_the_form_shows_servers_hooks_and_references_and_round_trips(ui, page):
    open_extensions(ui, page)
    expect(page.get_by_role("textbox", name="Server 1 command")).to_have_value("github-mcp-server")
    expect(page.get_by_role("textbox", name="Server 1 arguments")).to_have_value("stdio\n")
    expect(page.get_by_role("combobox", name="Server 1 environment variable 1 kind")).to_have_value("secret")
    expect(page.get_by_role("textbox", name="Server 1 environment variable 1 credential-file key")).to_have_value("GITHUB_TOKEN")
    expect(page.get_by_role("textbox", name="Server 1 environment variable 2 value")).to_have_value("debug")
    expect(page.get_by_role("textbox", name="Server 2 URL")).to_have_value("https://workspace.example.com/api/2.0/mcp/sql")
    expect(page.get_by_role("textbox", name="Server 2 header 1 prefix")).to_have_value("Key ")
    expect(page.get_by_role("checkbox", name="Server 2 uses workspace credentials")).to_be_checked()
    expect(page.get_by_role("checkbox", name="Server 2 enabled")).not_to_be_checked()
    expect(page.get_by_role("textbox", name="Hook 1 tool names")).to_have_value("write_file")
    expect(page.get_by_role("combobox", name="Hook 2 event")).to_have_value("agent_stop")
    # Form -> JSON -> form keeps every field.
    editor = json_editor(page)
    assert json.loads(editor.input_value()) == FULL_CONFIG
    page.get_by_role("button", name="Form", exact=True).click()
    page.get_by_role("button", name="Save configuration").click()
    expect(page.get_by_text("Configuration saved.")).to_be_visible()
    assert ui.bodies("/api/extensions", "PUT") == [FULL_CONFIG]


def test_editing_in_the_form_saves_the_changed_configuration(ui, page):
    open_extensions(ui, page)
    page.get_by_role("combobox", name="Server 1 environment variable 2 kind").select_option("secret")
    page.get_by_role("textbox", name="Server 1 environment variable 2 credential-file key").fill("LEVEL")
    page.get_by_role("button", name="Remove Server 2 header 1").click()
    page.get_by_role("checkbox", name="Server 2 uses workspace credentials").uncheck()
    page.get_by_role("button", name="Remove hook 2").click()
    page.get_by_role("combobox", name="Hook 1 tools").select_option("all")
    page.get_by_role("button", name="Add server").click()
    page.get_by_role("textbox", name="Server 3 ID").fill("docs")
    page.get_by_role("combobox", name="Server 3 transport").select_option("http")
    page.get_by_role("textbox", name="Server 3 URL").fill("https://mcp.example.com/mcp")
    page.get_by_role("button", name="Add header").last.click()
    page.get_by_role("textbox", name="Server 3 header 1 name").fill("Authorization")
    page.get_by_role("textbox", name="Server 3 header 1 credential-file key").fill("DOCS_TOKEN")
    page.get_by_role("textbox", name="Server 3 header 1 prefix").fill("Bearer ")
    page.get_by_role("button", name="Save configuration").click()
    expect(page.get_by_text("Configuration saved.")).to_be_visible()
    [saved] = ui.bodies("/api/extensions", "PUT")
    assert saved["servers"][0]["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": {"secret": "GITHUB_TOKEN"}, "LOG_LEVEL": {"secret": "LEVEL"}}
    assert "headers" not in saved["servers"][1] and "workspace_credentials" not in saved["servers"][1]
    assert saved["servers"][2] == {"id": "docs", "name": "docs", "transport": "http", "url": "https://mcp.example.com/mcp",
                                   "headers": {"Authorization": {"secret": "DOCS_TOKEN", "prefix": "Bearer "}}, "enabled": False}
    assert saved["hooks"] == [{"id": "check", "event": "before_tool", "command": "check-edit", "timeout_seconds": 5, "enabled": True}]


def test_json_that_does_not_parse_keeps_the_json_view(ui, page):
    open_extensions(ui, page)
    editor = json_editor(page)
    editor.fill('{"servers": [')
    page.get_by_role("button", name="Form", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("The JSON is not valid")
    expect(editor).to_be_visible()
    editor.fill(json.dumps({"servers": [{**FULL_CONFIG["servers"][0], "name": "Changed in JSON"}], "hooks": []}))
    page.get_by_role("button", name="Form", exact=True).click()
    expect(page.get_by_role("textbox", name="Server 1 name")).to_have_value("Changed in JSON")
    expect(page.get_by_role("textbox", name="Server 2 ID")).to_have_count(0)


def test_import_preview_adds_disabled_servers_to_the_form_without_saving(ui, page):
    open_extensions(ui, page, {"servers": [{"id": "github", "name": "Mine", "transport": "stdio", "command": "mine", "args": [],
                                            "enabled": True}], "hooks": []})
    page.get_by_text("Import MCP servers").click()
    page.get_by_role("button", name="This project's .mcp.json").click()
    preview = page.get_by_role("region", name="Import preview")
    expect(preview.get_by_role("heading")).to_contain_text("1 server to add, disabled")
    expect(preview).to_contain_text("GITHUB_TOKEN")
    expect(preview).to_contain_text("SSE servers are not supported")
    expect(preview).to_contain_text("runs its command (for example npx or uvx)")
    assert ui.bodies("/api/extensions/import?session_id=a", "POST") == [{"source": "project"}]
    assert ui.requests("/api/extensions", "PUT") == []
    preview.get_by_role("button", name="Add to the form").click()
    expect(page.get_by_role("region", name="Import preview")).to_have_count(0)
    expect(page.get_by_role("status")).to_contain_text("Added 1 disabled server to the form")
    expect(page.get_by_role("textbox", name="Server 2 ID")).to_have_value("github-2")  # Unique in the form.
    expect(page.get_by_role("checkbox", name="Server 2 enabled")).not_to_be_checked()
    assert ui.requests("/api/extensions", "PUT") == []
    page.get_by_role("button", name="Save configuration").click()
    expect(page.get_by_text("Configuration saved.")).to_be_visible()
    [saved] = ui.bodies("/api/extensions", "PUT")
    assert saved["servers"][1]["id"] == "github-2" and saved["servers"][1]["enabled"] is False
    assert saved["servers"][1]["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": {"secret": "GITHUB_TOKEN"}}
    # Pasted text is previewed the same way.
    page.get_by_role("textbox", name="Configuration to import (JSON)").fill('{"mcpServers": {}}')
    page.get_by_role("button", name="Preview pasted servers").click()
    expect(page.get_by_role("region", name="Import preview")).to_be_visible()
    assert ui.bodies("/api/extensions/import", "POST")[-1] == {"source": "text", "text": '{"mcpServers": {}}'}


def test_a_refused_import_is_shown(ui, page):
    open_tab(ui, page, "Extensions", """fake.override = path => path.startsWith('/api/extensions/import')
      ? fake.json({ detail: 'No .mcp.json was found.' }, 404) : undefined""")
    page.get_by_text("Import MCP servers").click()
    page.get_by_role("button", name="This project's .mcp.json").click()
    expect(page.get_by_role("alert").first).to_contain_text("No .mcp.json was found.")


def test_each_mcp_server_can_be_turned_off_in_the_chat(ui, page):
    open_tab(ui, page, "Tools", """window.toolList = { ...toolList, disabled: ['run_command', 'mcp__gone__*', 'mcp__docs__search'],
      servers: [{ id: 'docs', name: 'Docs', enabled: true }, { id: 'sql', name: 'Workspace SQL', enabled: false }] }""")
    tools = page.get_by_role("region", name="Tools")
    docs = tools.get_by_role("checkbox", name="Use the Docs server in this chat")
    expect(docs).to_be_checked()
    expect(tools.get_by_role("group", name="MCP servers")).to_contain_text("Disabled in Extensions")
    # A switch for a server no longer configured is listed with the turned-off MCP tools.
    expect(tools.get_by_role("list", name="Turned-off MCP tools")).to_contain_text("mcp__gone__*")
    docs.uncheck()
    tools.get_by_role("button", name="Save tools").click()
    expect(tools.get_by_role("status")).to_have_text("Tools saved for this chat.")
    assert ui.bodies("/api/sessions/a/disabled-tools", "PUT") == [{"tools": ["run_command", "mcp__gone__*", "mcp__docs__search", "mcp__docs__*"]}]
    expect(tools.get_by_role("list", name="Turned-off MCP tools")).not_to_contain_text("mcp__docs__*")


def test_the_extensions_form_is_readable(ui, page):
    from test_visual_tokens import CONTRAST_JS
    page.add_init_script(script=CONTRAST_JS)
    open_extensions(ui, page)
    page.get_by_text("Import MCP servers").click()
    page.get_by_role("button", name="This project's .mcp.json").click()
    expect(page.get_by_role("region", name="Import preview")).to_be_visible()
    problems = page.evaluate("() => [...visual.failures(document.querySelector('.agent-tools-dialog')), "
                             "...visual.iconFailures(document.querySelector('.agent-tools-dialog'))]")
    assert problems == []
    assert page.evaluate("() => visual.smallText(document.querySelector('.agent-tools-dialog'))") == []


@pytest.mark.parametrize("width", [390, 1280])
def test_the_extensions_form_fits_without_sideways_scrolling(ui, page, width):
    page.set_viewport_size({"width": width, "height": 900})
    open_extensions(ui, page)
    overflow = page.evaluate("""() => [...document.querySelectorAll('.extension-entry')].filter(entry =>
      entry.scrollWidth > entry.clientWidth + 1).length""")
    assert overflow == 0
