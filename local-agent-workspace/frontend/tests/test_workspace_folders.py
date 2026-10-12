"""Workspace -> Files: folders added to Files (view only), their change dots and the outside hint."""
import json
import re

from playwright.sync_api import expect

# A fake /api/workspace-folders plus listings of a few absolute folders, on top of the workspace scenario.
FOLDERS = """fake.workspaceFolders = []
fake.refuse = null
fake.override = (path, options) => {
  if (path.startsWith('/api/workspace-folders')) {
    const body = options.body ? JSON.parse(options.body) : null
    const result = extra => fake.json({ workspace: '/project', folders: fake.workspaceFolders, ...extra })
    if (options.method === 'POST') {
      if (fake.refuse) return fake.json({ detail: fake.refuse }, 400)
      if (!fake.workspaceFolders.some(folder => folder.path === body.path)) fake.workspaceFolders = [...fake.workspaceFolders, { path: body.path, missing: false }]
      return result({ added: body.path })
    }
    if (options.method === 'DELETE') {
      fake.workspaceFolders = fake.workspaceFolders.filter(folder => folder.path !== body.path)
      return result({})
    }
    return result({})
  }
  const listing = {
    '/srv/other': [{ path: '/srv/other/docs', name: 'docs', directory: true }, { path: '/srv/other/readme.md', name: 'readme.md', directory: false }],
    '/srv/other/docs': [{ path: '/srv/other/docs/guide.md', name: 'guide.md', directory: false }],
    '/srv/notes': [{ path: '/srv/notes/todo.md', name: 'todo.md', directory: false }],
  }
  const folder = path.startsWith('/api/files?') ? new URLSearchParams(path.split('?')[1]).get('path') : null
  if (folder in listing) return fake.json(listing[folder])
}
"""


def start(ui, page, setup="", session="a"):
    ui.app(session=session, scenario="workspace", setup=FOLDERS + setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    if session:
        ui.show_session(session)
    page.get_by_role("button", name=re.compile("^Workspace")).click()


def push(ui, *files):
    ui.emit("a", f"{{ type: 'changes', changes: makeChanges({', '.join(files)}) }}")


def roots(page):
    return page.get_by_role("group", name="Folders in Files")


def add_through_form(page, path):
    page.get_by_role("button", name="Add folder…").click()
    field = page.get_by_role("textbox", name="Folder path")
    expect(field).to_be_focused()
    field.fill(path)
    page.get_by_role("button", name="Add", exact=True).click()


def test_add_a_folder_through_the_form_browse_it_and_remove_it(ui, page):
    start(ui, page)
    ui.wait("fake.requests('/api/workspace-folders?session_id=a', 'GET').length === 1")
    expect(roots(page)).to_have_count(0)  # Only the project so far.
    page.get_by_role("button", name="Add folder…").click()
    field = page.get_by_role("textbox", name="Folder path")
    expect(field).to_have_attribute("placeholder", "~/projects/other")
    expect(field).to_have_attribute("spellcheck", "false")
    expect(page.get_by_text("Shows the folder in Files for this project, with dots for files the agent changed there. "
                            "The agent still needs Folder access to use it.")).to_be_visible()
    expect(page.get_by_role("button", name="Add", exact=True)).to_be_disabled()
    field.fill("/srv/other")
    page.get_by_role("button", name="Add", exact=True).click()

    added = roots(page).get_by_role("button", name="Added to Files: /srv/other", exact=True)
    expect(added).to_have_attribute("aria-pressed", "true")
    expect(roots(page).get_by_role("button", name="Project", exact=True)).to_have_attribute("aria-pressed", "false")
    expect(page.get_by_role("textbox", name="Folder path")).to_have_count(0)
    expect(page.get_by_role("button", name="Add folder…")).to_be_focused()
    assert ui.bodies("/api/workspace-folders?session_id=a", "POST") == [{"path": "/srv/other"}]
    expect(page.get_by_role("button", name="readme.md")).to_be_visible()
    assert ui.requests("/api/files?path=%2Fsrv%2Fother&session_id=a", "GET")
    parent = page.get_by_role("button", name="Parent folder")
    expect(parent).to_be_disabled()  # Never above the added folder.
    page.get_by_role("button", name="docs").click()
    expect(page.get_by_role("button", name="guide.md")).to_be_visible()
    parent.click()
    expect(parent).to_be_disabled()

    page.get_by_role("button", name="Remove /srv/other from Files").click()
    expect(roots(page)).to_have_count(0)
    assert ui.bodies("/api/workspace-folders?session_id=a", "DELETE") == [{"path": "/srv/other"}]
    expect(page.get_by_role("button", name="note.txt")).to_be_visible()  # Back to the project.


def test_a_root_with_changes_shows_a_dot_and_the_count(ui, page):
    start(ui, page, setup="fake.workspaceFolders = [{ path: '/srv/other', missing: false }, { path: '/srv/gone', missing: true }]")
    push(ui, "changedFile('/srv/other/readme.md')", "changedFile('/srv/other/docs/guide.md')", "changedFile('note.txt')")
    other = roots(page).get_by_role("button", name="Added to Files: /srv/other, 2 changed files")
    expect(other.locator(".change-dot")).to_be_visible()
    project = roots(page).get_by_role("button", name="Project, 1 changed file")
    expect(project.locator(".change-dot")).to_be_visible()
    gone = roots(page).get_by_role("button", name="Added to Files: /srv/gone, Not found")
    expect(gone).to_be_disabled()
    expect(roots(page).get_by_role("button", name="Remove /srv/gone from Files")).to_be_enabled()
    other.click()
    expect(page.get_by_role("button", name="readme.md").get_by_role("img", name="Changed by the agent")).to_be_visible()
    expect(page.get_by_role("button", name="docs").get_by_role("img", name="Contains changed files")).to_be_visible()
    # Keeping the changes clears the dot and the count.
    push(ui)
    expect(roots(page).get_by_role("button", name="Added to Files: /srv/other", exact=True).locator(".change-dot")).to_have_count(0)


def test_the_outside_hint_adds_and_selects_the_folder_of_a_changed_file(ui, page):
    start(ui, page)
    push(ui, "changedFile('/srv/notes/todo.md', 3, 0, 'created')", "changedFile('/srv/notes/other.md')",
         "changedFile('/var/a/one.txt')", "changedFile('/var/b/two.txt')", "changedFile('/var/c/three.txt')")
    expect(page.get_by_text("5 changed files outside these folders")).to_be_visible()
    # One button per distinct folder, at most three.
    expect(page.get_by_role("button", name=re.compile(r"^Add \/"))).to_have_text(["Add /srv/notes", "Add /var/a", "Add /var/b"])
    expect(roots(page).get_by_role("button", name="Project", exact=True)).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="Add /srv/notes").click()
    notes = roots(page).get_by_role("button", name="Added to Files: /srv/notes, 2 changed files")
    expect(notes).to_have_attribute("aria-pressed", "true")
    assert ui.bodies("/api/workspace-folders?session_id=a", "POST") == [{"path": "/srv/notes"}]
    expect(page.get_by_text("3 changed files outside these folders")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile(r"^Add \/"))).to_have_text(["Add /var/a", "Add /var/b", "Add /var/c"])
    todo = page.get_by_role("button", name="todo.md")
    expect(todo.get_by_role("img", name="Changed by the agent")).to_be_visible()
    todo.click()
    expect(page.get_by_role("region", name="Agent changes in /srv/notes/todo.md")).to_be_visible()
    ui.wait("fake.requests('/api/sessions/a/changes/diff?path=%2Fsrv%2Fnotes%2Ftodo.md').length === 1")


def test_a_granted_folder_covers_its_changes(ui, page):
    start(ui, page, setup="fake.sessions.set('a', { ...makeSession('a', 'Other conversation'), allowed_directories: ['/srv/notes'] })")
    push(ui, "changedFile('/srv/notes/todo.md')")
    expect(roots(page).get_by_role("button", name="Allowed in this chat: /srv/notes, 1 changed file")).to_be_visible()
    expect(page.get_by_text(re.compile("outside these folders"))).to_have_count(0)


def test_server_refusals_are_shown_inline(ui, page):
    refusal = "This folder is the app's state folder or inside it. It holds conversations and settings and is never shown in Files."
    start(ui, page, setup=f"fake.refuse = {json.dumps(refusal)}")
    add_through_form(page, "/srv/state")
    form_error = page.locator("form").get_by_role("alert")
    expect(form_error).to_have_text(refusal)
    expect(page.get_by_role("textbox", name="Folder path")).to_have_value("/srv/state")  # Kept for a correction.
    expect(roots(page)).to_have_count(0)
    page.get_by_role("textbox", name="Folder path").press("Escape")
    expect(page.get_by_role("textbox", name="Folder path")).to_have_count(0)
    expect(page.get_by_role("button", name="Add folder…")).to_be_focused()

    push(ui, "changedFile('/srv/state/settings.json')")
    page.get_by_role("button", name="Add /srv/state").click()
    expect(page.locator(".file-roots > [role=alert]")).to_have_text(refusal)
    expect(roots(page).get_by_role("button", name="Project, 0 changed files")).to_have_count(0)
    expect(roots(page).get_by_role("button", name="Project", exact=True)).to_have_attribute("aria-pressed", "true")


def test_the_new_chat_view_uses_the_project_from_settings(ui, page):
    start(ui, page, session=None, setup="fake.workspaceFolders = [{ path: '/srv/other', missing: false }]")
    ui.wait("fake.requests('/api/workspace-folders', 'GET').length === 1")
    roots(page).get_by_role("button", name="Added to Files: /srv/other").click()
    expect(page.get_by_role("button", name="readme.md")).to_be_visible()
    assert ui.requests("/api/files?path=%2Fsrv%2Fother&", "GET")


def test_narrow_layout_has_no_horizontal_page_overflow(ui, page):
    page.set_viewport_size({"width": 390, "height": 800})
    long = "/srv/" + "/".join(["a-rather-long-folder-name"] * 6)
    folders = [{"path": long, "missing": False}] + [{"path": f"/srv/folder-{n}", "missing": False} for n in range(8)]
    start(ui, page, setup=f"fake.workspaceFolders = {json.dumps(folders)}")
    push(ui, f"changedFile({json.dumps(long + '/x.txt')})", "changedFile('/var/" + "deep/" * 20 + "y.txt')")
    expect(roots(page).get_by_role("button", name=re.compile("^Added to Files"))).to_have_count(9)
    page.get_by_role("button", name="Add folder…").click()
    expect(page.get_by_role("textbox", name="Folder path")).to_be_visible()
    widths = ui.js("[document.documentElement.scrollWidth, document.documentElement.clientWidth, "
                   "document.querySelector('.workspace-panel').scrollWidth, document.querySelector('.workspace-panel').clientWidth, "
                   "document.querySelector('.file-roots').scrollWidth, document.querySelector('.file-roots').clientWidth]")
    assert widths[0] <= widths[1] and widths[2] <= widths[3] and widths[4] <= widths[5], widths
    # The file list stays reachable below the roots.
    expect(page.get_by_role("button", name="note.txt")).to_be_in_viewport()
