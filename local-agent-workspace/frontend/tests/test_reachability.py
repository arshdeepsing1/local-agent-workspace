"""Reachability after the UI revamp: every feature with a visible control is still reachable, and no path needs more
clicks or keys than it did in the base app (the commit before the revamp, BASE_COMMIT).

The table below is built from PROGRESS.md: each row names the Done item it comes from, the short path a person takes
(clicks and keys) and the element that proves the feature was reached. Every row is driven by CLICK in Chromium, once
with the row's item alone and once with Continue, queued messages, changes, tasks and a rewind notice all present.
Rows with a keys path are also driven by KEYBOARD only (Tab, Shift+Tab, Enter, Space, Escape) from the top of the
page, and the control that ends up focused must show B's focus mark.

How clicks are counted: only through the Driver. Driver.click, Driver.press and Driver.select are the one place an
interaction happens, and each adds one to its counter; no test adds to it by hand. select_option picks a value in one
call, so it counts as ONE interaction (a person opens the list and then picks: two clicks; design/README.md counts
those two for task 9, this file counts one, in the base numbers too). Typing text is not counted.

The limits are BASE_CLICKS: the clicks and keys each path needed in the base app. They were measured, not written by
hand: set MEASURE_BASE=1 and test_the_base_numbers_are_what_the_base_app_needs builds the base frontend with
`git archive BASE_COMMIT frontend` in a temporary folder, drives every path below against it and compares the result
with the table (it prints the table when they differ). Run it again after a path changes."""
import io
import importlib.util
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from playwright.sync_api import expect

APP = Path(__file__).resolve().parents[2]
PROGRESS = APP / "PROGRESS.md"
BASE_COMMIT = "e59f328"
GROUPS = ("Chat and conversations", "Tools and permissions", "Context and memory", "Interface")
FOCUS = "rgb(79, 70, 229)"  # --focus: B's indigo (the same value tests/test_visual_tokens.py compares with)

CONTEXT_INFO = {"estimated_tokens": 6000, "input_budget": 120832, "context_window": 131072, "reply_reserve": 8192,
                "compactions": 1, "summarized_messages": 6, "estimate_method": "weighted_utf8", "instruction_files": [], "warnings": [],
                "breakdown": {"system_instructions": 1000, "tool_definitions": 2000, "messages_and_results": 2500, "summary": 400,
                              "request_overhead": 100}}
REPLY = "Done. See `src/app.py`.\n\n```python\nprint('hello')\n```"
EVENTS = [{"id": "u1", "type": "user", "text": "First task", "in_wire": True},
          {"id": "a1", "type": "assistant", "text": REPLY},
          {"id": "u2", "type": "user", "text": "Second task", "in_wire": True},
          {"id": "a2", "type": "assistant", "text": "Second answer."}]
PREVIEW = {"before_event_id": "u2", "conversation": {"possible": True, "reason": None, "events_removed": 2, "messages_removed": 2, "summary_dropped": False},
           "code": {"possible": True, "reason": None, "files": [{"path": "a.txt", "status": "restore"}]}}
QUESTIONS = [{"question": "Which database?", "header": "Storage", "options": [{"label": "SQLite"}, {"label": "Postgres"}]}]
RULE = {"rememberable": ["chat", "project", "user"], "editable": ["command"],
        "rule_options": {"argv": ["git", "status"], "prefixes": [{"prefix": ["git", "status"], "flags": []}], "reason": None}}
COMMAND = {"id": "cmd", "type": "tool", "name": "run_command", "state": "pending", "preview": "git status", "input": {"command": "git status"}, **RULE}
TASKS = [{"id": f"t{n}", "title": f"Task number {n}", "status": "pending"} for n in range(3)]


def chat(id, title, events=None, **fields):
    return f"['{id}', {{ ...makeSession('{id}', '{title}'), events: {json.dumps(events or [])}, ...{json.dumps(fields)} }}]"


SETUP = "fake.sessions = new Map([" + ", ".join([
    chat("a", "Work", EVENTS, tags=["bug"], context_info=CONTEXT_INFO),
    chat("b", "Notes"),
    chat("p", "Parent task"),
    chat("c", "Child task", parent_session_id="p", is_subagent=True),
    chat("w", "Waiting", [EVENTS[0], COMMAND], status="awaiting_approval"),
    chat("q", "Question", [EVENTS[0], {"id": "ask", "type": "tool", "name": "ask_user", "state": "pending", "output": "", "input": {"questions": QUESTIONS}}],
         status="awaiting_approval"),
    chat("l", "Plan", [EVENTS[0], {"id": "plan", "type": "tool", "name": "submit_plan", "state": "pending", "output": "", "input": {"plan": "Do it."}}],
         status="awaiting_approval", permission_mode="plan"),
    chat("r", "Running", [EVENTS[0]], status="running"),
    chat("s", "Stopped", [EVENTS[0], EVENTS[1]], terminal_reason="step_limit"),
]) + """])
fake.override = path => path.startsWith('/api/sessions/a/rewind-preview?') ? fake.json(%s)
  : path.includes('mention-files?') ? fake.json([{ path: 'src/app.py', name: 'app.py' }])
  : path.startsWith('/api/jobs') ? fake.json([]) : undefined
""" % json.dumps(PREVIEW)


# Driving the page: every click, key press and choice is counted, here and nowhere else (typing text is not).

class Driver:
    def __init__(self, ui, page, session="a"):
        self.ui, self.page, self.session, self.clicks, self.keys = ui, page, session, 0, 0

    def click(self, locator):
        locator.click(timeout=3000)
        self.clicks += 1

    def select(self, locator, **choice):
        """One interaction, however many a person needs to open the list and pick (see the module docstring)."""
        locator.select_option(**choice)
        self.clicks += 1

    def press(self, key):
        self.page.keyboard.press(key)
        self.keys += 1

    def reach(self, target, *openers):
        """Click a control the way a person gets to it: when it is not on screen, click the first opener that is (a
        toggle, or a chip in the new status row), then the control itself. Every click is counted."""
        for opener in openers:
            if target.count() and target.first.is_visible():
                break
            if opener.count() and opener.first.is_visible():
                self.click(opener.first)
        self.click(target.first)

    def tab_to(self, locator, limit=160, back=False):
        """Press Tab (or Shift+Tab) until the locator has focus, from wherever focus is."""
        for _ in range(limit):
            if locator.evaluate("e => e === document.activeElement"):
                return
            self.press("Shift+Tab" if back else "Tab")
        raise AssertionError(f"Tab never reached {locator}")

    def from_top(self):
        """The keyboard starts at the top of the page, as it does after a reload."""
        self.page.evaluate("() => { document.activeElement?.blur(); window.scrollTo(0, 0) }")


ROUTES = """
const previous = fake.override
fake.override = (path, options) => {
  if (/\\/queue\\/send$/.test(path)) return fake.json({ ok: true })
  if (/\\/queue\\//.test(path) && options.method === 'DELETE') return fake.json({ queued_messages: [] })
  if (/\\/side-question$/.test(path)) return fake.json({ answer: 'It changed **two** files.', request_info: { status: 'completed', finish_reason: 'stop' } })
  return previous ? previous(path, options) : undefined
}
"""


def start(ui, page, session="a", width=1280, height=900, setup=""):
    page.set_viewport_size({"width": width, "height": height})
    ui.app(session=session, scenario="workspace", setup=SETUP + ROUTES + setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    if session:
        ui.show_session(session)
    return Driver(ui, page, session)


def push_changes(ui, files=1, session="a"):
    names = ["note.txt"] + [f"note{n}.txt" for n in range(1, files)]
    ui.run("fake.socketFor('%s').emit({ type: 'changes', changes: makeChanges(%s) })" % (session, ", ".join(f"changedFile('{name}', 2, 1)" for name in names)))


def open_changes(page):
    """What opens the changed-files list: its toggle, or (in the status row, when something else leads) its chip."""
    return page.locator(".changes-toggle").or_(page.locator(".status-chip", has_text="change"))


def open_tasks(page):
    """What opens the task list: its toggle, or its chip."""
    return page.locator(".task-checklist-toggle").or_(page.locator(".status-chip", has_text="Tasks"))


def has_focus_mark(locator):
    """B's ring: a 2 px solid outline in --focus, or the message box's lifted card with its --focus edge and ring."""
    return locator.evaluate("""(e, focus) => {
      const outline = getComputedStyle(e)
      if (outline.outlineStyle === 'solid' && parseFloat(outline.outlineWidth) >= 2 && outline.outlineColor === focus) return true
      const legend = e.matches('fieldset') ? e.querySelector(':scope > legend') : null  // a Settings section: its legend shows the ring
      if (legend && getComputedStyle(legend).outlineStyle === 'solid' && getComputedStyle(legend).outlineColor === focus) return true
      const card = e.closest('.composer')
      return !!card && getComputedStyle(card).boxShadow.startsWith(focus) && getComputedStyle(card).borderTopColor === focus
    }""", FOCUS)


def role(page, kind, name, **kw):
    return page.get_by_role(kind, name=name, **kw)


# The table. go(d) walks the click path and returns the proof element; keys(d) does the same by keyboard (or is None).
# Each row: (id, PROGRESS.md Done item, path text, session, go, keys, setup).

def sidebar_row(page, title):
    return page.locator(".conversation-row > button:first-child", has_text=title)


def tools_tab(d, name):
    d.click(role(d.page, "button", "Agent tools", exact=True))
    d.click(d.page.get_by_role("navigation", name="Agent tools sections").get_by_role("button", name=name, exact=True))
    return d.page.get_by_role("navigation", name="Agent tools sections").get_by_role("button", name=name, exact=True)


def panel_tab(d, name):
    d.click(role(d.page, "button", re.compile(r"^Workspace( \(|$)")))
    d.click(d.page.get_by_role("navigation", name="Workspace views").get_by_role("button", name=name))
    return d.page.get_by_role("navigation", name="Workspace views").get_by_role("button", name=name)


def key_open(d, opener, then=None):
    """Tab to a control and press Enter; then the optional Tab path to a control inside what opened."""
    d.from_top()
    d.tab_to(opener)
    d.press("Enter")


def go_new(d):
    d.click(role(d.page, "button", "New conversation"))
    return d.page.get_by_role("heading", name="What’s up next?")


def keys_new(d):
    key_open(d, role(d.page, "button", "New conversation"))
    return d.page.get_by_role("heading", name="What’s up next?")


def go_switch(d):
    d.click(sidebar_row(d.page, "Notes"))
    d.ui.show_session("b")
    return d.page.locator(".conversation-title", has_text="Notes")


def keys_switch(d):
    d.from_top()
    row = sidebar_row(d.page, "Notes")
    d.tab_to(row)
    d.press("Enter")
    d.ui.show_session("b")
    return d.page.locator(".conversation-title", has_text="Notes")


def go_running(d):
    d.click(role(d.page, "button", "Running", exact=True))
    return role(d.page, "button", "Running", exact=True).and_(d.page.locator("[aria-pressed=true]"))


def go_row_menu(d):
    button = role(d.page, "button", "More actions for Work")
    button.hover()
    d.click(button)
    return role(d.page, "menuitem", "Rename", exact=True)


def keys_row_menu(d):
    d.from_top()
    button = role(d.page, "button", "More actions for Work")
    d.tab_to(button)
    d.press("Enter")
    return role(d.page, "menuitem", "Rename", exact=True)


def go_delete_children(d):
    button = role(d.page, "button", "More actions for Parent task")
    button.hover()
    d.click(button)
    d.click(role(d.page, "menuitem", "Delete…", exact=True))
    return d.page.get_by_role("dialog", name="Delete conversation").get_by_role("button", name="Delete with subagents")


def go_copy_code(d):
    return d.page.get_by_role("button", name="Copy code").first


def keys_copy_code(d):
    d.from_top()
    d.tab_to(d.page.get_by_role("button", name="Copy code").first)
    return d.page.get_by_role("button", name="Copy code").first


def go_attach(d):
    return role(d.page, "button", "Attach files")


def keys_attach(d):
    d.from_top()
    d.tab_to(role(d.page, "button", "Attach files"))
    return role(d.page, "button", "Attach files")


def go_continue(d):
    return role(d.page, "button", "Continue", exact=True)


def keys_continue(d):
    d.from_top()
    d.tab_to(role(d.page, "button", "Continue", exact=True))
    return role(d.page, "button", "Continue", exact=True)


def go_stop(d):
    return role(d.page, "button", "Stop response")


def go_model(d):
    return role(d.page, "combobox", "Model")


def keys_model(d):
    d.from_top()
    d.tab_to(role(d.page, "combobox", "Model"))
    return role(d.page, "combobox", "Model")


def go_effort(d):
    return role(d.page, "combobox", "Reasoning effort")


def go_notify(d):
    return d.page.get_by_role("switch").first


def go_fork(d):
    d.click(role(d.page, "button", "Fork from here").first)
    return d.page.get_by_role("dialog", name="Fork conversation").get_by_role("button", name="Fork", exact=True)


def go_rewind(d):
    d.click(role(d.page, "button", "Rewind to here").nth(1))
    return d.page.get_by_role("dialog", name="Rewind conversation").get_by_role("button", name="Rewind", exact=True)


def go_edit_message(d):
    d.click(role(d.page, "button", "Edit message").first)
    return d.page.get_by_role("textbox", name="Edit message")


def go_export(d):
    tools_tab(d, "Export")
    return d.page.get_by_role("button", name="Export Markdown")


def go_import(d):
    return d.page.get_by_role("button", name="Export conversation")


def go_question(d):
    return d.page.get_by_role("radio", name="SQLite")


def go_approve(d):
    return role(d.page, "button", "Approve", exact=True)


def keys_approve(d):
    d.from_top()
    d.tab_to(role(d.page, "button", "Approve", exact=True))
    return role(d.page, "button", "Approve", exact=True)


def go_decline(d):
    return role(d.page, "button", "Decline", exact=True)


def go_edit_command(d):
    return d.page.get_by_role("button", name="Edit", exact=True)


def go_allow_chat(d):
    return role(d.page, "button", "Always allow this command in this chat", exact=True)


def go_remember(d):
    d.click(role(d.page, "button", "Always allow…"))
    return d.page.get_by_role("group", name="Remember this approval")


def go_plan(d):
    return d.page.get_by_role("button", name=re.compile("^Approve and use"))


def go_mode(d):
    d.click(d.page.locator(".permission-trigger"))
    return d.page.get_by_role("menuitemradio", name=re.compile("^Auto"))


def keys_mode(d):
    d.from_top()
    d.tab_to(d.page.locator(".permission-trigger"))
    d.press("Enter")
    return d.page.get_by_role("menu", name="Permission mode")


def go_rules(d):
    d.click(d.page.locator(".permission-trigger"))
    d.click(d.page.get_by_role("menuitem", name="Add a rule…"))
    return d.page.get_by_role("dialog", name="Add a permission rule").get_by_role("textbox", name="Command")


def go_folders(d):
    d.click(role(d.page, "button", "Folder access", exact=True))
    return d.page.get_by_role("dialog", name="Folder access").get_by_role("textbox", name="Folder path")


def keys_folders(d):
    key_open(d, role(d.page, "button", "Folder access", exact=True))
    return d.page.get_by_role("dialog", name="Folder access").get_by_role("textbox", name="Folder path")


def go_changes_bar(d):
    push_changes(d.ui)
    return role(d.page, "button", "Keep all changes")


def keys_changes_bar(d):
    push_changes(d.ui)
    d.from_top()
    d.tab_to(role(d.page, "button", "Keep all changes"))
    return role(d.page, "button", "Keep all changes")


def go_diff(d):
    push_changes(d.ui)
    d.reach(d.page.get_by_title("Review note.txt"), open_changes(d.page))
    return d.page.get_by_role("region", name="Agent changes in note.txt")


def keys_diff(d):
    push_changes(d.ui)
    d.from_top()
    toggle = d.page.get_by_role("button", name=re.compile("^1 file changed"))
    d.tab_to(toggle)
    d.press("Enter")
    d.tab_to(d.page.get_by_title("Review note.txt"))
    d.press("Enter")
    return d.page.get_by_role("region", name="Agent changes in note.txt")


def go_tasks(d):
    d.ui.emit(d.session, {"type": "tasks", "tasks": TASKS})
    item = d.page.get_by_role("region", name="Tasks").get_by_role("listitem")
    d.reach(item, open_tasks(d.page))
    return item.first


def go_queue(d):
    d.ui.run("fake.socketFor('a').emit({ type: 'queue', queued_messages: [{ id: 'q1', text: 'Then run the tests', created: 1 }] })")
    return d.page.get_by_role("group", name="Queued messages").get_by_role("button", name=re.compile("^Remove queued message"))


def go_files(d):
    d.click(role(d.page, "button", re.compile(r"^Workspace( \(|$)")))
    return d.page.get_by_role("navigation", name="Workspace views").get_by_role("button", name="Files")


def keys_files(d):
    key_open(d, role(d.page, "button", re.compile(r"^Workspace( \(|$)")))
    return role(d.page, "button", "Close workspace")


def go_changed_dots(d):
    push_changes(d.ui)
    d.click(role(d.page, "button", re.compile(r"^Workspace( \(|$)")))
    return d.page.get_by_role("button", name=re.compile("^note.txt"))


def go_git_view(d):
    panel_tab(d, "Changes")
    return role(d.page, "button", "Refresh changes")


def go_terminal(d):
    panel_tab(d, "Terminal")
    return d.page.get_by_role("textbox", name="Shell command")


def keys_terminal(d):
    key_open(d, role(d.page, "button", re.compile(r"^Workspace( \(|$)")))
    d.tab_to(d.page.get_by_role("navigation", name="Workspace views").get_by_role("button", name="Terminal"))
    d.press("Enter")
    return d.page.get_by_role("textbox", name="Shell command")


def go_progress(d):
    panel_tab(d, "Progress")
    return d.page.locator(".progress-panel")


def go_tool_tab(name, proof):
    def go(d):
        tools_tab(d, name)
        return proof(d.page)
    return go


def keys_tool_tab(name, proof):
    def keys(d):
        key_open(d, role(d.page, "button", "Agent tools", exact=True))
        button = d.page.get_by_role("navigation", name="Agent tools sections").get_by_role("button", name=name, exact=True)
        d.tab_to(button)
        d.press("Enter")
        return proof(d.page)
    return keys


def go_context(d):
    d.click(d.page.locator(".context-meter > summary"))
    return role(d.page, "button", "Compact now")


def keys_context(d):
    d.from_top()
    d.tab_to(d.page.locator(".context-meter > summary"))
    d.press("Enter")
    return role(d.page, "button", "Compact now")


def go_meter(d):
    return d.page.locator(".context-meter > summary")


def go_slash(d):
    d.click(role(d.page, "textbox", "Message"))
    d.page.keyboard.type("/")
    return d.page.get_by_role("listbox").get_by_role("option").first


def go_skill(d):
    d.click(role(d.page, "textbox", "Message"))
    d.page.keyboard.type("/rev")
    return d.page.get_by_role("option", name=re.compile("Code review"))


def go_mention(d):
    d.click(role(d.page, "textbox", "Message"))
    d.page.keyboard.type("@app")
    return d.page.get_by_role("option", name=re.compile("app.py"))


def go_settings_save(d):
    d.click(role(d.page, "button", "Settings", exact=True))
    return d.page.get_by_role("dialog", name="Settings").get_by_role("button", name="Save settings")


def keys_settings(d):
    key_open(d, role(d.page, "button", "Settings", exact=True))
    return d.page.get_by_role("dialog", name="Settings").get_by_role("button", name="Save settings")


def go_settings_section(name, label):
    def go(d):
        d.click(role(d.page, "button", "Settings", exact=True))
        d.click(d.page.get_by_role("navigation", name="Settings sections").get_by_role("button", name=name, exact=True))
        return d.page.get_by_role("dialog", name="Settings").get_by_role("combobox" if "size" in label or "style" in label else "spinbutton", name=re.compile(f"^{re.escape(label)}"))
    return go


def keys_settings_section(name, label):
    def keys(d):
        key_open(d, role(d.page, "button", "Settings", exact=True))
        d.tab_to(d.page.get_by_role("navigation", name="Settings sections").get_by_role("button", name=name, exact=True))
        d.press("Enter")
        return d.page.get_by_role("dialog", name="Settings").get_by_role("combobox", name=re.compile(f"^{re.escape(label)}"))
    return keys


def go_help(d):
    d.click(role(d.page, "textbox", "Message"))
    d.page.get_by_role("textbox", name="Message").fill("/help")
    expect(d.page.get_by_text("Loading skills…")).to_have_count(0)
    d.press("Enter")
    return d.page.get_by_role("region", name="Commands")


def region(name):
    return lambda page: page.get_by_role("region", name=name)


FEATURES = [
    # Chat and conversations
    ("new-chat", "Multiple persistent conversations", "click New conversation", "a", go_new, keys_new, ""),
    ("switch-chat", "Multiple persistent conversations", "click the chat's row", "a", go_switch, keys_switch, ""),
    ("running-filter", "Running filter in the sidebar", "click Running", "a", go_running, None, ""),
    ("row-menu", "Conversation status in the sidebar (needs approval, working, error, unread)", "hover the row, click More actions", "a", go_row_menu, keys_row_menu, ""),
    ("delete-subagents", "Forks of completed conversations with fresh permissions", "More actions, Delete… on a chat with subagents", "a", go_delete_children, None, ""),
    ("copy-code", "Highlighted code blocks with a Copy code button", "none: the button is on the code block", "a", go_copy_code, keys_copy_code, ""),
    ("attach-files", "Paste, drop or attach PDF, Word, Excel and PowerPoint files", "none: Attach files is below the message box", "a", go_attach, keys_attach, ""),
    ("continue-limit", "Continue button after a turn stopped at a step, output or spending limit", "none: the status row shows Continue", "s", go_continue, keys_continue, ""),
    ("stop-reply", "Stop an agent response, including its foreground command or pending approval", "none: Stop response replaces Send", "r", go_stop, None, ""),
    ("model-select", "Databricks model selection", "none: the Model list is below the message box", "a", go_model, keys_model, ""),
    ("effort", "Reasoning effort per conversation from Default to Max", "none: the Reasoning effort list is below the message box", "a", go_effort, None, ""),
    ("notifications", "Opt-in browser notifications", "none: the sidebar switch", "a", go_notify, None,
     "window.Notification = class { static permission = 'default'; static async requestPermission() { return 'granted' } }"),
    ("fork", "Forks of completed conversations with fresh permissions", "click Fork from here", "a", go_fork, None, ""),
    ("rewind", "Resume conversations after refreshing or restarting the app", "click Rewind to here", "a", go_rewind, None, ""),
    ("edit-message", "Full conversation history kept on disk", "click Edit message", "a", go_edit_message, None, ""),
    ("export-md", "Markdown transcript download or copy from the app", "Agent tools, Export", "a", go_export, keys_tool_tab("Export", lambda page: page.get_by_role("button", name="Export Markdown")), ""),
    ("import-json", "Safe JSON conversation export and import", "Agent tools, Export", "a", lambda d: (tools_tab(d, "Export"), go_import(d))[1], None, ""),
    ("question", "Questions from the agent with options to choose from", "none: the card is in the chat", "q", go_question, None, ""),
    # Tools and permissions
    ("approve", "Approval cards with proposed edits and command text", "none: Approve is on the card", "w", go_approve, keys_approve, ""),
    ("edit-before-approve", "Editing a proposed edit or command before approving", "none: Edit is on the card", "w", go_edit_command, None, ""),
    ("decline", "Declined actions do not run", "none: Decline is on the card", "w", go_decline, None, ""),
    ("allow-chat", "Allow an action for the rest of a chat, the project or all projects", "none: the card's chat button", "w", go_allow_chat, None, ""),
    ("allow-project", "Allow an action for the rest of a chat, the project or all projects", "click Always allow…", "w", go_remember, None, ""),
    ("rules-dialog", "Allow, Always ask and deny rules for a chat, a project or all projects", "permission mode button, Add a rule…", "a", go_rules, None, ""),
    ("permission-mode", "Permission mode switch below the composer", "click the mode button", "a", go_mode, keys_mode, ""),
    ("folder-access", "Additional local folders granted through an approval card", "click Folder access", "a", go_folders, keys_folders, ""),
    ("plan-review", "Plan review in Plan mode that approves the plan and switches the mode", "none: the plan card is in the chat", "l", go_plan, None, ""),
    ("changes-bar", "Files changed bar with Keep and Undo", "none: the status row shows the changes", "a", go_changes_bar, keys_changes_bar, ""),
    ("diff-review", "Inline red and green diff with Keep and Undo for each change", "the changes toggle, the file", "a", go_diff, keys_diff, ""),
    ("explorer-dots", "Dots on changed files in the file explorer", "open Workspace", "a", go_changed_dots, None, ""),
    ("file-explorer", "Workspace file explorer", "click Workspace", "a", go_files, keys_files, ""),
    ("git-view", "Git status and diff view", "Workspace, Changes", "a", go_git_view, None, ""),
    ("terminal", "Non-interactive command runner", "Workspace, Terminal", "a", go_terminal, keys_terminal, ""),
    ("checkpoints", "File checkpoints", "Agent tools, Checkpoints", "a", go_tool_tab("Checkpoints", region("Checkpoints")), None, ""),
    ("worktrees", "Managed Git worktrees", "Agent tools, Worktrees", "a", go_tool_tab("Worktrees", region("Worktrees")), None, ""),
    # Context and memory
    ("context-meter", "Visible context meter", "none: the line below the message box", "a", go_meter, None, ""),
    ("compact-now", "Compact command for a manual summary", "click the context line", "a", go_context, keys_context, ""),
    ("progress-tab", "Progress tab with context, tasks and running work", "Workspace, Progress", "a", go_progress, None, ""),
    ("memory", "Memory review in Agent tools", "Agent tools, Memory", "a", go_tool_tab("Memory", lambda page: page.get_by_role("button", name="Memory", exact=True)), None, ""),
    ("output-style", "Output styles Default, Concise and Explanatory", "Settings, Agent", "a", go_settings_section("Agent", "Output style"), None, ""),
    ("context-budget", "Configurable context budgeting", "Settings, Model and context", "a", go_settings_section("Model and context", "Context budget (tokens)"), None, ""),
    ("tasks", "Progress tab with context, tasks and running work", "the Tasks chip or bar", "a", go_tasks, None, ""),
    # Interface
    ("slash-commands", "Built-in slash commands such as /clear, /model, /mode, /rename and /copy", "type /", "a", go_slash, None, ""),
    ("slash-help", "One slash menu for commands and skills", "type /help, Enter", "a", go_help, None, ""),
    ("skills", "Skills sent with one message", "type /rev", "a", go_skill, None, ""),
    ("mention", "Workspace file explorer", "type @", "a", go_mention, None, ""),
    ("settings", "Settings page with defaults", "click Settings", "a", go_settings_save, keys_settings, ""),
    ("text-size", "Spacing, radius and type-size tokens", "Settings, Appearance", "a", go_settings_section("Appearance", "Text size"), keys_settings_section("Appearance", "Text size"), ""),
    ("agent-tools", "Agent tools panel", "click Agent tools", "a", go_tool_tab("Overview", region("Overview")), keys_tool_tab("Overview", region("Overview")), ""),
    ("tools-tools", "Agent tools panel", "Agent tools, Tools", "a", go_tool_tab("Tools", region("Tools")), None, ""),
    ("tools-extensions", "Agent tools panel", "Agent tools, Extensions", "a", go_tool_tab("Extensions", region("Extensions")), None, ""),
    ("tools-verify", "Agent tools panel", "Agent tools, Verify", "a", go_tool_tab("Verify", region("Verify")), None, ""),
    ("tools-schedules", "Agent tools panel", "Agent tools, Schedules", "a", go_tool_tab("Schedules", lambda page: page.get_by_role("button", name="Schedules", exact=True)), None, ""),
    ("tools-usage", "Agent tools panel", "Agent tools, Usage", "a", go_tool_tab("Usage", lambda page: page.get_by_role("button", name="Usage", exact=True)), None, ""),
]


def test_the_table_comes_from_progress_md_and_is_big_enough():
    text = PROGRESS.read_text()
    done = set()
    for block in re.split(r"^## ", text, flags=re.M)[1:]:
        if not any(block.splitlines()[0].endswith(group) for group in GROUPS):
            continue
        section = block.split("### Done", 1)[1].split("### Pending", 1)[0]
        done |= {line[6:].strip() for line in section.splitlines() if line.startswith("- [x] ")}
    missing = sorted({row[1] for row in FEATURES} - done)
    assert not missing, f"Not Done items of PROGRESS.md: {missing}"
    assert len(FEATURES) >= 30
    assert len({row[0] for row in FEATURES}) == len(FEATURES)
    assert sum(1 for row in FEATURES if row[5]) >= 10


# The paths that are measured. Each one is a Run: a chat, how to crowd it, and the steps a person takes. Run.key names
# the row of BASE_CLICKS that is its limit.

class Run:
    def __init__(self, key, session, step, prepare=None, setup=""):
        self.key, self.session, self.step, self.prepare, self.setup = key, session, step, prepare, setup

    def execute(self, ui, page):
        """Open the chat, put the state in it, walk the path. Returns the driver and the proof element (or None)."""
        d = start(ui, page, self.session, setup=self.setup)
        if self.prepare:
            self.prepare(d)
        return d, self.step(d)


def put_queue(d, count=2):
    items = [{"id": f"q{n}", "text": f"Queued message number {n}", "created": n, "paused": True} for n in range(count)]
    d.ui.emit(d.session, {"type": "queue", "queued_messages": items})


def put_tasks(d):
    d.ui.emit(d.session, {"type": "tasks", "tasks": TASKS})


def put_rewind(d):
    d.ui.js("import('/static/js/rewindNotice.js').then(m => { m.setRewindNotice('%s', { title: 'Before rewind', backupId: 'b', draftKept: true }); return 1 })" % d.session)


def crowd(d, changes=0):
    """Queued messages, tasks and a rewind notice, and changed files when asked, on top of what the chat has."""
    put_queue(d)
    put_tasks(d)
    put_rewind(d)
    if changes:
        push_changes(d.ui, changes, d.session)
    d.page.wait_for_timeout(150)


FEATURE_RUNS = []
for row in FEATURES:
    FEATURE_RUNS.append(Run(f"feature {row[0]}", row[3], row[4], setup=row[6]))
    if row[3] in ("a", "s"):  # A chat that is not waiting for you or running can have every item at once.
        FEATURE_RUNS.append(Run(f"feature {row[0]} crowded", row[3], row[4], prepare=crowd, setup=row[6]))


def within(d, key):
    clicks, keys = BASE_CLICKS[key]
    assert d.clicks <= clicks and d.keys <= keys, f"{key}: {d.clicks} clicks and {d.keys} keys, the base needed {clicks} and {keys}"


@pytest.mark.parametrize("run", FEATURE_RUNS, ids=[run.key for run in FEATURE_RUNS])
def test_a_feature_is_reached_by_click_in_no_more_clicks_than_in_the_base(ui, page, run):
    d, proof = run.execute(ui, page)
    expect(proof.first).to_be_visible()
    if proof.first.evaluate("e => e.matches('button, input, select, textarea')"):
        expect(proof.first).to_be_enabled()
    within(d, run.key)


KEYBOARD = [row for row in FEATURES if row[5]]


@pytest.mark.parametrize("feature", KEYBOARD, ids=[row[0] for row in KEYBOARD])
def test_a_feature_is_reached_by_keyboard_alone_and_shows_focus(ui, page, feature):
    _, _, _, session, _, keys, setup = feature
    d = start(ui, page, session, setup=setup)
    proof = keys(d)
    expect(proof.first).to_be_visible()
    assert d.clicks == 0
    focused = page.evaluate_handle("document.activeElement")
    assert page.evaluate("e => e !== document.body", focused)
    assert has_focus_mark(page.locator("*:focus").last), feature[0]


def test_a_dialog_reached_by_keyboard_closes_with_escape_and_gives_focus_back(ui, page):
    d = start(ui, page)
    key_open(d, role(page, "button", "Settings", exact=True))
    expect(page.get_by_role("dialog", name="Settings")).to_be_visible()
    d.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(role(page, "button", "Settings", exact=True)).to_be_focused()
    assert has_focus_mark(role(page, "button", "Settings", exact=True))


# The ten baseline tasks of design/README.md ("Task click counts").

def task_1(d):
    box = role(d.page, "textbox", "Message")
    expect(box).to_be_focused()  # The message box has focus when a chat opens.
    box.fill("Run the tests")
    d.press("Enter")
    d.ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")


def task_2(d):
    d.click(role(d.page, "button", "New conversation"))
    expect(d.page.get_by_role("heading", name="What’s up next?")).to_be_visible()
    expect(role(d.page, "textbox", "Message")).to_be_focused()


def task_3(d):
    d.click(sidebar_row(d.page, "Notes"))
    d.ui.show_session("b")
    expect(d.page.locator(".conversation-title", has_text="Notes")).to_be_visible()


def task_4(d):
    d.click(role(d.page, "button", "Approve", exact=True))
    d.ui.wait("fake.requests('/api/sessions/w/approvals/cmd', 'POST').length === 1")


def task_5(d):
    d.click(role(d.page, "button", "Always allow this command in this chat", exact=True))
    d.ui.wait("fake.requests('/api/sessions/w/approvals/cmd', 'POST').length === 1")
    assert d.ui.bodies("/api/sessions/w/approvals/cmd", "POST") == [{"allowed": True, "remember": True}]


def task_6(d):
    role(d.page, "textbox", "Message").focus()
    d.press("Escape")
    d.ui.wait("fake.requests('/api/sessions/r/stop', 'POST').length === 1")


def task_7(d):
    push_changes(d.ui)
    d.reach(d.page.get_by_title("Review note.txt"), open_changes(d.page))
    expect(d.page.get_by_role("region", name="Agent changes in note.txt")).to_be_visible()


def task_8(d):
    d.click(d.page.locator(".permission-trigger"))
    d.click(d.page.get_by_role("menuitemradio", name=re.compile("^Auto")))
    d.ui.wait("fake.requests('/api/sessions/a/permissions', 'PUT').length === 1")


def task_9(d):
    d.click(role(d.page, "button", "Settings", exact=True))
    d.click(d.page.get_by_role("navigation", name="Settings sections").get_by_role("button", name="Appearance", exact=True))
    d.select(d.page.get_by_role("dialog", name="Settings").get_by_role("combobox", name=re.compile("^Text size")), index=2)
    assert d.page.evaluate("document.documentElement.style.getPropertyValue('--text-scale')") not in ("", "1")
    assert not d.ui.requests("/api/settings", "PUT")  # It applies at once, without Save settings.


def task_10(d):
    d.click(d.page.locator(".context-meter > summary"))
    d.click(role(d.page, "button", "Compact now"))
    d.ui.wait("fake.requests('/api/sessions/a/compact', 'POST').length === 1")


def queue_and_tasks(d):
    put_queue(d, 1)
    put_tasks(d)


TASK_RUNS = [Run(f"task {n}", {4: "w", 5: "w", 6: "r"}.get(n, "a"), globals()[f"task_{n}"]) for n in range(1, 11)]
TASK_RUNS.append(Run("task 7 with more urgent items", "a", task_7, prepare=queue_and_tasks))


@pytest.mark.parametrize("run", TASK_RUNS, ids=[run.key for run in TASK_RUNS])
def test_a_baseline_task_needs_no_more_clicks_or_keys_than_in_the_base(ui, page, run):
    d, _ = run.execute(ui, page)
    within(d, run.key)


# Every control of the status row, once with only what it needs and once with everything present (Continue, two
# queued messages, three changed files, tasks and a rewind notice). A control may not need more clicks than it did in
# the base app, where every bar showed at once. A new control here needs a row in BASE_CLICKS: measure it (module docstring).

def named(name, **kw):
    return lambda page: page.get_by_role("button", name=name, **kw)


CONTROLS = {  # id: (what it needs, the chat, what is clicked, what opens it first)
    "continue": ({"stop"}, "s", named("Continue", exact=True), None),
    "send queued": ({"queue"}, "a", named("Send queued messages"), None),
    "edit queued": ({"queue"}, "a", lambda page: page.get_by_role("button", name=re.compile("^Edit queued message")), None),
    "remove queued": ({"queue"}, "a", lambda page: page.get_by_role("button", name=re.compile("^Remove queued message")), None),
    "keep all": ({"changes"}, "a", named("Keep all changes"), None),
    "undo all": ({"changes"}, "a", named("Undo all changes"), None),
    "keep a file": ({"changes"}, "a", named("Keep changes to note.txt"), open_changes),
    "undo a file": ({"changes"}, "a", named("Undo changes to note.txt"), open_changes),
    "open a diff": ({"changes"}, "a", lambda page: page.get_by_title("Review note.txt"), open_changes),
    "tasks toggle": ({"tasks"}, "a", open_tasks, None),
    "open earlier version": ({"rewind"}, "a", named("Open earlier version"), None),
    "dismiss rewind notice": ({"rewind"}, "a", named("Dismiss rewind notice"), None),
    "copy side answer": ({"side"}, "a", named("Copy answer"), None),
    "dismiss side answer": ({"side"}, "a", named("Dismiss", exact=True), None),
    "close commands": ({"help"}, "a", named("Close commands"), None),
}


def control_state(needs, crowded):
    def prepare(d):
        want = {"stop", "queue", "changes", "tasks", "rewind"} if crowded else needs
        if "queue" in want:
            put_queue(d)
        if "changes" in want:
            push_changes(d.ui, 3, d.session)
        if "tasks" in want:
            put_tasks(d)
        if "rewind" in want:
            put_rewind(d)
        d.page.wait_for_timeout(150)
        box = d.page.get_by_role("textbox", name="Message")
        if "side" in needs:
            box.fill("/btw how far along?")
            box.press("Enter")
            expect(d.page.get_by_role("button", name="Copy answer")).to_be_visible()
        if "help" in needs:
            box.fill("/help")
            expect(d.page.get_by_text("Loading skills…")).to_have_count(0)
            box.press("Enter")
            expect(d.page.get_by_role("region", name="Commands")).to_be_visible()
    return prepare


def control_step(find, opener):
    def step(d):
        target = find(d.page)
        d.reach(target, *([opener(d.page)] if opener else []))
    return step


CONTROL_RUNS = []
for name, (needs, chat, find, opener) in CONTROLS.items():
    for crowded in (False, True):
        # With everything present the stopped chat is the one Continue needs; the others keep chat "a" plus Continue.
        CONTROL_RUNS.append(Run(f"control {name}{' crowded' if crowded else ''}", "s" if crowded or "stop" in needs else chat,
                                control_step(find, opener), prepare=control_state(needs, crowded), setup=""))


@pytest.mark.parametrize("run", CONTROL_RUNS, ids=[run.key for run in CONTROL_RUNS])
def test_a_status_row_control_needs_no_more_clicks_than_in_the_base(ui, page, run):
    d, _ = run.execute(ui, page)
    within(d, run.key)


# The numbers the base app needs, measured by test_the_base_numbers_are_what_the_base_app_needs (clicks, keys).
BASE_CLICKS = {
    'feature new-chat': (1, 0),
    'feature new-chat crowded': (1, 0),
    'feature switch-chat': (1, 0),
    'feature switch-chat crowded': (1, 0),
    'feature running-filter': (1, 0),
    'feature running-filter crowded': (1, 0),
    'feature row-menu': (1, 0),
    'feature row-menu crowded': (1, 0),
    'feature delete-subagents': (2, 0),
    'feature delete-subagents crowded': (2, 0),
    'feature copy-code': (0, 0),
    'feature copy-code crowded': (0, 0),
    'feature attach-files': (0, 0),
    'feature attach-files crowded': (0, 0),
    'feature continue-limit': (0, 0),
    'feature continue-limit crowded': (0, 0),
    'feature stop-reply': (0, 0),
    'feature model-select': (0, 0),
    'feature model-select crowded': (0, 0),
    'feature effort': (0, 0),
    'feature effort crowded': (0, 0),
    'feature notifications': (0, 0),
    'feature notifications crowded': (0, 0),
    'feature fork': (1, 0),
    'feature fork crowded': (1, 0),
    'feature rewind': (1, 0),
    'feature rewind crowded': (1, 0),
    'feature edit-message': (1, 0),
    'feature edit-message crowded': (1, 0),
    'feature export-md': (2, 0),
    'feature export-md crowded': (2, 0),
    'feature import-json': (2, 0),
    'feature import-json crowded': (2, 0),
    'feature question': (0, 0),
    'feature approve': (0, 0),
    'feature edit-before-approve': (0, 0),
    'feature decline': (0, 0),
    'feature allow-chat': (0, 0),
    'feature allow-project': (1, 0),
    'feature rules-dialog': (2, 0),
    'feature rules-dialog crowded': (2, 0),
    'feature permission-mode': (1, 0),
    'feature permission-mode crowded': (1, 0),
    'feature folder-access': (1, 0),
    'feature folder-access crowded': (1, 0),
    'feature plan-review': (0, 0),
    'feature changes-bar': (0, 0),
    'feature changes-bar crowded': (0, 0),
    'feature diff-review': (2, 0),
    'feature diff-review crowded': (2, 0),
    'feature explorer-dots': (1, 0),
    'feature explorer-dots crowded': (1, 0),
    'feature file-explorer': (1, 0),
    'feature file-explorer crowded': (1, 0),
    'feature git-view': (2, 0),
    'feature git-view crowded': (2, 0),
    'feature terminal': (2, 0),
    'feature terminal crowded': (2, 0),
    'feature checkpoints': (2, 0),
    'feature checkpoints crowded': (2, 0),
    'feature worktrees': (2, 0),
    'feature worktrees crowded': (2, 0),
    'feature context-meter': (0, 0),
    'feature context-meter crowded': (0, 0),
    'feature compact-now': (1, 0),
    'feature compact-now crowded': (1, 0),
    'feature progress-tab': (2, 0),
    'feature progress-tab crowded': (2, 0),
    'feature memory': (2, 0),
    'feature memory crowded': (2, 0),
    'feature output-style': (2, 0),
    'feature output-style crowded': (2, 0),
    'feature context-budget': (2, 0),
    'feature context-budget crowded': (2, 0),
    'feature tasks': (2, 0),
    'feature tasks crowded': (2, 0),
    'feature slash-commands': (1, 0),
    'feature slash-commands crowded': (1, 0),
    'feature slash-help': (1, 1),
    'feature slash-help crowded': (1, 1),
    'feature skills': (1, 0),
    'feature skills crowded': (1, 0),
    'feature mention': (1, 0),
    'feature mention crowded': (1, 0),
    'feature settings': (1, 0),
    'feature settings crowded': (1, 0),
    'feature text-size': (2, 0),
    'feature text-size crowded': (2, 0),
    'feature agent-tools': (2, 0),
    'feature agent-tools crowded': (2, 0),
    'feature tools-tools': (2, 0),
    'feature tools-tools crowded': (2, 0),
    'feature tools-extensions': (2, 0),
    'feature tools-extensions crowded': (2, 0),
    'feature tools-verify': (2, 0),
    'feature tools-verify crowded': (2, 0),
    'feature tools-schedules': (2, 0),
    'feature tools-schedules crowded': (2, 0),
    'feature tools-usage': (2, 0),
    'feature tools-usage crowded': (2, 0),
    'task 1': (0, 1),
    'task 2': (1, 0),
    'task 3': (1, 0),
    'task 4': (1, 0),
    'task 5': (1, 0),
    'task 6': (0, 1),
    'task 7': (2, 0),
    'task 8': (2, 0),
    'task 9': (3, 0),
    'task 10': (2, 0),
    'task 7 with more urgent items': (2, 0),
    'control continue': (1, 0),
    'control continue crowded': (1, 0),
    'control send queued': (1, 0),
    'control send queued crowded': (1, 0),
    'control edit queued': (1, 0),
    'control edit queued crowded': (1, 0),
    'control remove queued': (1, 0),
    'control remove queued crowded': (1, 0),
    'control keep all': (1, 0),
    'control keep all crowded': (1, 0),
    'control undo all': (1, 0),
    'control undo all crowded': (1, 0),
    'control keep a file': (2, 0),
    'control keep a file crowded': (2, 0),
    'control undo a file': (2, 0),
    'control undo a file crowded': (2, 0),
    'control open a diff': (2, 0),
    'control open a diff crowded': (2, 0),
    'control tasks toggle': (1, 0),
    'control tasks toggle crowded': (1, 0),
    'control open earlier version': (1, 0),
    'control open earlier version crowded': (1, 0),
    'control dismiss rewind notice': (1, 0),
    'control dismiss rewind notice crowded': (1, 0),
    'control copy side answer': (1, 0),
    'control copy side answer crowded': (1, 0),
    'control dismiss side answer': (1, 0),
    'control dismiss side answer crowded': (1, 0),
    'control close commands': (1, 0),
    'control close commands crowded': (1, 0),
}


def base_frontend(folder):
    """The frontend of BASE_COMMIT, extracted from git into `folder` (its own tests/support and static files)."""
    data = subprocess.run(["git", "-C", str(APP), "archive", BASE_COMMIT, "frontend"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(folder, filter="data")
    return Path(folder) / "frontend"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not os.environ.get("MEASURE_BASE"), reason="set MEASURE_BASE=1 to measure the base app (needs the git history)")
def test_the_base_numbers_are_what_the_base_app_needs(browser, tmp_path):
    base = load_module("base_harness", base_frontend(tmp_path) / "tests" / "conftest.py")  # its own server and fake backend
    measured = {}
    for run in FEATURE_RUNS + TASK_RUNS + CONTROL_RUNS:
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.route(f"{base.ORIGIN}/**", base.serve)
        try:
            d, proof = run.execute(base.UI(page), page)
            if proof is not None:
                expect(proof.first).to_be_visible()
            measured[run.key] = (d.clicks, d.keys)
        finally:
            context.close()
    table = "BASE_CLICKS = {\n" + "".join(f"    {key!r}: {value},\n" for key, value in measured.items()) + "}"
    assert measured == BASE_CLICKS, "\n" + table
