"""Reviewing the agent's file changes: the changed-files bar, explorer dots and the inline diff."""
import json
import re

from playwright.sync_api import expect

CHANGES = """window.changes = { added: 7, removed: 3, files: [
  { path: 'src/app.py', status: 'modified', added: 4, removed: 3, current_hash: 'h1' },
  { path: 'docs/usage.md', status: 'created', added: 3, removed: 0, current_hash: 'h2' },
  { path: 'data.bin', status: 'unavailable', added: 0, removed: 0, current_hash: null, error: 'Binary files cannot be checkpointed.' },
] }"""


def mount_bar(ui, changes="window.changes", busy="false", action="async () => ({})", roots="[]"):
    ui.mount("components/ChangesBar.js", f"{{ changes: {changes}, busy: {busy}, onOpen: fake.spy('open'), "
             f"onAction: fake.spy('action', {action}), onError: fake.spy('error'), roots: {roots} }}", setup=CHANGES)


def bar(page):
    return page.get_by_role("region", name="Changed files")


def start_app(ui, page, setup=""):
    ui.app(session="a", scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    ui.show_session("a")


def push(ui, *files):
    ui.emit("a", f"{{ type: 'changes', changes: makeChanges({', '.join(files)}) }}")


def review(page, path="note.txt"):
    return page.get_by_role("region", name=f"Agent changes in {path}")


def test_bar_counts_changed_files_and_keeps_or_undoes_them(ui, page):
    mount_bar(ui)
    toggle = bar(page).get_by_role("button", name=re.compile("^3 files changed"))
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(toggle).to_contain_text("+7")
    expect(toggle).to_contain_text("−3")
    expect(page.get_by_role("listitem")).to_have_count(0)
    toggle.click()
    expect(page.get_by_role("listitem")).to_have_count(3)
    expect(page.get_by_role("listitem").nth(1)).to_contain_text("usage.mddocsNew file+3")
    expect(page.get_by_role("listitem").nth(2)).to_contain_text("Binary files cannot be checkpointed.")
    expect(page.get_by_role("button", name="Undo changes to data.bin")).to_be_disabled()

    page.get_by_role("button", name="Keep changes to src/app.py").click()
    page.get_by_title("Review docs/usage.md").click()
    page.get_by_role("button", name="Undo all changes").click()
    page.get_by_role("button", name="Keep all changes").click()
    ui.wait("fake.spies.action.length === 3")
    assert ui.spy("open") == [["docs/usage.md"]]
    assert ui.spy("action") == [
        ["keep", [{"path": "src/app.py", "current_hash": "h1"}]],
        # An unreadable file cannot be undone, only kept (no longer tracked).
        ["undo", [{"path": "src/app.py", "current_hash": "h1"}, {"path": "docs/usage.md", "current_hash": "h2"}]],
        ["keep", [{"path": "src/app.py", "current_hash": "h1"}, {"path": "docs/usage.md", "current_hash": "h2"},
                  {"path": "data.bin", "current_hash": None}]],
    ]


def test_undo_waits_for_the_response_and_failures_are_reported(ui, page):
    mount_bar(ui, busy="true", action="async () => { throw new Error('src/app.py changed since you reviewed it.') }")
    expect(page.get_by_role("button", name="Undo all changes")).to_be_disabled()
    expect(page.get_by_role("button", name="Undo all changes")).to_have_attribute("title", "Stop the response before undoing changes.")
    page.get_by_role("button", name=re.compile("^3 files changed")).click()
    expect(page.get_by_role("button", name="Undo changes to src/app.py")).to_be_disabled()
    page.get_by_role("button", name="Keep all changes").click()
    ui.wait("fake.spies.error.length === 1")
    assert ui.spy("error") == [["src/app.py changed since you reviewed it."]]
    expect(page.get_by_role("button", name="Keep all changes")).to_be_enabled()


def test_bar_is_hidden_without_changes_but_reports_a_failed_check(ui, page):
    mount_bar(ui, changes="null")
    expect(bar(page)).to_have_count(0)
    ui.rerender("{ changes: { files: [], added: 0, removed: 0 }, busy: false }")
    expect(bar(page)).to_have_count(0)
    ui.rerender("{ changes: { files: [], added: 0, removed: 0, error: 'database is locked' }, busy: false }")
    expect(bar(page).get_by_role("alert")).to_have_text("Could not check changed files: database is locked")


def test_pushed_changes_show_a_count_and_dots_on_changed_files_and_folders(ui, page):
    start_app(ui, page, setup="""fake.override = path => path.startsWith('/api/files?') ? fake.json([
      { path: 'src', name: 'src', directory: true }, { path: 'lib', name: 'lib', directory: true },
      { path: 'note.txt', name: 'note.txt', directory: false }, { path: 'other.txt', name: 'other.txt', directory: false }]) : undefined""")
    expect(bar(page)).to_have_count(0)
    push(ui, "changedFile('note.txt', 2, 1)", "changedFile('src/app.py', 4, 3)")
    expect(bar(page).get_by_role("button", name=re.compile("^2 files changed"))).to_contain_text("+6")
    workspace = page.get_by_role("button", name="Workspace (2 changed files)")
    expect(workspace).to_be_visible()
    workspace.click()
    expect(page.get_by_role("button", name="note.txt Changed by the agent")).to_be_visible()
    expect(page.get_by_role("button", name="src Contains changed files")).to_be_visible()
    expect(page.get_by_role("button", name="other.txt", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="lib", exact=True)).to_be_visible()
    push(ui)
    expect(page.get_by_role("img", name=re.compile("Changed by the agent|Contains changed files"))).to_have_count(0)
    expect(page.get_by_role("button", name="Workspace", exact=True)).to_be_visible()
    expect(bar(page)).to_have_count(0)


def test_review_shows_removed_and_added_lines_and_keeps_or_undoes_each_change(ui, page):
    start_app(ui, page, setup="fake.reviewResult = makeChanges(changedFile('note.txt', 1, 1))")
    push(ui, "changedFile('note.txt', 2, 1)")
    page.get_by_role("button", name=re.compile("^Workspace")).click()
    page.get_by_role("button", name="note.txt Changed by the agent").click()
    expect(review(page)).to_contain_text("Modified+2−12 changes by the agent")
    expect(page.locator(".diff-line.removed")).to_have_text(["2−Removed: two"])
    expect(page.locator(".diff-line.added")).to_have_text(["2+Added: TWO", "4+Added: fourNo newline at end of file"])
    expect(page.locator(".diff-line.context")).to_have_count(2)
    expect(page.get_by_role("button", name="Save file")).to_be_disabled()
    toolbar = page.get_by_role("toolbar", name="Review this file")
    expect(toolbar).to_contain_text("1 of 2")
    toolbar.get_by_role("button", name="Next change").click()
    expect(toolbar).to_contain_text("2 of 2")
    toolbar.get_by_role("button", name="Next change").click()
    expect(toolbar).to_contain_text("1 of 2")
    toolbar.get_by_role("button", name="Previous change").click()
    expect(toolbar).to_contain_text("2 of 2")

    page.get_by_role("button", name="Keep change 2").click()
    ui.wait("fake.requests('/api/sessions/a/changes/keep', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/changes/keep", "POST") == [{"files": [
        {"path": "note.txt", "current_hash": "hash-note.txt", "baseline_hash": "base", "hunk": 1}]}]
    # A kept change leaves the file on disk as it is, so the review asks for the new diff.
    ui.wait("fake.requests(/\\/changes\\/diff\\?path=note\\.txt$/).length === 2")
    page.get_by_role("button", name="Undo change 1").click()
    toolbar.get_by_role("button", name="Undo this file").click()
    ui.wait("fake.requests('/api/sessions/a/changes/undo', 'POST').length === 2")
    assert ui.bodies("/api/sessions/a/changes/undo", "POST") == [
        {"files": [{"path": "note.txt", "current_hash": "hash-note.txt", "baseline_hash": "base", "hunk": 0}]},
        {"files": [{"path": "note.txt", "current_hash": "hash-note.txt"}]}]
    # Once nothing is left to review, the editor shows the file as it is on disk.
    push(ui)
    expect(page.get_by_role("textbox", name="Edit note.txt")).to_have_value("original")
    expect(review(page)).to_have_count(0)


def test_undo_waits_while_a_response_runs_and_errors_stay_in_the_review(ui, page):
    start_app(ui, page, setup="""fake.override = (path, options) => path.endsWith('/changes/keep')
      ? fake.json({ detail: 'note.txt changed since you reviewed it. Review it again.' }, 400) : undefined""")
    push(ui, "changedFile('note.txt', 2, 1)")
    ui.emit("a", {"type": "status", "status": "running"})
    page.get_by_role("button", name=re.compile("^Workspace")).click()
    page.get_by_role("button", name="note.txt Changed by the agent").click()
    expect(page.get_by_role("button", name="Undo this file")).to_be_disabled()
    expect(page.get_by_role("button", name="Undo change 1")).to_be_disabled()
    page.get_by_role("button", name="Keep this file").click()
    expect(review(page).get_by_role("alert")).to_have_text("note.txt changed since you reviewed it. Review it again.")
    ui.emit("a", {"type": "status", "status": "idle"})
    expect(page.get_by_role("button", name="Undo this file")).to_be_enabled()


def test_bar_opens_a_file_review_and_other_chats_do_not_show_these_changes(ui, page):
    start_app(ui, page)
    push(ui, "changedFile('note.txt', 2, 1)")
    bar(page).get_by_role("button", name=re.compile("^1 file changed")).click()
    page.get_by_title("Review note.txt").click()
    expect(review(page)).to_be_visible()
    page.get_by_role("button", name="New conversation").click()
    expect(bar(page)).to_have_count(0)
    expect(review(page)).to_have_count(0)


def test_switching_between_review_and_editing_keeps_an_unsaved_draft(ui, page):
    start_app(ui, page)
    page.get_by_role("button", name="Workspace", exact=True).click()
    page.get_by_role("button", name="note.txt").click()
    page.get_by_role("textbox", name="Edit note.txt").fill("my unsaved draft")
    push(ui, "changedFile('note.txt', 2, 1)")
    page.get_by_role("button", name="Review changes").click()
    expect(review(page)).to_be_visible()
    page.get_by_role("button", name="Edit", exact=True).click()
    expect(page.get_by_role("textbox", name="Edit note.txt")).to_have_value("my unsaved draft")
    expect(page.get_by_text("Unsaved changes")).to_be_visible()


def test_a_pushed_list_newer_than_a_review_response_wins(ui):
    ui.probe("useWorkspace.js", "useWorkspace", session="a", scenario="workspace",
             setup="const held = fake.defer('keep'); fake.override = path => path.endsWith('/changes/keep') ? held.promise : undefined")
    ui.show_session("a")
    push(ui, "changedFile('note.txt')", "changedFile('b.txt')")
    ui.run("window.kept = hook.reviewChanges('keep', [{ path: 'note.txt', current_hash: 'hash-note.txt' }])")
    push(ui, "changedFile('c.txt')")
    ui.run("fake.deferreds.keep.resolve(fake.json(makeChanges(changedFile('b.txt'))))")
    ui.js("kept")
    assert ui.js("hook.changes.files.map(item => item.path)") == ["c.txt"]
    ui.run("fake.override = () => undefined; fake.reviewResult = makeChanges(changedFile('d.txt'))")
    ui.js("hook.reviewChanges('keep', [{ path: 'c.txt' }])")
    ui.wait("hook.changes.files[0].path === 'd.txt'")


def test_a_file_outside_the_project_is_listed_by_its_full_path_and_opens_for_review(ui, page):
    start_app(ui, page)
    push(ui, "changedFile('/srv/notes/todo.md', 3, 0, 'created')")
    bar(page).get_by_role("button", name=re.compile("^1 file changed")).click()
    expect(page.get_by_role("listitem")).to_contain_text("todo.md/srv/notesNew file+3")
    page.get_by_title("Review /srv/notes/todo.md").click()
    expect(review(page, "/srv/notes/todo.md")).to_be_visible()
    ui.wait("fake.requests('/api/sessions/a/changes/diff?path=%2Fsrv%2Fnotes%2Ftodo.md').length === 1")
    # The file list shows the project folder only, so it has no dot for this file.
    page.get_by_role("button", name="Back to files").click()
    expect(page.get_by_role("img", name=re.compile("Changed by the agent|Contains changed files"))).to_have_count(0)


def test_rows_name_folders_outside_the_project_by_their_allowed_folder(ui, page):
    # F2. Mutation: show the raw parent folder (a long absolute path, cut off mid-path).
    paths = ["src/app.py", "/srv/other/pkg/mod.py", "/srv/other/top.py", "/elsewhere/a/b/c.txt", "/opt/x.txt", "README.md"]
    files = ", ".join(f"{{ path: {json.dumps(path)}, status: 'modified', added: 1, removed: 0, current_hash: 'h{index}' }}"
                      for index, path in enumerate(paths))
    mount_bar(ui, changes=f"{{ added: 6, removed: 0, files: [{files}] }}", roots="['/srv/other', '/srv/code']")
    page.get_by_role("button", name=re.compile("^6 files changed")).click()
    rows = page.locator(".changes-file")
    expect(rows.locator(".changes-name")).to_have_text(["app.py", "mod.py", "top.py", "c.txt", "x.txt", "README.md"])
    assert rows.evaluate_all("items => items.map(item => item.querySelector('.changes-dir')?.textContent ?? null)") == [
        "src", "other/pkg", "other", "…/a/b", "/opt", None]
    # The full path stays in the title and in every accessible name.
    for path in paths:
        expect(page.get_by_title(f"Review {path}", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name=f"Keep changes to {path}", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name=f"Undo changes to {path}", exact=True)).to_be_visible()
    page.get_by_title("Review /srv/other/pkg/mod.py").click()
    assert ui.spy("open") == [["/srv/other/pkg/mod.py"]]


def test_rows_mark_subagent_and_command_changes(ui, page):
    mount_bar(ui, changes="""{ added: 3, removed: 1, files: [
      { path: 'a.txt', status: 'modified', added: 1, removed: 1, current_hash: 'h1', via_session_id: 'child' },
      { path: 'b.txt', status: 'created', added: 1, removed: 0, current_hash: 'h2', source: 'command' },
      { path: 'c.txt', status: 'modified', added: 1, removed: 0, current_hash: 'h3', via_session_id: 'child', source: 'command' },
      { path: 'd.txt', status: 'modified', added: 0, removed: 0, current_hash: 'h4' },
    ] }""")
    page.get_by_role("button", name=re.compile("^4 files changed")).click()
    rows = page.get_by_role("listitem")
    subagent, command = page.locator(".changes-status.subagent"), page.locator(".changes-status.command")
    expect(rows.nth(0).locator(".changes-status")).to_have_text(["Subagent"])
    expect(rows.nth(1).locator(".changes-status")).to_have_text(["New file", "Command"])
    expect(rows.nth(2).locator(".changes-status")).to_have_text(["Subagent", "Command"])
    expect(rows.nth(3).locator(".changes-status")).to_have_count(0)
    expect(subagent.first).to_have_attribute("title", "Changed by a subagent of this conversation")
    expect(command.first).to_have_attribute("title", "Changed by a command the agent ran")


def test_automatic_keep_is_off_by_default_and_saved_from_settings(ui, page):
    ui.app(scenario="workspace")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.get_by_role("dialog")
    box = dialog.get_by_role("checkbox", name="Keep agent changes automatically when a reply finishes")
    expect(box).not_to_be_checked()
    expect(dialog.get_by_text("The changes bar clears when a reply completes; a reply you stop, that fails or that reaches the "
                              "step limit keeps its list.")).to_be_visible()
    dialog.get_by_label("More about automatic Keep").click()
    expect(dialog.get_by_text("Edits made with the agent's file tools can still be undone from Agent tools → Checkpoints; edits "
                              "made by commands can no longer be undone from the app.")).to_be_visible()
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert "auto_keep_changes" not in ui.bodies("/api/settings", "PUT")[0]  # Never set: not sent.
    page.get_by_role("button", name="Settings", exact=True).click()
    page.get_by_role("dialog").get_by_role("checkbox", name="Keep agent changes automatically when a reply finishes").check()
    page.get_by_role("dialog").get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[1]["auto_keep_changes"] is True
    page.get_by_role("button", name="Settings", exact=True).click()
    expect(page.get_by_role("dialog").get_by_role("checkbox", name="Keep agent changes automatically when a reply finishes")).to_be_checked()


def test_on_a_narrow_screen_the_origin_badges_shrink_and_the_name_stays_readable(ui, page):
    page.set_viewport_size({"width": 390, "height": 700})
    mount_bar(ui, changes="""{ added: 120, removed: 0, files: [{ path: 'some_really_long_file_name_for_testing.py', status: 'created',
      added: 120, removed: 0, current_hash: 'h1', via_session_id: 'child', source: 'command' }] }""")
    page.get_by_role("button", name=re.compile("^1 file changed")).click()
    badges = page.locator(".changes-status.subagent, .changes-status.command")
    expect(badges).to_have_text(["Subagent", "Command"])  # The full words stay in the page for screen readers.
    shown = badges.evaluate_all("items => items.map(item => getComputedStyle(item, '::before').content)")
    assert [text.split(" / ")[0] for text in shown] == ['"S"', '"C"']  # The letters shown, with no spoken text.
    width = page.locator(".changes-name").evaluate("name => name.getBoundingClientRect().width")
    assert width >= 70, width
    row = page.locator(".changes-file")
    assert row.evaluate("row => row.scrollWidth <= row.clientWidth")
