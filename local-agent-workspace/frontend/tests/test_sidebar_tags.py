"""Filtering conversations by tag: the Tags menu, row tags and '#tag' in the search box."""
import json
import re

from playwright.sync_api import expect


def sessions(*items):
    rows = ", ".join(f"[{json.dumps(id)}, {{ ...makeSession({json.dumps(id)}, {json.dumps(title)}), ...{json.dumps(extra)} }}]"
                     for id, title, extra in items)
    return f"fake.sessions = new Map([{rows}])\n"


CHATS = sessions(("a", "Parser work", {"tags": ["bug", "parser"]}), ("b", "Docs work", {"tags": ["docs"]}),
                 ("c", "UI work", {"tags": ["Bug"]}), ("d", "Untagged chat", {}))


def start(ui, page, setup=CHATS):
    ui.app(scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")


def titles(page):
    return page.locator(".conversation-row .conversation-name")


def test_the_tags_menu_lists_every_tag_with_counts_and_filters_by_any_checked_tag(ui, page):
    start(ui, page)
    expect(titles(page)).to_have_count(4)
    toggle = page.get_by_role("button", name="Tags", exact=True)
    expect(toggle).to_have_attribute("aria-pressed", "false")
    toggle.click()
    menu = page.get_by_role("group", name="Filter by tag")
    # Tags that differ only in case are one tag; the most used comes first.
    expect(menu.locator("label")).to_have_text([re.compile("^bug\\s*2$"), re.compile("^docs\\s*1$"), re.compile("^parser\\s*1$")])
    expect(menu.get_by_role("checkbox", name="bug")).to_be_focused()
    menu.get_by_role("checkbox", name="bug").check()
    expect(titles(page)).to_have_text(["Parser work", "UI work"])
    menu.get_by_role("checkbox", name="docs").check()
    expect(titles(page)).to_have_text(["Parser work", "Docs work", "UI work"])
    expect(page.locator(".tag-filter")).to_contain_text("Tags: bug, docs")
    expect(page.get_by_role("button", name="Tags (2)")).to_have_attribute("aria-pressed", "true")
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    expect(page.get_by_role("button", name="Tags (2)")).to_be_focused()
    page.get_by_role("button", name="Clear tag filter").click()
    expect(titles(page)).to_have_count(4)


def test_clicking_a_rows_tag_adds_it_to_the_filter(ui, page):
    start(ui, page)
    page.locator(".tag-chip", has_text=re.compile("^parser$")).click()
    expect(page.locator(".tag-filter")).to_contain_text("Tag: parser")
    expect(titles(page)).to_have_text(["Parser work"])
    page.get_by_role("button", name="Tags (1)").click()
    expect(page.get_by_role("checkbox", name="parser")).to_be_checked()


def test_hash_words_in_the_search_box_filter_by_tag_and_are_not_searched_in_messages(ui, page):
    start(ui, page, CHATS + "fake.override = path => path.startsWith('/api/session-search') ? fake.json({ results: [], truncated: false }) : undefined")
    search = page.get_by_role("searchbox", name="Search conversations")
    expect(search).to_have_attribute("placeholder", "Search conversations or #tag")
    search.fill("#bu")
    expect(titles(page)).to_have_text(["Parser work", "UI work"])
    search.fill("#bug ui")
    expect(titles(page)).to_have_text(["UI work"])
    search.fill("#nothing")
    expect(titles(page)).to_have_count(0)
    expect(page.get_by_text("No conversation matches.")).to_be_visible()
    page.wait_for_timeout(500)
    assert not [call for call in ui.js("fake.calls.map(call => call.path)") if "search" in call and "%23" in call]


def test_without_tags_there_is_no_tags_menu(ui, page):
    start(ui, page, sessions(("a", "Plain chat", {})))
    expect(page.get_by_role("button", name="Running")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("^Tags"))).to_have_count(0)
