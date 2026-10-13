"""Sidebar organisation: project groups, subagents, pin, archive, tags, search and delete with subagents."""
import json
import re

from playwright.sync_api import expect


def sessions(*items):
    """Replace the fake conversation list: items are (id, title, extra fields)."""
    rows = ", ".join(f"[{json.dumps(id)}, {{ ...makeSession({json.dumps(id)}, {json.dumps(title)}), ...{json.dumps(extra)} }}]"
                     for id, title, extra in items)
    return f"fake.sessions = new Map([{rows}])\n"


def start(ui, page, setup="", session=None):
    ui.app(session=session, scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")


def row(page, title):
    return page.get_by_role("button", name=re.compile(f"^{re.escape(title)}"))


def group(page, name):
    return page.locator(".project-group-toggle", has_text=re.compile(f"^{re.escape(name)}"))


def paths(ui, pattern, method=None):
    """Paths of the requests matching a JavaScript regular expression."""
    return ui.js(f"fake.requests({pattern}, {json.dumps(method)}).map(call => call.path)")


def menu(page, title, item):
    page.get_by_role("button", name=f"More actions for {title}").click()
    page.get_by_role("menuitem", name=item, exact=True).click()


OTHER = "/srv/other"


def test_chats_are_grouped_by_project_with_the_current_one_first(ui, page):
    start(ui, page, sessions(
        ("c", "Other idle", {"workspace": OTHER, "updated": 9}),
        ("a", "Here first", {}),
        ("d", "Other waiting", {"workspace": OTHER, "status": "awaiting_approval"}),
        ("e", "Other busy", {"workspace": OTHER, "status": "running"}),
        ("b", "Here second", {}),
    ))
    toggles = page.locator(".project-group-toggle")
    expect(toggles).to_have_count(2)
    expect(toggles.nth(0)).to_contain_text("project")
    expect(toggles.nth(0)).to_have_attribute("aria-expanded", "true")
    expect(toggles.nth(1)).to_have_attribute("aria-expanded", "false")
    expect(toggles.nth(1)).to_contain_text("other")
    expect(toggles.nth(1)).to_contain_text("3")
    expect(toggles.nth(1).locator(".project-badge")).to_have_text(["1 needs approval", "1 running"])
    expect(page.locator(".conversation-row")).to_have_count(2)
    expect(row(page, "Here first")).to_be_visible()
    expect(row(page, "Other idle")).to_have_count(0)
    toggles.nth(1).click()
    expect(toggles.nth(1)).to_have_attribute("aria-expanded", "true")
    expect(row(page, "Other idle")).to_be_visible()
    expect(toggles.nth(1).locator(".project-badge")).to_have_count(0)
    expect(page.locator(".conversation-row")).to_have_count(5)
    toggles.nth(0).click()
    expect(row(page, "Here first")).to_have_count(0)


def test_worktree_chats_group_under_their_source_project_with_a_branch_badge(ui, page):
    start(ui, page, sessions(
        ("w", "Worktree chat", {"workspace": "/state/worktrees/abc",
                                "worktree": {"id": "abc", "branch": "agent/fix-parser", "source_workspace": "/project"}}),
        ("a", "Plain chat", {}),
    ))
    expect(page.locator(".project-group-toggle")).to_have_count(1)
    expect(page.locator(".conversation-row[data-session-id=w] + .row-tags .branch-badge")).to_have_text("agent/fix-parser")
    expect(page.locator(".branch-badge")).to_have_count(1)


def test_a_worktree_chat_with_badges_keeps_its_title_visible(ui, page):
    worktree = {"workspace": "/state/worktrees/abc", "unreviewed_changes": 2,
                "worktree": {"id": "abc", "branch": "agent/fix-the-parser-for-nested-arrays", "source_workspace": "/project"}}
    start(ui, page, sessions(("w", "Fix the parser for nested arrays", {**worktree, "status": "awaiting_approval"}),
                             ("x", "Worktree idle chat", worktree), ("a", "Plain chat", {})))
    def check():
        for title in ("Fix the parser for nested arrays", "Worktree idle chat"):
            main = row(page, title)
            assert main.locator(".conversation-name").evaluate("el => el.getBoundingClientRect().width") > 36
            # Nothing spills out of the row's main button under the row's other buttons.
            assert main.evaluate("el => el.scrollWidth <= el.clientWidth")
    check()
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("button", name="Open sidebar").click()
    expect(row(page, "Plain chat")).to_be_visible()
    check()
    page.set_viewport_size({"width": 1280, "height": 900})
    expect(row(page, "Fix the parser for nested arrays").locator(".row-badge")).to_have_text("Needs approval")


def test_subagent_chats_nest_under_their_parent(ui, page):
    start(ui, page, sessions(
        ("child", "Child task", {"parent_session_id": "parent", "is_subagent": True}),
        ("parent", "Parent task", {}),
        ("orphan", "Orphan task", {"parent_session_id": "gone", "is_subagent": True}),
        ("kept", "Detached task", {"parent_session_id": "parent", "is_subagent": True, "parent_deleted": True}),
    ))
    expect(row(page, "Child task")).to_have_count(0)
    expect(row(page, "Orphan task")).to_be_visible()
    expect(row(page, "Detached task")).to_be_visible()
    toggle = page.get_by_role("button", name="Show 1 subagent")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.click()
    expect(row(page, "Child task")).to_be_visible()
    hide = page.get_by_role("button", name="Hide subagents")
    expect(hide).to_have_attribute("aria-expanded", "true")
    hide.click()
    expect(row(page, "Child task")).to_have_count(0)


def test_a_working_subagent_is_shown_without_a_click(ui, page):
    start(ui, page, sessions(
        ("child", "Child task", {"parent_session_id": "parent", "is_subagent": True, "status": "running"}),
        ("parent", "Parent task", {"status": "delegating"}),
    ))
    expect(row(page, "Child task")).to_be_visible()
    expect(page.get_by_role("button", name="Hide subagents")).to_have_attribute("aria-expanded", "true")


def test_pin_moves_a_chat_to_pinned_and_archive_wins(ui, page):
    start(ui, page, sessions(("a", "First chat", {}), ("b", "Second chat", {}),
                             ("c", "Hidden pinned", {"pinned": True, "archived": True})))
    expect(page.get_by_role("region", name="Pinned")).to_have_count(0)
    menu(page, "Second chat", "Pin")
    pinned = page.get_by_role("region", name="Pinned")
    expect(pinned.get_by_role("button", name=re.compile("^Second chat"))).to_be_visible()
    expect(pinned.locator(".conversation-row")).to_have_count(1)
    assert ui.bodies("/api/sessions/b/meta", "PUT") == [{"pinned": True}]
    menu(page, "Second chat", "Unpin")
    expect(page.get_by_role("region", name="Pinned")).to_have_count(0)
    assert ui.bodies("/api/sessions/b/meta", "PUT")[-1] == {"pinned": False}


def test_a_pinned_subagent_is_listed_under_pinned(ui, page):
    start(ui, page, sessions(("child", "Child task", {"parent_session_id": "parent", "is_subagent": True, "pinned": True}),
                             ("kid", "Second child", {"parent_session_id": "parent", "is_subagent": True}),
                             ("parent", "Parent task", {})))
    pinned = page.get_by_role("region", name="Pinned")
    expect(pinned.get_by_role("button", name=re.compile("^Child task"))).to_be_visible()
    page.get_by_role("button", name="Show 1 subagent").click()
    menu(page, "Second child", "Pin")
    expect(pinned.locator(".conversation-row")).to_have_count(2)
    expect(pinned.get_by_role("button", name=re.compile("^Second child"))).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("^(Show|Hide) .*subagent"))).to_have_count(0)
    menu(page, "Second child", "Unpin")
    expect(pinned.locator(".conversation-row")).to_have_count(1)
    expect(page.locator(".subagent-rows").get_by_role("button", name=re.compile("^Second child"))).to_be_visible()


def test_keyboard_pin_and_archive_keep_focus_on_the_moved_chat(ui, page):
    start(ui, page, sessions(("a", "First chat", {}), ("b", "Second chat", {})))
    page.get_by_role("button", name="More actions for Second chat").focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("menuitem", name="Pin", exact=True)).to_be_focused()
    page.keyboard.press("Enter")
    pinned = page.get_by_role("region", name="Pinned")
    expect(pinned.get_by_role("button", name="More actions for Second chat")).to_be_focused()
    # Archive hides the chat: focus goes to the Archived toggle.
    page.keyboard.press("Enter")
    page.keyboard.press("ArrowDown")
    expect(page.get_by_role("menuitem", name="Archive", exact=True)).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Archived (1)")).to_be_focused()
    page.keyboard.press("Enter")
    page.get_by_role("region", name="Archived").get_by_role("button", name="More actions for Second chat").focus()
    page.keyboard.press("Enter")
    page.keyboard.press("Enter")  # Unarchive, the first item of an archived chat's menu.
    expect(page.get_by_role("region", name="Pinned").get_by_role("button", name="More actions for Second chat")).to_be_focused()


def test_the_row_menu_works_with_the_keyboard(ui, page):
    start(ui, page, sessions(("a", "First chat", {}), ("b", "Second chat", {})))
    trigger = page.get_by_role("button", name="More actions for First chat")
    trigger.focus()
    page.keyboard.press("Enter")
    item = lambda name: page.get_by_role("menuitem", name=name, exact=True)
    expect(item("Pin")).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(item("Archive")).to_be_focused()
    page.keyboard.press("End")
    expect(item("Edit tags…")).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(item("Pin")).to_be_focused()
    page.keyboard.press("ArrowUp")
    expect(item("Edit tags…")).to_be_focused()
    page.keyboard.press("Home")
    expect(item("Pin")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.get_by_role("menu")).to_have_count(0)
    expect(trigger).to_be_focused()
    # Tab leaves and closes the menu; so does focus moving elsewhere.
    page.keyboard.press("Enter")
    page.keyboard.press("Tab")
    expect(page.get_by_role("menu")).to_have_count(0)
    trigger.click()
    expect(page.get_by_role("menu")).to_have_count(1)
    page.get_by_role("searchbox", name="Search conversations").focus()
    expect(page.get_by_role("menu")).to_have_count(0)
    assert ui.requests("/api/sessions/a/meta", "PUT") == []


def test_archive_hides_a_chat_until_archived_is_opened(ui, page):
    start(ui, page, sessions(("a", "Keep chat", {}), ("b", "Old chat", {})))
    menu(page, "Old chat", "Archive")
    expect(row(page, "Old chat")).to_have_count(0)
    assert ui.bodies("/api/sessions/b/meta", "PUT") == [{"archived": True}]
    archived = page.get_by_role("button", name="Archived (1)")
    expect(archived).to_have_attribute("aria-expanded", "false")
    archived.click()
    section = page.get_by_role("region", name="Archived")
    expect(section.get_by_role("button", name=re.compile("^Old chat"))).to_be_visible()
    menu(page, "Old chat", "Unarchive")
    assert ui.bodies("/api/sessions/b/meta", "PUT")[-1] == {"archived": False}
    expect(page.get_by_role("button", name=re.compile("^Archived"))).to_have_count(0)
    expect(row(page, "Old chat")).to_be_visible()


def test_a_meta_change_from_the_socket_updates_the_list(ui, page):
    start(ui, page, sessions(("a", "Open chat", {}), ("b", "Other chat", {})), session="a")
    ui.show_session("a")
    ui.emit("a", {"type": "meta", "pinned": True, "archived": False, "tags": ["live"]})
    expect(page.get_by_role("region", name="Pinned").get_by_role("button", name=re.compile("^Open chat"))).to_be_visible()
    expect(page.locator(".tag-chip")).to_have_text(["live"])


def test_tags_are_edited_and_filter_the_list(ui, page):
    start(ui, page, sessions(("a", "Parser work", {"tags": ["bug"]}), ("b", "Docs work", {}), ("c", "UI work", {})))
    menu(page, "Docs work", "Edit tags…")
    field = page.get_by_role("textbox", name="Tags (comma-separated)")
    expect(field).to_be_focused()
    field.fill("Bug, docs ,")
    page.get_by_role("button", name="Save tags").click()
    assert ui.bodies("/api/sessions/b/meta", "PUT") == [{"tags": ["Bug", "docs"]}]
    expect(page.get_by_role("textbox", name="Tags (comma-separated)")).to_have_count(0)
    page.locator(".tag-chip", has_text=re.compile("^docs$")).click()
    expect(page.locator(".tag-filter")).to_contain_text("Tag: docs")
    expect(page.locator(".conversation-row")).to_have_count(1)
    page.get_by_role("button", name="Clear tag filter").click()
    expect(page.locator(".conversation-row")).to_have_count(3)
    # Tags match without regard to case.
    page.locator(".tag-chip", has_text=re.compile("^bug$")).click()
    expect(page.locator(".conversation-row")).to_have_count(2)
    # Cancel leaves the tags as they were.
    page.get_by_role("button", name="Clear tag filter").click()
    menu(page, "UI work", "Edit tags…")
    page.get_by_role("textbox", name="Tags (comma-separated)").fill("x")
    page.get_by_role("button", name="Cancel", exact=True).click()
    assert len(ui.bodies("/api/sessions/c/meta", "PUT")) == 0


def test_a_rejected_tag_shows_the_reason(ui, page):
    start(ui, page, sessions(("a", "Chat", {})) + """fake.override = (path, options) => path === '/api/sessions/a/meta'
      ? fake.json({ detail: 'Tags have 1 to 32 characters and no control characters.' }, 400) : undefined""")
    menu(page, "Chat", "Edit tags…")
    page.get_by_role("textbox", name="Tags (comma-separated)").fill("x" * 40)
    page.get_by_role("button", name="Save tags").click()
    expect(page.get_by_role("alert")).to_have_text("Tags have 1 to 32 characters and no control characters.")


def test_more_than_ten_tags_are_refused_before_saving(ui, page):
    start(ui, page, sessions(("a", "Chat", {})))
    menu(page, "Chat", "Edit tags…")
    field = page.get_by_role("textbox", name="Tags (comma-separated)")
    field.fill(", ".join(f"t{number}" for number in range(11)))
    page.get_by_role("button", name="Save tags").click()
    expect(page.get_by_role("alert")).to_have_text("Use at most 10 tags.")
    assert ui.requests("/api/sessions/a/meta", "PUT") == []
    # Tags that differ only in case count once, as on the server.
    field.fill(", ".join(f"t{number}" for number in range(10)) + ", T0,  t1 ")
    expect(page.get_by_role("alert")).to_have_count(0)
    page.get_by_role("button", name="Save tags").click()
    assert ui.bodies("/api/sessions/a/meta", "PUT") == [{"tags": [f"t{number}" for number in range(10)]}]


def test_a_header_whose_tags_are_not_a_list_does_not_break_the_list(ui, page):
    start(ui, page, sessions(("a", "Hand edited", {"tags": "bug", "worktree": {"branch": 7}}), ("b", "Other chat", {"tags": ["ok", 3]})))
    expect(page.locator(".conversation-row")).to_have_count(2)
    expect(page.locator(".tag-chip")).to_have_text(["ok"])
    expect(page.locator(".branch-badge")).to_have_count(0)
    page.locator(".tag-chip").click()
    expect(page.locator(".conversation-row")).to_have_count(1)


SEARCH = """fake.override = path => path.startsWith('/api/session-search?') ? fake.json({ truncated: TRUNCATED,
  results: path.includes('needle') ? [{ id: 'b', title: 'Second chat', updated: 2, workspace: '/project', archived: false,
    matches: [{ event_id: 'e1', role: 'user', snippet: 'the needle is here' }] }] : [] }) : undefined
"""


def test_titles_filter_instantly_and_messages_are_searched_after_a_pause(ui, page):
    start(ui, page, sessions(("a", "Parser rewrite", {}), ("b", "Second chat", {}), ("c", "Parser notes", {}))
          + SEARCH.replace("TRUNCATED", "false"))
    search = page.get_by_role("searchbox", name="Search conversations")
    search.fill("ne")
    page.wait_for_timeout(600)
    assert paths(ui, "/session-search/") == []  # Too short for a message search.
    search.fill("pars")
    expect(page.locator(".conversation-row")).to_have_count(2)
    search.fill("parser NOTES")
    expect(page.locator(".conversation-row")).to_have_count(1)
    expect(row(page, "Parser notes")).to_be_visible()
    page.wait_for_timeout(500)
    ui.run("fake.calls.length = 0")
    search.fill("needle")
    found = page.get_by_role("region", name="Found in messages")
    expect(found).to_contain_text("the needle is here")
    assert paths(ui, "/session-search/") == ["/api/session-search?q=needle"]
    expect(page.get_by_text("Search stopped early; refine your words.")).to_have_count(0)
    found.get_by_role("button", name=re.compile("^Second chat")).click()
    ui.wait("location.search === '?session=b'")
    search.press("Escape")
    expect(search).to_have_value("")
    expect(page.locator(".conversation-row")).to_have_count(3)
    expect(page.get_by_role("region", name="Found in messages")).to_have_count(0)


def test_messages_are_searched_once_typing_pauses(ui, page):
    ui.app(scenario="workspace", clock=True, setup=sessions(("a", "Parser rewrite", {}), ("b", "Second chat", {}))
           + SEARCH.replace("TRUNCATED", "false"))
    search = page.get_by_role("searchbox", name="Search conversations")
    search.focus()
    # Each change comes less than 400 ms after the one before (the clock also runs on its own).
    for text in ("nee", "need", "needl", "needle"):
        search.fill(text)
        ui.tick(250)
    assert paths(ui, "/session-search/") == []
    ui.tick(300)
    ui.wait("fake.requests(/session-search/).length === 1")
    expect(page.get_by_role("region", name="Found in messages")).to_contain_text("the needle is here")
    ui.tick(1000)
    assert paths(ui, "/session-search/") == ["/api/session-search?q=needle"]


TITLE_HIT = """fake.override = path => path.startsWith('/api/session-search?') ? fake.json({ truncated: false, results: [
  { id: 'a', title: 'Needle in the title', updated: 3, workspace: '/project', archived: false, matches: [] },
  ...(path.includes('needle') ? [{ id: 'b', title: 'Second chat', updated: 2, workspace: '/project', archived: false,
    matches: [{ event_id: 'e1', role: 'user', snippet: 'the needle is here' }] }] : [])] }) : undefined
"""


def test_found_in_messages_lists_only_message_matches(ui, page):
    start(ui, page, sessions(("a", "Needle in the title", {}), ("b", "Second chat", {})) + TITLE_HIT)
    search = page.get_by_role("searchbox", name="Search conversations")
    search.fill("needle")
    found = page.get_by_role("region", name="Found in messages")
    expect(found).to_contain_text("the needle is here")
    expect(found.locator(".found-result")).to_have_count(1)
    expect(found.get_by_role("button", name=re.compile("^Needle in the title"))).to_have_count(0)
    expect(page.locator(".conversation-row")).to_have_count(1)
    # A title-only hit is not a message match.
    search.fill("in the title")
    expect(page.locator(".conversation-row")).to_have_count(1)
    expect(found).to_contain_text("No messages match.")


def test_the_empty_note_does_not_contradict_message_matches(ui, page):
    start(ui, page, sessions(("a", "Alpha", {}), ("b", "Second chat", {})) + SEARCH.replace("TRUNCATED", "false"))
    search = page.get_by_role("searchbox", name="Search conversations")
    search.fill("zzz")
    expect(page.get_by_text("No titles match.")).to_be_visible()
    search.fill("needle")
    expect(page.get_by_role("region", name="Found in messages")).to_contain_text("the needle is here")
    expect(page.locator(".conversation-list .empty-sessions")).to_have_count(0)
    page.get_by_role("button", name="Running", exact=True).click()
    search.fill("")
    expect(page.get_by_text("No conversation is running.")).to_be_visible()


def test_a_truncated_search_says_so(ui, page):
    start(ui, page, sessions(("a", "Chat", {})) + SEARCH.replace("TRUNCATED", "true"))
    page.get_by_role("searchbox", name="Search conversations").fill("needle")
    expect(page.get_by_text("Search stopped early; refine your words.")).to_be_visible()


def test_deleting_a_chat_with_subagents_asks_what_to_do(ui, page):
    start(ui, page, sessions(("p", "Parent task", {}), ("c", "Child task", {"parent_session_id": "p", "is_subagent": True}),
                             ("q", "Second parent", {}), ("d", "Child two", {"parent_session_id": "q", "is_subagent": True})))
    page.get_by_role("button", name="Delete Parent task").click()
    dialog = page.get_by_role("dialog", name="Delete conversation")
    expect(dialog).to_contain_text("Delete “Parent task”? It has 1 subagent conversation. Your project files will remain.")
    assert ui.js("fake.confirms") == []
    dialog.get_by_role("button", name="Delete with subagents").click()
    expect(dialog).to_have_count(0)
    assert paths(ui, "/^\\/api\\/sessions\\/p/", "DELETE") == ["/api/sessions/p?children=delete"]

    page.get_by_role("button", name="Delete Second parent").click()
    page.get_by_role("dialog", name="Delete conversation").get_by_role("button", name="Delete, keep subagents").click()
    assert paths(ui, "/^\\/api\\/sessions\\/q/", "DELETE") == ["/api/sessions/q"]

    expect(row(page, "Child task")).to_be_visible()
    page.get_by_role("button", name="Delete Child task").click()  # No subagents: the usual question.
    assert ui.js("fake.confirms") == ["Delete this conversation? Your project files will remain."]
    assert paths(ui, "/^\\/api\\/sessions\\/c/", "DELETE") == ["/api/sessions/c"]


def test_cancel_keeps_the_chat(ui, page):
    start(ui, page, sessions(("p", "Parent task", {}), ("c", "Child task", {"parent_session_id": "p"})))
    page.get_by_role("button", name="Delete Parent task").click()
    page.get_by_role("dialog", name="Delete conversation").get_by_role("button", name="Cancel").click()
    expect(page.get_by_role("dialog", name="Delete conversation")).to_have_count(0)
    assert paths(ui, "/^\\/api\\/sessions\\/p/", "DELETE") == []


def test_unreviewed_changes_show_a_dot(ui, page):
    start(ui, page, sessions(("a", "Changed chat", {"unreviewed_changes": 2}), ("b", "Clean chat", {"unreviewed_changes": 0}),
                             ("c", "One change", {"unreviewed_changes": 1})))
    dot = row(page, "Changed chat").locator(".changes-dot")
    expect(dot).to_have_attribute("title", "2 files with unreviewed agent changes")
    expect(dot).to_have_text("Unreviewed changes")
    expect(row(page, "One change").locator(".changes-dot")).to_have_attribute("title", "1 file with unreviewed agent changes")
    expect(row(page, "Clean chat").locator(".changes-dot")).to_have_count(0)


MANY = """fake.sessions = new Map(Array.from({ length: 500 }, (_, index) =>
  [`s${index}`, { ...makeSession(`s${index}`, `Chat ${index}`), updated: 1000 - index }]))
"""


def test_five_hundred_chats_render_and_filter_quickly(ui, page):
    start(ui, page, MANY)
    expect(page.locator(".conversation-row")).to_have_count(50)
    expect(page.locator(".project-group-toggle")).to_contain_text("500")
    page.get_by_role("searchbox", name="Search conversations").fill("chat 499")
    expect(page.locator(".conversation-row")).to_have_count(1)
    expect(row(page, "Chat 499")).to_be_visible()
    page.get_by_role("searchbox", name="Search conversations").fill("")
    page.get_by_role("button", name="Show 450 more").click()
    expect(page.locator(".conversation-row")).to_have_count(500)
