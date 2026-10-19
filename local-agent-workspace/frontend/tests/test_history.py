"""Edit and resend, Retry, Rewind, Fork from here and New chat in worktree (whole app, fake backend)."""
import json
import re

from playwright.sync_api import expect

EVENTS = [
    {"id": "u1", "type": "user", "text": "First task", "in_wire": True},
    {"id": "a1", "type": "assistant", "text": "First answer."},
    {"id": "u2", "type": "user", "text": "Second task", "in_wire": True},
    {"id": "t2", "type": "tool", "name": "read_file", "input": {"path": "a.txt"}, "state": "completed", "output": "x"},
    {"id": "a2", "type": "assistant", "text": "Second answer."},
]
PREVIEW = {"before_event_id": "u2",
           "conversation": {"possible": True, "reason": None, "events_removed": 3, "messages_removed": 4,
                            "summary_dropped": False},
           "code": {"possible": True, "reason": None, "files": [{"path": "a.txt", "status": "restore"},
                                                                 {"path": "b.txt", "status": "unchanged"}]}}
SETUP = """
fake.sessions.set('a', { ...makeSession('a', 'Work'), events: %s, terminal_reason: 'completed' })
window.preview = %s
window.rewindError = null
window.resendError = null
fake.override = (path, options) => {
  if (path.startsWith('/api/sessions/a/rewind-preview?')) return fake.json(window.preview)
  if (path === '/api/sessions/a/shell') return fake.json({ ok: true }, 202)
  if (path === '/api/sessions/a/rewind' && options.method === 'POST') {
    if (window.rewindError) return fake.json(window.rewindError.body, window.rewindError.status)
    const body = JSON.parse(options.body)
    const events = fake.sessions.get('a').events
    const updated = { ...fake.sessions.get('a'), events: body.conversation ? events.slice(0, events.findIndex(e => e.id === body.before_event_id)) : events }
    fake.sessions.set('a', updated)
    fake.sessions.set('backup', { ...makeSession('backup', 'Work (before rewind)'), events, archived: true })
    // Like the server: the snapshot is broadcast before the response.
    if (body.conversation) fake.socketFor('a').emit({ type: 'snapshot', session: updated })
    const failed = body.resend_text && window.resendError
    return fake.json({ session: updated, backup_session_id: body.conversation ? 'backup' : null, files: [], resent: !!body.resend_text && !failed,
                       ...(failed ? { resend_error: window.resendError } : {}) })
  }
  if (path === '/api/sessions/a/fork' && options.method === 'POST') {
    fake.sessions.set('forked', { ...makeSession('forked', 'Work (fork)'), events: fake.sessions.get('a').events.slice(0, 2) })
    return fake.json(fake.sessions.get('forked'), 201)
  }
  if (path === '/api/worktrees' && options.method === 'POST') {
    return fake.json({ id: 'wt1', branch: JSON.parse(options.body).branch, path: '/srv/state/worktrees/wt1', source_workspace: '/project' })
  }
  if (path === '/api/worktrees/wt1/session' && options.method === 'POST') {
    fake.sessions.set('wtchat', { ...makeSession('wtchat', 'Worktree: chat/x'), worktree: { id: 'wt1', branch: 'chat/x', source_workspace: '/project' } })
    return fake.json(fake.sessions.get('wtchat'))
  }
}
"""


def start(ui, page, events=EVENTS, preview=PREVIEW, extra="", session="a"):
    ui.app(session=session, scenario="workspace", setup=SETUP % (json.dumps(events), json.dumps(preview)) + extra)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    if session:
        ui.show_session(session)
        expect(page.get_by_text(events[-1]["text"] or "", exact=True).first).to_be_visible()


def message_box(page):
    return page.get_by_role("textbox", name="Message")


def rewind_posts(ui):
    return ui.bodies("/api/sessions/a/rewind", "POST")


def selected(ui, session_id):
    ui.wait(f"fake.socketsFor({json.dumps(session_id)}).length >= 1")
    return True


def test_edit_and_resend_posts_the_exact_body_and_escape_cancels(ui, page):
    start(ui, page)
    page.get_by_role("button", name="Edit message").nth(1).click()
    editor = page.get_by_role("textbox", name="Edit message")
    expect(editor).to_have_value("Second task")
    expect(page.get_by_text("Replaces this message and everything after it. The current version is kept as an archived conversation.")).to_be_visible()
    editor.press("Escape")
    expect(editor).to_have_count(0)
    expect(page.get_by_text("Second task", exact=True)).to_be_visible()
    assert rewind_posts(ui) == []

    page.get_by_role("button", name="Edit message").nth(1).click()
    editor.fill("Second task, done better")
    page.get_by_role("button", name="Send edited message").click()
    ui.wait("fake.requests('/api/sessions/a/rewind', 'POST').length === 1")
    assert rewind_posts(ui) == [{"before_event_id": "u2", "conversation": True, "code": False,
                                 "resend_text": "Second task, done better"}]
    notice = page.get_by_role("status").filter(has_text="Rewound.")
    expect(notice).to_have_text(re.compile("archived conversation “Work \\(before rewind\\)”"))
    expect(notice.get_by_role("button", name="Open earlier version")).to_be_visible()


def test_edit_with_code_sends_code_and_keeps_the_editor_open_on_error(ui, page):
    start(ui, page, extra="window.rewindError = { status: 409, body: { detail: { message: 'Cannot rewind files: a.txt changed after the agent’s last edit.', files: ['a.txt'] } } }")
    page.get_by_role("button", name="Edit message").first.click()
    page.get_by_role("checkbox", name="Also undo file changes made from this message on").check()
    page.get_by_role("textbox", name="Edit message").press("Control+Enter")
    ui.wait("fake.requests('/api/sessions/a/rewind', 'POST').length === 1")
    assert rewind_posts(ui) == [{"before_event_id": "u1", "conversation": True, "code": True, "resend_text": "First task"}]
    alert = page.get_by_role("alert")
    expect(alert).to_contain_text("Cannot rewind files: a.txt changed")
    expect(alert.get_by_role("listitem")).to_have_text(["a.txt"])
    expect(page.get_by_role("textbox", name="Edit message")).to_have_value("First task")


def test_rewind_dialog_shows_statuses_disables_impossible_choices_and_sends_flags(ui, page):
    preview = json.loads(json.dumps(PREVIEW))
    preview["code"] = {"possible": False, "reason": "Cannot rewind files: b.txt changed between its edits.", "files": [
        {"path": "a.txt", "status": "restore"}, {"path": "b.txt", "status": "interleaved"},
        {"path": "c.txt", "status": "drifted"}, {"path": "d.txt", "status": "unchanged"},
        {"path": "e.txt", "status": "unavailable", "error": "Folder access was removed."}]}
    preview["conversation"]["summary_dropped"] = True
    start(ui, page, preview=preview)
    page.get_by_role("button", name="Rewind to here").nth(1).click()
    dialog = page.get_by_role("dialog", name="Rewind conversation")
    expect(dialog.get_by_role("heading", name="Rewind to this message")).to_be_visible()
    files = dialog.get_by_role("list", name="Files").get_by_role("listitem")
    expect(files).to_have_text([
        "a.txtWill be restored", "b.txtAlso changed by something else between the agent's edits",
        "c.txtChanged since the agent's last edit", "d.txtAlready as it was", "e.txtFolder access was removed."])
    expect(dialog.get_by_role("radio", name="Conversation and code")).to_be_disabled()
    expect(dialog.get_by_role("radio", name=re.compile("^Code only"))).to_be_disabled()
    expect(dialog.get_by_text("Cannot rewind files: b.txt changed between its edits.")).to_be_visible()
    expect(dialog.get_by_role("radio", name="Conversation only")).to_be_checked()
    expect(dialog.get_by_text("Removes 1 message and 1 reply after this point.")).to_be_visible()
    expect(dialog.get_by_text("The summary of earlier turns covers messages after this point, so it is dropped; the next message may compact again.")).to_be_visible()
    expect(dialog.get_by_text("Changes made by commands or by subagents are not rewound. The current version is kept as an archived conversation.")).to_be_visible()
    dialog.get_by_role("button", name="Rewind").click()
    expect(dialog).to_have_count(0)
    assert rewind_posts(ui) == [{"before_event_id": "u2", "conversation": True, "code": False}]
    expect(message_box(page)).to_have_value("Second task")
    expect(message_box(page)).to_be_focused()  # The control that opened the dialog is gone.
    notice = page.get_by_role("status").filter(has_text="Rewound.")
    expect(notice.locator("span")).to_have_text("Rewound. The earlier version is saved as the archived conversation “Work (before rewind)”.")
    notice.get_by_role("button", name="Open earlier version").click()
    assert selected(ui, "backup")


def test_rewind_both_by_default_keeps_a_typed_draft(ui, page):
    start(ui, page)
    message_box(page).fill("My unsent idea")
    page.get_by_role("button", name="Rewind to here").nth(1).click()
    dialog = page.get_by_role("dialog", name="Rewind conversation")
    expect(dialog.get_by_role("radio", name="Conversation and code")).to_be_checked()
    dialog.get_by_role("button", name="Rewind").click()
    expect(dialog).to_have_count(0)
    assert rewind_posts(ui) == [{"before_event_id": "u2", "conversation": True, "code": True}]
    expect(message_box(page)).to_have_value("My unsent idea")
    expect(page.get_by_role("status").filter(has_text="Rewound.")).to_contain_text("Your draft was kept.")
    page.get_by_role("button", name="Dismiss rewind notice").click()
    expect(page.get_by_role("status").filter(has_text="Rewound.")).to_have_count(0)


def test_code_only_rewind_leaves_the_conversation_and_composer(ui, page):
    start(ui, page)
    page.get_by_role("button", name="Rewind to here").nth(1).click()
    dialog = page.get_by_role("dialog", name="Rewind conversation")
    dialog.get_by_role("radio", name="Code only").check()
    expect(dialog.get_by_text("Removes 1 message and 1 reply after this point.")).to_have_count(0)
    dialog.get_by_role("button", name="Rewind").click()
    expect(dialog).to_have_count(0)
    assert rewind_posts(ui) == [{"before_event_id": "u2", "conversation": False, "code": True}]
    expect(message_box(page)).to_have_value("")
    expect(page.get_by_role("button", name="Rewind to here").nth(1)).to_be_focused()
    expect(page.get_by_text("Second answer.")).to_be_visible()
    expect(page.get_by_role("status").filter(has_text="Rewound.")).to_have_count(0)


def test_rewind_to_the_first_message_keeps_the_notice_on_the_welcome_screen(ui, page):
    start(ui, page)
    page.get_by_role("button", name="Rewind to here").first.click()
    page.get_by_role("dialog", name="Rewind conversation").get_by_role("button", name="Rewind").click()
    expect(page.get_by_role("heading", name="What’s up next?")).to_be_visible()
    notice = page.get_by_role("status").filter(has_text="Rewound.")
    expect(notice).to_be_visible()
    expect(message_box(page)).to_have_value("First task")
    notice.get_by_role("button", name="Open earlier version").click()
    assert selected(ui, "backup")


def test_a_drift_refusal_lists_the_files_in_the_dialog(ui, page):
    start(ui, page, extra="window.rewindError = { status: 409, body: { detail: { message: 'Cannot rewind files: a.txt, b.txt changed after the agent’s last edit or between its edits. Nothing was changed.', files: ['a.txt', 'b.txt'] } } }")
    page.get_by_role("button", name="Rewind to here").nth(1).click()
    dialog = page.get_by_role("dialog", name="Rewind conversation")
    dialog.get_by_role("button", name="Rewind").click()
    alert = dialog.get_by_role("alert")
    expect(alert).to_contain_text("Nothing was changed.")
    expect(alert.get_by_role("list", name="Files that changed").get_by_role("listitem")).to_have_text(["a.txt", "b.txt"])
    expect(dialog).to_be_visible()
    expect(message_box(page)).to_have_value("")


def test_retry_only_on_the_last_reply_of_an_idle_chat_without_steering(ui, page):
    start(ui, page)
    retry = page.get_by_role("button", name="Retry response")
    expect(retry).to_have_count(1)
    expect(page.locator(".reply-content").filter(has_text="Second answer.").get_by_role("button", name="Retry response")).to_be_visible()
    retry.focus()
    page.keyboard.press("Enter")
    ui.wait("fake.requests('/api/sessions/a/rewind', 'POST').length === 1")
    assert rewind_posts(ui) == [{"before_event_id": "u2", "conversation": True, "resend_text": "Second task"}]
    expect(page.get_by_role("status").filter(has_text="Rewound.")).to_be_visible()
    expect(message_box(page)).to_be_focused()
    expect(message_box(page)).to_have_value("")


def test_a_failed_resend_puts_the_text_in_the_message_box(ui, page):
    start(ui, page, extra="window.resendError = 'This managed worktree is being removed. Choose another workspace.'")
    page.get_by_role("button", name="Edit message").nth(1).click()
    page.get_by_role("textbox", name="Edit message").fill("Second task, edited")
    page.get_by_role("button", name="Send edited message").click()
    expect(page.locator(".app-error")).to_contain_text("This managed worktree is being removed.")
    expect(message_box(page)).to_have_value("Second task, edited")
    expect(message_box(page)).to_be_focused()
    # Retry of the then latest message: its text waits in the message box too.
    page.get_by_role("button", name="Dismiss error").click()
    message_box(page).fill("")
    page.get_by_role("button", name="Retry response").click()
    expect(message_box(page)).to_have_value("First task")
    ui.run("window.resendError = null")


# The latest message was sent as plain text: its '!' is part of the question, not a shell command.
PLAIN = "!git push --force is what the guide says; is that safe here?"
PLAIN_LATEST = [*EVENTS[:2], {**EVENTS[2], "text": PLAIN, "verbatim": True}, *EVENTS[3:]]


def expect_back_as_plain_text(ui, page):
    """The message is back in the box with Send as plain text on: Enter sends it as typed and runs no command."""
    expect(message_box(page)).to_have_value(PLAIN)
    expect(page.get_by_role("button", name="Send as plain text")).to_have_attribute("aria-pressed", "true")
    message_box(page).press("Enter")
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length + fake.requests('/api/sessions/a/shell').length === 1")
    assert ui.requests("/api/sessions/a/shell") == []
    assert ui.bodies("/api/sessions/a/messages", "POST") == [{"text": PLAIN, "verbatim": True}]


def test_a_failed_retry_of_a_plain_text_message_puts_it_back_as_plain_text(ui, page):
    start(ui, page, events=PLAIN_LATEST, extra="window.resendError = 'The project folder is gone.'")
    page.get_by_role("button", name="Retry response").click()
    ui.wait("fake.requests('/api/sessions/a/rewind', 'POST').length === 1")
    assert rewind_posts(ui)[0]["resend_verbatim"] is True
    expect(page.locator(".app-error")).to_contain_text("The project folder is gone.")
    expect_back_as_plain_text(ui, page)


def test_a_failed_edit_of_a_plain_text_message_puts_it_back_as_plain_text(ui, page):
    start(ui, page, events=PLAIN_LATEST, extra="window.resendError = 'The project folder is gone.'")
    page.get_by_role("button", name="Edit message").nth(1).click()
    expect(page.get_by_role("textbox", name="Edit message")).to_have_value(PLAIN)
    page.get_by_role("button", name="Send edited message").click()
    ui.wait("fake.requests('/api/sessions/a/rewind', 'POST').length === 1")
    assert rewind_posts(ui)[0]["resend_verbatim"] is True
    expect(page.locator(".app-error")).to_contain_text("The project folder is gone.")
    expect_back_as_plain_text(ui, page)


def test_rewinding_to_a_message_puts_it_in_the_box_with_its_own_send_as_plain_text(ui, page):
    start(ui, page, events=PLAIN_LATEST)
    toggle = page.get_by_role("button", name="Send as plain text")
    page.locator(".user-message").nth(1).get_by_role("button", name="Rewind to here").click()
    dialog = page.get_by_role("dialog", name="Rewind conversation")
    dialog.get_by_role("radio", name="Conversation only").check()
    dialog.get_by_role("button", name="Rewind").click()
    expect(dialog).to_have_count(0)
    expect(message_box(page)).to_have_value(PLAIN)
    expect(toggle).to_have_attribute("aria-pressed", "true")
    # One that was not sent as plain text comes back with Send as plain text off, so its command and @ files work.
    message_box(page).fill("")
    page.locator(".user-message").first.get_by_role("button", name="Rewind to here").click()
    dialog.get_by_role("radio", name="Conversation only").check()
    dialog.get_by_role("button", name="Rewind").click()
    expect(dialog).to_have_count(0)
    expect(message_box(page)).to_have_value("First task")
    expect(toggle).to_have_attribute("aria-pressed", "false")


def test_the_rewind_notice_is_in_view_after_a_retry_in_a_long_chat(ui, page):
    long = [event for number in range(1, 9) for event in (
        {"id": f"u{number}", "type": "user", "text": f"Task {number}", "in_wire": True},
        {"id": f"a{number}", "type": "assistant", "text": f"Answer {number}.\n\n" + "Details. " * 120})]
    start(ui, page, events=long + [{"id": "u9", "type": "user", "text": "Last task", "in_wire": True},
                                   {"id": "a9", "type": "assistant", "text": "Last answer."}])
    page.get_by_role("button", name="Retry response").click()
    notice = page.get_by_role("status").filter(has_text="Rewound.")
    expect(notice).to_be_in_viewport()
    expect(notice.get_by_role("button", name="Open earlier version")).to_be_in_viewport()


def test_no_retry_after_steering(ui, page):
    steered = EVENTS[:4] + [{"id": "s2", "type": "user", "text": "Also this", "in_wire": True, "steered": True},
                            {"id": "a2", "type": "assistant", "text": "Second answer."}]
    start(ui, page, events=steered)
    expect(page.get_by_role("button", name="Edit message")).to_have_count(3)
    expect(page.get_by_role("button", name="Retry response")).to_have_count(0)


WAKE = {"id": "w3", "type": "user", "text": "[Sent by the app: the background subagent 1a2b3c4d (explore) finished. Read its "
        "report with get_subagent_result and continue the task.]", "in_wire": True, "verbatim": True, "background_wake": True}


def test_the_apps_message_after_a_background_subagent_is_not_yours_to_edit(ui, page):
    start(ui, page, events=EVENTS + [WAKE, {"id": "a3", "type": "assistant", "text": "Read it; all done."}],
          extra="window.resendError = 'The project folder is gone.'")
    message = page.locator(".user-message").nth(2)
    expect(message.locator(".delegation-origin")).to_have_text("Sent by the app when a background subagent ended")
    expect(message.get_by_role("list", name="Sent with this message")).to_have_count(0)  # Not "Sent as plain text".
    expect(message.get_by_role("button", name="Edit message")).to_have_count(0)
    expect(page.get_by_role("button", name="Edit message")).to_have_count(2)
    # Retry sends the same text again (the server keeps it the app's); a failed one leaves your message box empty.
    page.get_by_role("button", name="Retry response").click()
    ui.wait("fake.requests('/api/sessions/a/rewind', 'POST').length === 1")
    assert rewind_posts(ui) == [{"before_event_id": "w3", "conversation": True, "resend_text": WAKE["text"],
                                 "resend_verbatim": True}]
    expect(page.locator(".app-error")).to_contain_text("The project folder is gone.")
    expect(message_box(page)).to_have_value("")


def test_the_label_of_the_apps_message_after_a_failed_subagent_does_not_say_it_finished(ui, page):
    failed = {**WAKE, "text": WAKE["text"].replace("(explore) finished.", "(explore) failed.")}
    start(ui, page, events=EVENTS + [failed, {"id": "a3", "type": "assistant", "text": "It failed; retrying."}])
    message = page.locator(".user-message").nth(2)
    expect(message).to_contain_text("(explore) failed.")
    expect(message.locator(".delegation-origin")).to_have_text("Sent by the app when a background subagent ended")


def test_rewinding_to_the_apps_message_does_not_put_it_in_your_message_box(ui, page):
    start(ui, page, events=EVENTS + [WAKE, {"id": "a3", "type": "assistant", "text": "Read it; all done."}])
    page.locator(".user-message").nth(2).get_by_role("button", name="Rewind to here").click()
    dialog = page.get_by_role("dialog", name="Rewind conversation")
    dialog.get_by_role("radio", name="Conversation only").check()
    dialog.get_by_role("button", name="Rewind").click()
    expect(dialog).to_have_count(0)
    assert rewind_posts(ui) == [{"before_event_id": "w3", "conversation": True, "code": False}]
    expect(page.get_by_role("status").filter(has_text="Rewound.")).to_be_visible()
    expect(message_box(page)).to_have_value("")


def test_actions_are_hidden_while_busy(ui, page):
    start(ui, page)
    expect(page.get_by_role("button", name="Edit message")).to_have_count(2)
    expect(page.get_by_role("button", name="Fork from here")).to_have_count(2)
    for status in ("running", "awaiting_approval"):
        ui.emit("a", {"type": "status", "status": status})
        expect(page.get_by_role("button", name="Edit message")).to_have_count(0)
        expect(page.get_by_role("button", name="Rewind to here")).to_have_count(0)
        expect(page.get_by_role("button", name="Retry response")).to_have_count(0)
        expect(page.get_by_role("button", name="Fork from here")).to_have_count(0)
        expect(page.get_by_role("button", name="Copy message")).to_have_count(2)
        expect(page.get_by_role("button", name="Copy response")).to_have_count(2)


FAILED_LATEST = EVENTS + [{"id": "u3", "type": "user", "text": "Third task", "in_wire": False},
                          {"id": "e3", "type": "error", "text": "Databricks returned HTTP 500."}]


def test_no_retry_when_the_latest_message_got_no_reply(ui, page):
    # The last reply answers an earlier message; Retry would resend a different, later one.
    start(ui, page, events=FAILED_LATEST)
    expect(page.get_by_role("button", name="Edit message")).to_have_count(3)
    expect(page.get_by_role("button", name="Retry response")).to_have_count(0)
    expect(page.get_by_role("button", name="Fork from here")).to_have_count(2)


def test_rewind_counts_messages_and_replies_separately(ui, page):
    start(ui, page, events=FAILED_LATEST)
    page.get_by_role("button", name="Rewind to here").nth(2).click()
    dialog = page.get_by_role("dialog", name="Rewind conversation")
    expect(dialog.get_by_text("Removes 1 message and 0 replies after this point.")).to_be_visible()
    dialog.get_by_role("button", name="Cancel").click()
    page.get_by_role("button", name="Rewind to here").first.click()
    expect(dialog.get_by_text("Removes 3 messages and 2 replies after this point.")).to_be_visible()


BUSY_NEIGHBOUR = """
window.rewindError = { status: 409, body: { detail: { message: '“Other work” is still running. Stop it before rewinding files.', session_id: 'other', title: 'Other work' } } }
fake.sessions.set('other', makeSession('other', 'Other work'))
"""


def test_a_busy_neighbour_refusing_a_file_rewind_can_be_opened(ui, page):
    start(ui, page, extra=BUSY_NEIGHBOUR)
    page.get_by_role("button", name="Rewind to here").nth(1).click()
    dialog = page.get_by_role("dialog", name="Rewind conversation")
    dialog.get_by_role("button", name="Rewind").click()
    expect(dialog.get_by_role("alert")).to_contain_text("is still running")
    dialog.get_by_role("button", name="Open “Other work”").click()
    assert selected(ui, "other")
    expect(dialog).to_have_count(0)


def test_a_busy_neighbour_refusing_an_edit_can_be_opened(ui, page):
    start(ui, page, extra=BUSY_NEIGHBOUR)
    page.get_by_role("button", name="Edit message").first.click()
    page.get_by_role("checkbox", name="Also undo file changes made from this message on").check()
    page.get_by_role("button", name="Send edited message").click()
    alert = page.get_by_role("alert")
    expect(alert).to_contain_text("is still running")
    alert.get_by_role("button", name="Open “Other work”").click()
    assert selected(ui, "other")


def test_edit_returns_focus_to_its_button_and_a_sent_edit_to_the_message_box(ui, page):
    start(ui, page)
    edit = page.get_by_role("button", name="Edit message").nth(1)
    edit.focus()
    page.keyboard.press("Enter")
    editor = page.get_by_role("textbox", name="Edit message")
    expect(editor).to_be_focused()
    editor.press("Escape")
    expect(edit).to_be_focused()
    page.keyboard.press("Enter")
    page.get_by_role("button", name="Cancel edit").click()
    expect(page.get_by_role("button", name="Edit message").nth(1)).to_be_focused()
    page.get_by_role("button", name="Edit message").nth(1).click()
    page.get_by_role("textbox", name="Edit message").press("Control+Enter")
    ui.wait("fake.requests('/api/sessions/a/rewind', 'POST').length === 1")
    expect(message_box(page)).to_be_focused()


def test_fork_from_here_posts_the_next_user_message_and_selects_the_fork(ui, page):
    start(ui, page)
    page.locator(".reply-content").filter(has_text="First answer.").get_by_role("button", name="Fork from here").click()
    dialog = page.get_by_role("dialog", name="Fork conversation")
    expect(dialog.get_by_role("textbox", name="Title")).to_have_value("Work (fork)")
    expect(dialog.get_by_role("textbox", name="Title")).to_be_focused()
    expect(dialog.get_by_role("textbox", name="Branch name")).to_have_count(0)
    dialog.get_by_role("button", name="Fork").click()
    assert selected(ui, "forked")
    assert ui.bodies("/api/sessions/a/fork", "POST") == [{"before_event_id": "u2", "title": "Work (fork)"}]
    expect(dialog).to_have_count(0)


def test_fork_of_the_last_reply_into_a_worktree(ui, page):
    start(ui, page)
    page.locator(".reply-content").filter(has_text="Second answer.").get_by_role("button", name="Fork from here").click()
    dialog = page.get_by_role("dialog", name="Fork conversation")
    dialog.get_by_role("textbox", name="Title").fill("Try another way")
    dialog.get_by_role("checkbox", name="Create it in a new Git worktree").check()
    branch = dialog.get_by_role("textbox", name="Branch name")
    expect(branch).to_have_value(re.compile(r"^fork/\d{4}-\d{2}-\d{2}-\d{6}$"))
    expect(dialog.get_by_text("The worktree starts from the last commit; uncommitted changes are not copied.")).to_be_visible()
    branch.fill("fork/other-way")
    dialog.get_by_role("button", name="Fork").click()
    assert selected(ui, "forked")
    assert ui.bodies("/api/sessions/a/fork", "POST") == [{"title": "Try another way", "worktree_branch": "fork/other-way"}]


def test_fork_errors_stay_in_the_dialog(ui, page):
    start(ui, page, extra="""
const forkOverride = fake.override
fake.override = (path, options) => path === '/api/sessions/a/fork'
  ? fake.json({ detail: 'Choose a valid new Git branch name of at most 200 characters.' }, 400) : forkOverride(path, options)
""")
    page.locator(".reply-content").filter(has_text="Second answer.").get_by_role("button", name="Fork from here").click()
    dialog = page.get_by_role("dialog", name="Fork conversation")
    dialog.get_by_role("button", name="Fork").click()
    expect(dialog.get_by_role("alert")).to_have_text("Choose a valid new Git branch name of at most 200 characters.")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_have_count(0)


def test_new_chat_in_worktree_from_the_welcome_screen(ui, page):
    start(ui, page, session=None)
    toggle = page.get_by_role("button", name="New chat in worktree")
    toggle.click()
    form = page.get_by_role("form", name="New chat in worktree")
    branch = form.get_by_role("textbox", name="Branch name")
    expect(branch).to_be_focused()
    branch.press("Escape")
    expect(form).to_have_count(0)
    expect(toggle).to_be_focused()
    page.keyboard.press("Enter")
    expect(branch).to_be_focused()
    form.get_by_role("button", name="Cancel").click()
    expect(toggle).to_be_focused()
    toggle.click()
    expect(branch).to_have_value(re.compile(r"^chat/\d{4}-\d{2}-\d{2}-\d{6}$"))
    branch.fill("chat/x")
    form.get_by_role("button", name="Create").click()
    assert selected(ui, "wtchat")
    assert [(call["path"], call["method"]) for call in ui.js("fake.calls") if "/worktrees" in call["path"]] == [
        ("/api/worktrees", "POST"), ("/api/worktrees/wt1/session", "POST")]
    assert ui.bodies("/api/worktrees", "POST") == [{"branch": "chat/x"}]
    # The project chip names the project and branch, not the worktree's internal folder.
    ui.show_session("wtchat")
    chip = page.get_by_role("button", name="Choose project")
    expect(chip).to_have_text("project · chat/x")


def test_a_refused_worktree_branch_keeps_focus_in_the_form(ui, page):
    start(ui, page, session=None, extra="""
const worktreeOverride = fake.override
fake.override = (path, options) => path === '/api/worktrees' && options.method === 'POST'
  ? fake.json({ detail: 'That branch already exists; choose a fresh branch name.' }, 400) : worktreeOverride(path, options)
""")
    page.get_by_role("button", name="New chat in worktree").click()
    branch = page.get_by_role("form", name="New chat in worktree").get_by_role("textbox", name="Branch name")
    branch.press("Enter")
    expect(page.locator(".app-error")).to_contain_text("That branch already exists")
    expect(branch).to_be_focused()
    branch.press("Escape")
    expect(page.get_by_role("form", name="New chat in worktree")).to_have_count(0)


def test_steered_label_and_attachment_chips(ui, page):
    events = [{"id": "u1", "type": "user", "text": "Look at these", "in_wire": True, "attachments": [
                  {"kind": "file", "path": "src/app.py", "start_line": 1, "end_line": 40},
                  {"kind": "file", "path": "big.log", "start_line": 10, "end_line": 900, "truncated": True},
                  {"kind": "shell", "command": "pytest -q " + "tests/" * 20, "exit_code": 1}]},
              {"id": "s1", "type": "user", "text": "Also check the docs", "in_wire": True, "steered": True},
              {"id": "a1", "type": "assistant", "text": "Checked."}]
    start(ui, page, events=events)
    chips = page.get_by_role("list", name="Attachments").get_by_role("listitem")
    command = ("pytest -q " + "tests/" * 20)[:59] + "…"
    expect(chips).to_have_text(["@src/app.py · lines 1–40", "@big.log · lines 10–900 · shortened", f"! {command} · exit 1"])
    steered = page.locator(".user-message").filter(has_text="Also check the docs")
    expect(steered.locator(".steered-label")).to_have_text("Sent while the agent was working")
    expect(page.locator(".steered-label")).to_have_count(1)
