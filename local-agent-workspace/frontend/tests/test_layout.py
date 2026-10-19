"""Shell layout: Workspace panel tiers (beside the chat, or a drawer with a scrim below 1,024 px), a top bar that
fits its column, the changes summary, the empty welcome and the context details popover."""
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


def start(ui, page, width, height, meter=False, files=3):
    """Chat 'a' with a reply, pushed changes and a task list, at this viewport."""
    page.set_viewport_size({"width": width, "height": height})
    extra = f", context_info: {json.dumps(CONTEXT_INFO)}" if meter else ""
    ui.app(session="a", scenario="workspace", setup=CHAT % extra)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    ui.show_session("a")
    changed = ", ".join(f"changedFile('src/module_{n}/file_{n}.py', {n + 1}, {n})" for n in range(files))
    ui.run(f"fake.socketFor('a').emit({{ type: 'changes', changes: makeChanges({changed}) }})")
    ui.emit("a", {"type": "tasks", "tasks": TASKS})
    expect(page.get_by_role("region", name="Tasks")).to_be_visible()


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
    page.get_by_role("button", name=re.compile("files changed")).click()
    page.get_by_role("button", name=re.compile(r"^Tasks 0\/3 done")).click()
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
    """A long changes list and the task list scroll inside the bars, which take at most 40% of the window."""
    start(ui, page, width, height, meter=True, files=12)
    page.get_by_role("button", name=re.compile("files changed")).click()
    page.get_by_role("button", name=re.compile(r"^Tasks 0\/3 done")).click()
    page.locator(".context-meter > summary").click()
    expect(page.get_by_role("button", name="Compact now")).to_be_attached()
    bars = ui.js("(e => ({ height: e.getBoundingClientRect().height, scrolls: e.scrollHeight > e.clientHeight }))(document.querySelector('.composer-bars'))")
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
