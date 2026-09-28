"""Whole-app behaviour: conversations, streaming, models, context, and editor drafts."""
import json
import re

import pytest
from playwright.sync_api import expect


def start_app(ui, page, session=None, setup="", clock=False):
    ui.app(session=session, scenario="workspace", setup=setup, clock=clock)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")


def start_hook(ui, session=None, setup="", clock=False):
    ui.probe("useWorkspace.js", "useWorkspace", session=session, scenario="workspace", setup=setup, clock=clock)


def pending(name, condition):
    """Setup that holds matching requests until the test resolves fake.deferreds[name]."""
    return f"const {name} = fake.defer('{name}'); fake.override = (path, options) => ({condition}) ? {name}.promise : undefined"


def resolve(ui, name, value):
    ui.run(f"fake.deferreds.{name}.resolve(fake.json({value}))")


def row(page, title):
    return page.get_by_role("button", name=re.compile(f"^{re.escape(title)}"))


def main(page):
    return page.get_by_role("main")


def model(page):
    return page.get_by_role("combobox", name="Model")


def send(page):
    return page.get_by_role("button", name="Send message")


def message(page):
    return page.get_by_role("textbox", name="Message")


def open_editor(page):
    page.get_by_role("button", name="Workspace", exact=True).click()
    page.get_by_role("button", name="note.txt").click()
    editor = page.get_by_role("textbox", name="Edit note.txt")
    expect(editor).to_be_visible()
    return editor


def settle(ui):
    """Let pending promise callbacks and re-renders finish (a task that a faked clock does not hold back)."""
    ui.js("new Promise(done => { const channel = new MessageChannel(); channel.port1.onmessage = () => done(); channel.port2.postMessage(0) })")


# Conversation activity

def test_sidebar_status_ignores_an_older_idle_list(ui, page):
    start_app(ui, page, "a")
    ui.run("""const stale = fake.defer('stale'); window.idleList = [...fake.sessions.values()]; let delay = true
      fake.override = path => { if (path === '/api/sessions' && delay) { delay = false; return stale.promise } }""")
    ui.show_session("a")
    dot = row(page, "Other conversation").locator(".activity-dot")
    expect(dot).to_have_count(0)
    ui.emit("a", {"type": "status", "status": "running"})
    expect(dot).to_have_count(1)
    ui.run("fake.deferreds.stale.resolve(fake.json(idleList))")
    settle(ui)
    expect(dot).to_have_count(1)
    ui.emit("a", {"type": "status", "status": "idle"})
    expect(dot).to_have_count(0)


def test_polls_an_active_background_conversation_until_idle(ui, page):
    start_app(ui, page, "a", clock=True)
    ui.show_session("a")
    ui.emit("a", {"type": "status", "status": "running"})
    dot = row(page, "Other conversation").locator(".activity-dot")
    expect(dot).to_have_count(1)
    page.get_by_role("button", name="New conversation", exact=True).click()
    ui.run("fake.sessions.set('a', { ...fake.sessions.get('a'), status: 'idle' })")
    ui.tick(1500)
    expect(dot).to_have_count(0)
    polls = len(ui.requests("/api/sessions", "GET"))
    ui.tick(3000)
    assert len(ui.requests("/api/sessions", "GET")) == polls


# Settings

def test_settings_edit_and_save_only_databricks_fields(ui, page):
    start_app(ui, page, setup="""fake.override = (path, options) => {
      if (path === '/api/bootstrap') return fake.json({ token: 'test-token', settings: { ...baseSettings, runtime: 'claude',
        claude_cli_path: '/old/cli', claude_mcp_config: '/old/mcp.json', claude_skills: true } })
      if (path === '/api/settings' && options.method === 'PUT') return fake.json({ ...baseSettings, ...JSON.parse(options.body) })
    }""")
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.get_by_role("dialog")
    for label in ["Project folder", "Databricks model endpoint", "Credential file"]:
        expect(dialog.get_by_label(label)).to_be_visible()
    budget = dialog.get_by_label("Context budget (tokens)")
    expect(budget).to_have_value("131072")
    expect(budget).to_have_attribute("min", "16384")
    expect(budget).to_have_attribute("max", "1048576")
    expect(dialog.get_by_text(re.compile("Set at or below your endpoint's total context limit, not your account usage quota"))).to_be_visible()
    expect(dialog.get_by_text(re.compile("reserve the output budget below plus 2,048 tokens for safety"))).to_be_visible()
    output = dialog.get_by_label("Max output tokens per request")
    expect(output).to_have_value("8192")
    expect(output).to_have_attribute("min", "1024")
    expect(output).to_have_attribute("max", "131072")
    expect(dialog.get_by_text("Current estimated input budget: 120,832 tokens.", exact=False)).to_be_visible()
    endpoint = dialog.get_by_label("Databricks model endpoint")
    endpoint.fill("databricks-claude-opus-4-8")
    budget.fill("131000")
    output.fill("121000")
    expect(dialog.get_by_text(re.compile("leaves too little room for a working conversation"))).to_be_visible()
    expect(dialog.get_by_text(re.compile("reserves max_tokens against a 20,000 output-tokens-per-minute limit"))).to_be_visible()
    output.fill("20000")
    expect(dialog.get_by_text(re.compile("reserves the full standard Claude Opus 4.8 output-per-minute quota"))).to_be_visible()
    expect(dialog.get_by_text(re.compile("8,192 default leaves room for multi-step work"))).to_be_visible()
    endpoint.fill("test-model")
    budget.fill("131072")
    output.fill("8192")
    steps = dialog.get_by_label("Agent steps per message")
    expect(steps).to_have_value("32")
    expect(steps).to_have_attribute("min", "1")
    expect(steps).to_have_attribute("max", "64")
    expect(dialog.get_by_text(re.compile("not individual tool calls"))).to_be_visible()
    expect(dialog.get_by_label("Agent runtime")).to_have_count(0)
    expect(dialog.get_by_text(re.compile("Claude|MCP|skills", re.I))).to_have_count(0)
    checkboxes = dialog.get_by_role("checkbox")
    expect(checkboxes).to_have_count(1)
    expect(dialog.locator("label:has(input[type=checkbox])")).to_have_text("Save a detailed handoff at each compaction")
    dialog.get_by_label("Project folder").fill("/updated-project")
    endpoint.fill("databricks-gpt-oss-120b")
    dialog.get_by_label("Credential file").fill("/credentials/env_vars.txt")
    budget.fill("65536")
    output.fill("32768")
    steps.fill("48")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT") == [{
        "workspace": "/updated-project", "model": "databricks-gpt-oss-120b", "env_file": "/credentials/env_vars.txt",
        "context_window": 65536, "max_output_tokens": 32768, "max_agent_steps": 48}]


def test_settings_open_with_focus_inside_the_dialog(ui, page):
    start_app(ui, page)
    page.get_by_role("button", name="Settings", exact=True).click()
    expect(page.get_by_role("button", name="Close settings")).to_be_focused()


def test_compaction_handoffs_can_be_turned_off(ui, page):
    start_app(ui, page)
    page.get_by_role("button", name="Settings", exact=True).click()
    toggle = page.get_by_role("dialog").get_by_role("checkbox", name="Save a detailed handoff at each compaction")
    expect(toggle).to_be_checked()
    toggle.click()
    expect(toggle).not_to_be_checked()
    page.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["compaction_handoffs"] is False


# Renaming

def test_rename_running_chat_keeps_stream_and_blocks_duplicates(ui, page):
    start_app(ui, page, "a", setup=pending("title", "path === '/api/sessions/a/title'"))
    ui.show_session("a")
    ui.emit("a", {"type": "status", "status": "running"})
    page.get_by_role("button", name="Rename Other conversation").click()
    field = page.get_by_role("textbox", name="Conversation name")
    expect(field).to_have_attribute("maxlength", "160")
    expect(field).to_be_focused()
    field.fill("  Clear   project name  ")
    page.get_by_role("form", name="Rename Other conversation").evaluate(
        "async form => { form.requestSubmit(); await new Promise(done => setTimeout(done)); form.requestSubmit() }")
    expect(page.get_by_role("button", name="Saving…")).to_be_disabled()
    expect(page.get_by_role("button", name="Cancel rename")).to_be_disabled()
    expect(field).to_be_disabled()
    ui.emit("a", {"type": "event", "event": {"id": "reply", "type": "assistant", "text": "Still "}})
    ui.emit("a", {"type": "delta", "id": "reply", "text": "streaming"})
    resolve(ui, "title", "{ ...makeSession('a', 'Clear project name'), events: [] }")
    expect(main(page).get_by_text("Clear project name")).to_be_visible()
    expect(page.get_by_text("Still streaming")).to_be_visible()
    expect(page.get_by_role("button", name="Stop response")).to_be_visible()
    expect(page.get_by_role("button", name="Rename Clear project name")).to_be_focused()
    requests = ui.requests("/api/sessions/a/title")
    assert [request["method"] for request in requests] == ["PUT"]
    assert json.loads(requests[0]["body"]) == {"title": "  Clear   project name  "}


def test_rename_inactive_chat_keeps_selection_and_draft(ui, page):
    start_app(ui, page, "a", setup="fake.sessions.set('b', makeSession('b', 'Second conversation'))")
    ui.show_session("a")
    message(page).fill("Unsent draft")
    page.get_by_role("button", name="Rename Second conversation").click()
    page.get_by_role("textbox", name="Conversation name").fill("Renamed second chat")
    page.get_by_role("button", name="Save name").click()
    expect(page.get_by_role("button", name="Rename Renamed second chat")).to_be_visible()
    expect(main(page).get_by_text("Other conversation")).to_be_visible()
    expect(message(page)).to_have_value("Unsent draft")
    assert ui.js("new URL(location.href).searchParams.get('session')") == "a"
    assert ui.js("fake.sockets.length") == 1


@pytest.mark.parametrize("action", ["Escape", "Cancel"])
def test_cancel_rename_restores_focus_and_discards_name(ui, page, action):
    start_app(ui, page)
    page.get_by_role("button", name="Rename Other conversation").click()
    field = page.get_by_role("textbox", name="Conversation name")
    field.fill("   ")
    expect(page.get_by_role("button", name="Save name")).to_be_disabled()
    field.fill("Discard this name")
    if action == "Escape":
        field.press("Escape")
    else:
        page.get_by_role("button", name="Cancel rename").click()
    expect(page.get_by_role("textbox", name="Conversation name")).to_have_count(0)
    button = page.get_by_role("button", name="Rename Other conversation")
    expect(button).to_be_focused()
    assert not ui.js("fake.calls.some(call => call.path.endsWith('/title'))")
    button.click()
    expect(page.get_by_role("textbox", name="Conversation name")).to_have_value("Other conversation")


def test_failed_rename_stays_editable_and_can_retry(ui, page):
    start_app(ui, page, setup="fake.override = path => path === '/api/sessions/a/title' ? fake.json({ detail: 'Could not save the conversation.' }, 503) : undefined")
    page.get_by_role("button", name="Rename Other conversation").click()
    page.get_by_role("textbox", name="Conversation name").fill("Try this name")
    page.get_by_role("button", name="Save name").click()
    expect(page.get_by_role("alert")).to_have_text("Could not save the conversation.")
    expect(page.get_by_role("textbox", name="Conversation name")).to_have_value("Try this name")
    assert ui.js("fake.sessions.get('a').title") == "Other conversation"
    ui.run("fake.override = () => undefined")
    page.get_by_role("button", name="Save name").click()
    expect(page.get_by_role("button", name="Rename Try this name")).to_be_visible()
    expect(page.get_by_role("alert")).to_have_count(0)


def test_late_rename_response_does_not_reopen_the_chat(ui):
    start_hook(ui, "a", setup="fake.sessions.set('b', makeSession('b', 'Second conversation'))")
    ui.show_session("a")
    ui.run(pending("title", "path === '/api/sessions/a/title'"))
    ui.run("window.saving = hook.renameSession('a', 'Renamed first chat')")
    ui.run("hook.setActiveId('b')")
    ui.show_session("b")
    ui.emit("b", {"type": "event", "event": {"id": "b-reply", "type": "assistant", "text": "Second chat reply"}})
    resolve(ui, "title", "makeSession('a', 'Renamed first chat')")
    ui.js("saving")
    ui.wait("hook.sessions.find(item => item.id === 'a').title === 'Renamed first chat'")
    assert ui.js("hook.activeId") == "b"
    assert ui.js("hook.session.title") == "Second conversation"
    assert ui.js("hook.session.events[0].text") == "Second chat reply"


def test_sidebar_summary_from_before_a_rename_is_ignored(ui):
    start_hook(ui)
    ui.wait("hook.ready")
    ui.run("window.oldList = [...fake.sessions.values()]")
    ui.run(pending("list", "path === '/api/sessions'"))
    ui.run("window.refreshing = hook.refreshSessions()")
    ui.js("hook.renameSession('a', 'A durable name')")
    ui.run("fake.deferreds.list.resolve(fake.json(oldList))")
    ui.js("refreshing")
    settle(ui)
    assert ui.js("hook.sessions[0].title") == "A durable name"


# Conversation models

def test_idle_model_change_keeps_history_and_defaults(ui, page):
    start_app(ui, page, "a", setup="""fake.sessions.set('a', { ...fake.sessions.get('a'), events: [
        { id: 'user', type: 'user', text: 'Existing request' }, { id: 'reply', type: 'assistant', text: 'Existing answer' }] })
      fake.sessions.set('b', makeSession('b', 'Second conversation'))""")
    ui.show_session("a")
    expect(model(page)).to_be_enabled()
    model(page).select_option("other-model")
    expect(model(page)).to_have_value("other-model")
    assert ui.bodies("/api/sessions/a/model", "PUT") == [{"model": "other-model"}]
    expect(page.get_by_text("Existing answer")).to_be_visible()
    assert len(ui.js("fake.sessions.get('a').events")) == 2
    assert ui.js("fake.settings.model") == "test-model"
    assert ui.requests("/api/settings") == []
    row(page, "Second conversation").click()
    ui.show_session("b")
    expect(model(page)).to_have_value("test-model")
    row(page, "Other conversation").click()
    ui.show_session("a", count=2)
    expect(model(page)).to_have_value("other-model")
    expect(page.get_by_text("Existing request")).to_be_visible()
    page.get_by_role("button", name="New conversation", exact=True).click()
    expect(model(page)).to_have_value("test-model")


def test_sending_is_blocked_while_the_model_saves(ui, page):
    start_app(ui, page, "a", setup=pending("model", "path === '/api/sessions/a/model'"))
    ui.show_session("a")
    message(page).fill("Use the selected model")
    model(page).select_option("other-model")
    expect(model(page)).to_be_disabled()
    expect(send(page)).to_be_disabled()
    message(page).press("Enter")
    send(page).click(force=True)
    assert not ui.js("fake.calls.some(call => call.path.endsWith('/messages'))")
    ui.emit("a", {"type": "event", "event": {"id": "streamed", "type": "assistant", "text": "New streamed answer"}})
    ui.run("fake.sessions.set('a', { ...fake.sessions.get('a'), model: 'other-model' })")
    resolve(ui, "model", "{ ...fake.sessions.get('a'), title: 'Stale title', events: [] }")
    expect(model(page)).to_have_value("other-model")
    expect(page.get_by_text("New streamed answer")).to_be_visible()
    expect(main(page).get_by_text("Other conversation")).to_be_visible()
    expect(send(page)).to_be_enabled()
    send(page).click()
    ui.wait("fake.requests('/api/sessions/a/messages').length === 1")


def test_model_select_disabled_while_unready_busy_or_sending(ui, page):
    ui.app(session="a", scenario="workspace", setup=pending("bootstrap", "path === '/api/bootstrap'"))
    expect(model(page)).to_be_disabled()
    resolve(ui, "bootstrap", "{ token: 'test-token', settings: baseSettings }")
    ui.wait("fake.socketsFor('a').length === 1")
    expect(model(page)).to_be_disabled()
    ui.show_session("a")
    for status in ["running", "awaiting_approval", "compacting", "naming", "delegating"]:
        ui.emit("a", {"type": "status", "status": status})
        expect(model(page)).to_be_disabled()
    ui.emit("a", {"type": "status", "status": "idle"})
    expect(model(page)).to_be_enabled()
    ui.run(pending("sending", "path.endsWith('/messages')"))
    message(page).fill("Send now")
    send(page).click()
    expect(model(page)).to_be_disabled()
    resolve(ui, "sending", "{ ok: true }")
    expect(model(page)).to_be_enabled()


def test_rejected_model_change_keeps_previous_model(ui, page):
    start_app(ui, page, "a", setup="fake.override = path => path === '/api/sessions/a/model' ? fake.json({ detail: 'Stop the response before changing models.' }, 409) : undefined")
    ui.show_session("a")
    message(page).fill("Still ready to send")
    model(page).select_option("other-model")
    expect(page.get_by_role("alert")).to_contain_text("Stop the response before changing models.")
    expect(model(page)).to_have_value("test-model")
    expect(model(page)).to_be_enabled()
    expect(send(page)).to_be_enabled()


def test_save_locks_are_scoped_across_navigation(ui, page):
    start_app(ui, page, "a", setup="fake.sessions.set('b', makeSession('b', 'Second conversation'))\n" + pending("model", "path === '/api/sessions/a/model'"))
    ui.show_session("a")
    ui.run("window.oldSocket = fake.socketFor('a')")
    message(page).fill("Saved draft")
    model(page).select_option("other-model")
    ui.run("fake.sessions.set('a', { ...fake.sessions.get('a'), model: 'other-model' })")
    row(page, "Second conversation").click()
    ui.show_session("b")
    expect(model(page)).to_be_enabled()
    ui.run("fake.sessions.set('b', { ...fake.sessions.get('b'), model: 'latest-model' })")
    ui.emit("b", {"type": "model", "model": "latest-model"})
    ui.run("oldSocket.emit({ type: 'model', model: 'ignored-old-model' })")
    expect(model(page)).to_have_value("latest-model")
    row(page, "Other conversation").click()
    ui.show_session("a", count=2)
    expect(model(page)).to_have_value("other-model")
    expect(model(page)).to_be_disabled()
    expect(send(page)).to_be_disabled()
    ui.run("fake.sessions.set('a', { ...fake.sessions.get('a'), model: 'latest-model' })")
    ui.emit("a", {"type": "model", "model": "latest-model"})
    resolve(ui, "model", "{ ...fake.sessions.get('a'), model: 'other-model' }")
    expect(model(page)).to_have_value("latest-model")
    expect(send(page)).to_be_enabled()


def test_older_model_save_does_not_replace_newer_stream(ui, page):
    start_app(ui, page, "a", setup=pending("model", "path === '/api/sessions/a/model'"))
    ui.show_session("a")
    model(page).select_option("other-model")
    ui.run("fake.sessions.set('a', { ...fake.sessions.get('a'), model: 'latest-model' })")
    ui.emit("a", {"type": "model", "model": "latest-model"})
    ui.emit("a", {"type": "status", "status": "running"})
    ui.emit("a", {"type": "event", "event": {"id": "new-event", "type": "assistant", "text": "Latest model is working"}})
    resolve(ui, "model", "{ ...fake.sessions.get('a'), model: 'other-model', status: 'idle', events: [] }")
    settle(ui)
    expect(model(page)).to_have_value("latest-model")
    expect(page.get_by_text("Latest model is working")).to_be_visible()
    expect(page.get_by_role("button", name="Stop response")).to_be_visible()


def test_reconnect_snapshot_beats_an_older_model_response(ui):
    start_hook(ui, "a", clock=True)
    ui.show_session("a")
    ui.run(pending("model", "path === '/api/sessions/a/model'"))
    ui.run("window.changing = hook.changeModel('other-model')")
    ui.run("fake.sessions.set('a', { ...fake.sessions.get('a'), model: 'latest-model' })")
    ui.run("fake.socketFor('a').close()")
    ui.tick(2000)
    ui.wait("fake.sockets.length === 2")
    ui.run("fake.sockets[1].emit({ type: 'snapshot', session: fake.sessions.get('a') })")
    ui.wait("hook.session?.model === 'latest-model'")
    resolve(ui, "model", "{ ...fake.sessions.get('a'), model: 'other-model' }")
    ui.js("changing")
    settle(ui)
    assert ui.js("hook.session.model") == "latest-model"
    assert ui.js("hook.sessions.find(item => item.id === 'a').model") == "latest-model"


def test_draft_model_changes_default_before_creating_conversation(ui, page):
    start_app(ui, page, setup="""const saving = fake.defer('settings')
      fake.override = (path, options) => {
        if (path === '/api/settings' && options.method === 'PUT') { fake.settings = { ...fake.settings, ...JSON.parse(options.body) }; return saving.promise }
      }""")
    message(page).fill("Use the new default")
    model(page).select_option("other-model")
    message(page).press("Enter")
    assert ui.requests("/api/sessions", "POST") == []
    expect(send(page)).to_be_disabled()
    resolve(ui, "settings", "fake.settings")
    expect(model(page)).to_have_value("other-model")
    assert ui.bodies("/api/settings") == [{"workspace": "/project", "model": "other-model", "env_file": "", "context_window": 131072,
                                           "max_output_tokens": 8192, "max_agent_steps": 32}]
    send(page).click()
    ui.show_session("created")
    assert ui.js("fake.sessions.get('created').model") == "other-model"
    assert not ui.js(r"fake.calls.some(call => /\/sessions\/[^/]+\/model$/.test(call.path))")


def test_folder_access_waits_for_a_pending_default_model(ui):
    start_hook(ui, setup=pending("settings", "path === '/api/settings' && options.method === 'PUT'"))
    ui.wait("hook.ready")
    ui.run("window.changing = hook.changeModel('other-model')")
    ui.wait("hook.modelSaving === true")
    ui.run("window.allowing = hook.allowFolder('/extra')")
    settle(ui)
    assert ui.requests("/api/sessions", "POST") == []
    assert ui.js("hook.activeId") is None
    ui.run("fake.settings = { ...fake.settings, model: 'other-model' }")
    resolve(ui, "settings", "fake.settings")
    ui.js("Promise.all([changing, allowing])")
    ui.show_session("created")
    ui.wait("hook.session?.model === 'other-model'")
    assert ui.js("hook.session.allowed_directories") == ["/extra"]
    assert ui.js("hook.modelSaving") is False
    assert ui.js("fake.sessions.get('created').model") == "other-model"


def test_model_change_during_folder_creation_targets_the_new_session(ui):
    start_hook(ui, setup="""const creation = fake.defer('creation'), modelSave = fake.defer('modelSave')
      fake.override = (path, options) => {
        if (path === '/api/sessions' && options.method === 'POST') return creation.promise
        if (path === '/api/sessions/created/model') return modelSave.promise
      }""")
    ui.wait("hook.ready")
    ui.run("window.allowing = hook.allowFolder('/extra')")
    ui.run("window.changing = hook.changeModel('other-model')")
    ui.wait("hook.modelSaving === true")
    assert not ui.js("fake.calls.some(call => call.path === '/api/settings' || call.path === '/api/sessions/created/model')")
    ui.run("const created = makeSession('created', 'Created conversation'); fake.sessions.set('created', created); fake.deferreds.creation.resolve(fake.json(created))")
    ui.js("allowing")
    ui.show_session("created")
    ui.wait("hook.activeId === 'created' && hook.session?.id === 'created'")
    assert ui.js("hook.modelSaving") is True
    assert ui.js("hook.session.model") == "test-model"
    ui.wait("fake.requests('/api/sessions/created/model').length === 1")
    assert ui.bodies("/api/sessions/created/model") == [{"model": "other-model"}]
    ui.run("fake.sessions.set('created', { ...fake.sessions.get('created'), model: 'other-model' })")
    resolve(ui, "modelSave", "fake.sessions.get('created')")
    ui.js("changing")
    ui.wait("hook.modelSaving === false")
    assert ui.js("hook.session.model") == "other-model"
    assert ui.js("hook.session.allowed_directories") == ["/extra"]
    assert ui.js("fake.settings.model") == "test-model"
    assert ui.requests("/api/settings") == []


# Context budget

def test_manual_compaction_keeps_stop_available(ui, page):
    start_app(ui, page, "a", setup="""fake.sessions.set('a', { ...makeSession('a'), context_info: contextInfo })
      fake.override = path => path === '/api/sessions/a/compact' ? fake.json({ ok: true }, 202) : undefined""")
    ui.show_session("a")
    page.get_by_text("Last model input · ~50%").click()
    page.get_by_role("textbox", name="Preservation note (optional)").fill("Preserve the migration decisions")
    page.get_by_role("button", name="Compact now").click()
    ui.wait("fake.requests('/api/sessions/a/compact').length === 1")
    assert ui.bodies("/api/sessions/a/compact", "POST") == [{"preservation_note": "Preserve the migration decisions"}]
    ui.emit("a", {"type": "status", "status": "compacting"})
    expect(page.get_by_role("button", name="Compact now")).to_be_disabled()
    expect(page.get_by_role("button", name="Stop response")).to_be_visible()
    ui.emit("a", "{ type: 'context', context_info: { ...contextInfo, prepared_for_next_turn: true, compactions: 2 } }")
    ui.emit("a", {"type": "status", "status": "idle"})
    expect(page.get_by_text("Context preview · ~50%")).to_be_visible()
    expect(page.get_by_role("button", name="Compact now")).to_be_enabled()


def test_one_composer_after_creating_and_switching(ui, page):
    start_app(ui, page)
    message(page).fill("Start a conversation")
    send(page).click()
    ui.show_session("created")
    ui.emit("created", {"type": "event", "event": {"id": "user", "type": "user", "text": "Start a conversation"}})
    ui.emit("created", "{ type: 'context', context_info: contextInfo }")
    expect(message(page)).to_have_count(1)
    expect(page.get_by_role("button", name=re.compile("^Permissions:"))).to_have_count(1)
    row(page, "Other conversation").click()
    ui.show_session("a")
    expect(message(page)).to_have_count(1)
    expect(page.get_by_role("button", name=re.compile("^Permissions:"))).to_have_count(1)
    row(page, "Created conversation").click()
    ui.show_session("created", count=2)
    expect(message(page)).to_have_count(1)
    expect(page.get_by_role("button", name=re.compile("^Permissions:"))).to_have_count(1)


def test_context_estimate_and_instructions_are_per_conversation(ui, page):
    start_app(ui, page, setup="""fake.sessions.set('a', { ...makeSession('a', 'Other conversation'), context_info: contextInfo })
      fake.sessions.set('b', makeSession('b', 'Empty conversation'))""")
    expect(page.get_by_text("Context usage is estimated after the first turn.")).to_be_visible()
    row(page, "Other conversation").click()
    ui.show_session("a")
    ui.run("window.firstSocket = fake.socketFor('a')")
    expect(page.get_by_text("Last model input · ~50%")).to_be_visible()
    meter = page.get_by_role("progressbar", name="Estimated model input usage")
    expect(meter).to_have_attribute("aria-valuetext", "Approximately 50% of input budget")
    page.get_by_text("Last model input · ~50%").click()
    expect(page.get_by_text("Approximately 60,380 of 120,832 input tokens used.")).to_be_visible()
    expect(page.get_by_text(re.compile("Heuristic text-size estimate, not a provider token count or billing usage"))).to_be_visible()
    expect(page.get_by_text(re.compile("Estimate for the last request; updates each model call"))).to_be_visible()
    expect(page.get_by_text(re.compile("8,192 tokens are reserved for the response and 2,048 for safety"))).to_be_visible()
    expect(page.get_by_text("Compactions: 1. Messages summarized: 8.")).to_be_visible()
    expect(page.get_by_text("AGENTS.md", exact=True)).to_be_visible()
    expect(page.get_by_text("src/AGENTS.md", exact=True)).to_be_visible()
    expect(page.get_by_text("Project instructions were truncated.")).to_be_visible()
    ui.emit("a", "{ type: 'context', context_info: { ...contextInfo, estimated_tokens: 90570, compactions: 2 } }")
    expect(page.get_by_text("Last model input · ~75%")).to_be_visible()
    expect(page.get_by_text("· 2 compactions")).to_be_visible()
    row(page, "Empty conversation").click()
    ui.show_session("b")
    ui.run("firstSocket.emit({ type: 'context', context_info: contextInfo })")
    expect(page.get_by_text("Context usage is estimated after the first turn.")).to_be_visible()
    expect(page.get_by_role("progressbar", name="Estimated model input usage")).to_have_count(0)
    expect(page.get_by_text("AGENTS.md", exact=True)).to_have_count(0)


def test_byte_based_estimates_are_not_shown_as_tokens(ui, page):
    start_app(ui, page, setup="fake.sessions.set('a', { ...makeSession('a', 'Other conversation'), context_info: { ...contextInfo, estimate_method: 'conservative_utf8' } })")
    row(page, "Other conversation").click()
    ui.show_session("a")
    page.get_by_text("Last model input · estimate outdated").click()
    expect(page.get_by_role("progressbar", name="Estimated model input usage")).to_have_count(0)
    expect(page.get_by_text(re.compile("The saved meter counted bytes as tokens"))).to_be_visible()
    expect(page.get_by_text(re.compile("Increasing the context budget does not increase the response limit"))).to_be_visible()
    ui.emit("a", "{ type: 'context', context_info: contextInfo }")
    expect(page.get_by_text("Last model input · ~50%")).to_be_visible()
    expect(page.get_by_role("progressbar", name="Estimated model input usage")).to_be_visible()
    expect(page.get_by_text(re.compile("The saved meter counted bytes as tokens"))).to_have_count(0)


def test_context_updates_ignore_abandoned_sockets(ui):
    start_hook(ui, "a", setup="fake.sessions.set('b', makeSession('b'))")
    ui.show_session("a")
    ui.run("window.firstSocket = fake.socketFor('a')")
    ui.wait("hook.sessions.length === 2")
    ui.emit("a", {"type": "status", "status": "running"})
    ui.emit("a", {"type": "event", "event": {"id": "reply", "type": "assistant", "text": "New response"}})
    ui.emit("a", "{ type: 'context', context_info: contextInfo }")
    ui.wait("hook.session?.context_info?.estimated_tokens === 60380")
    assert ui.js("hook.sessions.find(item => item.id === 'a').context_info.estimated_tokens") == 60380
    assert ui.js("hook.session.status") == "running"
    assert ui.js("hook.session.events[0].text") == "New response"
    ui.run("hook.setActiveId('b')")
    ui.show_session("b")
    ui.run("""window.nextContext = { ...contextInfo, estimated_tokens: 6000, instruction_files: [], warnings: [] }
      fake.socketFor('b').emit({ type: 'context', context_info: nextContext })
      firstSocket.emit({ type: 'context', context_info: { ...contextInfo, estimated_tokens: 99999 } })""")
    ui.wait("hook.session?.context_info?.estimated_tokens === 6000")
    assert ui.js("hook.session.id") == "b"
    assert ui.js("hook.sessions.find(item => item.id === 'b').context_info.estimated_tokens") == 6000
    assert ui.js("hook.sessions.find(item => item.id === 'a').context_info?.estimated_tokens") != 99999


def test_compaction_shows_busy_and_stop_works(ui, page):
    start_app(ui, page, "a", setup="fake.override = path => path === '/api/sessions/a/stop' ? fake.json({ ok: true }) : undefined")
    ui.show_session("a")
    ui.emit("a", {"type": "status", "status": "compacting"})
    expect(page.get_by_role("status")).to_have_text("Compacting context…")
    expect(send(page)).to_have_count(0)
    page.get_by_role("button", name="Stop response").click()
    ui.wait("fake.requests('/api/sessions/a/stop', 'POST').length === 1")


def test_newer_context_survives_an_older_summary_request(ui):
    start_hook(ui, "a")
    ui.show_session("a")
    ui.run(pending("list", "path === '/api/sessions'"))
    ui.run("window.refreshing = hook.refreshSessions()")
    ui.emit("a", "{ type: 'context', context_info: contextInfo }")
    ui.run("fake.deferreds.list.resolve(fake.json([...fake.sessions.values()]))")
    ui.js("refreshing")
    settle(ui)
    assert ui.js("hook.session.context_info.estimated_tokens") == 60380
    assert ui.js("hook.sessions.find(item => item.id === 'a').context_info.estimated_tokens") == 60380


# Editor drafts

def test_read_from_a_closed_panel_is_ignored(ui, page):
    start_app(ui, page, setup="let reads = 0; const first = fake.defer('read'); fake.override = path => path.startsWith('/api/file?') && ++reads === 1 ? first.promise : undefined")
    page.get_by_role("button", name="Workspace", exact=True).click()
    page.get_by_role("button", name="note.txt").click()
    page.get_by_role("button", name="Close workspace").click()
    open_editor(page).fill("new unsaved editor draft")
    resolve(ui, "read", "{ content: 'old read result' }")
    settle(ui)
    expect(page.get_by_role("textbox", name="Edit note.txt")).to_have_value("new unsaved editor draft")
    expect(page.get_by_text("Unsaved changes")).to_be_visible()


def test_edits_typed_during_save_are_kept(ui, page):
    start_app(ui, page, setup=pending("save", "path === '/api/file' && options.method === 'PUT'"))
    editor = open_editor(page)
    editor.fill("first edit")
    page.get_by_role("button", name="Save file").click()
    editor.fill("second edit while saving")
    resolve(ui, "save", "{ ok: true }")
    settle(ui)
    expect(editor).to_have_value("second edit while saving")
    expect(page.get_by_text("Unsaved changes")).to_be_visible()
    ui.run("fake.override = () => undefined")
    page.get_by_role("button", name="Save file").click()
    ui.wait("fake.requests('/api/file', 'PUT').length === 2")
    second = ui.bodies("/api/file", "PUT")[1]
    assert (second["content"], second["original"]) == ("second edit while saving", "first edit")


def test_older_save_after_reopening_is_ignored(ui, page):
    start_app(ui, page, setup="""let disk = 'original', writes = 0; const first = fake.defer('save')
      fake.override = (path, options) => {
        if (path.startsWith('/api/file?')) return fake.json({ content: disk })
        if (path === '/api/file' && options.method === 'PUT') {
          disk = JSON.parse(options.body).content
          return ++writes === 1 ? first.promise : fake.json({ ok: true })
        }
      }""")
    open_editor(page).fill("first edit")
    page.get_by_role("button", name="Save file").click()
    page.get_by_role("button", name="Back to files").click()
    page.get_by_role("button", name="Close workspace").click()
    open_editor(page).fill("original")
    page.get_by_role("button", name="Save file").click()
    expect(page.get_by_text("Saved on disk")).to_be_visible()
    resolve(ui, "save", "{ ok: true }")
    settle(ui)
    expect(page.get_by_role("textbox", name="Edit note.txt")).to_have_value("original")
    expect(page.get_by_text("Saved on disk")).to_be_visible()


@pytest.mark.parametrize("button", ["Workspace", "Close workspace", "Add this file to chat"])
def test_editor_draft_survives_closing_the_panel(ui, page, button):
    start_app(ui, page)
    open_editor(page).fill("unsaved editor draft")
    page.get_by_role("button", name=button, exact=True).click()
    expect(page.get_by_role("textbox", name="Edit note.txt")).to_have_count(0)
    page.get_by_role("button", name="Workspace", exact=True).click()
    expect(page.get_by_role("textbox", name="Edit note.txt")).to_have_value("unsaved editor draft")
    expect(page.get_by_text("Unsaved changes")).to_be_visible()


def test_editor_draft_follows_first_creation_and_is_per_conversation(ui, page):
    start_app(ui, page)
    open_editor(page).fill("created conversation draft")
    message(page).fill("Start a conversation")
    send(page).click()
    ui.show_session("created")
    expect(page.get_by_role("textbox", name="Edit note.txt")).to_have_value("created conversation draft")
    row(page, "Other conversation").click()
    ui.show_session("a")
    page.get_by_role("button", name="note.txt").click()
    page.get_by_role("textbox", name="Edit note.txt").fill("other conversation draft")
    row(page, "Created conversation").click()
    ui.show_session("created", count=2)
    expect(page.get_by_role("textbox", name="Edit note.txt")).to_have_value("created conversation draft")


# Session synchronization

def test_generated_title_beats_an_older_sidebar_response(ui):
    start_hook(ui, "a")
    ui.show_session("a")
    ui.run("""window.oldList = [...fake.sessions.values()]
      const older = fake.defer('older'), newer = fake.defer('newer'); let requests = 0
      fake.override = path => path === '/api/sessions' ? (++requests === 1 ? older.promise : newer.promise) : undefined
      window.oldRefresh = hook.refreshSessions()""")
    title = "Fix Parser Token Boundaries"
    ui.run(f"fake.sessions.set('a', {{ ...fake.sessions.get('a'), title: '{title}' }})")
    ui.emit("a", {"type": "title", "title": title})
    ui.wait(f"hook.session?.title === '{title}'")
    assert ui.js("hook.sessions.find(item => item.id === 'a').title") == title
    resolve(ui, "newer", "[...fake.sessions.values()]")
    settle(ui)
    ui.run("fake.deferreds.older.resolve(fake.json(oldList))")
    ui.js("oldRefresh")
    settle(ui)
    assert ui.js("hook.session.title") == title
    assert ui.js("hook.sessions.find(item => item.id === 'a').title") == title


def test_generated_titles_survive_switches_and_late_responses(ui, page):
    start_app(ui, page, "a", setup="fake.sessions.set('b', makeSession('b', 'Second conversation'))")
    ui.show_session("a")
    ui.run("""window.firstSocket = fake.socketFor('a'); window.oldList = [...fake.sessions.values()]
      const older = fake.defer('older'); let requests = 0
      fake.override = path => path === '/api/sessions' && ++requests === 1 ? older.promise : undefined""")
    ui.emit("a", {"type": "status", "status": "idle"})
    title = "Fix Parser Token Boundaries"
    ui.run(f"fake.sessions.set('a', {{ ...fake.sessions.get('a'), title: '{title}' }})")
    ui.emit("a", {"type": "title", "title": title})
    expect(row(page, title)).to_be_visible()
    expect(main(page).get_by_text(title)).to_be_visible()
    row(page, "Second conversation").click()
    ui.show_session("b")
    ui.run("firstSocket.emit({ type: 'title', title: 'Ignored abandoned socket title' }); fake.deferreds.older.resolve(fake.json(oldList))")
    settle(ui)
    expect(main(page).get_by_text("Second conversation")).to_be_visible()
    expect(row(page, title)).to_be_visible()
    expect(page.get_by_text("Ignored abandoned socket title")).to_have_count(0)
    row(page, title).click()
    ui.show_session("a", count=2)
    expect(main(page).get_by_text(title)).to_be_visible()
    expect(row(page, title)).to_be_visible()


def test_sidebar_refreshes_changes_missed_before_the_first_snapshot(ui):
    start_hook(ui, "a")
    ui.wait("fake.socketsFor('a').length === 1")
    ui.run("""fake.sessions.set('a', { ...makeSession('a', 'The completed first turn'), status: 'idle' })
      fake.socketFor('a').emit({ type: 'snapshot', session: fake.sessions.get('a') })""")
    ui.wait("hook.sessions[0].title === 'The completed first turn'")
    assert ui.js("hook.sessions[0].status") == "idle"


@pytest.mark.parametrize("action", ["permissions", "allow folder", "remove folder"])
def test_late_permission_or_folder_response_keeps_streamed_events(ui, action):
    start_hook(ui, "a", setup=pending("change", "path === '/api/sessions/a/permissions' || path === '/api/sessions/a/folders'"))
    ui.show_session("a")
    call = {"permissions": "hook.setPermissionMode('plan')", "allow folder": "hook.allowFolder('/extra')",
            "remove folder": "hook.removeFolder('/extra')"}[action]
    ui.run(f"window.request = {call}")
    ui.emit("a", {"type": "status", "status": "running"})
    ui.emit("a", {"type": "event", "event": {"id": "user", "type": "user", "text": "Hello"}})
    ui.emit("a", {"type": "event", "event": {"id": "reply", "type": "assistant", "text": "New response"}})
    resolve(ui, "change", "{ ...fake.sessions.get('a'), permission_mode: 'plan', allowed_directories: ['/extra'] }")
    ui.js("request")
    settle(ui)
    assert ui.js("hook.session.events.map(event => event.id)") == ["user", "reply"]
    assert ui.js("hook.session.status") == "running"
    if action == "permissions":
        assert ui.js("hook.permissionMode") == "plan"
    else:
        assert ui.js("hook.session.allowed_directories") == ["/extra"]


def test_history_back_to_a_deleted_conversation_recovers(ui):
    start_hook(ui, "a")
    ui.show_session("a")
    ui.js("hook.deleteSession('a')")
    ui.wait("hook.activeId === null")
    ui.run("history.replaceState(null, '', '?session=a'); dispatchEvent(new PopStateEvent('popstate'))")
    ui.wait("hook.error.includes('no longer exists')")
    assert ui.js("hook.activeId") is None
    assert ui.js("location.search") == ""
    assert ui.js("fake.sockets.length") == 1


def test_reconnecting_stops_when_the_conversation_was_deleted(ui):
    start_hook(ui, "a", clock=True)
    ui.show_session("a")
    ui.run("fake.sessions.delete('a'); fake.socketFor('a').close()")
    ui.tick(2000)
    ui.wait("hook.error.includes('no longer exists')")
    assert ui.js("hook.activeId") is None
    assert ui.js("fake.sockets.length") == 1
    calls = ui.js("fake.calls.length")
    ui.tick(10000)
    assert ui.js("fake.calls.length") == calls
