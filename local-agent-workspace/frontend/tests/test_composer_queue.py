"""The composer while the agent works (queued messages, Esc to stop), ! shell commands and /btw side questions."""
import json
import re

import pytest
from playwright.sync_api import expect

QUEUED = [{"id": "q1", "text": "Also update the README", "created": 1, "paused": False},
          {"id": "q2", "text": "x" * 130, "created": 2, "paused": True}]
ROUTES = """fake.override = (path, options) => {
  if (path.startsWith('/api/sessions/a/queue/') && options.method === 'DELETE') return fake.json({ queued_messages: [] })
  if (path === '/api/sessions/a/queue/send') return fake.json({ ok: true })
  if (path === '/api/sessions/a/stop') return fake.json({ ok: true })
  if (path === '/api/sessions/a/shell') return fake.json({ ok: true }, 202)
  if (path === '/api/sessions/a/side-question') return fake.sideAnswer ? fake.sideAnswer.promise
    : fake.json({ answer: 'It changed **two** files.', request_info: { status: 'completed', finish_reason: 'stop' } })
}"""


def start_app(ui, page, session="a", status="idle", setup="", **fields):
    chats = (f"fake.sessions.set('a', {{ ...makeSession('a', 'Work'), status: '{status}', "
             f"events: [{{ id: 'u', type: 'user', text: 'Do the work' }}], ...{json.dumps(fields)} }}); ")
    ui.app(session=session, scenario="workspace", setup=chats + ROUTES + ";" + setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    if session:
        ui.show_session(session)


def message(page):
    return page.get_by_role("textbox", name="Message")


def queue_bar(page):
    return page.get_by_role("group", name="Queued messages")


def test_a_message_sent_while_busy_is_queued_and_can_be_edited_or_removed(ui, page):
    start_app(ui, page, status="running")
    expect(page.get_by_role("button", name="Stop response")).to_be_visible()
    expect(page.get_by_role("button", name="Queue message")).to_have_count(0)
    message(page).fill("Also update the README")
    queue = page.get_by_role("button", name="Queue message")
    expect(queue).to_have_attribute("title", "Sent to the agent after its current step")
    expect(page.get_by_role("button", name="Stop response")).to_be_visible()
    message(page).press("Enter")
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/messages", "POST") == [{"text": "Also update the README"}]
    expect(message(page)).to_have_value("")
    ui.emit("a", {"type": "queue", "queued_messages": QUEUED})
    bar = queue_bar(page)
    expect(bar).to_contain_text("Queued — sent after the agent’s current step")
    expect(bar.locator(".queue-item")).to_have_count(2)
    expect(bar.locator(".queue-item").nth(0)).to_contain_text("Also update the README")
    expect(bar.locator(".queue-item").nth(1).locator(".queue-text")).to_have_text("x" * 119 + "…")
    expect(bar.locator(".queue-item").nth(1)).to_contain_text("Paused")
    expect(bar.locator(".queue-item").nth(0)).not_to_contain_text("Paused")
    expect(bar.get_by_role("button", name="Send queued messages")).to_have_count(0)

    message(page).fill("Draft")
    bar.get_by_role("button", name="Edit queued message").first.click()
    ui.wait("fake.requests('/api/sessions/a/queue/q1', 'DELETE').length === 1")
    expect(message(page)).to_have_value("Draft\nAlso update the README")
    expect(queue_bar(page)).to_have_count(0)  # The response's empty list replaced the queue.
    ui.emit("a", {"type": "queue", "queued_messages": QUEUED[1:]})
    queue_bar(page).get_by_role("button", name="Remove queued message").click()
    ui.wait("fake.requests('/api/sessions/a/queue/q2', 'DELETE').length === 1")
    expect(queue_bar(page)).to_have_count(0)


def test_an_idle_chat_offers_to_send_waiting_messages_and_shows_queue_errors(ui, page):
    start_app(ui, page, queued_messages=[{**QUEUED[0], "paused": True}])
    bar = queue_bar(page)
    expect(bar).to_contain_text("Not sent yet: queued messages wait when a reply is stopped or fails.")
    bar.get_by_role("button", name="Send queued messages").click()
    ui.wait("fake.requests('/api/sessions/a/queue/send', 'POST').length === 1")
    ui.emit("a", {"type": "queue", "queued_messages": [{**QUEUED[0], "paused": True}],
                  "error": "This managed worktree is being removed. Choose another workspace."})
    expect(bar.get_by_role("alert")).to_have_text("This managed worktree is being removed. Choose another workspace.")


def test_a_queue_error_pushed_before_send_is_answered_stays(ui, page):
    # The order slow runners hit: the server's queue error arrives before its answer to Send.
    start_app(ui, page, queued_messages=[{**QUEUED[0], "paused": True}], setup="""const answer = fake.defer('send')
fake.override = (path, options) => path === '/api/sessions/a/queue/send' && options.method === 'POST'
  ? answer.promise.then(() => fake.json({ ok: true })) : undefined""")
    bar = queue_bar(page)
    bar.get_by_role("button", name="Send queued messages").click()
    ui.wait("fake.requests('/api/sessions/a/queue/send', 'POST').length === 1")
    ui.emit("a", {"type": "queue", "queued_messages": [{**QUEUED[0], "paused": True}],
                  "error": "This managed worktree is being removed. Choose another workspace."})
    expect(bar.get_by_role("alert")).to_have_text("This managed worktree is being removed. Choose another workspace.")
    ui.run("fake.deferreds.send.resolve()")
    page.wait_for_timeout(100)  # Let the answered request finish its update.
    expect(bar.get_by_role("alert")).to_have_text("This managed worktree is being removed. Choose another workspace.")


def test_queued_message_buttons_name_their_message_and_keep_focus_in_the_page(ui, page):
    three = [{"id": f"q{number}", "text": text, "created": number, "paused": True}
             for number, text in ((1, "First note"), (2, "Second note " + "y" * 60), (3, "Third note"))]
    start_app(ui, page, queued_messages=three, setup="""
const queueOverride = fake.override
fake.override = (path, options) => {
  if (path.startsWith('/api/sessions/a/queue/') && options.method === 'DELETE') {
    const id = path.split('/').pop()
    const left = fake.sessions.get('a').queued_messages.filter(item => item.id !== id)
    fake.sessions.set('a', { ...fake.sessions.get('a'), queued_messages: left })
    return fake.json({ queued_messages: left })
  }
  return queueOverride(path, options)
}""")
    bar = queue_bar(page)
    expect(bar.get_by_role("button", name=re.compile("^Remove queued message: "))).to_have_count(3)
    expect(bar.get_by_role("button", name="Edit queued message: First note", exact=True)).to_be_visible()
    second = "Second note " + "y" * 27 + "…"
    expect(bar.get_by_role("button", name=f"Remove queued message: {second}", exact=True)).to_be_visible()
    bar.get_by_role("button", name=f"Remove queued message: {second}", exact=True).click()
    ui.wait("fake.requests('/api/sessions/a/queue/q2', 'DELETE').length === 1")
    # The Remove button now in that row, never <body>.
    expect(bar.get_by_role("button", name="Remove queued message: Third note", exact=True)).to_be_focused()
    page.keyboard.press("Enter")
    expect(bar.get_by_role("button", name="Remove queued message: First note", exact=True)).to_be_focused()
    page.keyboard.press("Enter")
    expect(queue_bar(page)).to_have_count(0)
    expect(message(page)).to_be_focused()


def test_sending_queued_messages_moves_focus_to_the_message_box(ui, page):
    start_app(ui, page, queued_messages=[{**QUEUED[0], "paused": True}],
              setup="fake.override = (path, options) => path === '/api/sessions/a/queue/send' ? "
                    "(fake.sessions.set('a', { ...fake.sessions.get('a'), queued_messages: [] }), fake.json({ ok: true })) : undefined")
    queue_bar(page).get_by_role("button", name="Send queued messages").click()
    ui.wait("fake.requests('/api/sessions/a/queue/send', 'POST').length === 1")
    ui.emit("a", {"type": "queue", "queued_messages": []})
    expect(queue_bar(page)).to_have_count(0)
    expect(message(page)).to_be_focused()


def test_editing_a_queued_plain_text_message_keeps_it_plain_text(ui, page):
    # Back in the box with Send as plain text on: its '!' does not run as a shell command, nor '/x' as a command.
    queued = [{"id": "q1", "text": "!git status", "created": 1, "paused": True, "verbatim": True},
              {"id": "q2", "text": "/rename Shipping notes", "created": 2, "paused": True, "verbatim": True},
              {"id": "q3", "text": "!ls", "created": 3, "paused": True}]
    start_app(ui, page, queued_messages=queued, setup="""
const queueOverride = fake.override
fake.override = (path, options) => {
  if (path.startsWith('/api/sessions/a/queue/') && options.method === 'DELETE') {
    const left = fake.sessions.get('a').queued_messages.filter(item => item.id !== path.split('/').pop())
    fake.sessions.set('a', { ...fake.sessions.get('a'), queued_messages: left })
    return fake.json({ queued_messages: left })
  }
  return queueOverride(path, options)
}""")
    bar = queue_bar(page)
    toggle = page.get_by_role("button", name="Send as plain text")
    expect(bar.locator(".queue-tag", has_text="Plain text")).to_have_count(2)
    for number, text in ((1, "!git status"), (2, "/rename Shipping notes")):
        bar.get_by_role("button", name=f"Edit queued message: {text}", exact=True).click()
        expect(message(page)).to_have_value(text)
        expect(toggle).to_have_attribute("aria-pressed", "true")
        expect(page.get_by_role("status").filter(has_text="Plain text: sent exactly as typed")).to_be_visible()
        message(page).press("Enter")
        ui.wait(f"fake.requests('/api/sessions/a/messages', 'POST').length === {number}")
        expect(message(page)).to_have_value("")
        expect(toggle).to_have_attribute("aria-pressed", "false")
    assert ui.bodies("/api/sessions/a/messages", "POST") == [{"text": "!git status", "verbatim": True},
                                                              {"text": "/rename Shipping notes", "verbatim": True}]
    assert ui.requests("/api/sessions/a/shell") == [] and ui.requests("/api/sessions/a/title") == []
    # A queued message that was not plain text comes back as it was typed.
    bar.get_by_role("button", name="Edit queued message: !ls", exact=True).click()
    expect(message(page)).to_have_value("!ls")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    expect(page.get_by_role("status").filter(has_text="Shell command: Enter runs it")).to_be_visible()


def test_escape_in_the_message_box_stops_a_busy_chat(ui, page):
    start_app(ui, page, status="running")
    message(page).press("Escape")
    ui.wait("fake.requests('/api/sessions/a/stop', 'POST').length === 1")
    ui.emit("a", {"type": "status", "status": "idle"})
    message(page).press("Escape")
    page.wait_for_timeout(100)
    assert len(ui.requests("/api/sessions/a/stop", "POST")) == 1


def test_a_shell_command_runs_once_and_its_output_waits_for_the_next_message(ui, page):
    start_app(ui, page)
    box = message(page)
    box.fill("!ls -la src")
    expect(page.get_by_role("status").filter(has_text="Shell command: Enter runs it in the project folder, even in Plan mode. "
                                             "Its output is sent to the agent with your next message.")).to_be_visible()
    box.press("Enter")
    ui.wait("fake.requests('/api/sessions/a/shell', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/shell", "POST") == [{"command": "ls -la src"}]
    assert not ui.requests("/api/sessions/a/messages")
    expect(box).to_have_value("")
    # What the server pushes when the command has finished, and again when a message took the output.
    ui.emit("a", {"type": "pending_context", "pending_context": [{"kind": "shell", "command": "ls -la src", "exit_code": 0}]})
    expect(page.locator(".shell-chip")).to_have_text("Output of ! ls -la src (exit 0) goes with your next message")
    ui.emit("a", {"type": "pending_context", "pending_context": []})
    expect(page.locator(".shell-chip")).to_have_count(0)
    # More than one line is never sent, and neither is a command while the chat is busy.
    box.fill("!echo one\necho two")
    expect(page.get_by_role("status").filter(has_text="Shell mode runs one line.")).to_be_visible()
    box.press("End")
    box.press("Enter")
    ui.emit("a", {"type": "status", "status": "running"})
    box.fill("!make test")
    expect(page.get_by_role("status").filter(has_text="Wait for the reply to finish before running a command.")).to_be_visible()
    expect(page.get_by_role("button", name="Queue message")).to_have_count(0)
    box.press("Enter")
    page.wait_for_timeout(100)
    assert len(ui.requests("/api/sessions/a/shell", "POST")) == 1 and not ui.requests("/api/sessions/a/messages")


def test_a_side_question_shows_its_answer_without_sending_a_message(ui, page):
    start_app(ui, page, setup="fake.sideAnswer = fake.deferred()")
    box = message(page)
    box.fill("/btw what changed so far?")
    box.press("Enter")
    ui.wait("fake.requests('/api/sessions/a/side-question', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/side-question", "POST") == [{"question": "what changed so far?"}]
    region = page.get_by_role("region", name="Side question")
    expect(region).to_contain_text("what changed so far?")
    expect(region.get_by_role("status")).to_have_text("Answering…")
    ui.run("fake.sideAnswer.resolve(fake.json({ answer: 'It changed **two** files.', request_info: { status: 'completed' } }))")
    expect(region.locator(".side-body strong")).to_have_text("two")
    expect(region).to_contain_text("Not added to the conversation. Uses one model request with the whole conversation as context.")
    expect(box).to_have_value("")
    region.get_by_role("button", name="Copy answer").click()
    ui.wait("fake.clipboard.length === 1")
    assert ui.js("fake.clipboard") == ["It changed **two** files."]
    region.get_by_role("button", name="Dismiss").click()
    expect(page.get_by_role("region", name="Side question")).to_have_count(0)
    expect(box).to_be_focused()
    assert not ui.requests("/api/sessions/a/messages")


def test_the_queue_button_is_an_outlined_icon_beside_stop(ui, page):
    start_app(ui, page, status="running")
    message(page).fill("Also update the README")
    queue = page.get_by_role("button", name="Queue message")
    style = queue.evaluate("""button => { const style = getComputedStyle(button), icon = getComputedStyle(button.querySelector('svg'))
      return { background: style.backgroundColor, color: style.color, stroke: icon.stroke, margin: style.marginRight,
               icon: button.querySelector('svg').getAttribute('class') } }""")
    assert style["background"] == "rgb(255, 255, 255)" and style["margin"] == "6px"
    assert style["color"] == style["stroke"] != style["background"]
    assert "lucide-list-plus" in style["icon"]


def test_a_pending_side_question_leaves_the_box_free(ui, page):
    start_app(ui, page, status="running", setup="fake.sideAnswer = fake.deferred()")
    box = message(page)
    box.fill("/btw how far along?")
    expect(page.get_by_role("button", name="Queue message")).to_have_count(0)
    page.get_by_role("button", name="Ask side question").click()
    ui.wait("fake.requests('/api/sessions/a/side-question', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/side-question", "POST") == [{"question": "how far along?"}]
    region = page.get_by_role("region", name="Side question")
    expect(region.get_by_role("status")).to_have_text("Answering…")
    expect(box).to_have_value("")
    box.fill("Also fix the tests")
    expect(page.get_by_role("button", name="Queue message")).to_be_enabled()
    # A slow answer to the queued message, as on a busy machine: Enter is ignored while a message is
    # being sent, so the next step waits until Queue message is enabled again.
    ui.run("""const send = window.fetch; window.fetch = async (url, options) => {
      const response = await send(url, options)
      if (String(url).endsWith('/messages')) await new Promise(done => setTimeout(done, 400))
      return response
    }""")
    box.press("Enter")
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/messages", "POST") == [{"text": "Also fix the tests"}]
    box.fill("x")
    expect(page.get_by_role("button", name="Queue message")).to_be_enabled()
    box.fill("/btw and now?")  # One side question at a time: the text stays, with the reason.
    box.press("Enter")
    expect(page.get_by_role("alert")).to_contain_text("A side question is already being answered in this conversation.")
    expect(box).to_have_value("/btw and now?")
    assert len(ui.requests("/api/sessions/a/side-question", "POST")) == 1
    ui.run("fake.sideAnswer.resolve(fake.json({ answer: 'Halfway.', request_info: { status: 'completed' } }))")
    expect(region.get_by_role("status")).to_have_text("Side answer ready.")
    expect(region.locator(".side-body")).to_have_text("Halfway.")


def test_a_failed_side_question_returns_its_text_and_copy_failures_are_reported(ui, page):
    start_app(ui, page, setup="fake.sideAnswer = fake.deferred()")
    box = message(page)
    box.fill("/btw what changed?")
    box.press("Enter")
    ui.wait("fake.requests('/api/sessions/a/side-question', 'POST').length === 1")
    expect(box).to_have_value("")
    ui.run("fake.sideAnswer.resolve(fake.json({ detail: 'This conversation is too long for a side question right now. Compact it first.' }, 400))")
    expect(page.get_by_role("alert")).to_contain_text("This conversation is too long for a side question right now.")
    expect(box).to_have_value("/btw what changed?")
    expect(page.get_by_role("region", name="Side question")).to_have_count(0)
    ui.run("fake.sideAnswer = null; navigator.clipboard.writeText = async () => { throw new Error('denied') }")
    box.press("Enter")
    region = page.get_by_role("region", name="Side question")
    copy = region.get_by_role("button", name="Copy answer")
    copy.click()
    expect(page.get_by_role("alert")).to_contain_text("Could not copy the answer. Select its text and copy it instead.")
    expect(copy).to_have_text("Copy answer")


def test_a_side_question_while_busy_and_without_a_conversation(ui, page):
    start_app(ui, page, status="running")
    message(page).fill("/btw is it nearly done?")
    message(page).press("Enter")
    expect(page.get_by_role("region", name="Side question")).to_contain_text("It changed two files.")
    assert not ui.requests("/api/sessions/a/messages")
    page.get_by_role("button", name="Dismiss").click()

    ui.run("window.history.pushState(null, '', '?'); window.dispatchEvent(new PopStateEvent('popstate'))")
    expect(page.get_by_role("heading", name="What’s up next?")).to_be_visible()
    message(page).fill("/btw anything?")
    message(page).press("Enter")
    expect(page.get_by_role("alert")).to_contain_text("Start a conversation first.")
    assert not ui.requests("/api/sessions", "POST")


def test_the_slash_menu_offers_the_side_question_first(ui, page):
    start_app(ui, page)
    box = message(page)
    box.press_sequentially("/")
    first = page.get_by_role("listbox", name="Skills").get_by_role("option").first
    expect(first).to_contain_text("/btw")
    expect(first).to_contain_text("Side question")
    expect(first).to_contain_text("Ask about this conversation without adding to it (one model request).")
    box.press("Enter")
    expect(box).to_have_value("/btw ")


def put_text(page, text):
    """Set the message box as a paste does (fill() types long text slowly)."""
    message(page).evaluate("(box, text) => { box.value = text; box.dispatchEvent(new Event('input', { bubbles: true })) }",
                           text)


def test_a_message_over_50000_characters_is_not_sent_and_gets_advice(ui, page):
    start_app(ui, page)
    put_text(page, "x" * 50_001)
    message(page).press("Enter")
    expect(page.get_by_role("alert")).to_contain_text(
        "Messages are limited to 50,000 characters (this one has 50,001). Save long text such as a handoff as a file "
        "in the project and attach it with @")
    assert not ui.requests("/api/sessions/a/messages")
    assert message(page).input_value() == "x" * 50_001
    # The limit counts code points, as the server does: 30,000 emoji are 60,000 UTF-16 units.
    page.get_by_role("button", name="Dismiss").click()
    put_text(page, "\U0001F600" * 30_000)
    message(page).press("Enter")
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")

    # In a new draft no empty conversation is created.
    ui.run("window.history.pushState(null, '', '?'); window.dispatchEvent(new PopStateEvent('popstate'))")
    expect(page.get_by_role("heading", name="What’s up next?")).to_be_visible()
    put_text(page, "y" * 60_000)
    message(page).press("Enter")
    expect(page.get_by_role("alert")).to_contain_text("(this one has 60,000)")
    assert not ui.requests("/api/sessions", "POST")


# "Local" is a label, not a button

@pytest.mark.parametrize("width, height", [(1440, 900), (360, 740)])
def test_local_reads_as_a_label_beside_the_project_buttons(ui, page, width, height):
    # B-T29. Mutation: restore the border on .context-label.
    page.set_viewport_size({"width": width, "height": height})
    start_app(ui, page)
    expect(page.get_by_role("button", name="Local")).to_have_count(0)
    local = page.locator(".project-context .context-label")
    expect(local).to_have_count(1)
    assert local.evaluate("e => getComputedStyle(e).borderTopColor") == "rgba(0, 0, 0, 0)"
    assert "runs on this computer" in local.get_attribute("title")
    if width > 600:
        expect(local).to_be_visible()
        expect(local).to_have_text("Local")
    else:
        expect(local).to_be_hidden()  # Narrow chat columns hide it.
    for name in ("Choose project", "Folder access"):
        chip = page.get_by_role("button", name=name)
        expect(chip).to_be_visible()
        color, width_px = chip.evaluate("e => [getComputedStyle(e).borderTopColor, getComputedStyle(e).borderTopWidth]")
        assert color != "rgba(0, 0, 0, 0)" and width_px != "0px", (name, color, width_px)
    assert page.locator(".project-context").evaluate("e => e.scrollWidth <= e.clientWidth")
