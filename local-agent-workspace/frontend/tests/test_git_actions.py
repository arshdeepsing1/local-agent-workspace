"""The Git package's UI: Stage, Unstage and Commit in the Changes view, the pull request chip, the review findings
card, the "Git status" chip under a message and Settings → Git status in messages."""
import json
import re

from playwright.sync_api import expect

from test_workspace import GIT_STATUS, start_app

COMMITTED = {"repository": True, "branch": "feature/login", "truncated": False, "files": [
    {"path": "notes/todo.md", "area": "untracked", "status": "untracked", "added": None, "removed": None, "hidden": None}]}
OPEN_PR = {"available": True, "number": 12, "title": "Add the login form", "state": "open", "draft": False,
           "url": "https://example.com/o/r/pull/12", "review_decision": "changes_requested",
           "checks": {"total": 4, "passed": 3, "failed": 1, "pending": 0}}


def open_changes(ui, page, setup="", session=None):
    """The Changes tab over GIT_STATUS; POST /api/git/stage answers window.stageAnswer, /api/git/commit commitAnswer,
    GET /api/git/pr window.prAnswer (default: gh not installed)."""
    start_app(ui, page, session=session, setup=f"""window.gitStatus = {json.dumps(GIT_STATUS)}
      window.stageAnswer = () => fake.json(gitStatus)
      window.commitAnswer = () => fake.json({{ commit: {{ hash: 'abc1234', subject: 'Add login' }}, status: {json.dumps(COMMITTED)} }})
      window.prAnswer = null
      fake.override = (path, options) => path.startsWith('/api/git/status') ? fake.json(gitStatus)
        : path.startsWith('/api/git/stage') ? stageAnswer(JSON.parse(options.body))
        : path.startsWith('/api/git/commit') ? commitAnswer(JSON.parse(options.body))
        : path.startsWith('/api/git/pr') && prAnswer ? fake.json(prAnswer) : undefined; {setup}""")
    if session:
        ui.show_session(session)
    page.get_by_role("button", name="Workspace", exact=True).click()
    page.get_by_role("button", name="Changes", exact=True).click()
    panel = page.get_by_role("complementary", name="Workspace panel")
    expect(panel.get_by_role("heading", name="Staged changes (2)")).to_be_visible()
    return panel


def test_stage_and_unstage_buttons_send_the_chosen_paths_and_show_the_new_status(ui, page):
    panel = open_changes(ui, page)
    changes = panel.get_by_role("region", name="Changes", exact=True)
    stage = changes.locator("button.git-stage")
    # One button per file the app may stage; none for the credential file or a generated one.
    expect(stage).to_have_count(1)
    expect(stage).to_have_accessible_name("Stage")
    expect(stage).to_have_accessible_description("src/app.py")
    expect(panel.locator(".git-row.hidden button")).to_have_count(0)
    expect(panel.get_by_role("region", name="Untracked").locator("button.git-stage")).to_have_count(1)
    ui.run("""stageAnswer = body => fake.json({ ...gitStatus, files: gitStatus.files.filter(entry =>
      !(entry.area === 'unstaged' && body.paths.includes(entry.path))) })""")
    stage.click()
    expect(panel.get_by_role("heading", name="Changes (1)")).to_be_visible()
    assert ui.bodies("/api/git/stage", "POST") == [{"paths": ["src/app.py"], "staged": True}]
    # A rename unstages both of its sides; the staged group can unstage all at once.
    staged = panel.get_by_role("region", name="Staged changes")
    staged.locator("button.git-stage[data-path='src/new name.py']").click()
    staged.get_by_role("button", name="Unstage all").click()
    expect(page.get_by_role("button", name="Unstage all")).to_be_enabled()
    assert ui.bodies("/api/git/stage", "POST")[1:] == [
        {"paths": ["src/new name.py", "src/old.py"], "staged": False},
        {"paths": ["src/app.py", "src/new name.py", "src/old.py"], "staged": False}]


def test_conflicts_submodules_and_folders_listed_whole_have_no_stage_button(ui, page):
    files = [*GIT_STATUS["files"],
             {"path": "src/merge.py", "area": "unstaged", "status": "conflicted", "added": None, "removed": None, "hidden": None},
             {"path": "lib", "area": "unstaged", "status": "modified", "added": None, "removed": None, "hidden": None,
              "submodule": True},
             {"path": "vendor/lib/", "area": "untracked", "status": "untracked", "added": None, "removed": None, "hidden": None}]
    panel = open_changes(ui, page, setup=f"gitStatus = {json.dumps({**GIT_STATUS, 'files': files})}")
    untracked = panel.get_by_role("region", name="Untracked")
    expect(untracked.locator("button.git-row")).to_have_count(2)
    expect(untracked.locator("button.git-stage")).to_have_count(1)
    expect(untracked.locator("button.git-stage")).to_have_accessible_description("notes/todo.md")
    expect(untracked.get_by_role("button", name="Stage all")).to_have_count(0)
    changes = panel.get_by_role("region", name="Changes", exact=True)
    expect(changes.locator("button.git-row")).to_have_count(3)
    expect(changes.locator("button.git-stage")).to_have_count(1)
    # The submodule row has no button and Stage all leaves it out (the server stages files only): one file left.
    expect(changes.locator(".git-entry", has=page.locator("button.git-row[data-path='lib']"))).to_have_count(1)
    expect(changes.locator("button.git-stage[data-path='lib']")).to_have_count(0)
    expect(changes.get_by_role("button", name="Stage all")).to_have_count(0)


def test_commit_sends_the_message_and_names_the_commit(ui, page):
    panel = open_changes(ui, page)
    message = panel.get_by_role("textbox", name="Commit message")
    commit = panel.get_by_role("button", name="Commit 2 files")
    expect(commit).to_be_disabled()
    message.fill("Add login\n\nWith a form.")
    commit.click()
    expect(panel.get_by_role("status")).to_have_text("Committed abc1234: Add login")
    assert ui.bodies("/api/git/commit", "POST") == [{"message": "Add login\n\nWith a form."}]
    expect(panel.get_by_role("heading", name=re.compile("^Staged"))).to_have_count(0)
    expect(panel.get_by_role("textbox", name="Commit message")).to_have_count(0)
    # The next Stage clears the note.
    panel.get_by_role("region", name="Untracked").locator("button.git-stage").click()
    expect(panel.locator(".git-committed")).to_have_count(0)


def test_a_refused_action_shows_why_and_the_buttons_wait_while_the_chat_works(ui, page):
    panel = open_changes(ui, page, session="a", setup="""fake.sessions.set('a', makeSession('a', 'Chat'))""")
    ui.run("""stageAnswer = () => fake.json({ detail: { message: '“Chat” is still running. Stop it before staging files.',
      session_id: 'a', title: 'Chat' } }, 409)""")
    panel.get_by_role("region", name="Changes", exact=True).locator("button.git-stage").click()
    expect(panel.get_by_role("alert")).to_have_text("“Chat” is still running. Stop it before staging files.")
    ui.emit("a", {"type": "status", "status": "running"})
    expect(panel.locator("button.git-stage").first).to_be_disabled()
    expect(panel.get_by_role("button", name="Unstage all")).to_be_disabled()
    ui.emit("a", {"type": "status", "status": "idle"})
    expect(panel.locator("button.git-stage").first).to_be_enabled()


def pr_paths(ui):
    return ui.js("fake.calls.filter(call => call.path.startsWith('/api/git/pr')).map(call => call.path)")


def test_the_pull_request_chip_links_to_it_and_refresh_asks_gh_again(ui, page):
    panel = open_changes(ui, page, setup=f"prAnswer = {json.dumps(OPEN_PR)}")
    chip = panel.get_by_role("link", name="PR #12 · open · 3/4 checks, 1 failing")
    expect(chip).to_have_attribute("href", "https://example.com/o/r/pull/12")
    expect(chip).to_have_attribute("target", "_blank")
    expect(chip).to_have_attribute("rel", "noopener noreferrer")
    expect(chip).to_have_attribute("title", "Add the login form · changes requested")
    assert pr_paths(ui) == ["/api/git/pr?branch=feature%2Flogin"]
    ui.run(f"prAnswer = {json.dumps({**OPEN_PR, 'draft': True, 'checks': {'total': 0}})}")
    panel.get_by_role("button", name="Refresh changes").click()
    expect(panel.get_by_role("link", name="PR #12 · draft")).to_be_visible()
    assert pr_paths(ui)[-1] == "/api/git/pr?branch=feature%2Flogin&refresh=true"


def test_without_gh_or_a_pull_request_nothing_shows_and_a_sign_in_problem_is_named(ui, page):
    panel = open_changes(ui, page)
    expect(panel.locator(".git-pr")).to_have_count(0)
    ui.run("prAnswer = { available: false, code: 'no_pr', reason: 'No pull request for this branch.' }")
    panel.get_by_role("button", name="Refresh changes").click()
    ui.wait("fake.calls.filter(call => call.path.startsWith('/api/git/pr')).length === 2")
    expect(panel.locator(".git-pr")).to_have_count(0)
    ui.run("prAnswer = { available: false, code: 'not_signed_in', reason: 'GitHub CLI is not signed in: run gh auth login in a terminal.' }")
    panel.get_by_role("button", name="Refresh changes").click()
    expect(panel.locator(".git-pr-note")).to_have_text("GitHub CLI is not signed in: run gh auth login in a terminal.")


FINDINGS = {"summary": "Two problems in the parser.", "findings": [
    {"path": "src/app.py", "line": 12, "end_line": 14, "severity": "high", "title": "Unchecked index",
     "detail": "items[0] fails on an empty list.", "suggestion": "Return early when items is empty."},
    {"path": "README.md", "severity": "info", "title": "Typo in the usage section"}]}
EVENTS = [
    {"id": "u", "type": "user", "text": "/review the parser", "skills": ["review"], "git_context": True},
    {"id": "f", "type": "tool", "name": "report_findings", "state": "completed", "input": FINDINGS,
     "output": "Recorded 2 findings; the user sees them as a card."},
    {"id": "r", "type": "assistant", "text": "One high finding."},
]


def open_review(ui, page, events=EVENTS):
    start_app(ui, page, session="a", setup=f"""fake.sessions.set('a', {{ ...makeSession('a', 'Review'), events: {json.dumps(events)} }})
      fake.override = path => path.startsWith('/api/file?') ? fake.json({{ path: 'src/app.py', content: 'x\\n'.repeat(20),
        absolute: '/project/src/app.py' }}) : undefined""")
    ui.show_session("a")
    return page.get_by_role("region", name="Review findings")


def test_the_findings_card_shows_severity_locations_and_copies_markdown(ui, page):
    card = open_review(ui, page)
    expect(card.locator(".findings-counts")).to_have_text(re.compile(r"1 high\s*1 info"))
    expect(card.locator(".findings-summary")).to_have_text("Two problems in the parser.")
    items = card.locator(".finding")
    expect(items).to_have_count(2)
    expect(items.nth(0).locator(".finding-severity")).to_have_text("high")
    expect(items.nth(0).locator(".finding-title")).to_have_text("Unchecked index")
    expect(items.nth(0).locator(".finding-suggestion")).to_have_text("Suggestion: Return early when items is empty.")
    expect(items.nth(1).get_by_role("button", name="README.md")).to_be_visible()
    card.get_by_role("button", name="Copy findings as Markdown").click()
    page.wait_for_function("fake.clipboard.length === 1")
    assert ui.js("fake.clipboard[0]") == (
        "## Review findings\n\nTwo problems in the parser.\n\n"
        "1. **high** `src/app.py:12-14` Unchecked index\n   items[0] fails on an empty list.\n"
        "   Suggestion: Return early when items is empty.\n2. **info** `README.md` Typo in the usage section\n")
    # The activity list names the call; the location opens the file at its line.
    expect(page.locator(".activity-summary li")).to_have_text(re.compile("report findings · 2 findings"))
    card.get_by_role("button", name="src/app.py:12-14").click()
    panel = page.get_by_role("complementary", name="Workspace panel")
    expect(panel.get_by_role("textbox", name="Edit src/app.py")).to_be_visible()
    expect(panel.locator(".editor-status")).to_contain_text("Line 12")


def test_a_running_or_refused_report_is_an_ordinary_tool_card(ui, page):
    events = [EVENTS[0], {**EVENTS[1], "state": "rejected", "output": "Blocked by a deny rule for this chat."}]
    open_review(ui, page, events)
    expect(page.get_by_role("region", name="Review findings")).to_have_count(0)
    expect(page.locator(".tool-card .tool-outcome")).to_have_text("Not run: Blocked by a deny rule for this chat.")


def test_a_message_sent_with_git_status_says_so(ui, page):
    open_review(ui, page)
    chips = page.get_by_role("list", name="Sent with this message").locator(".attachment-chip")
    expect(chips).to_have_text(["Skill /review", "Git status"])
    expect(chips.nth(1)).to_have_attribute("title", re.compile("^The branch, upstream, change counts and recent commits"))


def test_git_status_in_messages_is_on_by_default_and_saves(ui, page):
    start_app(ui, page)
    page.get_by_role("button", name="Settings", exact=True).click()
    group = page.get_by_role("dialog").get_by_role("group", name="Agent")
    toggle = group.get_by_role("checkbox", name="Git status in messages")
    expect(toggle).to_be_checked()
    expect(group.get_by_text(re.compile(r"^The first message of each reply carries a short Git snapshot"))).to_be_visible()
    toggle.uncheck()
    page.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["git_context"] is False
