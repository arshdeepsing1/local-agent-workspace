"""The composer while the agent works (queued messages, Esc to stop), ! shell commands and /btw side questions."""
import json

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
    ui.emit("a", {"type": "snapshot", "session": {**{"id": "a", "title": "Work", "workspace": "/project", "model": "test-model",
                                                      "status": "idle", "events": [], "updated": 2, "permission_mode": "plan",
                                                      "allowed_directories": []},
                                                   "pending_context": [{"kind": "shell", "command": "ls -la src", "exit_code": 0}]}})
    expect(page.locator(".shell-chip")).to_have_text("Output of ! ls -la src (exit 0) goes with your next message")
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
    assert not ui.requests("/api/sessions/a/messages")


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
