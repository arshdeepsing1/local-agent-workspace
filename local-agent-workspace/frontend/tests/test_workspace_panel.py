"""The Workspace panel's frame: resizing and expanding it, the worktree header, and files that the
agent changes again after Keep, or that a reply names from another folder."""
import re

from playwright.sync_api import expect


def start_app(ui, page, setup="", session="a"):
    ui.app(session=session, scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    ui.show_session(session)


def panel(page):
    return page.get_by_role("complementary", name="Workspace panel")


def resizer(page):
    return page.get_by_role("separator", name="Resize workspace panel")


def width(page):
    return round(panel(page).bounding_box()["width"])


def push(ui, *files):
    ui.emit("a", f"{{ type: 'changes', changes: makeChanges({', '.join(files)}) }}")


def test_the_panel_resizes_by_dragging_or_with_the_keyboard_and_keeps_its_width(ui, page):
    start_app(ui, page)
    page.get_by_role("button", name="Workspace", exact=True).click()
    expect(panel(page)).to_be_visible()
    start = width(page)
    box = resizer(page).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + 200
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 150, y, steps=5)
    page.mouse.up()
    assert abs(width(page) - (start + 150)) <= 2
    expect(resizer(page)).to_have_attribute("aria-valuenow", str(width(page)))
    resizer(page).focus()
    page.keyboard.press("ArrowLeft")
    assert abs(width(page) - (start + 182)) <= 2
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    assert abs(width(page) - (start + 118)) <= 2
    page.keyboard.press("Home")
    assert width(page) == 320
    # The width is kept for this browser.
    saved = ui.js("localStorage.getItem('workspace-panel-width')")
    assert saved == "320"
    page.reload()
    page.wait_for_function("window.harnessReady === true")
    ui.page.evaluate("() => { mount('App.js', () => ({}), 'default') }")
    ui.show_session("a")
    page.get_by_role("button", name="Workspace", exact=True).click()
    expect(panel(page)).to_be_visible()
    assert width(page) == 320
    # Enter (or a double-click) restores the stylesheet's width.
    resizer(page).focus()
    page.keyboard.press("Enter")
    assert width(page) == start
    assert ui.js("localStorage.getItem('workspace-panel-width')") is None


def test_expand_makes_the_panel_as_wide_as_the_chat_allows_and_restore_undoes_it(ui, page):
    start_app(ui, page)
    page.get_by_role("button", name="Workspace", exact=True).click()
    start = width(page)
    page.get_by_role("button", name="Expand workspace").click()
    sidebar = round(page.locator(".app-shell > .sidebar").bounding_box()["width"])
    assert width(page) == 1280 - sidebar - 420
    expect(page.get_by_role("main")).to_be_visible()
    page.get_by_role("button", name="Restore workspace width").click()
    assert width(page) == start
    expect(page.get_by_role("button", name="Expand workspace")).to_be_visible()


def test_a_worktree_chat_names_its_project_and_branch_in_the_header(ui, page):
    start_app(ui, page, setup="""fake.sessions.set('w', { ...makeSession('w', 'Worktree chat'), workspace: '/state/worktrees/0b1c',
      worktree: { branch: 'chat/fix-login', source_workspace: '/code/shop' } })""", session="w")
    page.get_by_role("button", name="Workspace", exact=True).click()
    header = panel(page).locator("header")
    expect(header).to_contain_text("shop")
    expect(header.get_by_title("Worktree branch chat/fix-login")).to_be_visible()
    expect(header).not_to_contain_text("0b1c")


def test_a_file_the_agent_edits_again_after_keep_opens_its_review_again(ui, page):
    start_app(ui, page)
    push(ui, "changedFile('note.txt', 2, 1)")
    page.get_by_role("region", name="Changed files").get_by_role("button", name=re.compile("^1 file changed")).click()
    page.get_by_title("Review note.txt").click()
    review = page.get_by_role("region", name="Agent changes in note.txt")
    expect(review).to_be_visible()
    push(ui)  # Kept: the editor shows the file as it is now.
    expect(page.get_by_role("textbox", name="Edit note.txt")).to_be_visible()
    expect(review).to_have_count(0)
    push(ui, "{ ...changedFile('note.txt', 1, 1), current_hash: 'hash-2' }")  # The agent edits it again.
    expect(review).to_be_visible()


def test_an_open_file_without_edits_follows_the_agents_later_edits(ui, page):
    start_app(ui, page, setup="""let version = 0
      fake.override = path => path.startsWith('/api/file?') ? fake.json({ content: `version ${++version}` }) : undefined""")
    push(ui, "changedFile('note.txt', 2, 1)")
    page.get_by_role("button", name=re.compile("^Workspace")).click()
    page.get_by_role("button", name=re.compile("^note.txt")).click()
    page.get_by_role("button", name="Edit", exact=True).click()
    editor = page.get_by_role("textbox", name="Edit note.txt")
    expect(editor).to_have_value("version 2")
    push(ui, "{ ...changedFile('note.txt', 3, 1), current_hash: 'hash-2' }")
    expect(editor).to_have_value("version 3")
    editor.fill("my draft")
    push(ui, "{ ...changedFile('note.txt', 4, 1), current_hash: 'hash-3' }")
    expect(editor).to_have_value("my draft")  # Unsaved edits are never replaced.


def test_a_reply_that_names_a_file_from_another_folder_opens_the_file_the_server_found(ui, page):
    start_app(ui, page, setup="""fake.sessions.get('a').events = [
        { id: 'u1', type: 'user', text: 'Where is it?', created: 1, in_wire: true },
        { id: 'r1', type: 'assistant', text: 'It is in `project/note.txt` and `gone/missing.txt`.', created: 2 }]
      fake.override = path => path === '/api/file?path=project%2Fnote.txt&session_id=a'
        ? fake.json({ path: 'note.txt', content: 'found', absolute: '/project/note.txt' })
        : path === '/api/file?path=gone%2Fmissing.txt&session_id=a'
          ? fake.json({ detail: 'gone/missing.txt does not exist in /project. It may have been moved or deleted, or it was named relative to another folder.' }, 400)
          : undefined""")
    page.get_by_role("link", name="project/note.txt").click()
    expect(page.get_by_role("textbox", name="Edit note.txt")).to_have_value("found")
    expect(panel(page).locator(".editor-note")).to_have_text("Opened note.txt: the reply named it project/note.txt.")
    page.get_by_role("button", name="Back to files").click()
    page.get_by_role("link", name="gone/missing.txt").click()
    expect(panel(page).get_by_role("alert")).to_contain_text("gone/missing.txt does not exist in /project.")
