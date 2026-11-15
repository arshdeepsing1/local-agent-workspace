"""Shell layout: Workspace panel tiers (beside the chat, or a drawer with a scrim below 1,024 px), a top bar that
fits its column, the changes summary, the empty welcome and the context details popover. The UI revamp adds the first
screen (panel closed, a saved choice kept), the one status row above the message box and the shared dialog frame."""
import json
import re

import pytest
from playwright.sync_api import expect

TASKS = [{"id": f"t{n}", "title": f"Task number {n}", "status": "pending"} for n in range(3)]
CONTEXT_INFO = {"estimated_tokens": 6000, "input_budget": 120832, "context_window": 131072, "reply_reserve": 8192,
                "compactions": 1, "summarized_messages": 6, "estimate_method": "weighted_utf8", "instruction_files": [], "warnings": [],
                "breakdown": {"system_instructions": 1000, "tool_definitions": 2000, "messages_and_results": 2500, "summary": 400,
                              "request_overhead": 100}}
CHAT = ("fake.sessions.set('a', { ...makeSession('a', 'Fix the login redirect after a password reset'), "
        "events: [{ id: 'u', type: 'user', text: 'Do the work' }, { id: 'r', type: 'assistant', text: 'Done.' }]%s })")


def start(ui, page, width, height, meter=False, files=3, fields="", tasks=True):
    """Chat 'a' with a reply, pushed changes and a task list, at this viewport. `fields` adds session fields
    (', terminal_reason: ...'); with 3 files and tasks the status row shows the changes and a Tasks chip."""
    page.set_viewport_size({"width": width, "height": height})
    extra = (f", context_info: {json.dumps(CONTEXT_INFO)}" if meter else "") + fields
    ui.app(session="a", scenario="workspace", setup=CHAT % extra)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    ui.show_session("a")
    changed = ", ".join(f"changedFile('src/module_{n}/file_{n}.py', {n + 1}, {n})" for n in range(files))
    ui.run(f"fake.socketFor('a').emit({{ type: 'changes', changes: makeChanges({changed}) }})")
    if tasks:
        ui.emit("a", {"type": "tasks", "tasks": TASKS})
        # The checklist is the status row's item, or its chip when something more urgent is shown.
        expect(page.get_by_role("region", name="Tasks").or_(page.get_by_role("button", name=re.compile("^Show Tasks")))).to_be_visible()


def box(ui, selector):
    return ui.js(f"(e => e && e.getBoundingClientRect().toJSON())(document.querySelector({selector!r}))")


def workspace_button(page):
    return page.get_by_role("button", name=re.compile(r"^Workspace( \(|$)"))


def panel(page):
    return page.get_by_role("complementary", name="Workspace panel")


def open_panel(page):
    workspace_button(page).click()
    expect(panel(page)).to_be_visible()


# L1: the Workspace panel tiers

VIEWPORTS = [(1440, 900), (1280, 800), (1024, 768), (820, 1180), (390, 844), (360, 740)]


@pytest.mark.parametrize("opened", [False, True], ids=["closed", "open"])
@pytest.mark.parametrize("width,height", VIEWPORTS, ids=[f"{w}x{h}" for w, h in VIEWPORTS])
def test_shell_tiers(ui, page, width, height, opened):
    start(ui, page, width, height)
    if opened:
        if width <= 600:  # Phones: the panel covers the top bar, so the button is pressed before measuring.
            workspace_button(page).click()
        else:
            open_panel(page)
        expect(panel(page)).to_be_visible()
    assert ui.js("document.documentElement.scrollWidth <= innerWidth")
    main = box(ui, ".main-area")
    assert main["width"] >= 360, main
    # Docked panels are positioned relative (in the flow) for their resize handle; drawers are fixed.
    in_flow = opened and ui.js("getComputedStyle(document.querySelector('.workspace-panel')).position") in ("static", "relative")
    if in_flow:
        assert main["width"] >= min(width, 420), main
    assert box(ui, ".conversation-title")["width"] >= 120
    if width >= 1024:
        for button in [page.get_by_role("button", name="Agent tools", exact=True), workspace_button(page)]:
            assert button.evaluate("b => { const r = b.getBoundingClientRect(); const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2); return !!hit && b.contains(hit) }")
    if opened:
        aside = box(ui, ".workspace-panel")
        assert in_flow == (width >= 1024)
        if width < 1024:
            assert aside["left"] >= width - 520 and aside["height"] == height, aside


def test_tablet_drawer_and_scrim(ui, page):
    start(ui, page, 820, 1180)
    assert ui.js("location.search") == "?session=a"
    sidebar = box(ui, ".sidebar")["width"]
    assert sidebar == 230
    open_panel(page)
    assert ui.js("getComputedStyle(document.querySelector('.workspace-panel')).position") == "fixed"
    assert box(ui, ".sidebar")["width"] == 230
    assert box(ui, ".workspace-panel")["left"] == 300
    # The scrim has no role: there is still one Close workspace.
    expect(page.get_by_role("button", name="Close workspace")).to_have_count(1)
    page.mouse.click(260, 400)  # The dimmed strip of the chat between the sidebar and the drawer.
    expect(panel(page)).to_have_count(0)
    open_panel(page)
    # The scrim covers only the chat: the sidebar, which Tab reaches from the drawer, takes the mouse too.
    # Mutation: drop the sidebar's z-index above the scrim.
    row = page.locator(".conversation-row > button:first-child", has_text="Fix the login redirect")
    assert row.evaluate("b => { const r = b.getBoundingClientRect(); return b.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) }")
    page.get_by_role("button", name="New conversation").hover()
    page.mouse.click(100, 400)  # Empty sidebar space: the drawer stays open.
    expect(panel(page)).to_be_visible()
    page.get_by_role("button", name="Running").focus()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    focused = page.get_by_role("button", name="Running")
    expect(focused).to_be_focused()
    assert focused.evaluate("b => getComputedStyle(b).outlineStyle === 'solid' && parseFloat(getComputedStyle(b).outlineWidth) >= 2")
    assert focused.evaluate("b => { const r = b.getBoundingClientRect(); return b.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) }")
    page.mouse.click(260, 400)
    expect(panel(page)).to_have_count(0)
    assert ui.js("location.search") == "?session=a"
    expect(page.locator(".workspace-scrim")).to_have_count(0)


@pytest.mark.parametrize("width,height", [(820, 1180), (390, 844)])
def test_drawer_works_with_the_keyboard(ui, page, width, height):
    start(ui, page, width, height)
    workspace_button(page).focus()
    page.keyboard.press("Enter")
    expect(panel(page)).to_be_visible()
    expect(page.get_by_role("button", name="Close workspace")).to_be_focused()
    assert ui.js("document.querySelector('.workspace-panel').contains(document.activeElement)")
    assert ui.js("document.querySelector('.main-area').inert") is True
    page.keyboard.press("Escape")
    expect(panel(page)).to_have_count(0)
    expect(workspace_button(page)).to_be_focused()
    assert ui.js("document.querySelector('.main-area').inert") is False
    page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Close workspace")).to_be_focused()
    for _ in range(3):
        page.keyboard.press("Tab")
        assert not ui.js("document.querySelector('.main-area').contains(document.activeElement)")


@pytest.mark.parametrize("width", [640, 700, 760])
def test_drawer_leaves_the_sidebar_uncovered(ui, page, width):
    """The sidebar stays reachable from the keyboard beside the drawer, so the drawer must not cover it."""
    start(ui, page, width, 900)
    workspace_button(page).focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Close workspace")).to_be_focused()
    left = box(ui, ".workspace-panel")["left"]
    assert left >= box(ui, ".sidebar")["right"] and box(ui, ".workspace-panel")["width"] >= 360
    seen, covered = set(), []
    for _ in range(80):
        page.keyboard.press("Tab")
        state = ui.js("""(a => ({ inSidebar: !!a.closest('.sidebar'), name: a.getAttribute('aria-label') || a.textContent.trim().slice(0, 40),
          right: a.getBoundingClientRect().right }))(document.activeElement)""")
        if not state["inSidebar"]:
            continue
        seen.add(state["name"])
        if state["right"] > left + 0.5:
            covered.append(state)
    assert {"New conversation", "More actions for Fix the login redirect after a password reset"} <= seen, seen
    assert covered == []


def test_choosing_a_chat_in_the_sidebar_keeps_focus_on_it(ui, page):
    page.set_viewport_size({"width": 820, "height": 1180})
    ui.app(session="a", scenario="workspace", setup=CHAT % "" + "; fake.sessions.set('b', makeSession('b', 'Second chat'))")
    ui.show_session("a")
    workspace_button(page).focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Close workspace")).to_be_focused()
    second = page.locator(".conversation-row > button:first-child", has_text="Second chat")
    second.focus()
    page.keyboard.press("Enter")
    ui.show_session("b")
    expect(page.locator(".conversation-title")).to_contain_text("Second chat")
    expect(panel(page)).to_be_visible()
    ui.wait("document.querySelector('.main-area').inert")  # The drawer is back once the chat has loaded.
    expect(second).to_be_focused()
    # Escape there still closes the drawer, as from the panel.
    page.keyboard.press("Escape")
    expect(panel(page)).to_have_count(0)
    expect(second).to_be_focused()


@pytest.mark.parametrize("width,height", [(820, 1180), (390, 844)])
def test_an_error_while_the_drawer_is_open_is_announced_and_visible(ui, page, width, height):
    start(ui, page, width, height)
    if width <= 600:
        workspace_button(page).click()
    else:
        open_panel(page)
    expect(page.get_by_role("button", name="Close workspace")).to_be_focused()
    ui.run("fake.override = (path, options) => path === '/api/sessions' && (options.method || 'GET') === 'GET' "
           "? fake.json({ detail: 'Could not list conversations.' }, 500) : undefined")
    ui.emit("a", {"type": "status", "status": "idle"})
    alert = page.locator(".drawer-alert")
    expect(alert).to_have_text(re.compile("Could not list conversations."))
    expect(alert).to_have_attribute("role", "alert")
    # It is outside the inert chat column, so it is in the accessibility tree (the column's own banner is not).
    assert ui.js("document.querySelector('.main-area').inert") is True
    assert alert.evaluate("e => !e.closest('[inert]')")
    cdp = page.context.new_cdp_session(page)
    nodes = cdp.send("Accessibility.getFullAXTree")["nodes"]
    alerts = [n for n in nodes if not n.get("ignored") and n.get("role", {}).get("value") == "alert"]
    assert len(alerts) == 1, alerts
    expect(alert).to_be_in_viewport(ratio=1)
    assert alert.evaluate("e => { const r = e.getBoundingClientRect(); return e.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) }")
    aside = box(ui, ".workspace-panel")
    inside = alert.bounding_box()
    assert inside["x"] >= aside["left"] and inside["x"] + inside["width"] <= aside["right"], (inside, aside)
    alert.get_by_role("button", name="Dismiss error").click()
    expect(page.locator(".drawer-alert")).to_have_count(0)
    expect(page.locator(".app-error")).to_have_count(0)
    expect(page.get_by_role("button", name="Close workspace")).to_be_focused()


def test_wide_panel_keeps_focus_and_ignores_escape(ui, page):
    start(ui, page, 1280, 800)
    workspace_button(page).focus()
    page.keyboard.press("Enter")
    expect(panel(page)).to_be_visible()
    expect(workspace_button(page)).to_be_focused()
    assert ui.js("document.querySelector('.main-area').inert") is False
    page.get_by_role("button", name="Close workspace").focus()
    page.keyboard.press("Escape")
    expect(panel(page)).to_be_visible()
    assert ui.js("document.querySelector('.main-area').inert") is False
    expect(page.locator(".workspace-scrim")).to_be_hidden()


@pytest.mark.parametrize("width", [1280, 1024, 820, 700])
def test_sidebar_width_is_stable(ui, page, width):
    start(ui, page, width, 800)
    before = box(ui, ".sidebar")["width"]
    open_panel(page)
    assert box(ui, ".sidebar")["width"] == before


# L2: the top bar fits its column

@pytest.mark.parametrize("width,height,title", [(360, 740, 150), (390, 844, 180)])
def test_phone_top_bar(ui, page, width, height, title):
    start(ui, page, width, height)
    tools = page.get_by_role("button", name="Agent tools", exact=True)
    workspace = page.get_by_role("button", name="Workspace (3 changed files)")
    expect(tools).to_be_visible()
    expect(workspace).to_be_visible()
    assert tools.bounding_box()["width"] <= 44
    badge = page.locator(".topbar .change-count").bounding_box()["width"]
    assert workspace.bounding_box()["width"] <= 44 + badge
    labels = ui.js("[...document.querySelectorAll('.topbar .toggle-label')].map(e => e.getBoundingClientRect().width)")
    assert len(labels) == 2 and all(w <= 1 for w in labels), labels
    assert box(ui, ".conversation-title")["width"] >= title
    expect(page.locator(".conversation-title")).to_have_attribute("title", re.compile(r"^Fix the login redirect after a password reset\nSession: a$"))
    expect(page.locator(".topbar .session-id")).to_be_hidden()
    expect(tools).to_have_attribute("title", "Agent tools")
    expect(workspace).to_have_attribute("title", "Workspace")


def test_wide_top_bar_shows_labels(ui, page):
    start(ui, page, 1280, 800)
    tools = page.get_by_role("button", name="Agent tools", exact=True)
    expect(tools).to_have_text("Agent tools")
    assert tools.bounding_box()["width"] > 60
    expect(page.locator(".topbar .session-id")).to_be_visible()
    assert all(w > 30 for w in ui.js("[...document.querySelectorAll('.topbar .toggle-label')].map(e => e.getBoundingClientRect().width)"))


# L3: the changes summary shortens its text, not its stat

@pytest.mark.parametrize("width,height", [(1024, 768), (360, 740)])
def test_changes_summary_fits(ui, page, width, height):
    start(ui, page, width, height)
    page.add_style_tag(content=".changes-bar{width:230px}")  # Narrower than the summary's content.
    fits = ui.js("""(() => {
      const toggle = document.querySelector('.changes-toggle')
      const stat = toggle.querySelector('.diff-stat').getBoundingClientRect()
      const outer = toggle.getBoundingClientRect()
      const text = toggle.querySelector(':scope > span:not(.diff-stat)')
      return { overflow: toggle.scrollWidth - toggle.clientWidth, inside: stat.left >= outer.left && stat.right <= outer.right + 0.5,
               shortened: text.scrollWidth > text.clientWidth }
    })()""")
    assert fits["overflow"] <= 1 and fits["inside"] and fits["shortened"], fits


# L12: the empty welcome sits just above the message box

@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_welcome_sits_above_the_composer(ui, page, width, height):
    page.set_viewport_size({"width": width, "height": height})
    ui.app(scenario="workspace")
    expect(page.get_by_role("heading", name="What’s up next?")).to_be_visible()
    gap = box(ui, ".chat-composer")["top"] - box(ui, ".suggestions")["bottom"]
    assert 0 <= gap <= 60, gap


@pytest.mark.parametrize("height", [390, 300])
def test_short_welcome_scrolls_from_the_top(ui, page, height):
    page.set_viewport_size({"width": 844, "height": height})
    ui.app(scenario="workspace")
    expect(page.get_by_role("heading", name="What’s up next?")).to_be_attached()
    if height == 300:  # Clearly taller than its box, so content pushed to the end (align-items:flex-end) would be cut off.
        assert ui.js("(e => e.scrollHeight - e.clientHeight)(document.querySelector('.welcome'))") >= 40
    assert ui.js("document.querySelector('.welcome').scrollTop") == 0
    assert box(ui, ".welcome-heading")["top"] >= box(ui, ".welcome")["top"]
    explore = page.get_by_role("button", name="Explore this project")
    explore.scroll_into_view_if_needed()
    expect(explore).to_be_in_viewport(ratio=1)


# L13: the Workspace panel's four tabs fit it: labelled while the labels fit, icons only otherwise (a phone at
# large text, or a docked panel at 125% and up); each icon-only tab keeps its label as its name and tooltip

TAB_VIEWPORTS = [(1280, 900, 100, False), (1280, 900, 150, True), (1100, 800, 100, None), (820, 1180, 150, False),
                 (390, 844, 100, False), (360, 740, 100, None), (360, 740, 150, True)]


@pytest.mark.parametrize("width,height,scale,icons", TAB_VIEWPORTS, ids=[f"{w}x{h}-{s}%" for w, h, s, _ in TAB_VIEWPORTS])
def test_the_four_workspace_tabs_fit_the_panel_with_labels_or_icons(ui, page, width, height, scale, icons):
    page.set_viewport_size({"width": width, "height": height})
    ui.app(session="a", scenario="workspace", setup=CHAT % "" + f"; localStorage.setItem('local.textSize', '{scale}')")
    ui.show_session("a")
    open_panel(page)
    nav = panel(page).get_by_role("navigation", name="Workspace views")
    tabs = nav.get_by_role("button")
    expect(tabs).to_have_text(["Files", "Changes", "Terminal", "Progress"])
    tabs.last.click()
    expect(tabs.last).to_have_attribute("aria-pressed", "true")
    fit = nav.evaluate("""nav => ({ icons: nav.classList.contains('icons-only'), overflow: nav.scrollWidth - nav.clientWidth,
      clipped: [...nav.querySelectorAll('.tab-label')].filter(label => label.scrollWidth > label.clientWidth + 0.5).length,
      hit: [...nav.querySelectorAll('button')].every(b => { const r = b.getBoundingClientRect()
        return b.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) }) })""")
    assert fit["overflow"] <= 1 and fit["hit"], fit
    if icons is not None:  # None: either, at the edge of fitting
        assert fit["icons"] == icons, fit
    if fit["icons"]:
        for tab, label in zip(tabs.all(), ["Files", "Changes", "Terminal", "Progress"]):
            expect(tab).to_have_attribute("title", label)
            assert tab.locator(".tab-label").evaluate("e => e.getBoundingClientRect().width") <= 1
    else:
        assert fit["clipped"] == 0, fit
    assert ui.js("document.documentElement.scrollWidth <= innerWidth")
    assert ui.js("(e => e.scrollWidth <= e.clientWidth)(document.querySelector('.progress-panel'))")
    main = box(ui, ".main-area")
    assert main["width"] >= 360, main
    if width >= 1024:
        assert main["width"] >= 420, main


def test_resizing_the_panel_switches_the_tabs_between_labels_and_icons_without_a_resize_loop(ui, page):
    # The fit watches the panel, not the tabs (whose height changes with their labels). Mutation: observe the tab bar
    # (the browser then reports a ResizeObserver loop).
    page.set_viewport_size({"width": 1280, "height": 900})
    ui.app(session="a", scenario="workspace", setup=CHAT % "" + "; window.pageErrors = []; addEventListener('error', e => pageErrors.push(e.message))")
    ui.show_session("a")
    open_panel(page)
    nav = page.locator(".workspace-tabs")
    resizer = page.get_by_role("separator", name="Resize workspace panel")
    resizer.focus()
    for key, icons in [("Home", True), ("End", False), ("Home", True), ("Enter", False)]:
        page.keyboard.press(key)
        expect(nav).to_have_class(re.compile(r"\bicons-only\b") if icons else re.compile(r"^workspace-tabs$"))
    page.set_viewport_size({"width": 390, "height": 844})
    expect(nav).to_have_class(re.compile(r"^workspace-tabs$"))
    ui.run("document.documentElement.style.setProperty('--text-scale', '1.5')")  # Settings → Appearance → Text size
    expect(nav).to_have_class(re.compile(r"\bicons-only\b"))
    assert ui.js("pageErrors") == []


# L6: the context details open as a popover above the meter

POPOVER = [(390, 664), (390, 844), (844, 390), (667, 375), (1440, 900), (360, 740)]


@pytest.mark.parametrize("width,height", POPOVER, ids=[f"{w}x{h}" for w, h in POPOVER])
def test_context_details_popover(ui, page, width, height):
    start(ui, page, width, height, meter=True)
    page.get_by_role("button", name=re.compile("files changed")).click()  # The status row shows one bar at a time.
    summary = page.locator(".context-meter > summary")
    summary.click()
    compact = page.get_by_role("button", name="Compact now")
    expect(compact).to_be_attached()
    details = box(ui, ".context-details")
    head = box(ui, ".context-meter > summary")
    assert details["top"] >= 0 and details["bottom"] <= height and details["left"] >= 0 and details["right"] <= width, details
    assert details["bottom"] <= head["top"] or details["top"] >= head["bottom"], (details, head)
    assert ui.js("(e => e.scrollHeight === e.clientHeight)(document.querySelector('.context-meter'))")
    if (width, height) == (360, 740):
        assert box(ui, ".transcript")["height"] >= 200
    compact.focus()
    expect(compact).to_be_in_viewport(ratio=1)
    assert compact.bounding_box()["y"] >= box(ui, ".topbar")["bottom"]
    # Escape inside the details closes them and focuses the summary.
    page.keyboard.press("Escape")
    assert ui.js("document.querySelector('.context-meter').open") is False
    expect(summary).to_be_focused()
    # A click elsewhere closes them.
    summary.click()
    assert ui.js("document.querySelector('.context-meter').open") is True
    transcript = box(ui, ".transcript")
    page.mouse.click(transcript["left"] + 10, transcript["top"] + 10)
    ui.wait("!document.querySelector('.context-meter').open")
    # Moving focus out of them (Shift+Tab from the summary) closes them; focus is not moved by the handler.
    summary.click()
    assert ui.js("document.querySelector('.context-meter').open") is True
    summary.focus()
    page.keyboard.press("Shift+Tab")
    ui.wait("!document.querySelector('.context-meter').open")
    assert ui.js("(a => a !== document.body && !document.querySelector('.context-meter').contains(a) && document.querySelector('.chat-composer').contains(a))(document.activeElement)")


@pytest.mark.parametrize("width,height,transcript", [(360, 740, 200), (1024, 768, 200), (390, 664, 160)])
def test_composer_bars_are_capped(ui, page, width, height, transcript):
    """A long changes list scrolls inside the bars, which take at most 40% of the window."""
    start(ui, page, width, height, meter=True, files=24)
    page.get_by_role("button", name=re.compile("files changed")).click()  # The status row shows one bar at a time.
    page.locator(".context-meter > summary").click()
    expect(page.get_by_role("button", name="Compact now")).to_be_attached()
    # The one open bar is a long list: it scrolls inside the bars (or the bars scroll), and takes at most 40%.
    bars = ui.js("""(e => ({ height: e.getBoundingClientRect().height, scrolls: e.scrollHeight > e.clientHeight
      || [...e.querySelectorAll('.changes-list')].some(list => list.scrollHeight > list.clientHeight) }))(document.querySelector('.composer-bars'))""")
    assert bars["height"] <= 0.4 * height + 1 and bars["scrolls"], bars
    assert box(ui, ".transcript")["height"] >= transcript



def test_a_long_file_name_has_its_full_name_as_a_tooltip(ui, page):
    # A Files row cuts a long name with an ellipsis. Mutation: drop the name span's title.
    name = "very_long_directory_name_for_generated_client_bindings_and_more_words"
    page.set_viewport_size({"width": 1440, "height": 900})
    setup = (CHAT % "") + ("; const base = fake.handle; fake.handle = (path, options) => path.startsWith('/api/files?') ? "
                           f"fake.json([{{ path: '{name}', name: '{name}', directory: true }}]) : base(path, options)")
    ui.app(session="a", scenario="workspace", setup=setup)
    ui.show_session("a")
    open_panel(page)
    row = page.locator(".file-row", has_text=name).locator("span").first
    expect(row).to_have_attribute("title", name)
    assert row.evaluate("e => e.scrollWidth > e.clientWidth")


TOOLBAR_VIEWPORTS = [(1280, 800), (1024, 768), (820, 1180), (390, 844), (360, 740)]


@pytest.mark.parametrize("width,height", TOOLBAR_VIEWPORTS, ids=[f"{w}x{h}" for w, h in TOOLBAR_VIEWPORTS])
def test_the_composer_toolbar_fits_with_attach_files(ui, page, width, height):
    """Attach files sits between Add project file and Send as plain text, and the toolbar still fits its box."""
    start(ui, page, width, height)
    composer = box(ui, ".chat-composer .composer-block")
    buttons = [page.get_by_role("button", name=name, exact=True).bounding_box()
               for name in ("Add project file", "Attach files", "Send as plain text")]
    model = page.get_by_role("combobox", name="Model").bounding_box()
    for item in [*buttons, model]:
        assert composer["left"] - 1 <= item["x"] and item["x"] + item["width"] <= composer["right"] + 1, (item, composer)
    assert buttons[0]["x"] + buttons[0]["width"] <= buttons[1]["x"] and buttons[1]["x"] + buttons[1]["width"] <= buttons[2]["x"]
    assert ui.js("document.documentElement.scrollWidth <= innerWidth")


# L14: the first screen. A first visit has the Workspace panel closed: the chat list, the chat and the message box
# are what shows. A saved choice (localStorage 'workspace-panel') is kept; a phone keeps the drawer closed.

def first_screen(ui, page, width, height, setup=""):
    page.set_viewport_size({"width": width, "height": height})
    ui.app(session="a", scenario="workspace", setup=CHAT % "" + setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    ui.show_session("a")


@pytest.mark.parametrize("width,height", [(1280, 800), (1024, 768), (820, 1180)], ids=["1280", "1024", "820"])
def test_a_first_visit_shows_the_chat_list_the_chat_and_the_message_box_only(ui, page, width, height):
    first_screen(ui, page, width, height)
    expect(panel(page)).to_have_count(0)
    expect(page.locator(".workspace-scrim")).to_have_count(0)
    expect(workspace_button(page)).not_to_have_class(re.compile(r"\bactive\b"))
    expect(page.get_by_role("button", name="New conversation")).to_be_visible()
    expect(page.locator(".conversation-row > button:first-child", has_text="Fix the login redirect")).to_be_visible()
    expect(page.locator(".transcript")).to_be_visible()
    expect(page.get_by_role("textbox", name="Message")).to_be_visible()
    assert ui.js("localStorage.getItem('workspace-panel')") is None  # Nothing was saved by looking.
    assert ui.js("document.documentElement.scrollWidth <= innerWidth")
    assert nothing_open(ui, page)


def nothing_open(ui, page):
    """No dialog, menu, popover or banner that needs dismissing is open (the page is as it is to read)."""
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.get_by_role("menu")).to_have_count(0)
    expect(page.get_by_role("alert")).to_have_count(0)
    expect(page.locator(".connection-banner, .storage-notice, .error-banner, .workspace-scrim")).to_have_count(0)
    return ui.js("!document.querySelector('dialog[open], :modal, details[open]')")


def test_the_first_screen_fits_a_phone(ui, page):
    first_screen(ui, page, 390, 844)
    expect(panel(page)).to_have_count(0)
    expect(page.get_by_role("textbox", name="Message")).to_be_visible()
    assert ui.js("document.documentElement.scrollWidth <= innerWidth")
    assert box(ui, ".sidebar")["right"] <= 1  # The sidebar is a closed drawer.
    assert nothing_open(ui, page)
    page.get_by_role("button", name="Open sidebar").click()
    ui.wait("document.querySelector('.sidebar').getBoundingClientRect().left >= -1")
    assert ui.js("document.documentElement.scrollWidth <= innerWidth")


def test_a_saved_open_panel_is_kept_and_a_choice_is_saved(ui, page):
    first_screen(ui, page, 1280, 800, "; localStorage.setItem('workspace-panel', 'open')")
    expect(panel(page)).to_be_visible()
    page.get_by_role("button", name="Close workspace").click()
    expect(panel(page)).to_have_count(0)
    assert ui.js("localStorage.getItem('workspace-panel')") == "closed"
    workspace_button(page).click()
    expect(panel(page)).to_be_visible()
    assert ui.js("localStorage.getItem('workspace-panel')") == "open"


def test_a_saved_closed_panel_stays_closed(ui, page):
    first_screen(ui, page, 1280, 800, "; localStorage.setItem('workspace-panel', 'closed')")
    expect(panel(page)).to_have_count(0)
    expect(page.get_by_role("textbox", name="Message")).to_be_visible()


def test_a_saved_open_panel_does_not_cover_a_phone(ui, page):
    first_screen(ui, page, 390, 844, "; localStorage.setItem('workspace-panel', 'open')")
    expect(panel(page)).to_have_count(0)
    expect(page.get_by_role("textbox", name="Message")).to_be_visible()


# L15: one status row above the message box. At most one item shows, chosen by a fixed priority (Continue, queued
# messages, changes, tasks, side answer, rewind notice, command help); the others are chips that open their item.

MANY = (", terminal_reason: 'stopped', queued_messages: [{ id: 'q1', text: 'Then run the tests', created: 1 }, "
        "{ id: 'q2', text: 'And update the docs', created: 2 }]")


def status_row(page):
    return page.get_by_role("group", name="Conversation status")


def chips(page):
    return status_row(page).locator(".status-chip")


def primary(page):
    return status_row(page).locator(".status-primary")


def test_the_status_row_shows_continue_with_the_queue_and_chips_for_the_rest(ui, page):
    start(ui, page, 1280, 900, fields=MANY)
    expect(status_row(page)).to_be_visible()
    expect(primary(page)).to_have_attribute("data-item", "continue")
    expect(primary(page).get_by_role("button", name="Continue", exact=True)).to_be_visible()
    # Continue and the queue always occur together after a stop: both show, neither hides the other.
    expect(primary(page).get_by_role("group", name="Queued messages").locator(".queue-item")).to_have_count(2)
    for hidden in (page.get_by_role("region", name="Changed files"), page.get_by_role("region", name="Tasks")):
        expect(hidden).to_have_count(0)
    expect(chips(page)).to_have_text(["3 changes", "Tasks 0/3"])
    # The changes chip carries Keep all and Undo all, so they are one click away as before.
    expect(status_row(page).get_by_role("button", name="Keep all changes")).to_be_visible()
    expect(status_row(page).get_by_role("button", name="Undo all changes")).to_be_visible()
    # Above the message box, never over it.
    assert box(ui, ".status-row")["bottom"] <= box(ui, ".chat-composer .composer")["top"] + 1


@pytest.mark.parametrize("chip,item,proof", [
    ("3 changes", "changes", lambda page: page.get_by_role("region", name="Changed files").locator(".changes-file")),
    ("Tasks 0/3", "tasks", lambda page: page.get_by_role("region", name="Tasks").get_by_role("listitem"))])
def test_each_chip_opens_its_item_in_one_click_and_the_leading_items_become_chips(ui, page, chip, item, proof):
    start(ui, page, 1280, 900, fields=MANY)
    chips(page).filter(has_text=chip).click()
    expect(primary(page)).to_have_attribute("data-item", item)
    expect(proof(page).first).to_be_visible()  # Open already: the rows show, no second click.
    expect(chips(page).filter(has_text=chip)).to_have_count(0)  # The item is no chip while it is shown.
    # Continue and the queue are chips now, and each keeps its button.
    expect(chips(page).filter(has_text="Stopped")).to_have_count(1)
    expect(chips(page).filter(has_text="2 queued")).to_have_count(1)
    expect(status_row(page).get_by_role("button", name="Continue", exact=True)).to_be_visible()
    expect(status_row(page).get_by_role("button", name="Send queued messages")).to_be_visible()
    assert ui.js("document.documentElement.scrollWidth <= innerWidth")


def test_with_one_item_the_row_shows_it_fully_and_has_no_chips(ui, page):
    start(ui, page, 1280, 900, files=0)
    expect(primary(page)).to_have_attribute("data-item", "tasks")
    expect(chips(page)).to_have_count(0)
    toggle = page.get_by_role("button", name=re.compile(r"^Tasks 0\/3 done"))
    expect(toggle).to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "false")  # Collapsed as the bar always was.
    toggle.click()
    expect(page.get_by_role("region", name="Tasks").get_by_role("listitem")).to_have_count(3)


def test_the_changes_lead_the_tasks_and_a_chip_never_repeats_the_item_shown(ui, page):
    start(ui, page, 1280, 900)
    expect(primary(page)).to_have_attribute("data-item", "changes")
    expect(chips(page)).to_have_text(["Tasks 0/3"])
    expect(page.get_by_role("button", name=re.compile("^3 files changed"))).to_be_visible()
    chips(page).first.click()
    expect(primary(page)).to_have_attribute("data-item", "tasks")
    expect(chips(page)).to_have_text(["3 changes"])
    # Focus is in the tasks now, so a message that arrives waits as a chip (and is announced) instead of replacing them.
    ui.run("fake.socketFor('a').emit({ type: 'queue', queued_messages: [{ id: 'q1', text: 'Next', created: 1 }] })")
    expect(chips(page)).to_have_text(["1 queued", "3 changes"])
    expect(primary(page)).to_have_attribute("data-item", "tasks")
    page.mouse.move(0, 0)  # The pointer rests elsewhere too.
    page.get_by_role("textbox", name="Message").focus()  # Focus leaves: the queue outranks the tasks and takes the lead.
    expect(primary(page)).to_have_attribute("data-item", "queue")
    expect(chips(page)).to_have_text(["3 changes", "Tasks 0/3"])


def test_the_row_and_its_chips_work_with_the_keyboard(ui, page):
    start(ui, page, 1280, 900, fields=MANY)
    expect(status_row(page)).to_have_attribute("aria-label", "Conversation status")
    assert [chip.get_attribute("aria-label") for chip in chips(page).all()] == ["Show 3 changes", "Show Tasks 0/3 done"]
    chips(page).nth(1).focus()
    page.keyboard.press("Enter")
    expect(primary(page).get_by_role("region", name="Tasks").get_by_role("listitem")).to_have_count(3)
    assert ui.js("!!document.activeElement.closest('.status-primary')")  # Focus moved into what opened.
    expect(page.get_by_role("button", name=re.compile(r"^Tasks 0\/3 done"))).to_have_attribute("aria-expanded", "true")
    # Escape collapses the opened item and gives the row back to its first item by priority.
    page.get_by_role("button", name=re.compile(r"^Tasks 0\/3 done")).focus()
    page.keyboard.press("Escape")
    expect(page.get_by_role("region", name="Tasks")).to_have_count(0)
    expect(primary(page)).to_have_attribute("data-item", "continue")
    assert ui.js("document.activeElement !== document.body && !!document.activeElement.closest('.status-row')")
    # Space opens a chip too.
    chips(page).filter(has_text="Tasks").focus()
    page.keyboard.press("Space")
    expect(page.get_by_role("region", name="Tasks").get_by_role("listitem")).to_have_count(3)


def test_escape_collapses_an_item_that_is_the_only_one(ui, page):
    start(ui, page, 1280, 900, files=0)
    toggle = page.get_by_role("button", name=re.compile(r"^Tasks 0\/3 done"))
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    toggle.focus()
    page.keyboard.press("Escape")
    expect(page.get_by_role("button", name=re.compile(r"^Tasks 0\/3 done"))).to_have_attribute("aria-expanded", "false")


def test_the_row_keeps_its_height_while_you_type(ui, page):
    start(ui, page, 1280, 900, fields=MANY)
    row, composer = box(ui, ".status-row"), box(ui, ".chat-composer .composer")
    message = page.get_by_role("textbox", name="Message")
    message.click()
    for text in ("Hello", "Hello there\nsecond line\nthird line", "/", "@src"):
        message.fill(text)
        after, field = box(ui, ".status-row"), box(ui, ".chat-composer .composer")
        assert after["height"] == row["height"], (row, after)  # The message box may grow; the row keeps its height.
        assert after["bottom"] <= field["top"] + 1
    assert composer["height"] > 0


def test_an_item_that_arrives_while_you_type_keeps_the_focus_and_the_text(ui, page):
    start(ui, page, 1280, 900, files=0, tasks=False)
    message = page.get_by_role("textbox", name="Message")
    message.click()
    message.press_sequentially("Half a thought")
    before = box(ui, ".chat-composer .composer")
    ui.emit("a", {"type": "tasks", "tasks": TASKS})
    ui.run("fake.socketFor('a').emit({ type: 'queue', queued_messages: [{ id: 'q1', text: 'Next', created: 1 }] })")
    expect(status_row(page)).to_be_visible()
    message.press_sequentially(" and more")
    expect(message).to_be_focused()
    expect(message).to_have_value("Half a thought and more")
    # The row grew by what arrived; it did not move the message box's bottom edge, and it still sits above it.
    after = box(ui, ".chat-composer .composer")
    assert after["bottom"] == before["bottom"], (before, after)
    assert box(ui, ".status-row")["bottom"] <= after["top"] + 1
    row = box(ui, ".status-row")
    for letter in "abc":  # Typing never changes it from here on.
        message.press_sequentially(letter)
        assert box(ui, ".status-row")["height"] == row["height"]


def test_an_item_that_arrives_while_focus_is_in_the_row_does_not_move_that_item(ui, page):
    start(ui, page, 1280, 900)
    page.get_by_role("button", name=re.compile("^3 files changed")).click()
    keep = page.get_by_role("button", name="Keep changes to src/module_1/file_1.py")
    keep.focus()
    list_box = box(ui, ".changes-list")
    ui.run("fake.socketFor('a').emit({ type: 'status', status: 'idle', terminal_reason: 'stopped' })")
    expect(page.get_by_role("button", name="Continue", exact=True)).to_be_visible()
    assert ui.js("document.activeElement.getAttribute('aria-label')") == "Keep changes to src/module_1/file_1.py"
    after = box(ui, ".changes-list")
    assert after["top"] == list_box["top"] and after["height"] == list_box["height"], (list_box, after)  # Not unmounted, not moved.


def test_the_commands_help_stays_a_line_when_more_items_arrive(ui, page):
    start(ui, page, 1280, 900, files=0, tasks=False)
    message = page.get_by_role("textbox", name="Message")
    message.fill("/help")
    message.press("Enter")
    expect(page.get_by_role("region", name="Commands")).to_be_visible()
    expect(chips(page)).to_have_count(0)
    # Tasks arrive while focus is on the help's Close button: they wait as a chip, the help stays a line and keeps focus.
    ui.emit("a", {"type": "tasks", "tasks": TASKS})
    expect(chips(page)).to_have_text(["Tasks 0/3"])
    expect(page.get_by_role("region", name="Commands")).to_be_visible()
    expect(page.get_by_role("button", name="Close commands")).to_be_focused()
    page.get_by_role("textbox", name="Message").focus()
    expect(primary(page).get_by_role("region", name="Tasks")).to_be_visible()  # Focus left: the tasks take the lead.
    expect(page.get_by_role("region", name="Commands")).to_be_visible()  # The help is still its own line.
    expect(chips(page)).to_have_count(0)


# No sideways scroll at phone width: the first screen, the chat with every bar, and every dialog.

def test_a_phone_chat_with_every_bar_fits(ui, page):
    start(ui, page, 390, 844, fields=MANY, meter=True)
    for chip in ("Tasks 0/3", "3 changes"):
        chips(page).filter(has_text=chip).click()
        assert ui.js("document.documentElement.scrollWidth <= innerWidth"), chip
        row = box(ui, ".status-row")
        assert row["right"] <= 390 and row["left"] >= 0, row
        assert row["bottom"] <= box(ui, ".chat-composer .composer")["top"] + 1
    assert box(ui, ".chat-composer .composer")["bottom"] <= 844


# L16: one dialog frame.

DIALOG_EVENTS = [{"id": "u1", "type": "user", "text": "First task", "in_wire": True}, {"id": "a1", "type": "assistant", "text": "First answer."},
                 {"id": "u2", "type": "user", "text": "Second task", "in_wire": True}, {"id": "a2", "type": "assistant", "text": "Second answer."}]
REWIND_PREVIEW = {"before_event_id": "u2", "conversation": {"possible": True, "reason": None, "events_removed": 2, "messages_removed": 2, "summary_dropped": False},
                  "code": {"possible": True, "reason": None, "files": [{"path": "a.txt", "status": "restore"}]}}
DIALOG_SETUP = f"""
fake.sessions = new Map([
  ['a', {{ ...makeSession('a', 'Work'), events: {json.dumps(DIALOG_EVENTS)}, terminal_reason: 'completed' }}],
  ['p', makeSession('p', 'Parent task')],
  ['c', {{ ...makeSession('c', 'Child task'), parent_session_id: 'p', is_subagent: true }}],
])
fake.override = path => path.startsWith('/api/sessions/a/rewind-preview?') ? fake.json({json.dumps(REWIND_PREVIEW)}) : undefined
"""
TRUST_FILE = {"layer": "project", "name": ".agents/settings.json", "sha256": "b" * 64, "values": {"max_agent_steps": 12}, "problems": [], "ignored": []}
TRUST_SETUP = f"""fake.projectLayers = {json.dumps({"workspace": "/project", "status": "pending", "changed": False, "decision": None, "sha256": "a" * 64,
                                                      "files": [TRUST_FILE], "values": {}, "sources": {}, "followed": [], "notes": []})}
fake.settings = {{ ...fake.settings, project_layers: fake.projectLayers }}"""
# kind: (dialog name, has a footer button row)
DIALOGS = {"settings": ("Settings", True), "tools": ("Agent tools", False), "folders": ("Folder access", True), "rules": ("Add a permission rule", True),
           "trust": ("Project settings files", True), "delete": ("Delete conversation", True), "fork": ("Fork conversation", True),
           "rewind": ("Rewind conversation", True), "checks": ("Check endpoint capabilities", True)}
# The endpoint checks open from Settings and sit above it: the one dialog that is nested in another.
NESTED = {"checks"}
CHECKS_SETUP = """
const planned = fake.override
fake.override = (path, options) => path === '/api/endpoints/plan' ? fake.json({ model: JSON.parse(options.body).model, probes: [
    { id: 'basic', title: 'Smoke test and usage fields', question: 'Does a small streamed request work?', requests: [
      { id: 'basic', label: 'One short streamed request', estimated_input_tokens: 381, max_tokens: 2048, estimated_dbu: 0.607 }] }],
  total_dbu: 0.607, priced: true, max_output_tokens: 2048, pricing_effective_at: '2026-10-13', plan_id: 'p' }) : planned(path, options)
"""


def start_dialogs(ui, page, width, kind="settings"):
    page.set_viewport_size({"width": width, "height": 844})
    ui.app(session="a", scenario="workspace", setup=DIALOG_SETUP + CHECKS_SETUP + (TRUST_SETUP if kind == "trust" else ""))
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    ui.show_session("a")
    expect(page.get_by_text("Second answer.")).to_be_visible()


def open_dialog(ui, page, kind):
    """Open one dialog the way a person does. Returns the control that opened it (None when it opens by itself or
    through a menu item that goes away)."""
    phone = page.viewport_size["width"] <= 600
    name = DIALOGS[kind][0]
    if kind in ("settings", "delete", "checks") and phone:
        page.get_by_role("button", name="Open sidebar").click()
    opener = None
    if kind == "settings":
        opener = page.get_by_role("button", name="Settings", exact=True)
    elif kind == "tools":
        opener = page.get_by_role("button", name="Agent tools", exact=True)
    elif kind == "folders":
        opener = page.get_by_role("button", name="Folder access", exact=True)
    elif kind == "fork":
        opener = page.get_by_role("button", name="Fork from here").first
    elif kind == "rewind":
        opener = page.get_by_role("button", name="Rewind to here").nth(1)
    elif kind == "checks":
        page.get_by_role("button", name="Settings", exact=True).click()
        expect(page.get_by_role("dialog", name="Settings")).to_be_visible()
        opener = page.get_by_role("dialog", name="Settings").get_by_role("button", name="Check endpoint capabilities…")
    elif kind == "rules":
        page.locator(".permission-trigger").click()
        page.get_by_role("menuitem", name="Add a rule…").click()
    elif kind == "delete":
        ui.row_action("Parent task", "Delete…")
    if opener is not None:
        opener.click()
    dialog = page.get_by_role("dialog", name=name)
    expect(dialog).to_be_visible()
    if kind == "rewind":  # Its choices arrive from the preview request: measure once they are there.
        expect(dialog.get_by_role("radio", name="Conversation only")).to_be_visible()
    return dialog, opener


def frame_measures(dialog):
    return dialog.evaluate("""d => {
      const get = (selector, props) => { const e = d.querySelector(selector); if (!e) return null
        const s = getComputedStyle(e); return props.map(p => s[p]) }
      return { radius: getComputedStyle(d).borderTopLeftRadius,
        header: get(':scope > .dialog-frame > .dialog-header', ['paddingLeft', 'paddingTop']),
        body: get('.dialog-body', ['paddingLeft', 'overflowY']),
        footer: get(':scope > .dialog-frame > .dialog-footer', ['paddingLeft', 'paddingTop', 'columnGap', 'justifyContent']) }
    }""")


@pytest.mark.parametrize("kind", list(DIALOGS))
def test_every_dialog_has_the_shared_frame(ui, page, kind):
    start_dialogs(ui, page, 1280, kind)
    dialog, _ = open_dialog(ui, page, kind)
    frame = dialog.locator(":scope > .dialog-frame")
    expect(frame).to_have_count(1)
    header = frame.locator(":scope > header.dialog-header")
    expect(header.get_by_role("heading").first).to_be_visible()
    expect(header.get_by_role("button", name=re.compile(r"^(Close|Decide later)")).first).to_be_visible()
    body = frame.locator(".dialog-body")
    expect(body).to_be_visible()
    assert body.evaluate("e => ['auto', 'scroll'].includes(getComputedStyle(e).overflowY)")
    assert dialog.evaluate("d => d.scrollHeight <= d.clientHeight + 1")  # Only the body scrolls.
    head_box, body_box = header.bounding_box(), body.bounding_box()
    assert head_box["y"] + head_box["height"] <= body_box["y"] + 1
    footer = frame.locator(":scope > footer.dialog-footer")
    if DIALOGS[kind][1]:
        expect(footer).to_be_visible()
        foot = footer.bounding_box()
        assert body_box["y"] + body_box["height"] <= foot["y"] + 1
        buttons = footer.get_by_role("button")
        rights = [b.bounding_box()["x"] + b.bounding_box()["width"] for b in buttons.all()]
        primary_button = footer.locator(".primary").last
        if len(rights) > 1:  # The primary action is the right-most button.
            assert primary_button.bounding_box()["x"] + primary_button.bounding_box()["width"] >= max(rights) - 1, rights
        # The dialog sits inside the window.
        shown = dialog.bounding_box()
        assert shown["x"] >= 0 and shown["x"] + shown["width"] <= 1280 and shown["y"] >= 0 and shown["y"] + shown["height"] <= 844
    else:
        expect(footer).to_have_count(0)


def test_every_dialog_uses_the_same_radius_and_spacing(ui, page):
    start_dialogs(ui, page, 1280)
    token = page.evaluate("""() => { const e = document.createElement('div'); e.style.cssText = 'border-radius:var(--radius-l);padding-left:var(--space-5)'
      document.body.append(e); const s = getComputedStyle(e); const out = { radius: s.borderTopLeftRadius, space: s.paddingLeft }; e.remove(); return out }""")
    seen = {}
    for kind in DIALOGS:
        if kind == "trust":
            continue  # Opens by itself on a project with settings files: measured in its own test below.
        dialog, _ = open_dialog(ui, page, kind)
        measured = frame_measures(dialog)
        assert measured["radius"] == token["radius"], (kind, measured)
        seen[kind] = measured
        for _ in range(2 if kind in NESTED else 1):
            page.keyboard.press("Escape")
        expect(page.get_by_role("dialog")).to_have_count(0)
    assert len({tuple(m["header"]) for m in seen.values()}) == 1, seen
    assert len({m["body"][0] for m in seen.values()}) == 1, seen
    assert len({tuple(m["footer"]) for m in seen.values() if m["footer"]}) == 1, seen


def test_the_trust_dialog_has_the_same_radius(ui, page):
    start_dialogs(ui, page, 1280, "trust")
    dialog = page.get_by_role("dialog", name="Project settings files")
    expect(dialog).to_be_visible()
    radius = page.evaluate("() => { const e = document.createElement('div'); e.style.borderRadius = 'var(--radius-l)'; document.body.append(e); const v = getComputedStyle(e).borderTopLeftRadius; e.remove(); return v }")
    assert frame_measures(dialog)["radius"] == radius


@pytest.mark.parametrize("kind", list(DIALOGS))
def test_escape_closes_a_dialog_and_returns_focus(ui, page, kind):
    start_dialogs(ui, page, 1280, kind)
    dialog, opener = open_dialog(ui, page, kind)
    assert dialog.evaluate("d => d.contains(document.activeElement)")  # Focus is inside, on a modal.
    page.keyboard.press("Escape")
    if kind in NESTED:  # Only the top dialog closes; focus goes back to its opener inside the one below.
        expect(dialog).to_have_count(0)
        expect(page.get_by_role("dialog", name="Settings")).to_be_visible()
        expect(opener).to_be_focused()
        page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    if opener is not None and kind not in NESTED:
        expect(opener).to_be_focused()
    elif kind != "trust":  # A menu item that went away: focus goes to a control, never to the page's start.
        assert ui.js("document.activeElement !== document.body")


@pytest.mark.parametrize("kind", [k for k in DIALOGS if k != "trust"])
def test_focus_stays_inside_an_open_dialog(ui, page, kind):
    start_dialogs(ui, page, 1280, kind)
    dialog, _ = open_dialog(ui, page, kind)
    for key in ["Tab"] * 40 + ["Shift+Tab"] * 40:
        page.keyboard.press(key)
        assert dialog.evaluate("d => d.contains(document.activeElement)"), key


@pytest.mark.parametrize("kind", list(DIALOGS))
def test_every_dialog_fits_a_phone(ui, page, kind):
    start_dialogs(ui, page, 390, kind)
    dialog, _ = open_dialog(ui, page, kind)
    assert ui.js("document.documentElement.scrollWidth <= innerWidth")
    shown = dialog.bounding_box()
    assert shown["x"] >= 0 and shown["x"] + shown["width"] <= 390, shown
    assert dialog.evaluate("d => d.scrollWidth <= d.clientWidth + 1")
    assert dialog.locator(".dialog-body").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
    # The header's Close stays reachable at the top and the footer's buttons stay on screen.
    close = dialog.locator(":scope > .dialog-frame > .dialog-header").get_by_role("button", name=re.compile(r"^(Close|Decide later)")).first
    expect(close).to_be_in_viewport(ratio=1)
    if DIALOGS[kind][1]:
        expect(dialog.locator(":scope > .dialog-frame > .dialog-footer").get_by_role("button").first).to_be_in_viewport(ratio=1)


def test_a_dialog_with_sections_lists_them_beside_the_body_and_above_it_on_a_phone(ui, page):
    start_dialogs(ui, page, 1280)
    dialog, _ = open_dialog(ui, page, "settings")
    nav = dialog.get_by_role("navigation", name="Settings sections")
    expect(nav).to_be_visible()
    side, body = nav.bounding_box(), dialog.locator(".dialog-body").bounding_box()
    assert side["x"] + side["width"] <= body["x"] + 2, (side, body)
    page.keyboard.press("Escape")
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Open sidebar").click()
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.get_by_role("dialog", name="Settings")
    nav = dialog.get_by_role("navigation", name="Settings sections")
    expect(nav).to_be_visible()
    side, body = nav.bounding_box(), dialog.locator(".dialog-body").bounding_box()
    assert side["y"] + side["height"] <= body["y"] + 2, (side, body)
    assert ui.js("document.documentElement.scrollWidth <= innerWidth")


def test_escape_waits_while_settings_are_saving(ui, page):
    start_dialogs(ui, page, 1280)
    ui.run("const base = fake.override; fake.override = (path, options) => path === '/api/settings' && options.method === 'PUT' "
           "? new Promise(() => {}) : base(path, options)")
    dialog, _ = open_dialog(ui, page, "settings")
    dialog.get_by_role("button", name="Save settings").click()
    expect(dialog.get_by_role("button", name="Saving…")).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_visible()


def test_the_backdrop_click_each_dialog_has_today_is_kept(ui, page):
    start_dialogs(ui, page, 1280)
    for kind in ("settings", "tools", "folders", "rules", "fork", "rewind"):  # These close on a click outside.
        dialog, _ = open_dialog(ui, page, kind)
        page.mouse.click(4, 4)
        expect(page.get_by_role("dialog")).to_have_count(0)
    dialog, _ = open_dialog(ui, page, "delete")  # This one asks for a choice: a click outside decides nothing.
    page.mouse.click(4, 4)
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)


# L3: a dialog that is saving cannot be closed from its Close button or its Cancel.

def pending(route, method):
    return ("const before = fake.override; fake.override = (path, options) => " + f"{route} && options.method === '{method}' "
            "? new Promise(() => {}) : before(path, options)")


def test_close_and_cancel_wait_while_the_fork_is_made(ui, page):
    start_dialogs(ui, page, 1280)
    ui.run(pending("/\\/fork$/.test(path)", "POST"))
    dialog, _ = open_dialog(ui, page, "fork")
    dialog.get_by_role("button", name="Fork", exact=True).click()
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_disabled()
    expect(dialog.get_by_role("button", name="Cancel")).to_be_disabled()
    page.keyboard.press("Escape")
    page.mouse.click(4, 4)
    expect(dialog).to_be_visible()


def test_close_waits_while_a_chat_is_deleted(ui, page):
    start_dialogs(ui, page, 1280)
    ui.run(pending("/\\/sessions\\/p$/.test(path)", "DELETE"))
    dialog, _ = open_dialog(ui, page, "delete")
    dialog.get_by_role("button", name="Delete with subagents").click()
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_disabled()
    page.keyboard.press("Escape")
    expect(dialog).to_be_visible()


def test_close_waits_while_a_rewind_runs(ui, page):
    start_dialogs(ui, page, 1280)
    ui.run(pending("/\\/rewind$/.test(path)", "POST"))
    dialog, _ = open_dialog(ui, page, "rewind")
    dialog.get_by_role("button", name="Rewind", exact=True).click()
    expect(dialog.get_by_role("button", name="Close", exact=True)).to_be_disabled()
    expect(dialog.get_by_role("button", name="Cancel")).to_be_disabled()
    page.keyboard.press("Escape")
    expect(dialog).to_be_visible()


def test_close_and_cancel_wait_while_a_check_starts(ui, page):
    start_dialogs(ui, page, 1280)
    ui.run(pending("path === '/api/endpoints/run'", "POST"))
    dialog, opener = open_dialog(ui, page, "checks")
    dialog.get_by_role("button", name=re.compile(r"^Run \(")).click()
    expect(dialog.get_by_role("button", name="Close check endpoint capabilities")).to_be_disabled()
    expect(dialog.get_by_role("button", name="Cancel")).to_be_disabled()
    page.keyboard.press("Escape")
    expect(dialog).to_be_visible()


# L2: the endpoint checks sit above Settings. Escape closes only them, focus goes back inside Settings and stays in
# the top dialog, never on the page behind.

def test_the_endpoint_checks_trap_focus_and_leave_settings_open(ui, page):
    start_dialogs(ui, page, 1280)
    dialog, opener = open_dialog(ui, page, "checks")
    settings = page.get_by_role("dialog", name="Settings")
    for key in ["Tab"] * 30 + ["Shift+Tab"] * 30:
        page.keyboard.press(key)
        assert dialog.evaluate("d => d.contains(document.activeElement)"), key
    page.get_by_role("button", name="Close check endpoint capabilities").click()
    expect(dialog).to_have_count(0)
    expect(settings).to_be_visible()
    expect(opener).to_be_focused()
    for key in ["Tab"] * 12:  # And Settings traps it again.
        page.keyboard.press(key)
        assert settings.evaluate("d => d.contains(document.activeElement)"), key
