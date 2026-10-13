"""Line comments in the change review, sent to the agent as one message."""
import re

from playwright.sync_api import expect

APP_DIFF = """window.appDiff = {
  path: 'src/app.py', status: 'modified', added: 1, removed: 1, baseline_hash: 'base-app', current_hash: 'hash-src/app.py',
  hunks: [{ index: 0, line: 1, removed: 1, added: 1 }],
  lines: [
    { kind: 'context', text: 'import os', old: 1, new: 1 },
    { kind: 'removed', text: 'print(`old`)', old: 2, hunk: 0 }, { kind: 'added', text: 'print("new")', new: 2, hunk: 0 },
  ],
}
fake.override = path => path.includes('/changes/diff?path=src') ? fake.json(appDiff) : undefined"""

MESSAGE = ("Review comments on your changes (file:line, from the review panel):\n\n"
           "- `note.txt:2` (added line: `TWO`): Use lower case here\n"
           "- `note.txt:2` (removed line: `two`): Why remove this?\n"
           "- `src/app.py:2` (removed line: `` print(`old`) ``): Keep the backticks\n\n"
           "Address each comment, then summarize what you changed.")


def review(page, path="note.txt"):
    return page.get_by_role("region", name=f"Agent changes in {path}")


def open_review(ui, page, path):
    bar = page.get_by_role("region", name="Changed files")
    toggle = bar.get_by_role("button", name=re.compile("files? changed"))
    if toggle.get_attribute("aria-expanded") == "false":
        toggle.click()
    page.get_by_title(f"Review {path}").click()
    expect(review(page, path)).to_be_visible()


def comment(page, line_label, text, submit="click"):
    line = page.get_by_role("button", name=line_label, exact=True)
    line.hover()
    line.click()
    box = page.get_by_role("textbox", name="Comment", exact=True)
    expect(box).to_be_focused()
    expect(box).to_have_attribute("maxlength", "2000")
    box.fill(text)
    if submit == "keys":
        box.press("Control+Enter")
    else:
        page.get_by_role("button", name="Add comment").click()
    expect(box).to_have_count(0)


def test_comments_on_added_and_removed_lines_survive_switching_files_and_are_sent_as_one_message(ui, page):
    ui.app(session="a", scenario="workspace", setup=APP_DIFF)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    ui.show_session("a")
    ui.emit("a", "{ type: 'changes', changes: makeChanges(changedFile('note.txt', 2, 1), changedFile('src/app.py', 1, 1)) }")
    open_review(ui, page, "note.txt")
    expect(page.get_by_role("button", name=re.compile("^Send .* review comment"))).to_have_count(0)
    comment(page, "Comment on line 2", "Use lower case here", submit="keys")
    comment(page, "Comment on removed line 2", "Why remove this?")
    expect(page.locator(".review-comment")).to_have_text([
        re.compile("Removed line 2.*Why remove this\\?"), re.compile("Line 2.*Use lower case here")])
    send = page.get_by_role("button", name="Send 2 review comments")
    expect(send).to_have_text("Send 2 comments")

    open_review(ui, page, "src/app.py")
    expect(page.locator(".review-comment")).to_have_count(0)
    expect(send).to_be_visible()  # Comments in any file of this chat.
    comment(page, "Comment on removed line 2", "Keep the backticks")
    page.get_by_role("button", name="Close workspace").click()  # Comments outlive the panel.
    expect(review(page, "src/app.py")).to_have_count(0)
    open_review(ui, page, "note.txt")
    expect(page.locator(".review-comment")).to_have_count(2)

    page.get_by_role("button", name="Send 3 review comments").click()
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/messages", "POST") == [{"text": MESSAGE}]
    expect(page.locator(".review-comment")).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("^Send .* review comment"))).to_have_count(0)


def mount_review(ui, busy="false", send="async () => ({})"):
    ui.mount("components/DiffReview.js", f"{{ sessionId: 'a', path: 'note.txt', version: 'hash-note.txt', busy: {busy}, "
             f"onAction: fake.spy('action'), onSelectSession: fake.spy('select'), onSendComments: fake.spy('send', {send}) }}",
             scenario="workspace")


def test_sending_waits_for_the_reply_and_a_failure_keeps_the_comments(ui, page):
    mount_review(ui, busy="true", send="async () => { throw new Error('The agent is not reachable.') }")
    comment(page, "Comment on line 4", "Add a test")
    send = page.get_by_role("button", name="Send 1 review comment")
    expect(send).to_be_disabled()
    expect(send).to_have_attribute("title", "Wait for the reply to finish")
    ui.rerender("{ sessionId: 'a', path: 'note.txt', version: 'hash-note.txt', busy: false, onAction: fake.spy('action'), "
                "onSelectSession: fake.spy('select'), onSendComments: fake.spy('send', async () => { throw new Error('The agent is not reachable.') }) }")
    expect(send).to_be_enabled()
    send.click()
    expect(review(page).get_by_role("alert")).to_have_text("The agent is not reachable.")
    assert ui.spy("send") == [["Review comments on your changes (file:line, from the review panel):\n\n"
                               "- `note.txt:4` (added line: `four`): Add a test\n\n"
                               "Address each comment, then summarize what you changed."]]
    expect(page.locator(".review-comment")).to_have_count(1)
    expect(send).to_be_enabled()


def test_a_comment_can_be_cancelled_or_removed(ui, page):
    mount_review(ui)
    line = page.get_by_role("button", name="Comment on line 1", exact=True)
    line.hover()
    line.click()
    box = page.get_by_role("textbox", name="Comment", exact=True)
    box.fill("Never mind")
    box.press("Escape")
    expect(box).to_have_count(0)
    line.click()
    expect(page.get_by_role("textbox", name="Comment", exact=True)).to_have_value("")
    page.get_by_role("button", name="Cancel").click()
    expect(page.get_by_role("button", name="Add comment")).to_have_count(0)
    comment(page, "Comment on line 3", "Context matters")
    comment(page, "Comment on line 1", "First line")
    expect(page.locator(".review-comment")).to_have_count(2)
    page.locator(".review-comment").filter(has_text="Context matters").get_by_role("button", name="Remove comment").click()
    expect(page.locator(".review-comment")).to_have_text([re.compile("Line 1.*First line")])
    expect(page.get_by_role("button", name="Send 1 review comment")).to_be_visible()
    page.get_by_role("button", name="Send 1 review comment").click()
    ui.wait("fake.spies.send.length === 1")
    assert "`note.txt:1` (context line: `one`): First line" in ui.spy("send")[0][0]
    expect(page.locator(".review-comment")).to_have_count(0)
