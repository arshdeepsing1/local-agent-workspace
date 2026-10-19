"""Touch-size controls: on a coarse pointer every button, summary, select and menu item is at least 24 x 24 px
(styles.css `@media(pointer:coarse)`), while a mouse keeps the dense desktop sizes."""
import json
import re

from playwright.sync_api import expect

REPLY = "Done. See `src/app.py`.\n\n```python\nprint('hello')\n```"
EVENTS = [
    {"id": "u", "type": "user", "text": "Fix the exporter"},
    {"id": "t", "type": "tool", "name": "read_file", "state": "completed", "input": {"path": "README.md"}, "output": "# Readme"},
    {"id": "r", "type": "assistant", "text": REPLY, "request_info": {"model": "m", "status": "completed", "finish_reason": "stop",
                                                                      "usage": {"input_tokens": 900, "output_tokens": 50}}},
]
SETUP = f"""
fake.sessions = new Map([
  ['a', {{ ...makeSession('a', 'Exporter work'), events: {json.dumps(EVENTS)}, tags: ['bug', 'docs'] }}],
  ['b', {{ ...makeSession('b', 'Notes'), tags: ['ui'] }}],
  ['k', {{ ...makeSession('k', 'Child task'), parent_session_id: 'a', is_subagent: true }}],
])
fake.override = path => path.includes('mention-files?') ? fake.json([])
  : path.startsWith('/api/jobs') ? fake.json([]) : undefined
"""
# Visible controls under 24 x 24 px (inline links and checkboxes or radios, whose labels are clickable, are left out).
SMALL_CONTROLS = """root => [...root.querySelectorAll('button, summary, select, [role=menuitem], [role=option], [role=switch], a.icon-button')]
  .filter(el => !el.matches('input[type=checkbox], input[type=radio]') && !el.closest('.sr-only')
    && el.checkVisibility({ visibilityProperty: true, opacityProperty: true }) && el.getBoundingClientRect().width > 0)
  .map(el => [el, el.getBoundingClientRect()])
  .filter(([, box]) => box.width < 23.5 || box.height < 23.5)
  .map(([el, box]) => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')} "${(el.getAttribute('aria-label') || el.textContent).trim().slice(0, 30)}" ${Math.round(box.width)}x${Math.round(box.height)}`)"""


def small_controls(page, root="body"):
    return page.locator(root).first.evaluate(SMALL_CONTROLS)


def no_page_scroll(ui):
    return ui.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


def start(ui, page):
    ui.app(session="a", scenario="workspace", setup=SETUP)
    ui.show_session("a")
    ui.emit("a", "{ type: 'changes', changes: makeChanges(changedFile('note.txt', 2, 1)) }")
    expect(page.locator(".transcript .user-message")).to_be_visible()


def test_every_control_is_touch_sized_on_a_phone(coarse_ui, coarse_page):
    # V-T9. Mutation: delete the coarse-pointer block (tag chips 17 px, Show 1 subagent 20 px, Activity 14 px, ...).
    ui, page = coarse_ui, coarse_page
    start(ui, page)
    assert page.evaluate("matchMedia('(pointer: coarse)').matches")
    page.get_by_text("Activity · 1 action").click()
    page.get_by_text("Request details · completed").click()
    page.get_by_role("textbox", name="Message").fill("Read @src/parser.py and @README.md")
    expect(page.locator(".mention-chip")).to_have_count(2)
    # An attached PDF whose text is not read: its chip and the Attach as path button.
    ui.run("const record = fake.uploadRecord; fake.uploadRecord = (name, file) => ({ ...record(name, file), unreadable: 'Needs the documents extra.' })")
    with page.expect_file_chooser() as chooser:
        page.get_by_role("button", name="Attach files").click()
    chooser.value.set_files([{"name": "paper.pdf", "mimeType": "application/pdf", "buffer": b"%PDF"}])
    expect(page.get_by_role("button", name="Attach as path")).to_be_visible()
    expect(page.locator(".mention-chip")).to_have_count(3)
    expect(page.locator(".code-copy")).to_be_visible()
    assert small_controls(page, ".main-area") == []
    assert no_page_scroll(ui)
    page.get_by_role("button", name="Open sidebar").click()
    expect(page.get_by_role("button", name="Show 1 subagent")).to_be_visible()
    expect(page.locator(".sidebar .tag-chip").first).to_be_visible()
    assert small_controls(page, ".sidebar") == []
    page.get_by_role("button", name="More actions for Notes", exact=True).click()
    expect(page.get_by_role("menuitem", name="Rename", exact=True)).to_be_visible()
    assert small_controls(page, ".sidebar") == []
    assert no_page_scroll(ui)
    page.keyboard.press("Escape")
    page.locator(".sidebar").get_by_role("button", name="Close sidebar").click()
    page.locator(".permission-trigger").click()
    expect(page.locator(".permissions-menu")).to_be_visible()
    assert small_controls(page, ".permissions-menu") == []
    page.keyboard.press("Escape")
    page.get_by_role("button", name=re.compile("^Workspace")).click()
    page.get_by_role("button", name="note.txt Changed by the agent").click()
    expect(page.get_by_role("button", name="Keep change 1")).to_be_visible()
    assert small_controls(page, ".workspace-panel") == []
    page.get_by_role("button", name="Terminal", exact=True).click()
    command = page.get_by_role("textbox", name="Shell command")
    expect(command).to_be_visible()
    assert command.bounding_box()["height"] >= 28
    assert small_controls(page, ".workspace-panel") == []
    assert no_page_scroll(ui)


def test_a_mouse_keeps_the_dense_desktop_sizes(ui, page):
    # V-T10. Mutation: drop the @media(pointer:coarse) wrapper (tag chips grow to 24 px on desktop).
    start(ui, page)
    assert not page.evaluate("matchMedia('(pointer: coarse)').matches")
    chip = page.locator(".sidebar .tag-chip").first
    expect(chip).to_be_visible()
    assert chip.bounding_box()["height"] <= 18


def test_the_progress_tab_is_touch_sized_on_a_phone(coarse_ui, coarse_page):
    # A waiting and a running subagent, a running and an ended command, the task list and the context.
    ui, page = coarse_ui, coarse_page
    pending = {"event_id": "p1", "name": "write_file", "kind": "approval", "title": "Write src/app.py"}
    context = {"estimated_tokens": 6000, "input_budget": 120832, "context_window": 131072, "reply_reserve": 8192, "compactions": 0,
               "summarized_messages": 0, "estimate_method": "weighted_utf8", "instruction_files": ["AGENTS.md"], "warnings": []}
    children = "".join(f"""fake.sessions.set('{child}', {{ ...makeSession('{child}', '{title}'), parent_session_id: 'a', is_subagent: true,
      delegation: {json.dumps({"status": status, "completed_tools": 2, "agent_type": "explore", "background": True, "started": 1,
                               **({"pending": pending} if status == "awaiting_approval" else {})})} }})
""" for child, title, status in [("k1", "Check the tests", "running"), ("k2", "Update the docs", "awaiting_approval")])
    ui.app(session="a", scenario="workspace", setup=SETUP + children + f"""fake.sessions.get('a').context_info = {json.dumps(context)}
fake.override = path => path.startsWith('/api/jobs?') ? fake.json([workspaceJob('dev', {{ command: 'npm run dev' }}),
  workspaceJob('build', {{ command: 'make build', state: 'completed', exit_code: 0, updated: 150 }})]) : undefined""")
    ui.show_session("a")
    ui.emit("a", {"type": "tasks", "tasks": [{"id": "t1", "title": "Fix the exporter", "status": "in_progress"}]})
    page.get_by_role("button", name=re.compile("^Workspace")).click()
    page.get_by_role("button", name="Progress", exact=True).click()
    expect(page.get_by_role("list", name="Background tasks").get_by_role("listitem")).to_have_count(4)
    expect(page.get_by_role("button", name="Context details")).to_be_visible()
    assert small_controls(page, ".workspace-panel") == []
    assert no_page_scroll(ui)
