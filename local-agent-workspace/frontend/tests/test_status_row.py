"""The status row above the message box (StatusRow.js), after the review of the UI revamp.

What shows together: Continue and the queued messages (they always occur together after a stop), else the changes, else
the tasks; every other item is a chip that carries its main action. The /btw answer, the commands help and the rewind
notice are a line each. An error always shows its text in a visible alert. A control never needs more clicks than
in the base app (tests/test_reachability.py counts them). While keyboard focus is in the row, a newly arriving item
waits as a chip and is announced; it never replaces the item that has focus."""
import json
import re

import pytest
from playwright.sync_api import expect

TASKS = [{"id": f"t{n}", "title": f"Task number {n}", "status": "pending"} for n in range(3)]
CHAT = ("fake.sessions.set('a', { ...makeSession('a', 'Fix the login redirect'), "
        "events: [{ id: 'u', type: 'user', text: 'Do the work' }, { id: 'r', type: 'assistant', text: 'Done.' }]%s })")
ROUTES = """
const previous = fake.override
fake.override = (path, options) => {
  if (/\\/side-question$/.test(path)) return fake.json({ answer: 'It changed **two** files.', request_info: { status: 'completed', finish_reason: 'stop' } })
  return previous ? previous(path, options) : undefined
}
"""
QUEUE_ERROR = "The queue could not start: the model is gone."


def open_chat(ui, page, width=1280, height=900, fields="", setup=""):
    page.set_viewport_size({"width": width, "height": height})
    ui.app(session="a", scenario="workspace", setup=CHAT % fields + ROUTES + setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    ui.show_session("a")


def queue(ui, count=2, error=None):
    items = [{"id": f"q{n}", "text": f"Queued message number {n}", "created": n, "paused": True} for n in range(count)]
    ui.emit("a", {"type": "queue", "queued_messages": items, **({"error": error} if error else {})})


def changes(ui, files=3, error=None):
    names = ["note.txt"] + [f"note{n}.txt" for n in range(1, files)]
    listed = ", ".join(f"changedFile('{name}', 2, 1)" for name in names)
    value = f"makeChanges({listed})" if not error else f"{{ ...makeChanges({listed}), error: {json.dumps(error)} }}"
    ui.run("fake.socketFor('a').emit({ type: 'changes', changes: %s })" % value)


def no_files_but_an_error(ui, error):
    ui.run("fake.socketFor('a').emit({ type: 'changes', changes: { files: [], added: 0, removed: 0, error: %s } })" % json.dumps(error))


def tasks(ui):
    ui.emit("a", {"type": "tasks", "tasks": TASKS})


def stop(ui):
    ui.emit("a", {"type": "status", "status": "idle", "terminal_reason": "stopped"})


def rewind(ui, title="Work (before rewind)"):
    ui.js("import('/static/js/rewindNotice.js').then(m => { m.setRewindNotice('a', { title: %s, backupId: 'b', draftKept: true }); return 1 })" % json.dumps(title))


def everything(ui, page, width=1280, height=900):
    """A stopped chat with two queued messages, three changed files, tasks and a rewind notice."""
    open_chat(ui, page, width, height, fields=", terminal_reason: 'stopped'")
    queue(ui)
    changes(ui)
    tasks(ui)
    rewind(ui)
    expect(page.get_by_role("button", name="Open earlier version")).to_be_visible()


def row(page):
    return page.get_by_role("group", name="Conversation status")


def chips(page):
    return row(page).locator(".status-chip")


def alerts(page):
    return row(page).locator("[role=alert]")


def live(page):
    return page.locator(".status-live")


def no_sideways_scroll(ui, width):
    assert ui.js("document.documentElement.scrollWidth <= innerWidth")
    box = ui.js("(() => { const r = document.querySelector('.status-row').getBoundingClientRect(); return { left: r.left, right: r.right } })()")
    assert box["left"] >= 0 and box["right"] <= width, box
    assert ui.js("(() => { const r = document.querySelector('.status-row'); return [...r.querySelectorAll('*')].every(e => { const b = e.getBoundingClientRect(); return !b.width || b.right <= r.getBoundingClientRect().right + 1 }) })()")


# H1: an error from an item is never hidden behind a chip.

def test_a_queue_error_and_a_changes_error_show_while_continue_leads(ui, page):
    open_chat(ui, page, fields=", terminal_reason: 'stopped'")
    queue(ui, count=0, error=QUEUE_ERROR)
    no_files_but_an_error(ui, "git failed")
    expect(row(page).get_by_role("button", name="Continue", exact=True)).to_be_visible()
    expect(alerts(page)).to_have_count(2)
    expect(row(page).get_by_text(QUEUE_ERROR)).to_be_visible()
    expect(row(page).get_by_text("Could not check changed files: git failed")).to_be_visible()
    for alert in alerts(page).all():
        expect(alert).to_be_visible()
    assert ui.js("document.querySelectorAll('[role=alert]').length") == 2  # Nothing else on the page, nothing twice.


def test_an_error_stays_visible_whatever_else_arrives_and_its_alert_stays_mounted(ui, page):
    open_chat(ui, page)
    queue(ui, count=0, error=QUEUE_ERROR)
    expect(alerts(page)).to_have_count(1)
    page.evaluate("() => { document.querySelector('.status-row [role=alert]').dataset.probe = 'same' }")
    tasks(ui)
    changes(ui)
    stop(ui)
    rewind(ui)
    expect(row(page).get_by_role("button", name="Continue", exact=True)).to_be_visible()
    expect(row(page).get_by_text(QUEUE_ERROR)).to_be_visible()
    assert page.evaluate("() => document.querySelector('.status-row [role=alert][data-probe=same]')?.isConnected === true")


def test_a_changes_error_behind_a_chip_has_its_own_alert_and_the_chip_says_error(ui, page):
    open_chat(ui, page)
    changes(ui, error="git failed")
    tasks(ui)
    chips(page).filter(has_text="Tasks").click()  # The tasks lead; the changes are a chip now.
    expect(row(page).get_by_role("region", name="Tasks")).to_be_visible()
    expect(row(page).get_by_role("region", name="Changed files")).to_have_count(0)
    expect(chips(page)).to_have_text(["Changes error"])
    expect(chips(page).first).to_have_attribute("aria-label", re.compile("error"))
    expect(alerts(page)).to_have_count(1)
    expect(alerts(page).first).to_be_visible()
    expect(alerts(page).first).to_contain_text("Could not check changed files: git failed")


def test_errors_beside_queued_messages_and_changes_that_are_chips_show_too(ui, page):
    # The reviewer's case with the items present: Continue leads with the queue, the changes are a chip, both fail.
    open_chat(ui, page, fields=", terminal_reason: 'stopped'")
    queue(ui, error=QUEUE_ERROR)
    changes(ui, error="git failed")
    expect(row(page).locator(".status-primary")).to_have_attribute("data-item", "continue")
    expect(chips(page)).to_have_text(["Changes error"])
    expect(alerts(page)).to_have_count(2)
    expect(row(page).get_by_text(QUEUE_ERROR)).to_be_visible()
    expect(row(page).get_by_text("Could not check changed files: git failed")).to_be_visible()
    assert ui.js("document.querySelectorAll('[role=alert]').length") == 2


# M1: no control needs another click because something more urgent is present.

CONTROLS = {
    "Continue": lambda page: row(page).get_by_role("button", name="Continue", exact=True),
    "Send queued messages": lambda page: row(page).get_by_role("button", name="Send queued messages"),
    "Edit queued": lambda page: row(page).get_by_role("button", name=re.compile("^Edit queued message")).first,
    "Remove queued": lambda page: row(page).get_by_role("button", name=re.compile("^Remove queued message")).first,
    "Keep all changes": lambda page: row(page).get_by_role("button", name="Keep all changes"),
    "Undo all changes": lambda page: row(page).get_by_role("button", name="Undo all changes"),
    "Tasks toggle": lambda page: chips(page).filter(has_text="Tasks 0/3").or_(row(page).locator(".task-checklist-toggle")),
    "Open earlier version": lambda page: row(page).get_by_role("button", name="Open earlier version"),
    "Dismiss rewind notice": lambda page: row(page).get_by_role("button", name="Dismiss rewind notice"),
}


@pytest.mark.parametrize("name", list(CONTROLS))
def test_with_everything_present_each_control_is_on_screen_without_a_chip_click(ui, page, name):
    everything(ui, page)
    expect(CONTROLS[name](page)).to_be_visible()
    expect(CONTROLS[name](page)).to_be_enabled()


def test_continue_and_the_queued_messages_show_together(ui, page):
    everything(ui, page)
    primary = row(page).locator(".status-primary")
    expect(primary.get_by_role("button", name="Continue", exact=True)).to_be_visible()
    expect(primary.get_by_role("group", name="Queued messages").locator(".queue-item")).to_have_count(2)
    assert chips(page).filter(has_text=re.compile("Stopped|queued")).count() == 0
    # Not one over the other: both are on screen at the same time, Continue first.
    cont = primary.get_by_role("button", name="Continue", exact=True).bounding_box()
    queued = primary.get_by_role("group", name="Queued messages").bounding_box()
    assert cont["y"] + cont["height"] <= queued["y"] + 1, (cont, queued)


def test_the_tasks_chip_is_the_toggle_and_the_changes_chip_carries_keep_and_undo(ui, page):
    everything(ui, page)
    expect(chips(page)).to_have_text(["3 changes", "Tasks 0/3"])
    keep = row(page).get_by_role("button", name="Keep all changes")
    undo = row(page).get_by_role("button", name="Undo all changes")
    expect(keep).to_be_visible()
    expect(undo).to_be_visible()
    undo.click()
    ui.wait("fake.requests('/api/sessions/a/changes/undo', 'POST').length === 1")
    assert [file["path"] for file in ui.bodies("/api/sessions/a/changes/undo", "POST")[0]["files"]] == ["note.txt", "note1.txt", "note2.txt"]
    changes(ui)  # The undo answered with no files left; the changes come back.
    expect(keep).to_be_visible()
    keep.click()
    ui.wait("fake.requests('/api/sessions/a/changes/keep', 'POST').length === 1")


def test_the_tasks_chip_opens_the_tasks_in_one_click(ui, page):
    everything(ui, page)
    chips(page).filter(has_text="Tasks 0/3").click()  # One click, as the toggle was one click.
    expect(row(page).get_by_role("region", name="Tasks").get_by_role("listitem")).to_have_count(3)


def test_the_changes_chip_itself_opens_the_file_list(ui, page):
    everything(ui, page)
    chips(page).filter(has_text="3 changes").click()
    expect(row(page).get_by_role("region", name="Changed files").locator(".changes-file")).to_have_count(3)


def test_a_picked_chip_turns_continue_and_the_queue_into_chips_that_keep_their_buttons(ui, page):
    everything(ui, page)
    chips(page).filter(has_text="Tasks 0/3").click()
    expect(chips(page)).to_have_text(["Stopped", "2 queued", "3 changes"])
    expect(row(page).get_by_role("button", name="Continue", exact=True)).to_be_visible()
    expect(row(page).get_by_role("button", name="Send queued messages")).to_be_visible()
    row(page).get_by_role("button", name="Continue", exact=True).click()
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/messages", "POST")[0]["text"] == "Continue from where you stopped."


@pytest.mark.parametrize("command,proof", [("/btw how far along?", "Side question"), ("/help", "Commands")])
def test_a_command_answer_is_an_extra_line_and_does_not_demote_continue(ui, page, command, proof):
    everything(ui, page)
    box = page.get_by_role("textbox", name="Message")
    box.fill(command)
    box.press("Enter")
    expect(row(page).get_by_role("region", name=proof)).to_be_visible()
    expect(row(page).get_by_role("button", name="Continue", exact=True)).to_be_visible()
    expect(row(page).locator(".status-primary").get_by_role("button", name="Continue", exact=True)).to_be_visible()
    assert chips(page).filter(has_text=re.compile("Stopped|queued|Commands|Side")).count() == 0
    expect(row(page).get_by_role("button", name="Send queued messages")).to_be_visible()


def test_the_help_line_has_its_close_button_at_once(ui, page):
    everything(ui, page)
    box = page.get_by_role("textbox", name="Message")
    box.fill("/help")
    box.press("Enter")
    expect(row(page).get_by_role("button", name="Close commands")).to_be_visible()


def test_the_side_answer_has_copy_and_dismiss_at_once(ui, page):
    everything(ui, page)
    box = page.get_by_role("textbox", name="Message")
    box.fill("/btw how far along?")
    box.press("Enter")
    expect(row(page).get_by_role("button", name="Copy answer")).to_be_visible()
    expect(row(page).get_by_role("button", name="Dismiss", exact=True)).to_be_visible()


@pytest.mark.parametrize("width", [390, 320])
def test_a_phone_row_with_everything_wraps_and_does_not_scroll_sideways(ui, page, width):
    everything(ui, page, width, 844)
    box = page.get_by_role("textbox", name="Message")
    box.fill("/btw how far along?")
    box.press("Enter")
    expect(row(page).get_by_role("region", name="Side question")).to_be_visible()
    chips(page).filter(has_text="Tasks").click()  # The tasks lead; Continue, the queue and the changes are chips that wrap.
    expect(row(page).get_by_role("button", name="Keep all changes")).to_be_visible()
    no_sideways_scroll(ui, width)
    # The bars scroll inside their own cap, above the message box.
    assert ui.js("document.querySelector('.composer-bars').getBoundingClientRect().bottom <= document.querySelector('.chat-composer .composer').getBoundingClientRect().top + 1")


# M2: a new rewind notice shows at once, as its own line.

def test_a_new_rewind_notice_shows_at_once_beside_other_items(ui, page):
    open_chat(ui, page)
    tasks(ui)
    changes(ui)
    rewind(ui)
    notice = row(page).get_by_role("status").filter(has_text="Rewound.")
    expect(notice).to_be_visible()
    assert notice.evaluate("e => !e.closest('[hidden]') && e.checkVisibility({ visibilityProperty: true })")
    expect(row(page).get_by_role("button", name="Open earlier version")).to_be_visible()
    expect(row(page).get_by_role("button", name="Dismiss rewind notice")).to_be_visible()
    # It sits in a region that is live already, so a screen reader hears it arrive.
    assert notice.evaluate("e => !!e.closest('[aria-live]') || e.getAttribute('role') === 'status'")
    assert page.evaluate("() => !!document.querySelector('.status-rewind')?.closest('[aria-live=polite]') || document.querySelector('.status-rewind')?.getAttribute('aria-live') === 'polite'")


def test_a_rewind_notice_alone_shows_the_row_and_dismissing_it_hides_the_row(ui, page):
    open_chat(ui, page)
    expect(page.locator(".status-row")).not_to_be_visible()
    rewind(ui)
    expect(row(page).get_by_role("status").filter(has_text="Rewound.")).to_be_visible()
    row(page).get_by_role("button", name="Dismiss rewind notice").click()
    expect(page.locator(".status-row")).not_to_be_visible()
    rewind(ui, "Again")  # A later one shows again.
    expect(row(page).get_by_text("Again")).to_be_visible()


def test_a_notice_set_before_the_page_opened_shows_too(ui, page):
    page.set_viewport_size({"width": 1280, "height": 900})
    ui.start(scenario="workspace", setup=CHAT % "" + ROUTES, query="?session=a")
    ui.js("import('/static/js/rewindNotice.js').then(m => { m.setRewindNotice('a', { title: 'early', backupId: 'b' }); return 1 })")
    page.evaluate("() => preload('App.js', 'default')")
    page.evaluate("() => { mount('App.js', () => ({}), 'default') }")
    ui.show_session("a")
    expect(row(page).get_by_role("status").filter(has_text="Rewound.")).to_be_visible()
    assert row(page).get_by_role("status").filter(has_text="Rewound.").evaluate("e => !e.closest('[hidden]')")


# M3: focus and the item under the pointer are never replaced.

def test_a_stop_while_focus_is_in_the_open_changes_list_keeps_the_list_and_the_focus(ui, page):
    open_chat(ui, page)
    changes(ui)
    row(page).get_by_role("button", name=re.compile("^3 files changed")).click()
    keep = row(page).get_by_role("button", name="Keep changes to note1.txt")
    keep.focus()
    keep.evaluate("e => { e.dataset.probe = 'held' }")
    before = ui.js("Math.round(document.querySelector('.status-row').getBoundingClientRect().height)")
    stop(ui)
    expect(live(page)).to_have_text("Stopped. Continue is available.")
    assert live(page).evaluate("e => e.getAttribute('aria-live') === 'polite' && !e.closest('[hidden]')")
    # Nothing unmounted: the list is open, the focus is on the same button.
    expect(row(page).get_by_role("region", name="Changed files").locator(".changes-file")).to_have_count(3)
    assert ui.js("document.activeElement?.dataset.probe") == "held"
    assert ui.js("Math.round(document.querySelector('.status-row').getBoundingClientRect().height)") >= before
    # Continue is there at once, as a chip's own button, and it works.
    expect(row(page).get_by_role("button", name="Continue", exact=True)).to_be_visible()
    # When focus leaves, Continue takes its place as the lead.
    page.get_by_role("textbox", name="Message").focus()
    expect(row(page).locator(".status-primary").get_by_role("button", name="Continue", exact=True)).to_be_visible()
    expect(chips(page).filter(has_text="Stopped")).to_have_count(0)


def test_a_new_item_waits_as_a_chip_while_focus_is_in_the_item_that_leads(ui, page):
    open_chat(ui, page)
    tasks(ui)
    toggle = row(page).get_by_role("button", name=re.compile(r"^Tasks 0\/3 done"))
    toggle.click()
    toggle.focus()
    toggle.evaluate("e => { e.dataset.probe = 'held' }")
    changes(ui)
    expect(chips(page)).to_have_text(["3 changes"])
    expect(row(page).locator(".status-primary")).to_have_attribute("data-item", "tasks")
    assert ui.js("document.activeElement?.dataset.probe") == "held"
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(live(page)).to_contain_text("3 files changed")
    # Its buttons are reachable from the chip meanwhile.
    expect(row(page).get_by_role("button", name="Keep all changes")).to_be_visible()
    page.get_by_role("textbox", name="Message").focus()
    expect(row(page).locator(".status-primary")).to_have_attribute("data-item", "changes")
    expect(chips(page)).to_have_text(["Tasks 0/3"])


def test_an_item_that_arrives_when_focus_is_elsewhere_leads_at_once(ui, page):
    open_chat(ui, page)
    tasks(ui)
    page.get_by_role("textbox", name="Message").focus()
    changes(ui)
    expect(row(page).locator(".status-primary")).to_have_attribute("data-item", "changes")
    stop(ui)
    expect(row(page).locator(".status-primary").get_by_role("button", name="Continue", exact=True)).to_be_visible()
    expect(live(page)).to_have_text("Stopped. Continue is available.")


# L1: a pick is forgotten when its item goes away.

def test_a_stale_pick_does_not_reopen_the_list_later(ui, page):
    open_chat(ui, page)
    changes(ui)
    tasks(ui)
    chips(page).filter(has_text="Tasks").click()
    expect(row(page).get_by_role("region", name="Tasks").get_by_role("listitem")).to_have_count(3)
    tasks(ui)  # Same list again: nothing changes.
    ui.emit("a", {"type": "tasks", "tasks": []})
    expect(row(page).get_by_role("region", name="Tasks")).to_have_count(0)
    page.get_by_role("textbox", name="Message").click()
    tasks(ui)
    toggle = row(page).locator(".task-checklist-toggle").or_(chips(page).filter(has_text="Tasks"))
    expect(toggle).to_be_visible()
    expect(row(page).get_by_role("listitem")).to_have_count(0)  # Closed: the old pick is gone.
    expect(row(page).locator(".status-primary")).to_have_attribute("data-item", "changes")


def test_a_stale_changes_pick_does_not_reopen_the_files_later(ui, page):
    # The reviewer's sequence: the changes chip was picked, everything went away, new changes arrive later.
    open_chat(ui, page)
    queue(ui, count=1)
    changes(ui)
    chips(page).filter(has_text="3 changes").click()
    expect(row(page).get_by_role("button", name=re.compile("^3 files changed"))).to_have_attribute("aria-expanded", "true")
    ui.run("fake.socketFor('a').emit({ type: 'changes', changes: makeChanges() })")
    queue(ui, count=0)
    expect(page.locator(".status-row")).not_to_be_visible()
    page.get_by_role("textbox", name="Message").click()
    changes(ui, files=6)
    toggle = row(page).get_by_role("button", name=re.compile("^6 files changed"))
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(row(page).locator(".changes-list")).to_have_count(0)
