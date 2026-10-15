"""Transcript paging: the first snapshot holds the latest events, earlier ones load on request."""
import re

from playwright.sync_api import expect

# Chat 'a' holds events 100..249 of 250; /events answers the page before the first loaded one.
SETUP = """
window.messages = (from, to) => Array.from({ length: to - from }, (_, k) => ({
  id: `e${from + k}`, type: 'user', text: `Message ${from + k}`, created: from + k }))
fake.sessions = new Map([['a', { ...makeSession('a', 'Long chat'), events: messages(100, 250) }],
  ['b', makeSession('b', 'Short chat')]])
fake.earlier = null
fake.override = path => {
  const before = path.match(/^\\/api\\/sessions\\/a\\/events\\?before=([^&]+)&limit=100$/)?.[1]
  if (!before) return undefined
  const response = { events: messages(Math.max(0, Number(before.slice(1)) - 100), Number(before.slice(1))),
    start: Math.max(0, Number(before.slice(1)) - 100), total: 250 }
  return fake.earlier ? fake.earlier.promise.then(() => fake.json(response)) : fake.json(response)
}
"""


def start(ui, page, setup=""):
    ui.app(session="a", scenario="workspace", setup=SETUP + setup)
    ui.wait("fake.socketsFor('a').length >= 1")
    ui.run("const socket = fake.socketFor('a'); socket.open(); "
           "socket.emit({ type: 'snapshot', session: { ...fake.sessions.get('a'), events_start: 100, events_total: 250 } })")
    expect(page.locator(".user-message").last).to_contain_text("Message 249")


def texts(page):
    return [text.removesuffix("Copy message").strip() for text in page.locator(".user-message").all_inner_texts()]


def test_connect_checks_the_header_not_the_full_conversation(ui, page):
    start(ui, page)
    assert len(ui.requests("/api/sessions/a/info", "GET")) == 1
    assert ui.requests("/api/sessions/a", "GET") == []


# Records the scroll position at the click (what the app anchors to; the page can change height
# after an earlier measurement) and as soon as the earlier messages are in the page, before any
# animation frame: a smooth scroll would still be near the top then.
FIRST_POSITION = """const el = document.querySelector('.transcript')
document.querySelector('.load-earlier').addEventListener('click', () => {
  window.clickPosition = { top: el.scrollTop, height: el.scrollHeight }
}, { capture: true, once: true })
new MutationObserver((records, observer) => {
  if (!el.querySelector('.user-message')?.textContent.includes('Message 0')) return
  window.firstPosition = { top: el.scrollTop, height: el.scrollHeight }
  observer.disconnect()
}).observe(el, { childList: true, subtree: true })"""


def test_earlier_messages_are_prepended_without_moving_the_view(ui, page):
    start(ui, page)
    button = page.get_by_role("button", name="Load earlier messages (100 more)")
    expect(button).to_be_visible()
    ui.run("document.querySelector('.transcript').scrollTo({ top: 0, behavior: 'instant' })")
    before = ui.js("(el => ({ top: el.scrollTop, height: el.scrollHeight }))(document.querySelector('.transcript'))")
    assert before["top"] == 0
    ui.run(FIRST_POSITION)
    button.click()
    expect(page.locator(".user-message").first).to_contain_text("Message 0")
    expect(button).to_have_count(0)
    assert texts(page) == [f"Message {number}" for number in range(250)]
    assert [call["path"] for call in ui.js("fake.requests(/\\/events\\?/)")] == ["/api/sessions/a/events?before=e100&limit=100"]
    first, clicked = ui.js("window.firstPosition"), ui.js("window.clickPosition")
    after = ui.js("(el => ({ top: el.scrollTop, height: el.scrollHeight }))(document.querySelector('.transcript'))")
    assert after["height"] > before["height"]
    assert first["top"] > 0 and abs(first["top"] - (clicked["top"] + first["height"] - clicked["height"])) <= 1
    assert abs(after["top"] - first["top"]) <= 1


def test_focus_moves_to_the_transcript_when_the_last_page_is_loaded(ui, page):
    start(ui, page)
    button = page.get_by_role("button", name="Load earlier messages (100 more)")
    button.focus()
    page.keyboard.press("Enter")
    expect(page.locator(".user-message").first).to_contain_text("Message 0")
    expect(button).to_have_count(0)
    assert ui.js("document.activeElement.className") == "transcript"


def test_one_page_request_at_a_time(ui, page):
    start(ui, page, "fake.earlier = fake.deferred()")
    ui.run("document.querySelector('.load-earlier').click(); document.querySelector('.load-earlier').click()")
    ui.run("fake.earlier.resolve()")
    expect(page.locator(".user-message").first).to_contain_text("Message 0")
    assert len(ui.js("fake.requests(/\\/events\\?/)")) == 1


def test_live_updates_still_apply_and_updates_to_unloaded_events_are_ignored(ui, page):
    start(ui, page)
    # A newer event is appended, and its streamed text arrives as deltas.
    ui.emit("a", {"type": "event", "event": {"id": "reply", "type": "assistant", "text": "Hel", "created": 300}})
    ui.emit("a", {"type": "delta", "id": "reply", "text": "lo"})
    expect(page.locator(".assistant-reply").last).to_contain_text("Hello")
    # An update to an event on the unloaded page is not added out of order.
    ui.emit("a", {"type": "event", "event": {"id": "e5", "type": "user", "text": "Changed 5", "created": 5}})
    expect(page.get_by_text("Changed 5")).to_have_count(0)
    # An update to a loaded event replaces it.
    ui.emit("a", {"type": "event", "event": {"id": "e120", "type": "user", "text": "Changed 120", "created": 120}})
    expect(page.locator(".user-message", has_text="Changed 120")).to_have_count(1)
    page.get_by_role("button", name="Load earlier messages (100 more)").click()
    expect(page.locator(".user-message").first).to_contain_text("Message 0")
    # After prepending, older events are loaded and their updates apply.
    ui.emit("a", {"type": "event", "event": {"id": "e5", "type": "user", "text": "Changed 5", "created": 5}})
    expect(page.locator(".user-message").nth(5)).to_contain_text("Changed 5")
    ui.emit("a", {"type": "delta", "id": "reply", "text": "!"})
    expect(page.locator(".assistant-reply").last).to_contain_text("Hello!")
    assert len(page.locator(".user-message").all()) == 250


def test_a_complete_snapshot_has_no_load_button(ui, page):
    start(ui, page)
    ui.emit("a", {"type": "snapshot", "session": {**{"id": "a", "title": "Long chat", "workspace": "/project", "model": "test-model",
                                                     "status": "idle", "updated": 1, "permission_mode": "manual",
                                                     "allowed_directories": []},
                                                  "events": [{"id": "x", "type": "user", "text": "Only one", "created": 1}]}})
    expect(page.locator(".user-message")).to_have_count(1)
    expect(page.get_by_role("button", name=re.compile("^Load earlier messages"))).to_have_count(0)
