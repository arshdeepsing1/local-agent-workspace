"""Regressions found in the 2026-10-14 review: the Tags menu and heading fit the sidebar, durations of runs a
restart cut short, focus after Overview's Open, tag filters in message search, the drawer's widest width, and
the effort picker after a failed later save."""
import json
import re

import pytest
from playwright.sync_api import expect


def sessions(*items):
    rows = ", ".join(f"[{json.dumps(id)}, {{ ...makeSession({json.dumps(id)}, {json.dumps(title)}), ...{json.dumps(extra)} }}]"
                     for id, title, extra in items)
    return f"fake.sessions = new Map([{rows}])\n"


LONG_TAG = "a-rather-long-tag-name-of-32-ch"
TAGGED = sessions(("a", "Parser work", {"tags": ["bug", LONG_TAG]}), ("b", "Docs work", {"tags": ["docs"]}),
                  ("c", "UI work", {"tags": ["bug"]}))


def start(ui, page, setup=TAGGED, session=None):
    ui.app(session=session, scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")


def box(locator):
    return locator.bounding_box()


@pytest.mark.parametrize("width", [1280, 1024, 820, 360])
def test_the_tags_menu_opens_inside_the_list_and_the_heading_fits(ui, page, width):
    page.set_viewport_size({"width": width, "height": 800})
    start(ui, page)
    if width <= 600:
        page.get_by_role("button", name="Open sidebar").click()
    heading = page.locator(".conversation-list-heading")
    assert heading.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    page.get_by_role("button", name="Tags", exact=True).click()
    menu = page.get_by_role("group", name="Filter by tag")
    expect(menu).to_be_visible()
    listed = box(page.locator(".conversation-list"))
    shown = box(menu)
    assert shown["x"] >= listed["x"] - 0.5 and shown["x"] + shown["width"] <= listed["x"] + listed["width"] + 0.5
    checkbox = box(menu.get_by_role("checkbox", name=LONG_TAG))
    assert checkbox["x"] >= listed["x"]
    menu.get_by_role("checkbox", name="bug").check()
    page.keyboard.press("Escape")
    toggle = page.get_by_role("button", name="Tags (1)")
    assert toggle.evaluate("e => e.getBoundingClientRect().height") < 30  # One line.
    assert heading.evaluate("e => e.scrollWidth <= e.clientWidth + 1")


def test_message_search_results_follow_the_tag_filter(ui, page):
    start(ui, page, TAGGED + """fake.override = path => path.startsWith('/api/session-search?') ? fake.json({ truncated: false,
      results: ['a', 'b'].map(id => ({ id, title: fake.sessions.get(id).title, updated: 2, workspace: '/project', archived: false,
        matches: [{ event_id: 'e1', role: 'user', snippet: 'deploy steps' }] })) }) : undefined""")
    search = page.get_by_role("searchbox", name="Search conversations")
    search.fill("deploy")
    found = page.get_by_role("region", name="Found in messages")
    expect(found.locator(".found-title")).to_have_text(["Parser work", "Docs work"])
    search.fill("#bug deploy")
    expect(found.locator(".found-title")).to_have_text(["Parser work"])
    search.fill("deploy")
    page.get_by_role("button", name="Tags", exact=True).click()
    page.get_by_role("checkbox", name="docs").check()
    expect(found.locator(".found-title")).to_have_text(["Docs work"])


SETUP = """
window.makeSession = (events, overrides = {}) => ({
  id: 'parent', title: 'Parent task', workspace: '/project', model: 'test-model', status: 'idle',
  events, updated: 1, permission_mode: 'manual', allowed_directories: [], ...overrides,
})
"""


def test_runs_a_restart_cut_short_show_no_growing_duration(ui, page):
    events = """[
      { id: 'u', type: 'user', text: 'Go' },
      { id: 'd', type: 'tool', name: 'delegate_task', call_id: 'c1', input: { task: 'Map it' }, state: 'error', child_session_id: 'child',
        delegation: { status: 'interrupted', terminal_reason: 'interrupted', completed_tools: 1, started: Date.now() / 1000 - 7200 } },
      { id: 'r', type: 'assistant', text: 'Partial.', reasoning_summary: 'Plan.', reasoning_started: Date.now() / 1000 - 7200,
        request_info: { model: 'm', status: 'interrupted' } }]"""
    ui.mount("components/Conversation.js", f"{{ session: makeSession({events}), onError: fake.spy('onError') }}", setup=SETUP)
    progress = page.get_by_label("Subagent progress")
    expect(progress).to_contain_text("interrupted")
    expect(progress.locator(".delegation-elapsed")).to_have_count(0)
    expect(page.locator("details.thinking summary")).to_have_text("Thought")


def test_overview_open_moves_focus_to_the_opened_section(ui, page):
    ui.mount("components/AgentToolsDialog.js", "{ ...dialogProps(session()) }", scenario="agent_tools")
    expect(page.get_by_role("region", name="Overview")).to_be_visible()
    page.get_by_role("button", name="Open Tools", exact=True).focus()
    page.keyboard.press("Enter")
    tools = page.get_by_role("button", name="Tools", exact=True)
    expect(tools).to_have_attribute("aria-pressed", "true")
    expect(tools).to_be_focused()


def test_the_drawer_never_covers_the_sidebar(ui, page):
    page.set_viewport_size({"width": 820, "height": 900})
    ui.app(session="a", scenario="workspace")
    ui.show_session("a")
    page.get_by_role("button", name=re.compile("^Workspace")).first.click()
    panel = page.get_by_role("complementary", name="Workspace panel")
    expect(panel).to_be_visible()
    page.get_by_role("button", name="Expand workspace").click()
    sidebar = box(page.locator(".app-shell > .sidebar"))
    assert box(panel)["x"] >= sidebar["x"] + sidebar["width"]
    assert page.evaluate("getComputedStyle(document.querySelector('.app-shell')).getPropertyValue('--panel-width')") \
        == f"{round(box(panel)['width'])}px"


def test_a_failed_later_effort_save_keeps_the_level_the_server_accepted(ui, page):
    chat = "fake.sessions.set('a', { ...makeSession('a', 'Effort chat'), model: 'databricks-claude-opus-5-5' })"
    ui.app(session="a", scenario="workspace", setup=chat + """;const first = fake.defer('first')
      fake.override = (path, options) => path !== '/api/sessions/a/reasoning-effort' ? undefined
        : fake.requests(path, 'PUT').length === 1 ? first.promise : fake.json({ detail: 'Saving failed.' }, 400)""")
    ui.show_session("a")
    picker = page.get_by_role("combobox", name="Reasoning effort")
    picker.select_option("low")
    ui.wait("fake.requests('/api/sessions/a/reasoning-effort', 'PUT').length === 1")
    picker.select_option("max")
    ui.run("fake.deferreds.first.resolve(fake.json({ ...fake.sessions.get('a'), reasoning_effort: 'low' }))")
    ui.wait("fake.requests('/api/sessions/a/reasoning-effort', 'PUT').length === 2")
    expect(page.locator(".app-error")).to_contain_text("Saving failed.")
    expect(picker).to_have_value("low")
