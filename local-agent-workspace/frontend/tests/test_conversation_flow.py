"""Whole-app conversation helpers: the Continue button, the live task checklist and saved drafts."""
import json
import re
from datetime import datetime

import pytest
from playwright.sync_api import expect

CONTINUE = "Continue from where you stopped."


def start_app(ui, page, session=None, setup="", **start):
    ui.app(session=session, scenario="workspace", setup=setup, **start)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")


def remount(ui, page):
    """Reload the page (localStorage stays) and start the app again."""
    page.reload()
    page.wait_for_function("window.harnessReady === true")
    page.evaluate("() => preload('App.js')")
    page.evaluate("() => { mount('App.js', () => ({})) }")
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
    continue_button(page).focus()
    page.keyboard.press("Enter")
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/messages", "POST") == [{"text": CONTINUE}]
    expect(message(page)).to_have_value("My unsent idea")
    # Never while busy, and not after the next turn completes.
    ui.emit("a", {"type": "status", "status": "running"})
    expect(continue_button(page)).to_have_count(0)
    expect(message(page)).to_be_focused()  # Not lost with the removed button.
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
    ("[{ type: 'error', text: 'Rate limited', error_kind: 'rate_limit' }]", {"terminal_reason": "inference_error"},
     "Databricks rate limited this chat."),
    ("[{ type: 'error', text: 'Bad arguments', error_kind: 'invalid_tool_arguments' }]", {"terminal_reason": "inference_error"},
     "The model's reply could not be used."),
    ("[{ type: 'error', text: 'Ended early', error_kind: 'incomplete_response' }]", {"terminal_reason": "inference_error"},
     "The model's reply could not be used."),
    # A provider content-filter stop is shown as its own error, without Continue.
    ("[{ type: 'assistant', text: 'No', request_info: { status: 'error', finish_reason: 'content_filter', "
     "error_kind: 'content_filter' } }, { type: 'error', text: 'Filtered', error_kind: 'content_filter' }]",
     {"terminal_reason": "inference_error"}, None),
    # Only the turn's last error counts, and only for an inference error.
    ("[{ type: 'error', text: 'Old', error_kind: 'rate_limit' }, { type: 'error', text: 'Server', error_kind: 'server' }]",
     {"terminal_reason": "inference_error"}, None),
    ("[{ type: 'error', text: 'Bad arguments', error_kind: 'invalid_tool_arguments' }]", {"terminal_reason": "error"}, None),
    # A hidden automatic retry leaves the earlier reply's status 'error'; the turn then completed.
    ("[{ type: 'assistant', text: 'Part', request_info: { status: 'error', error_kind: 'invalid_tool_arguments' } }, "
     "{ type: 'assistant', text: 'All' }]", {"terminal_reason": "completed"}, None),
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


@pytest.mark.parametrize("kind,shown", [("invalid_tool_arguments", "The model's reply could not be used."),
                                        ("rate_limit", "Databricks rate limited this chat.")])
def test_continue_bar_after_an_inference_error(ui, page, kind, shown):
    error = f"[{{ id: 'e', type: 'error', text: 'Request failed.', error_kind: '{kind}' }}]"
    start_app(ui, page, session="a", setup=with_turn(error, terminal_reason="inference_error"))
    ui.show_session("a")
    expect(page.get_by_text(shown)).to_be_visible()
    continue_button(page).click()
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/messages", "POST") == [{"text": CONTINUE}]


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


def test_task_checklist_shows_long_titles_in_full_on_a_phone(ui, page):
    page.set_viewport_size({"width": 390, "height": 844})
    start_app(ui, page, session="a")
    ui.show_session("a")
    title = "Fix empty input handling in the tokenizer and the parser so that a blank line never wraps the result"
    ui.emit("a", {"type": "tasks", "tasks": [{**TASKS[1], "title": title}]})
    page.get_by_role("button", name=re.compile(r"^Tasks 0\/1 done")).click()
    item = page.locator(".task-checklist-title")
    expect(item).to_have_text(title)
    # Wrapped, not cut off with an ellipsis (the title attribute is out of reach on touch screens).
    assert ui.js("(e => e.scrollWidth <= e.clientWidth && e.getBoundingClientRect().height > 20)(document.querySelector('.task-checklist-title'))")


# The bars above the message box never push it off-screen

CONTEXT_INFO = {"estimated_tokens": 6000, "input_budget": 120832, "context_window": 131072, "reply_reserve": 8192,
                "compactions": 1, "summarized_messages": 6, "estimate_method": "weighted_utf8", "instruction_files": [], "warnings": [],
                "breakdown": {"system_instructions": 1000, "tool_definitions": 2000, "messages_and_results": 2500, "summary": 400,
                              "request_overhead": 100}}


@pytest.mark.parametrize("meter", [False, True])
@pytest.mark.parametrize("width,height", [(390, 664), (390, 844), (844, 390), (667, 375), (1440, 900)])
def test_open_changes_and_tasks_keep_the_message_box_on_screen(ui, page, width, height, meter):
    page.set_viewport_size({"width": width, "height": height})
    problems = json.dumps([{"file": "broken-1.jsonl", "reason": "line 1 is not valid JSON"}])
    notice = "[{ id: 'n', type: 'notice', text: 'Reached the limit.', terminal_reason: 'step_limit' }]"
    fields = {"terminal_reason": "step_limit", **({"context_info": CONTEXT_INFO} if meter else {})}
    start_app(ui, page, session="a", setup=with_turn(notice, **fields) + "; fake.override = path => "
              f"path === '/api/bootstrap' ? fake.json({{ token: 'test-token', settings: fake.settings, storage_problems: {problems} }}) : undefined")
    ui.show_session("a")
    files = ", ".join(f"changedFile('src/module_{n}/file_{n}.py', {n + 1}, {n})" for n in range(12))
    ui.run(f"fake.socketFor('a').emit({{ type: 'changes', changes: makeChanges({files}) }})")
    ui.emit("a", {"type": "tasks", "tasks": [{**TASKS[2], "id": f"t{n}", "title": f"Task number {n}"} for n in range(12)]})
    expect(page.get_by_role("alert")).to_contain_text("broken-1.jsonl")
    page.get_by_role("button", name=re.compile("files changed")).click()
    page.get_by_role("button", name=re.compile(r"^Tasks 0\/12 done")).click()
    expect(page.get_by_role("region", name="Tasks").get_by_role("listitem")).to_have_count(12)
    compact = page.get_by_role("button", name="Compact now")
    if meter:
        page.get_by_text(re.compile("^Last model input")).click()
        expect(compact).to_be_attached()

    def on_screen():
        for control in [message(page), page.get_by_role("button", name="Send message"),
                        page.get_by_role("button", name=re.compile("^Permissions:")), page.get_by_role("combobox", name="Model")]:
            box = control.bounding_box()
            assert box and box["y"] >= 0 and box["y"] + box["height"] <= height and box["x"] + box["width"] <= width, box
        assert ui.js("[document.scrollingElement.scrollTop, document.querySelector('.app-shell').scrollTop]") == [0, 0]
        transcript = ui.js("(b => [b.top, b.height])(document.querySelector('.transcript').getBoundingClientRect())")
        assert transcript[0] >= 0 and transcript[1] >= 56, transcript

    on_screen()
    if meter:  # The open meter scrolls, so the wheel brings Compact now into view.
        details = page.locator(".context-details").bounding_box()
        page.mouse.move(details["x"] + 20, details["y"] + 5)
        for _ in range(20):
            page.mouse.wheel(0, 200)
        expect(compact).to_be_in_viewport(ratio=1)
        on_screen()
        ui.run("for (const e of document.querySelectorAll('.context-meter, .context-details')) e.scrollTop = 0")
        if height < 600:  # Out of view again, for the keyboard check below.
            expect(compact).not_to_be_in_viewport(ratio=1)
    # Every bar (or the open meter's button) is still reachable with the keyboard, with its focus shown.
    for control in [compact] if meter else [page.get_by_role("button", name="Undo changes to src/module_11/file_11.py"), continue_button(page)]:
        control.focus()
        expect(control).to_be_in_viewport(ratio=1)
        on_screen()


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
    remount(ui, page)  # Reloads at ?session=a (the pagehide save is checked deterministically below).
    ui.show_session("a")
    expect(message(page)).to_have_value("Reply for a")
    page.get_by_role("button", name="New conversation").click()
    expect(message(page)).to_have_value("A new idea")
    assert stored(ui) == {"new": "A new idea", "a": "Reply for a"}


def test_a_draft_is_saved_when_the_page_is_hidden_before_the_save_delay(ui, page):
    start_app(ui, page, session="a", clock=True)
    ui.show_session("a")
    page.clock.pause_at(datetime.fromtimestamp(ui.js("Date.now()") / 1000 + 1))
    message(page).fill("Typed just before closing")
    ui.tick(100)  # The page renders and schedules the save; its 500 ms delay has not passed.
    expect(message(page)).to_have_value("Typed just before closing")
    assert stored(ui) == {}
    ui.run("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    assert stored(ui) == {"a": "Typed just before closing"}


def test_text_typed_while_sending_is_saved(ui, page):
    start_app(ui, page, session="a", setup="""fake.override = (path, options) =>
      path === '/api/sessions/a/messages' && options.method === 'POST' ? fake.defer('send').promise : undefined""")
    ui.show_session("a")
    message(page).fill("First")
    page.get_by_role("button", name="Send message").click()
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    message(page).fill("Second thought")
    ui.run("fake.deferreds.send.resolve(fake.json({ ok: true }))")
    ui.wait("localStorage.getItem('local-workspace:draft:a') === 'Second thought'")
    expect(message(page)).to_have_value("Second thought")
    remount(ui, page)
    ui.show_session("a")
    expect(message(page)).to_have_value("Second thought")


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


DEFERRED_CREATION = """fake.override = (path, options) =>
  path === '/api/sessions' && options.method === 'POST' ? fake.defer('create').promise : undefined"""


def test_a_new_view_opened_while_the_first_message_is_sent_starts_empty(ui, page):
    start_app(ui, page, setup=DEFERRED_CREATION)
    message(page).fill("Hello there")
    ui.wait("localStorage.getItem('local-workspace:draft:new') === 'Hello there'")
    page.get_by_role("button", name="Send message").click()
    ui.wait("fake.requests('/api/sessions', 'POST').length === 1")
    assert stored(ui) == {}
    page.get_by_role("button", name="New conversation").click()
    expect(message(page)).to_have_value("")
    ui.run("""fake.override = () => undefined
      const created = makeSession('created', 'Created conversation'); fake.sessions.set('created', created)
      fake.deferreds.create.resolve(fake.json(created))""")
    ui.wait("fake.requests('/api/sessions/created/messages', 'POST').length === 1")
    assert ui.bodies("/api/sessions/created/messages", "POST") == [{"text": "Hello there"}]
    page.wait_for_timeout(700)
    expect(message(page)).to_have_value("")
    assert stored(ui) == {}


@pytest.mark.parametrize("switch", [False, True])
def test_a_failed_first_message_is_saved_again_as_the_new_draft(ui, page, switch):
    start_app(ui, page, setup=DEFERRED_CREATION)
    message(page).fill("Hello there")
    page.get_by_role("button", name="Send message").click()
    ui.wait("fake.requests('/api/sessions', 'POST').length === 1")
    if switch:  # The user opened another chat while the first one was being created.
        page.get_by_role("button", name=re.compile("^Other conversation")).click()
        ui.show_session("a")
    ui.run("fake.deferreds.create.resolve(fake.json({ detail: 'Could not create the conversation.' }, 500))")
    if not switch:
        expect(page.get_by_role("alert")).to_contain_text("Could not create the conversation.")
    ui.wait("localStorage.getItem('local-workspace:draft:new') === 'Hello there'")
    page.wait_for_timeout(700)
    assert stored(ui) == {"new": "Hello there"}
    if switch:
        page.get_by_role("button", name="New conversation").click()
    expect(message(page)).to_have_value("Hello there")


def test_a_failed_first_message_comes_back_in_a_new_view_opened_during_the_send(ui, page):
    start_app(ui, page, setup=DEFERRED_CREATION)
    message(page).fill("Hello there")
    page.get_by_role("button", name="Send message").click()
    ui.wait("fake.requests('/api/sessions', 'POST').length === 1")
    page.get_by_role("button", name="New conversation").click()
    expect(message(page)).to_have_value("")
    ui.run("fake.deferreds.create.resolve(fake.json({ detail: 'Could not create the conversation.' }, 500))")
    expect(message(page)).to_have_value("Hello there")
    message(page).press("End")
    message(page).type("!")
    ui.wait("localStorage.getItem('local-workspace:draft:new') === 'Hello there!'")
    page.get_by_role("button", name="New conversation").click()
    expect(message(page)).to_have_value("Hello there!")


def test_a_first_message_that_fails_after_creation_stays_with_the_created_chat(ui, page):
    start_app(ui, page, setup="""fake.override = (path, options) => path === '/api/sessions/created/messages'
      ? fake.json({ detail: 'The model is unavailable.' }, 500) : undefined""")
    message(page).fill("Hello there")
    page.get_by_role("button", name="Send message").click()
    expect(page.get_by_role("alert")).to_contain_text("The model is unavailable.")
    ui.wait("localStorage.getItem('local-workspace:draft:created') === 'Hello there'")
    page.wait_for_timeout(700)
    assert stored(ui) == {"created": "Hello there"}
    expect(message(page)).to_have_value("Hello there")


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
