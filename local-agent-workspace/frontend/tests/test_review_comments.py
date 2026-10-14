"""Line comments in the change review, sent to the agent as one message."""
import re

import pytest
from conftest import ORIGIN, UI, serve
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
    # Comments on other files are listed below the diff, where they can be removed.
    expect(page.locator(".diff-line + .review-comment")).to_have_count(0)
    expect(page.locator(".review-comment")).to_have_text([
        re.compile("note.txt · line 2.*Use lower case here"), re.compile("note.txt · removed line 2.*Why remove this\\?")])
    expect(page.get_by_text("Comments on other files")).to_be_visible()
    expect(send).to_be_visible()  # Comments in any file of this chat.
    comment(page, "Comment on removed line 2", "Keep the backticks")
    page.get_by_role("button", name="Close workspace").click()  # Comments outlive the panel.
    expect(review(page, "src/app.py")).to_have_count(0)
    open_review(ui, page, "note.txt")
    expect(page.locator(".diff-line ~ .review-comment")).to_have_count(2)
    expect(page.locator(".review-comment-orphans .review-comment")).to_have_text([re.compile("src/app.py · removed line 2.*Keep the backticks")])

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


def test_an_open_comment_form_stays_with_its_file(ui, page):
    ui.app(session="a", scenario="workspace", setup=APP_DIFF)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    ui.show_session("a")
    ui.emit("a", "{ type: 'changes', changes: makeChanges(changedFile('note.txt', 2, 1), changedFile('src/app.py', 1, 1)) }")
    open_review(ui, page, "note.txt")
    page.get_by_role("button", name="Comment on line 2", exact=True).click()
    page.get_by_role("textbox", name="Comment", exact=True).fill("draft for note")
    ui.run("appDiff.lines[2].text = 'TWO'")  # The same line, with the same text, in the other file.
    open_review(ui, page, "src/app.py")
    expect(review(page, "src/app.py").get_by_text("import os")).to_be_visible()
    expect(page.get_by_role("textbox", name="Comment", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("^Send .* review comment"))).to_have_count(0)


def test_comments_of_a_file_that_left_the_list_stay_visible_and_removable(ui, page):
    ui.app(session="a", scenario="workspace", setup=APP_DIFF)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    ui.show_session("a")
    ui.emit("a", "{ type: 'changes', changes: makeChanges(changedFile('note.txt', 2, 1), changedFile('src/app.py', 1, 1)) }")
    open_review(ui, page, "note.txt")
    comment(page, "Comment on line 2", "Use lower case here")
    ui.emit("a", "{ type: 'changes', changes: makeChanges(changedFile('src/app.py', 1, 1)) }")  # note.txt was kept.
    open_review(ui, page, "src/app.py")
    other = page.locator(".review-comment-orphans").filter(has_text="Comments on other files")
    expect(other.locator(".review-comment")).to_have_text([re.compile("note.txt · line 2.*Use lower case here")])
    other.get_by_role("button", name="Remove comment").click()
    expect(page.locator(".review-comment")).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("^Send .* review comment"))).to_have_count(0)


def test_comments_outlive_automatic_keep_and_can_be_sent_after_the_reply(ui, page):
    ui.app(session="a", scenario="workspace", setup=APP_DIFF)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    ui.show_session("a")
    ui.emit("a", "{ type: 'changes', changes: makeChanges(changedFile('note.txt', 2, 1)) }")
    open_review(ui, page, "note.txt")
    comment(page, "Comment on line 4", "Add a test")
    expect(page.get_by_role("region", name="Review comments not sent")).to_have_count(0)  # The review has its own.
    ui.emit("a", "{ type: 'changes', changes: makeChanges() }")  # Automatic Keep cleared the list.
    expect(review(page)).to_have_count(0)
    unsent = page.get_by_role("region", name="Review comments not sent")
    expect(unsent).to_contain_text("A review comment is not sent yet")
    expect(unsent.locator(".review-comment")).to_have_text([re.compile("note.txt · line 4.*Add a test")])
    unsent.get_by_role("button", name="Send 1 review comment").click()
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    assert "- `note.txt:4` (added line: `four`): Add a test" in ui.bodies("/api/sessions/a/messages", "POST")[0]["text"]
    expect(unsent).to_have_count(0)
    expect(page.get_by_role("status").filter(has_text="Comments sent.")).to_have_count(1)


def test_focus_returns_to_the_line_after_adding_cancelling_removing_and_sending(ui, page):
    mount_review(ui)
    line = page.get_by_role("button", name="Comment on line 3", exact=True)
    line.focus()
    page.keyboard.press("Enter")
    box = page.get_by_role("textbox", name="Comment", exact=True)
    expect(box).to_be_focused()
    box.press("Escape")
    expect(line).to_be_focused()
    page.keyboard.press("Enter")
    box.fill("Context matters")
    box.press("Control+Enter")
    expect(line).to_be_focused()
    page.locator(".review-comment").get_by_role("button", name="Remove comment").click()
    expect(line).to_be_focused()
    comment(page, "Comment on line 1", "First line")
    page.get_by_role("button", name="Send 1 review comment").click()
    expect(page.get_by_role("status").filter(has_text="Comments sent.")).to_have_count(1)
    expect(page.locator(".diff-review-lines")).to_be_focused()


def test_the_line_buttons_are_one_tab_stop_moved_with_the_arrow_keys(ui, page):
    mount_review(ui)
    expect(page.locator(".line-comment")).to_have_count(5)
    expect(page.locator('.line-comment[tabindex="0"]')).to_have_count(1)
    page.locator(".diff-review-lines").focus()
    page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Comment on line 1", exact=True)).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(page.get_by_role("button", name="Comment on removed line 2", exact=True)).to_be_focused()
    page.keyboard.press("End")
    expect(page.get_by_role("button", name="Comment on line 4", exact=True)).to_be_focused()
    page.keyboard.press("ArrowUp")
    expect(page.get_by_role("button", name="Comment on line 3", exact=True)).to_be_focused()
    expect(page.locator('.line-comment[tabindex="0"]')).to_have_attribute("aria-label", "Comment on line 3")
    page.keyboard.press("Tab")  # Past the other line buttons, to the next change's actions.
    expect(page.get_by_role("button", name="Keep change 2")).to_be_focused()


def test_a_comment_stays_with_its_code_when_the_line_numbers_shift(ui, page):
    mount_review(ui)
    comment(page, "Comment on line 4", "Name this better")
    ui.run("""noteDiff.lines = [...noteDiff.lines.slice(0, 4), { kind: 'added', text: 'also inserted', new: 4, hunk: 1 },
      { kind: 'added', text: 'four', new: 5, hunk: 1, newline: false }]""")
    ui.rerender("{ sessionId: 'a', path: 'note.txt', version: 'changed', busy: false, onAction: fake.spy('action'), "
                "onSelectSession: fake.spy('select'), onSendComments: fake.spy('send', async () => ({})) }")
    expect(page.get_by_role("button", name="Comment on line 5", exact=True)).to_be_visible()
    expect(page.locator(".review-comment-orphans")).to_contain_text("Comments on lines no longer in this diff")
    expect(page.locator(".review-comment-orphans .review-comment")).to_have_text([re.compile("Line 4.*Name this better")])
    expect(page.locator(".diff-line + .review-comment")).to_have_count(0)


def add_quickly(page, label, text):
    page.get_by_role("button", name=label, exact=True).click()
    box = page.get_by_role("textbox", name="Comment", exact=True)
    box.fill(text)
    box.press("Control+Enter")
    expect(box).to_have_count(0)


def test_the_comment_and_message_limits(ui, page):
    mount_review(ui)
    for number in range(25):
        add_quickly(page, "Comment on line 1", f"{number:02d}" + "x" * 1998)
    send = page.get_by_role("button", name="Send 25 review comments")
    send.click()
    expect(review(page).get_by_role("alert")).to_have_text("The comments are too long to send at once; remove some first.")
    assert ui.spy("send") == []
    expect(page.locator(".review-comment")).to_have_count(25)
    for number in range(25):
        add_quickly(page, "Comment on line 3", f"short {number}")
    page.get_by_role("button", name="Comment on line 4", exact=True).click()
    page.get_by_role("textbox", name="Comment", exact=True).fill("one too many")
    page.get_by_role("button", name="Add comment").click()
    expect(review(page).get_by_role("alert")).to_have_text("Too many comments to send at once; send some first.")
    expect(page.get_by_role("button", name="Send 50 review comments")).to_be_visible()


def opacity(page, label):
    return page.get_by_role("button", name=label, exact=True).evaluate("button => getComputedStyle(button).opacity")


def test_line_buttons_show_on_hover_and_on_keyboard_focus(ui, page):
    mount_review(ui)
    page.mouse.move(0, 0)
    page.wait_for_function("[...document.querySelectorAll('.line-comment')].every(b => getComputedStyle(b).opacity === '0')")
    page.locator(".diff-line").nth(3).hover()
    page.wait_for_function("getComputedStyle(document.querySelector('[aria-label=\"Comment on line 3\"]')).opacity === '1'")
    assert opacity(page, "Comment on line 1") == "0"
    page.mouse.move(0, 0)
    page.locator(".diff-review-lines").focus()
    page.keyboard.press("Tab")
    page.wait_for_function("getComputedStyle(document.querySelector('[aria-label=\"Comment on line 1\"]')).opacity === '1'")


@pytest.fixture
def touch_ui(browser):
    context = browser.new_context(viewport={"width": 390, "height": 800}, has_touch=True, is_mobile=True)
    page = context.new_page()
    page.route(f"{ORIGIN}/**", serve)
    yield UI(page), page
    context.close()


def test_line_buttons_are_visible_and_large_enough_on_touch_screens(touch_ui):
    ui, page = touch_ui
    mount_review(ui)
    assert page.evaluate("matchMedia('(hover: none)').matches")
    page.wait_for_function("[...document.querySelectorAll('.line-comment')].every(b => getComputedStyle(b).opacity === '0.8')")
    boxes = page.locator(".line-comment").evaluate_all("buttons => buttons.map(b => b.getBoundingClientRect().toJSON())")
    assert all(box["width"] >= 24 and box["height"] >= 24 for box in boxes)
