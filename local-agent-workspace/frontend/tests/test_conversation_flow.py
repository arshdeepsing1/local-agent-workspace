"""Whole-app conversation helpers: the Continue button, the live task checklist and saved drafts."""
import json
import re

import pytest
from playwright.sync_api import expect

CONTINUE = "Continue from where you stopped."


def start_app(ui, page, session=None, setup=""):
    ui.app(session=session, scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")


def remount(ui, page):
    """Reload the page (localStorage stays) and start the app again."""
    page.reload()
    page.wait_for_function("window.harnessReady === true")
    page.evaluate("() => mount('App.js', () => ({}))")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")


def message(page):
    return page.get_by_role("textbox", name="Message")


def continue_button(page):
    return page.get_by_role("button", name="Continue", exact=True)


def with_turn(events, **fields):
    """A saved conversation 'a' whose latest turn ended with these events and fields."""
    return (f"fake.sessions.set('a', {{ ...makeSession('a', 'Other conversation'), "
            f"events: [{{ id: 'u', type: 'user', text: 'Do the work' }}, ...{events}], ...{json.dumps(fields)} }})")


# Continue button

def test_continue_after_the_step_limit_sends_the_message_and_keeps_the_draft(ui, page):
    notice = "[{ id: 'n', type: 'notice', text: 'Reached the 32-request agent limit.', terminal_reason: 'step_limit' }]"
    start_app(ui, page, session="a", setup=with_turn(notice, terminal_reason="step_limit"))
    ui.show_session("a")
    expect(page.get_by_text("The agent reached its step limit.")).to_be_visible()
    message(page).fill("My unsent idea")
    continue_button(page).click()
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/messages", "POST") == [{"text": CONTINUE}]
    expect(message(page)).to_have_value("My unsent idea")
    # Never while busy, and not after the next turn completes.
    ui.emit("a", {"type": "status", "status": "running"})
    expect(continue_button(page)).to_have_count(0)
    ui.emit("a", {"type": "event", "event": {"id": "u2", "type": "user", "text": CONTINUE}})
    ui.emit("a", {"type": "event", "event": {"id": "r2", "type": "assistant", "text": "Finished."}})
    ui.emit("a", {"type": "status", "status": "idle", "terminal_reason": "completed"})
    expect(page.get_by_text("Finished.")).to_be_visible()
    expect(continue_button(page)).to_have_count(0)


def test_continue_follows_live_turn_endings(ui, page):
    start_app(ui, page, session="a", setup=with_turn("[{ id: 'r', type: 'assistant', text: 'Done.' }]", terminal_reason="completed"))
    ui.show_session("a")
    expect(page.get_by_text("Done.")).to_be_visible()
    expect(continue_button(page)).to_have_count(0)
    ui.emit("a", {"type": "status", "status": "running"})
    ui.emit("a", {"type": "event", "event": {"id": "u2", "type": "user", "text": "Next"}})
    ui.emit("a", {"type": "event", "event": {"id": "s", "type": "notice", "text": "Stopped. You can continue this conversation."}})
    expect(continue_button(page)).to_have_count(0)
    ui.emit("a", {"type": "status", "status": "idle", "terminal_reason": "stopped"})
    expect(page.get_by_text("The response was stopped.")).to_be_visible()
    expect(continue_button(page)).to_be_enabled()
    # While the composer cannot send (here: reconnecting), Continue cannot either.
    ui.run("fake.socketFor('a').close()")
    expect(continue_button(page)).to_be_disabled()


@pytest.mark.parametrize("events,fields,shown", [
    ("[]", {"terminal_reason": "step_limit"}, "The agent reached its step limit."),
    ("[]", {"terminal_reason": "stopped"}, "The response was stopped."),
    ("[]", {"terminal_reason": "interrupted"}, "The response was interrupted."),
    ("[{ type: 'error', text: 'Output limit', error_kind: 'output_limit' }]", {"terminal_reason": "inference_error"},
     "The reply reached the output limit."),
    # An older record cut short by a restart, with the previous turn's reason.
    ("[{ type: 'assistant', text: 'Part', request_info: { status: 'interrupted', error_kind: 'incomplete_response' } }]",
     {"terminal_reason": "completed"}, "The response was interrupted."),
    ("[{ type: 'error', text: 'Rate limited', error_kind: 'rate_limit' }]", {"terminal_reason": "inference_error"}, None),
    ("[{ type: 'error', text: 'Broken' }]", {"terminal_reason": "error"}, None),
    # An automatic output-limit retry marks a reply interrupted; that turn then completed.
    ("[{ type: 'assistant', text: 'Part', request_info: { status: 'interrupted' } }, { type: 'assistant', text: 'All' }]",
     {"terminal_reason": "completed"}, None),
    ("[]", {"terminal_reason": "step_limit", "status": "running"}, None),
    ("[]", {"terminal_reason": "step_limit", "status": "awaiting_approval"}, None),
    ("[]", {}, None),
])
def test_continue_reasons(ui, events, fields, shown):
    ui.start()
    session = f"{{ status: 'idle', events: [{{ type: 'user', text: 'Work' }}, ...{events}], ...{json.dumps(fields)} }}"
    assert ui.js(f"import('/static/js/components/ContinueButton.js').then(m => m.stopReason({session}))") == shown
    # A conversation without a user message has no turn to continue.
    assert ui.js("import('/static/js/components/ContinueButton.js').then(m => m.stopReason({ status: 'idle', events: [], terminal_reason: 'stopped' }))") is None


def test_continue_failure_is_reported(ui, page):
    start_app(ui, page, session="a", setup=with_turn("[]", terminal_reason="stopped")
              + "; fake.override = path => path === '/api/sessions/a/messages' ? fake.json({ detail: 'A response is already running in this conversation.' }, 400) : undefined")
    ui.show_session("a")
    continue_button(page).click()
    expect(page.get_by_role("alert")).to_contain_text("A response is already running")
    expect(continue_button(page)).to_be_enabled()


# Task checklist

TASKS = [
    {"id": "t1", "title": "Read the parser", "description": "Start at parse()", "status": "completed", "depends_on": []},
    {"id": "t2", "title": "Fix empty input", "description": "", "status": "in_progress", "depends_on": ["t1"]},
    {"id": "t3", "title": "Add tests", "description": "", "status": "pending", "depends_on": ["t2"]},
    {"id": "t4", "title": "Rewrite everything", "description": "", "status": "cancelled", "depends_on": []},
]


def test_task_checklist_shows_live_tasks_per_conversation(ui, page):
    start_app(ui, page, session="a", setup="fake.sessions.set('b', makeSession('b', 'Second chat'))")
    ui.show_session("a")
    expect(page.get_by_role("region", name="Tasks")).to_have_count(0)
    ui.emit("a", {"type": "tasks", "tasks": TASKS})
    toggle = page.get_by_role("button", name=re.compile(r"^Tasks 1\/3 done"))
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(toggle).to_contain_text("Fix empty input")
    toggle.click()
    items = page.get_by_role("region", name="Tasks").get_by_role("listitem")
    expect(items).to_have_text(["Done: Read the parser", "In progress: Fix empty input", "Pending: Add tests",
                                "Cancelled: Rewrite everything"])
    updated = [{**task, "status": "completed"} if task["id"] == "t2" else task for task in TASKS]
    ui.emit("a", {"type": "tasks", "tasks": updated})
    expect(page.get_by_role("button", name=re.compile(r"^Tasks 2\/3 done"))).to_be_visible()
    expect(items.nth(1)).to_have_text("Done: Fix empty input")
    # Another conversation without tasks shows none of these.
    page.get_by_role("button", name=re.compile("^Second chat")).click()
    ui.show_session("b")
    expect(page.get_by_role("region", name="Tasks")).to_have_count(0)
    ui.emit("b", {"type": "tasks", "tasks": []})
    expect(page.get_by_role("region", name="Tasks")).to_have_count(0)


# Saved drafts

def stored(ui):
    return ui.js("Object.fromEntries(Object.keys(localStorage).filter(k => k.startsWith('local-workspace:draft:')).map(k => [k.slice(22), localStorage.getItem(k)]))")


def test_drafts_survive_a_reload_per_conversation(ui, page):
    start_app(ui, page)
    message(page).fill("A new idea")
    ui.wait("localStorage.getItem('local-workspace:draft:new') === 'A new idea'")
    page.get_by_role("button", name=re.compile("^Other conversation")).click()
    ui.show_session("a")
    expect(message(page)).to_have_value("")
    message(page).fill("Reply for a")
    remount(ui, page)  # Reloads at ?session=a before the save delay ends.
    ui.show_session("a")
    expect(message(page)).to_have_value("Reply for a")
    page.get_by_role("button", name="New conversation").click()
    expect(message(page)).to_have_value("A new idea")
    assert stored(ui) == {"new": "A new idea", "a": "Reply for a"}


def test_sent_drafts_are_removed_at_once(ui, page):
    start_app(ui, page, session="a")
    ui.show_session("a")
    message(page).fill("Saved then sent")
    ui.wait("localStorage.getItem('local-workspace:draft:a') === 'Saved then sent'")
    message(page).fill("Sent quickly")
    page.get_by_role("button", name="Send message").click()
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    expect(message(page)).to_have_value("")
    assert stored(ui) == {}
    page.wait_for_timeout(700)
    assert stored(ui) == {}
    remount(ui, page)
    ui.show_session("a")
    expect(message(page)).to_have_value("")


def test_new_conversation_draft_moves_to_the_created_chat_and_is_cleared_on_send(ui, page):
    start_app(ui, page)
    message(page).fill("First message")
    ui.wait("localStorage.getItem('local-workspace:draft:new') === 'First message'")
    page.get_by_role("button", name="Send message").click()
    ui.wait("fake.requests('/api/sessions/created/messages', 'POST').length === 1")
    expect(message(page)).to_have_value("")
    page.wait_for_timeout(700)
    assert stored(ui) == {}


def test_folder_access_creation_keeps_the_restored_draft_with_the_chat(ui, page):
    start_app(ui, page, setup="try { localStorage.setItem('local-workspace:draft:new', 'Restored idea') } catch {}")
    expect(message(page)).to_have_value("Restored idea")
    ui.run("fake.override = path => path === '/api/folder-access/check' ? fake.json({ accessible: true, path: '/data' }) : undefined")
    page.get_by_role("button", name="Folder access").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("textbox").first.fill("/data")
    dialog.get_by_role("button", name="Check access").click()
    dialog.get_by_role("button", name="Allow folder").click()
    ui.wait("fake.requests('/api/sessions/created/folders', 'POST').length === 1")
    ui.wait("new URL(location.href).searchParams.get('session') === 'created'")
    dialog.get_by_role("button", name="Done").click()
    expect(message(page)).to_have_value("Restored idea")
    ui.wait("localStorage.getItem('local-workspace:draft:created') === 'Restored idea'")
    assert stored(ui) == {"created": "Restored idea"}


def test_oldest_drafts_are_evicted_and_oversized_drafts_are_not_saved(ui, page):
    setup = """try {
      if (!localStorage.getItem('local-workspace:drafts')) {
        const index = {}
        for (let n = 0; n < 50; n++) { localStorage.setItem(`local-workspace:draft:old-${n}`, `draft ${n}`); index[`old-${n}`] = 1000 + n }
        localStorage.setItem('local-workspace:drafts', JSON.stringify(index))
      }
    } catch {}"""
    start_app(ui, page, setup=setup)
    message(page).fill("Newest")
    ui.wait("localStorage.getItem('local-workspace:draft:new') === 'Newest'")
    drafts = stored(ui)
    assert len(drafts) == 50 and "old-0" not in drafts and drafts["old-1"] == "draft 1"
    assert set(json.loads(ui.js("localStorage.getItem('local-workspace:drafts')"))) == set(drafts)
    # Over 100 KB: not saved, and the shorter saved version is removed.
    ui.run("const box = document.querySelector('textarea'); box.value = 'é'.repeat(51 * 1024); box.dispatchEvent(new Event('input', { bubbles: true }))")
    ui.wait("localStorage.getItem('local-workspace:draft:new') === null")
    assert len(stored(ui)) == 49


def test_drafts_work_without_storage(ui, page):
    setup = """for (const name of ['getItem', 'setItem', 'removeItem']) {
      Storage.prototype[name] = () => { throw new DOMException('Blocked', 'SecurityError') }
    }"""
    start_app(ui, page, session="a", setup=setup)
    ui.show_session("a")
    message(page).fill("Kept in memory")
    page.wait_for_timeout(700)
    page.get_by_role("button", name="New conversation").click()
    page.get_by_role("button", name=re.compile("^Other conversation")).click()
    ui.show_session("a", count=2)
    expect(message(page)).to_have_value("Kept in memory")
    page.get_by_role("button", name="Send message").click()
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    expect(message(page)).to_have_value("")
