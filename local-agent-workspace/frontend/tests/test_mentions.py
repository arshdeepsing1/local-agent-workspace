"""@ file mentions in the message box: the Files menu, inserted paths and the chips above the box."""
import re

from playwright.sync_api import expect

FILES = """fake.override = path => {
  const query = new URL(path, location.origin).searchParams.get('q')
  if (path.startsWith('/api/sessions/a/mention-files?') || path.startsWith('/api/mention-files?')) {
    const all = [{ path: 'src/parser.py', name: 'parser.py' }, { path: 'docs/parsing.md', name: 'parsing.md' },
                 { path: 'README.md', name: 'README.md' }]
    return fake.json(all.filter(file => file.path.toLowerCase().includes(query.toLowerCase())))
  }
}"""


def start_app(ui, page, session="a"):
    ui.app(session=session, scenario="workspace", setup=FILES)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    if session:
        ui.show_session(session)


def message(page):
    return page.get_by_role("textbox", name="Message")


def files(page):
    return page.get_by_role("listbox", name="Files").get_by_role("option")


def test_the_menu_filters_files_and_inserts_the_chosen_path(ui, page):
    start_app(ui, page)
    box = message(page)
    box.press_sequentially("Compare @pars")
    expect(files(page)).to_have_count(2)
    expect(files(page).nth(0)).to_contain_text("parser.py")
    expect(files(page).nth(0)).to_contain_text("src/parser.py")
    queries = [path for path in ui.js("fake.calls.map(call => call.path)") if re.match(r"/api/sessions/a/mention-files\?", path)]
    assert queries[-1] == "/api/sessions/a/mention-files?q=pars"
    assert len(queries) <= 2  # Debounced: not one request per key.
    box.press("ArrowDown")
    expect(files(page).nth(1)).to_have_attribute("aria-selected", "true")
    expect(box).to_have_attribute("aria-activedescendant", files(page).nth(1).get_attribute("id"))
    box.press("Enter")
    expect(box).to_have_value("Compare @docs/parsing.md ")
    expect(page.get_by_role("listbox", name="Files")).to_have_count(0)
    assert not ui.requests("/api/sessions/a/messages")
    box.press_sequentially("with @READ")
    expect(files(page)).to_have_count(1)
    box.press("Tab")
    expect(box).to_have_value("Compare @docs/parsing.md with @README.md ")
    box.press_sequentially("and @x")
    expect(page.get_by_role("status").filter(has_text="No file matches @x.")).to_be_visible()
    box.press("Escape")
    expect(page.locator(".mention-menu")).to_have_count(0)
    expect(box).to_have_value("Compare @docs/parsing.md with @README.md and @x")
    assert not ui.requests("/api/sessions/a/stop")


def test_chips_show_each_mention_and_remove_its_token(ui, page):
    start_app(ui, page)
    box = message(page)
    box.fill("Read @src/parser.py:3-40, then @README.md and `@not/this` or a@b.c")
    chips = page.locator(".mention-chip")
    expect(chips).to_have_count(2)
    expect(chips.nth(0)).to_contain_text("@src/parser.py:3-40")
    expect(chips.nth(0)).to_have_attribute("title", "@src/parser.py:3-40\nA snapshot is attached when you send (files only)")
    expect(chips.nth(1)).to_contain_text("@README.md")
    page.get_by_role("button", name="Remove @src/parser.py").click()
    expect(box).to_have_value("Read , then @README.md and `@not/this` or a@b.c")
    page.get_by_role("button", name="Remove @README.md").click()
    expect(box).to_have_value("Read , then and `@not/this` or a@b.c")
    expect(page.locator(".mention-chips")).to_have_count(0)
    box.press("Enter")
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/messages", "POST") == [{"text": "Read , then and `@not/this` or a@b.c"}]


def test_tokens_inside_code_spans_of_any_length_get_no_chip(ui, page):
    """As on the server: a span opened by N backticks closes only at the next run of exactly N, as in the
    line text a review comment quotes."""
    start_app(ui, page)
    message(page).fill("- `README.md:4` (added line: `` Run `make` then @docs/parsing.md ``): see @src/parser.py")
    expect(page.locator(".mention-chip")).to_have_count(1)
    expect(page.locator(".mention-chip")).to_contain_text("@src/parser.py")


def test_removing_a_chip_keeps_the_rest_of_the_message_as_typed(ui, page):
    start_app(ui, page)
    box = message(page)
    box.fill("Fix @src/a.py so that\n```python\ndef f():\n    return 1\n```\nTable:  a  b\nSee @src/a.py\n@src/a.py first")
    page.get_by_role("button", name="Remove @src/a.py").click()
    expect(box).to_have_value("Fix so that\n```python\ndef f():\n    return 1\n```\nTable:  a  b\nSee\nfirst")


def test_a_new_conversation_searches_the_default_project(ui, page):
    start_app(ui, page, session=None)
    message(page).press_sequentially("@read")
    expect(files(page)).to_have_count(1)
    assert ui.requests("/api/mention-files?q=read")


def test_long_mention_chip_is_one_line(ui, page):
    """A long path shortens its folder with an ellipsis and keeps the file name whole (one line, not five)."""
    page.set_viewport_size({"width": 390, "height": 844})
    start_app(ui, page)
    folder = "src/" + "/".join(f"very_long_directory_name_{n}" for n in range(4)) + "/"
    name = "configuration_file_with_a_long_name.py"
    path = folder + name
    assert len(path) >= 140
    message(page).fill(f"Read @{path} and @README.md")
    chips = page.locator(".mention-chip")
    expect(chips).to_have_count(2)
    composer = page.locator(".composer").bounding_box()
    for index in range(2):
        box = chips.nth(index).bounding_box()
        assert box["height"] <= 32 and box["x"] + box["width"] <= composer["x"] + composer["width"] + 0.5, box
    expect(chips.nth(0)).to_have_attribute("title", re.compile(f"^@{re.escape(path)}\n"))
    expect(chips.nth(0).locator(".mention-base")).to_have_text(name)
    assert ui.js("(e => e.scrollWidth > e.clientWidth)(document.querySelector('.mention-dir'))")  # The folder is what shortens.
    expect(chips.nth(1).locator(".mention-base")).to_have_text("@README.md")
