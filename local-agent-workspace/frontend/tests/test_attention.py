"""Attention: browser notifications, the tab-title count, sidebar status and the Files root switcher."""
import json
import re

from playwright.sync_api import expect

# A recording Notification API and a controllable document.hidden.
NOTIFICATIONS = """
window.notifications = []
window.permissionRequests = 0
window.pageHidden = false
window.focusCalls = 0
class TestNotification {
  constructor(title, options) {
    const replaced = notifications.find(n => n.tag === options.tag && !n.closed)
    if (replaced) replaced.closed = true
    Object.assign(this, { title, body: options.body, tag: options.tag, closed: false, alerted: !replaced || !!options.renotify })
    notifications.push(this)
  }
  close() { this.closed = true }
}
TestNotification.permission = 'default'
TestNotification.requestPermission = async () => {
  permissionRequests++
  TestNotification.permission = window.permissionAnswer || 'granted'
  return TestNotification.permission
}
window.Notification = TestNotification
Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.pageHidden })
window.focus = () => { focusCalls++ }
"""
GRANTED = NOTIFICATIONS + "TestNotification.permission = 'granted'; localStorage.setItem('local.notifications', 'on')\n"
TITLE = "addEventListener('DOMContentLoaded', () => { document.title = 'Local' })\n"


def sessions(*items):
    """Replace the fake conversation list: items are (id, title, extra fields)."""
    rows = ", ".join(f"[{json.dumps(id)}, {{ ...makeSession({json.dumps(id)}, {json.dumps(title)}), ...{json.dumps(extra)} }}]"
                     for id, title, extra in items)
    return f"fake.sessions = new Map([{rows}])\n"


def seen(values):
    return f"localStorage.setItem('local.seenConversations', {json.dumps(json.dumps(values))})\n"


def start(ui, page, session=None, setup="", clock=False):
    ui.app(session=session, scenario="workspace", setup=setup, clock=clock)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")


def update(ui, id, **fields):
    ui.run(f"fake.sessions.set({json.dumps(id)}, {{ ...fake.sessions.get({json.dumps(id)}), ...{json.dumps(fields)} }})")


def row(page, title):
    return page.get_by_role("button", name=re.compile(f"^{re.escape(title)}"))


def notes(ui):
    return ui.js("notifications.map(n => [n.title, n.body, n.tag])")


def settle(ui):
    ui.js("new Promise(done => { const channel = new MessageChannel(); channel.port1.onmessage = () => done(); channel.port2.postMessage(0) })")


# Browser notifications

def test_permission_is_asked_only_when_notifications_are_turned_on(ui, page):
    start(ui, page, setup=NOTIFICATIONS)
    switch = page.get_by_role("switch", name="Notify me")
    expect(switch).to_have_attribute("aria-checked", "false")
    assert ui.js("permissionRequests") == 0
    switch.click()
    expect(switch).to_have_attribute("aria-checked", "true")
    assert ui.js("permissionRequests") == 1
    assert ui.js("localStorage.getItem('local.notifications')") == "on"
    switch.click()
    expect(switch).to_have_attribute("aria-checked", "false")
    assert ui.js("localStorage.getItem('local.notifications')") == "off"


def test_a_refused_permission_leaves_notifications_off(ui, page):
    start(ui, page, setup=NOTIFICATIONS + "window.permissionAnswer = 'denied'")
    switch = page.get_by_role("switch", name="Notify me")
    switch.click()
    expect(page.get_by_text("Notifications are blocked for this page in the browser settings.")).to_be_visible()
    expect(switch).to_have_attribute("aria-checked", "false")
    expect(switch).to_be_disabled()


def test_the_choice_is_remembered_only_while_permission_lasts(ui, page):
    start(ui, page, setup=NOTIFICATIONS + "localStorage.setItem('local.notifications', 'on')")
    # Remembered "on", but the browser no longer grants permission.
    expect(page.get_by_role("switch", name="Notify me")).to_have_attribute("aria-checked", "false")
    assert ui.js("permissionRequests") == 0


def test_background_chats_notify_when_they_need_approval_finish_or_fail(ui, page):
    start(ui, page, "b", setup=GRANTED + sessions(("a", "Other conversation", {}), ("b", "Selected chat", {})), clock=True)
    ui.show_session("b")
    update(ui, "a", status="running")
    ui.tick(10000)  # Chats started elsewhere are noticed while everything is idle.
    expect(row(page, "Other conversation").locator(".activity-dot")).to_have_count(1)
    assert notes(ui) == []
    update(ui, "a", status="awaiting_approval")
    ui.tick(1500)
    ui.wait("notifications.length === 1")
    update(ui, "a", status="running")
    ui.tick(1500)
    update(ui, "a", status="idle", terminal_reason="completed", updated=2, turn_ended=2)
    ui.tick(1500)
    ui.wait("notifications.length === 2")
    update(ui, "a", status="running")
    ui.tick(10000)
    update(ui, "a", status="idle", terminal_reason="error", updated=3, turn_ended=3)
    ui.tick(1500)
    ui.wait("notifications.length === 3")
    update(ui, "a", status="running")
    ui.tick(10000)
    update(ui, "a", status="idle", terminal_reason="stopped", updated=4, turn_ended=4)
    ui.tick(1500)
    settle(ui)
    expect(row(page, "Other conversation").locator(".activity-dot")).to_have_count(0)
    # Only a status and the title: never transcript text.
    assert notes(ui) == [
        ["Local", "Needs your approval: Other conversation", "a:approval"],
        ["Local", "Finished: Other conversation", "a:finished"],
        ["Local", "Stopped with an error: Other conversation", "a:error"],
    ]
    # Clicking a notification focuses the window and opens that chat.
    ui.run("notifications[0].onclick()")
    expect(row(page, "Other conversation")).to_have_attribute("aria-current", "page")
    assert ui.js("focusCalls") == 1 and ui.js("notifications[0].closed")


def test_the_open_chat_notifies_only_while_the_page_is_hidden(ui, page):
    long_title = "A conversation title that is much longer than sixty characters in total"
    start(ui, page, "a", setup=GRANTED + sessions(("a", long_title, {})))
    ui.show_session("a")
    ui.emit("a", {"type": "status", "status": "running"})
    ui.emit("a", {"type": "status", "status": "awaiting_approval"})
    settle(ui)
    assert notes(ui) == []
    ui.run("window.pageHidden = true")
    ui.emit("a", {"type": "status", "status": "running"})
    ui.emit("a", {"type": "status", "status": "awaiting_approval"})
    ui.emit("a", {"type": "status", "status": "running"})
    ui.emit("a", {"type": "status", "status": "idle", "terminal_reason": "inference_error", "turn_ended": 5})
    ui.wait("notifications.length === 2")
    short = long_title[:59] + "…"
    assert notes(ui) == [["Local", f"Needs your approval: {short}", "a:approval"],
                         ["Local", f"Stopped with an error: {short}", "a:error"]]


def test_a_delegated_turn_notifies_only_when_it_needs_approval(ui, page):
    start(ui, page, setup=GRANTED + sessions(("c", "Delegated check", {"is_subagent": True, "status": "running",
                                                                       "turn_delegated": True})), clock=True)
    update(ui, "c", status="awaiting_approval")
    ui.tick(1500)
    ui.wait("notifications.length === 1")
    update(ui, "c", status="idle", terminal_reason="completed", updated=2, turn_ended=2)
    ui.tick(1500)
    expect(row(page, "Delegated check").locator(".row-badge")).to_have_count(0)
    settle(ui)
    assert notes(ui) == [["Local", "Needs your approval: Delegated check", "c:approval"]]
    # Continued by you, not by the parent: nobody else reports its end.
    update(ui, "c", status="running", turn_ended=None, turn_delegated=False)
    ui.tick(10000)
    update(ui, "c", status="idle", terminal_reason="completed", updated=3, turn_ended=3)
    ui.tick(1500)
    ui.wait("notifications.length === 2")
    assert notes(ui)[-1] == ["Local", "Finished: Delegated check", "c:finished"]


def test_a_subagent_chat_open_in_a_hidden_tab_notifies_when_your_turn_ends(ui, page):
    start(ui, page, "c", setup=GRANTED + sessions(("c", "Delegated check", {"is_subagent": True, "turn_ended": 1,
                                                                            "turn_delegated": True})))
    ui.show_session("c")
    ui.run("window.pageHidden = true")
    ui.emit("c", {"type": "status", "status": "running"})
    # The status that ends your turn carries no turn_delegated: this turn was not delegated.
    ui.emit("c", {"type": "status", "status": "idle", "terminal_reason": "completed", "turn_ended": 5})
    ui.wait("notifications.length === 1")
    assert notes(ui) == [["Local", "Finished: Delegated check", "c:finished"]]


def test_a_turn_stopped_at_the_step_limit_is_not_announced_as_finished(ui, page):
    start(ui, page, "b", setup=GRANTED + sessions(("a", "Long job", {"status": "running", "turn_ended": None}),
                                                   ("b", "Selected chat", {})), clock=True)
    ui.show_session("b")
    update(ui, "a", status="idle", terminal_reason="step_limit", turn_ended=6, updated=6)
    ui.tick(1500)
    ui.wait("notifications.length === 1")
    assert notes(ui) == [["Local", "Reached the step limit: Long job", "a:step_limit"]]


def test_turn_ends_notify_after_automatic_compaction_but_not_after_a_manual_one(ui, page):
    start(ui, page, "b", setup=GRANTED + sessions(("a", "Other conversation", {"turn_ended": 1}),
                                                   ("b", "Selected chat", {})), clock=True)
    ui.show_session("b")
    ui.run("window.pageHidden = true")
    # A manual compaction: its end is not a reply.
    update(ui, "a", status="compacting")
    ui.tick(10000)
    update(ui, "a", status="idle", updated=2)
    ui.tick(1500)
    # A turn that compacts automatically and then fails.
    update(ui, "a", status="running", turn_ended=None)
    ui.tick(10000)
    update(ui, "a", status="compacting")
    ui.tick(1500)
    update(ui, "a", status="idle", terminal_reason="inference_error", turn_ended=3, updated=3)
    ui.tick(1500)
    ui.wait("notifications.length === 1")
    # A turn that ran entirely between two polls is still reported.
    update(ui, "a", terminal_reason="completed", turn_ended=4, updated=4)
    ui.tick(10000)
    ui.wait("notifications.length === 2")
    settle(ui)
    assert notes(ui) == [["Local", "Stopped with an error: Other conversation", "a:error"],
                         ["Local", "Finished: Other conversation", "a:finished"]]


def test_a_turn_cut_off_by_a_server_restart_is_an_error_not_finished(ui, page):
    start(ui, page, "b", setup=GRANTED + sessions(("a", "Other conversation", {"status": "running", "turn_ended": None}),
                                                   ("b", "Selected chat", {})) + seen({"a": 1, "b": 1}), clock=True)
    ui.show_session("b")
    update(ui, "a", status="idle", terminal_reason="interrupted", turn_ended=6, updated=6)
    ui.tick(1500)
    ui.wait("notifications.length === 1")
    assert notes(ui) == [["Local", "Stopped with an error: Other conversation", "a:error"]]
    expect(row(page, "Other conversation")).to_contain_text("Error")


def test_a_repeated_approval_alerts_again_and_answered_notifications_close(ui, page):
    start(ui, page, "b", setup=GRANTED + sessions(("a", "Other conversation", {"status": "running"}),
                                                   ("b", "Selected chat", {})), clock=True)
    ui.show_session("b")
    for _ in range(2):
        update(ui, "a", status="awaiting_approval")
        ui.tick(1500)
        update(ui, "a", status="running")
        ui.tick(1500)
    ui.wait("notifications.length === 2")
    # Each request alerts; the answered one was closed, not silently replaced.
    assert ui.js("notifications.map(n => [n.tag, n.closed, n.alerted])") == [
        ["a:approval", True, True], ["a:approval", True, True]]
    update(ui, "a", status="idle", terminal_reason="completed", turn_ended=2, updated=2)
    ui.tick(1500)
    ui.wait("notifications.length === 3")
    assert ui.js("notifications[2].closed") is False
    # Opening the chat answers its notification.
    row(page, "Other conversation").click()
    ui.wait("notifications[2].closed")


def test_no_notifications_unless_turned_on(ui, page):
    start(ui, page, "b", setup=NOTIFICATIONS + "TestNotification.permission = 'granted'\n"
          + sessions(("a", "Other conversation", {}), ("b", "Selected chat", {})), clock=True)
    ui.show_session("b")
    update(ui, "a", status="awaiting_approval")
    ui.tick(10000)
    expect(row(page, "Other conversation")).to_contain_text("Needs approval")
    assert notes(ui) == []


# Tab title

def test_every_page_holds_one_presence_socket_for_the_server(ui, page):
    start(ui, page, clock=True)
    ui.wait("fake.presence.length === 1")
    assert ui.js("fake.presence[0].protocols[0]") == "local-workspace"
    assert ui.js("fake.presence[0].url").endswith("/api/presence")
    assert ui.js("fake.sockets.length") == 0  # No conversation is open.
    ui.run("fake.presence[0].close()")  # The server restarted.
    ui.tick(5000)
    ui.wait("fake.presence.length === 2")


def test_the_notify_switch_shows_its_state_in_text(ui, page):
    start(ui, page, setup=NOTIFICATIONS)
    switch = page.get_by_role("switch", name="Notify me", exact=True)
    expect(switch.locator(".notify-state")).to_have_text("Off")
    expect(switch.locator(".lucide-bell-off")).to_have_count(1)
    switch.click()
    expect(switch.locator(".notify-state")).to_have_text("On")
    expect(switch.locator(".lucide-bell")).to_have_count(1)


def test_tab_title_counts_chats_waiting_for_approval(ui, page):
    start(ui, page, setup=TITLE + sessions(("a", "First", {"status": "awaiting_approval"}),
                                           ("b", "Second", {"status": "awaiting_approval"}), ("c", "Third", {})), clock=True)
    expect(page).to_have_title("(2) Local")
    update(ui, "a", status="running")
    ui.tick(1500)
    expect(page).to_have_title("(1) Local")
    update(ui, "a", status="idle")
    update(ui, "b", status="idle")
    ui.tick(1500)
    expect(page).to_have_title("Local")


# Sidebar status

STATUS_CHATS = sessions(
    ("a", "Waiting chat", {"status": "awaiting_approval"}),
    ("b", "Busy chat", {"status": "running"}),
    ("c", "Failed chat", {"terminal_reason": "error", "updated": 5}),
    ("d", "Changed chat", {"updated": 7}),
    ("e", "Quiet chat", {"updated": 3}),
)


def test_rows_show_approval_activity_error_and_unread(ui, page):
    start(ui, page, setup=STATUS_CHATS + seen({"a": 1, "b": 1, "c": 1, "d": 1, "e": 3}))
    expect(row(page, "Waiting chat")).to_contain_text("Needs approval")
    expect(row(page, "Waiting chat").locator(".activity-dot")).to_have_count(0)
    expect(row(page, "Busy chat").locator(".activity-dot")).to_have_count(1)
    expect(row(page, "Busy chat")).to_have_accessible_name(re.compile("^Busy chat.*Running$"))
    expect(row(page, "Failed chat")).to_contain_text("Error")
    expect(row(page, "Changed chat")).to_have_accessible_name(re.compile("^Changed chat.*Unread$"))
    expect(row(page, "Changed chat").locator(".unread-dot")).to_have_count(1)
    expect(row(page, "Quiet chat").locator(".unread-dot, .row-badge")).to_have_count(0)
    # Opening a chat marks it read, and it stays read after leaving it.
    row(page, "Changed chat").click()
    expect(row(page, "Changed chat").locator(".unread-dot")).to_have_count(0)
    row(page, "Failed chat").click()
    expect(row(page, "Failed chat")).not_to_contain_text("Error")
    row(page, "Quiet chat").click()
    expect(row(page, "Changed chat").locator(".unread-dot")).to_have_count(0)
    expect(row(page, "Failed chat").locator(".row-badge")).to_have_count(0)
    assert ui.js("JSON.parse(localStorage.getItem('local.seenConversations'))") == {"a": 1, "b": 1, "c": 5, "d": 7, "e": 3}


def test_read_state_is_shared_between_tabs(ui, page):
    key = "local.seenConversations"
    start(ui, page, setup=STATUS_CHATS + seen({"a": 1, "b": 1, "c": 1, "d": 1, "e": 3}))
    expect(row(page, "Failed chat")).to_contain_text("Error")
    # Another tab opened the failed chat.
    ui.run(f"""const value = JSON.stringify({{ a: 1, b: 1, c: 5, d: 1, e: 3 }})
      localStorage.setItem('{key}', value)
      dispatchEvent(new StorageEvent('storage', {{ key: '{key}', newValue: value }}))""")
    expect(row(page, "Failed chat").locator(".row-badge")).to_have_count(0)
    # A write from a tab that has not heard of another tab's read keeps it.
    ui.run(f"localStorage.setItem('{key}', JSON.stringify({{ a: 1, b: 1, c: 5, d: 1, e: 9 }}))")
    row(page, "Changed chat").click()
    expect(row(page, "Changed chat")).to_have_attribute("aria-current", "page")
    ui.wait(f"JSON.parse(localStorage.getItem('{key}')).d === 7")
    assert ui.js(f"JSON.parse(localStorage.getItem('{key}'))") == {"a": 1, "b": 1, "c": 5, "d": 7, "e": 9}


def test_first_use_marks_existing_chats_read_and_later_changes_unread(ui, page):
    start(ui, page, setup=STATUS_CHATS, clock=True)
    expect(row(page, "Waiting chat")).to_contain_text("Needs approval")
    expect(page.locator(".unread-dot")).to_have_count(0)
    expect(row(page, "Failed chat").locator(".row-badge")).to_have_count(0)
    assert ui.js("JSON.parse(localStorage.getItem('local.seenConversations'))") == {"a": 1, "b": 1, "c": 5, "d": 7, "e": 3}
    update(ui, "e", updated=9)
    update(ui, "b", status="idle", terminal_reason="error", updated=8)
    ui.tick(1500)
    expect(row(page, "Quiet chat").locator(".unread-dot")).to_have_count(1)
    expect(row(page, "Busy chat")).to_contain_text("Error")


def test_running_filter_shows_only_chats_that_are_not_idle(ui, page):
    start(ui, page, setup=STATUS_CHATS + seen({}))
    expect(page.locator(".conversation-row")).to_have_count(5)
    running = page.get_by_role("button", name="Running", exact=True)
    expect(running).to_have_attribute("aria-pressed", "false")
    running.click()
    expect(running).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".conversation-row")).to_have_count(2)
    expect(row(page, "Waiting chat")).to_be_visible()
    expect(row(page, "Busy chat")).to_be_visible()
    running.click()
    expect(page.locator(".conversation-row")).to_have_count(5)


def test_running_filter_says_when_nothing_runs(ui, page):
    start(ui, page)
    page.get_by_role("button", name="Running", exact=True).click()
    expect(page.get_by_text("No conversation is running.")).to_be_visible()
    expect(page.locator(".conversation-row")).to_have_count(0)


def test_renaming_a_read_chat_keeps_it_read(ui, page):
    start(ui, page, setup=seen({"a": 1}) + """fake.override = (path, options) => {
      if (path === '/api/sessions/a/title' && options.method === 'PUT') {
        const updated = { ...fake.sessions.get('a'), title: JSON.parse(options.body).title, updated: 9 }
        fake.sessions.set('a', updated)
        return fake.json(updated)
      }
    }""", clock=True)
    ui.row_action("Other conversation", "Rename")
    page.get_by_role("textbox", name="Conversation name").fill("Renamed chat")
    page.get_by_role("button", name="Save name").click()
    expect(row(page, "Renamed chat")).to_be_visible()
    ui.tick(10000)
    ui.wait("fake.requests('/api/sessions', 'GET').length >= 2")
    settle(ui)
    expect(row(page, "Renamed chat").locator(".unread-dot")).to_have_count(0)


def test_everything_works_without_local_storage(ui, page):
    blocked = "Object.defineProperty(window, 'localStorage', { configurable: true, get() { throw new DOMException('blocked', 'SecurityError') } })\n"
    start(ui, page, setup=NOTIFICATIONS + blocked + STATUS_CHATS)
    expect(row(page, "Waiting chat")).to_contain_text("Needs approval")
    expect(page.locator(".unread-dot")).to_have_count(0)
    switch = page.get_by_role("switch", name="Notify me")
    switch.click()
    expect(switch).to_have_attribute("aria-checked", "true")


# Settings

def test_desktop_notifications_setting_is_off_by_default_and_saved(ui, page):
    start(ui, page, setup="fake.settings = { ...baseSettings, system_notifications: false }")
    page.get_by_role("button", name="Settings", exact=True).click()
    toggle = page.get_by_role("dialog").get_by_role("checkbox", name="Desktop notification when no browser tab is open")
    expect(toggle).not_to_be_checked()
    toggle.click()
    page.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["system_notifications"] is True


# Files root switcher

FOLDERS = sessions(("a", "Other conversation", {"allowed_directories": ["/srv/data"]}),
                   ("b", "Second chat", {"allowed_directories": ["/srv/data"]})) + """fake.override = path => {
  const listing = {
    '/srv/data': [{ path: '/srv/data/sub', name: 'sub', directory: true }, { path: '/srv/data/readme.md', name: 'readme.md', directory: false }],
    '/srv/data/sub': [{ path: '/srv/data/sub/out.csv', name: 'out.csv', directory: false }],
  }
  const folder = path.startsWith('/api/files?') ? new URLSearchParams(path.split('?')[1]).get('path') : null
  if (folder in listing) return fake.json(listing[folder])
}"""


def test_granted_folders_can_be_browsed_with_change_dots(ui, page):
    start(ui, page, "a", setup=FOLDERS)
    ui.show_session("a")
    page.get_by_role("button", name="Workspace", exact=True).click()
    ui.emit("a", "{ type: 'changes', changes: makeChanges(changedFile('/srv/data/sub/out.csv'), changedFile('note.txt')) }")
    roots = page.get_by_role("group", name="Folders in Files")
    project = roots.get_by_role("button", name="Project, 1 changed file")
    granted = roots.get_by_role("button", name="Allowed in this chat: /srv/data, 1 changed file")
    expect(project).to_have_attribute("aria-pressed", "true")
    expect(granted).to_have_attribute("aria-pressed", "false")
    expect(roots.get_by_role("button")).to_have_count(2)  # A granted folder cannot be removed from Files.
    expect(page.get_by_role("button", name="note.txt").get_by_role("img", name="Changed by the agent")).to_be_visible()
    granted.click()
    expect(granted).to_have_attribute("aria-pressed", "true")
    folder = page.get_by_role("button", name="sub")
    expect(folder.get_by_role("img", name="Contains changed files")).to_be_visible()
    expect(page.get_by_role("button", name="readme.md").get_by_role("img")).to_have_count(0)
    parent = page.get_by_role("button", name="Parent folder")
    expect(parent).to_be_disabled()  # Not above the granted folder.
    assert ui.requests("/api/files?path=%2Fsrv%2Fdata&session_id=a", "GET")
    folder.click()
    out = page.get_by_role("button", name="out.csv")
    expect(out.get_by_role("img", name="Changed by the agent")).to_be_visible()
    parent.click()
    expect(page.get_by_role("button", name="sub")).to_be_visible()
    expect(parent).to_be_disabled()
    project.click()
    expect(page.get_by_role("button", name="note.txt")).to_be_visible()
    expect(parent).to_be_disabled()


def test_a_file_from_a_granted_folder_is_not_called_a_project_file(ui, page):
    start(ui, page, "a", setup=FOLDERS)
    ui.show_session("a")
    page.get_by_role("button", name="Workspace", exact=True).click()
    page.get_by_role("button", name="note.txt").click()
    page.get_by_role("button", name="Add this file to chat", exact=True).click()
    message = page.get_by_role("textbox", name="Message")
    expect(message).to_have_value("Please read the project file: note.txt")
    page.get_by_role("button", name="Workspace", exact=True).click()
    page.get_by_role("button", name="Back to files", exact=True).click()
    page.get_by_role("button", name="Allowed in this chat: /srv/data").click()
    page.get_by_role("button", name="readme.md").click()
    page.get_by_role("button", name="Add this file to chat", exact=True).click()
    expect(message).to_have_value("Please read the project file: note.txt\nPlease read the file: /srv/data/readme.md")


def test_the_root_resets_for_another_chat_or_a_removed_grant(ui, page):
    start(ui, page, "a", setup=FOLDERS)
    ui.show_session("a")
    page.get_by_role("button", name="Workspace", exact=True).click()
    granted = page.get_by_role("button", name="Allowed in this chat: /srv/data")
    granted.click()
    expect(page.get_by_role("button", name="sub")).to_be_visible()
    row(page, "Second chat").click()
    ui.show_session("b")
    expect(page.get_by_role("button", name="Project", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_role("button", name="note.txt")).to_be_visible()
    granted.click()
    expect(page.get_by_role("button", name="sub")).to_be_visible()
    ui.emit("b", {"type": "permissions", "permission_mode": "manual", "allowed_directories": []})
    expect(page.get_by_role("group", name="Folders in Files")).to_have_count(0)
    expect(page.get_by_role("button", name="note.txt")).to_be_visible()


def test_no_root_switcher_without_granted_folders(ui, page):
    start(ui, page, "a")
    ui.show_session("a")
    page.get_by_role("button", name="Workspace", exact=True).click()
    expect(page.get_by_role("button", name="note.txt")).to_be_visible()
    expect(page.get_by_role("group", name="Folders in Files")).to_have_count(0)
    expect(page.get_by_role("button", name="Add folder…")).to_be_visible()
