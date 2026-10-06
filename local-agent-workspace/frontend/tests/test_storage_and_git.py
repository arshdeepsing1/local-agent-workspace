"""Unreadable conversation files are named in a dismissible notice; the Changes tab shows server Git output."""
import json
import re

from playwright.sync_api import expect


def start(ui, page, problems):
    setup = ("fake.override = path => path === '/api/bootstrap' ? fake.json({ token: 'test-token', "
             f"settings: fake.settings, storage_problems: {json.dumps(problems)} }}) : undefined")
    ui.app(scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")


def problem(index, reason="line 1 is not valid JSON"):
    return {"file": f"broken-{index}.jsonl", "reason": reason}


def test_one_unreadable_file_is_named_and_the_notice_can_be_dismissed(ui, page):
    start(ui, page, [problem(1, "the file is empty")])
    notice = page.get_by_role("alert")
    expect(notice).to_have_text("1 conversation file could not be read and is hidden: broken-1.jsonl — the file is empty. "
                                "The file was left unchanged in the state folder.")
    # The readable conversations are still listed.
    expect(page.get_by_role("button", name=re.compile("^Other conversation"))).to_be_visible()
    page.get_by_role("button", name="Dismiss storage notice").click()
    expect(page.get_by_role("alert")).to_have_count(0)


def test_several_unreadable_files_are_listed_with_a_bound(ui, page):
    start(ui, page, [problem(index) for index in range(1, 8)])
    notice = page.get_by_role("alert")
    expect(notice.get_by_role("paragraph")).to_have_text(
        "7 conversation files could not be read and are hidden. The files were left unchanged in the state folder.")
    expect(notice.get_by_role("listitem")).to_have_text(
        [f"broken-{index}.jsonl — line 1 is not valid JSON" for index in range(1, 6)] + ["and 2 more"])


def test_no_notice_when_every_file_was_read(ui, page):
    start(ui, page, [])
    expect(page.get_by_role("button", name=re.compile("^Other conversation"))).to_be_visible()
    expect(page.get_by_role("alert")).to_have_count(0)


# The Changes tab shows GET /api/git output (run by the server without a shell).

def open_changes(ui, page, body, status=200):
    setup = f"fake.override = path => path.startsWith('/api/git') ? fake.json({json.dumps(body)}, {status}) : undefined"
    ui.app(scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    page.get_by_role("button", name="Workspace", exact=True).click()
    page.get_by_role("button", name="Changes", exact=True).click()
    return page.locator(".diff-output")


def test_changes_tab_shows_status_and_diff(ui, page):
    output = " M app.py\ndiff --git a/app.py b/app.py\n-print('old')\n+print('new')\n"
    diff = open_changes(ui, page, {"exit_code": 0, "output": output, "truncated": False})
    expect(diff).to_contain_text(" M app.py")
    expect(diff.locator(".added")).to_have_text("+print('new')\n")
    expect(diff.locator(".removed")).to_have_text("-print('old')\n")


def test_changes_tab_explains_a_folder_without_a_repository(ui, page):
    message = "fatal: not a git repository (or any of the parent directories): .git"
    diff = open_changes(ui, page, {"exit_code": 128, "output": message + "\n", "truncated": False})
    expect(diff).to_contain_text(message)


def test_changes_tab_reports_a_git_timeout(ui, page):
    open_changes(ui, page, {"detail": "Git did not finish within 60 seconds."}, 400)
    expect(page.get_by_role("complementary", name="Workspace panel").get_by_role("alert")).to_have_text(
        "Git did not finish within 60 seconds.")
