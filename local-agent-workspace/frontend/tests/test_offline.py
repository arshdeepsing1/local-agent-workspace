"""Offline and connection messages: one reconnect banner, the sidebar status, the Databricks banner and API errors."""
import json

import pytest
from playwright.sync_api import expect

RECONNECTING = "Reconnecting to your local server… Refresh if the server was restarted."
OFFLINE = "Cannot reach the local server. Check that it is still running."
# Requests to /api/sessions… fail the way the browser's fetch does when the server is gone.
GO_OFFLINE = "fake.override = path => path.startsWith('/api/sessions') ? Promise.reject(new TypeError('Failed to fetch')) : undefined"
GO_ONLINE = "fake.override = () => undefined"
NETWORK = {"connected": False, "models": [], "error": "All connection attempts failed", "error_kind": "network"}
CONNECTED = {"connected": True, "models": ["test-model"], "error": None}
# Answers for GET /api/connection, one per request (functions returning a Response or a promise of one).
CONNECTION_QUEUE = """
fake.connectionAnswers = []
const scenarioHandle = fake.handle
fake.handle = (path, options) => {
  if (path === '/api/connection' && fake.connectionAnswers.length) return fake.connectionAnswers.shift()()
  return scenarioHandle(path, options)
}
"""


def open_chat(ui, status="idle", setup=""):
    events = [{"id": "u", "type": "user", "text": "Run the tests"}]
    ui.app(session="a", scenario="workspace",
           setup=f"fake.sessions.set('a', {{ ...makeSession('a', 'Tests'), status: '{status}', events: {json.dumps(events)} }})\n{setup}")
    ui.show_session("a")


def footer(page):
    return page.locator(".sidebar-footer .connection-state")


def test_one_banner_and_the_sidebar_say_the_server_is_offline(ui, page):
    # B-T20. Mutations: keep `&& app.activeId`, or keep setError(e.message) in the poll.
    open_chat(ui, status="running")
    expect(footer(page)).to_have_text("Databricks connected")
    ui.run(GO_OFFLINE)
    calls = ui.js("fake.calls.length")
    ui.run("fake.socketFor('a').close()")
    # The 1.5 s poll of a busy chat fails while the server is gone.
    ui.wait(f"fake.calls.slice({calls}).some(call => call.path === '/api/sessions')")
    page.wait_for_timeout(300)
    banner = page.locator(".connection-banner")
    expect(banner).to_have_count(1)
    expect(banner).to_have_text(RECONNECTING)
    expect(banner).to_have_attribute("role", "status")
    expect(page.locator(".app-error")).to_have_count(0)
    expect(page.get_by_text("Failed to fetch")).to_have_count(0)
    expect(footer(page)).to_have_text("Local server offline")
    expect(footer(page)).to_have_attribute("title", "Reconnecting to your local server…")
    checks = len(ui.requests("/api/connection"))
    ui.run(GO_ONLINE)
    ui.show_session("a", count=2)
    expect(page.locator(".connection-banner")).to_have_count(0)
    expect(footer(page)).to_have_text("Databricks connected")
    ui.wait(f"fake.requests('/api/connection').length > {checks}")
    expect(page.locator(".app-error")).to_have_count(0)


def test_the_welcome_screen_notices_the_server_is_gone_and_back(ui, page):
    # B-T21. Mutation: leave the poll interval at 10 s while unreachable (the banner stays after 2 s).
    ui.app(scenario="workspace", clock=True)
    expect(footer(page)).to_have_text("Databricks connected")
    ui.run(GO_OFFLINE)
    ui.tick(10_000)
    expect(page.locator(".connection-banner")).to_have_text(RECONNECTING)
    expect(footer(page)).to_have_text("Local server offline")
    expect(page.locator(".app-error")).to_have_count(0)
    ui.run(GO_ONLINE)
    ui.tick(2_000)
    expect(page.locator(".connection-banner")).to_have_count(0)
    expect(footer(page)).to_have_text("Databricks connected")


def test_a_send_while_offline_keeps_the_draft_and_shows_one_banner(ui, page):
    # B-T22. Mutation: show the error banner while offline.
    open_chat(ui)
    ui.run(GO_OFFLINE)
    ui.run("fake.socketFor('a').close()")
    expect(page.locator(".connection-banner")).to_have_text(RECONNECTING)
    box = page.get_by_role("textbox", name="Message")
    box.fill("Please check the logs")
    calls = ui.js("fake.calls.length")
    box.press("Enter")
    ui.wait(f"fake.calls.slice({calls}).some(call => call.path === '/api/sessions/a/messages')")
    page.wait_for_timeout(200)
    expect(page.locator(".app-error")).to_have_count(0)
    expect(page.locator(".connection-banner")).to_have_count(1)
    expect(box).to_have_value("Please check the logs")
    ui.run(GO_ONLINE)
    ui.show_session("a", count=2)
    expect(page.locator(".connection-banner")).to_have_count(0)
    expect(page.locator(".app-error")).to_have_count(0)
    expect(box).to_have_value("Please check the logs")


def test_a_databricks_network_failure_explains_itself_and_check_again_keeps_focus(ui, page):
    # B-T23. Mutations: print connection.error directly; call checkConnection() without keep.
    ui.app(scenario="workspace", setup=CONNECTION_QUEUE + f"""
      fake.settings = {{ ...fake.settings, host: 'https://example.cloud.databricks.com' }}
      fake.connectionAnswers.push(() => fake.json({json.dumps(NETWORK)}),
        () => fake.defer('second').promise, () => fake.defer('third').promise)""")
    banner = page.locator(".connection-banner")
    expect(banner).to_contain_text("Can't reach Databricks at https://example.cloud.databricks.com. Check your network or VPN connection.")
    details = banner.locator("details.banner-details")
    assert details.evaluate("e => e.open") is False
    expect(details).not_to_contain_text("All connection attempts failed", use_inner_text=True)
    banner.get_by_text("Details", exact=True).click()
    expect(details).to_contain_text("All connection attempts failed", use_inner_text=True)
    expect(footer(page)).to_have_text("Connection needs attention")
    check = page.get_by_role("button", name="Check again")
    expect(check).to_have_attribute("aria-busy", "false")
    check.focus()
    check.press("Enter")
    ui.wait("fake.requests('/api/connection').length === 2")
    expect(banner).to_be_visible()
    expect(check).to_have_attribute("aria-busy", "true")
    expect(check).to_have_attribute("aria-disabled", "true")
    expect(footer(page)).not_to_have_text("Checking Databricks…")
    check.press("Enter")  # Ignored while checking.
    ui.run(f"fake.deferreds.second.resolve(fake.json({json.dumps(NETWORK)}))")
    expect(check).to_have_attribute("aria-busy", "false")
    assert ui.js("document.activeElement.textContent") == "Check again"
    assert len(ui.requests("/api/connection")) == 2
    check.press("Enter")
    ui.wait("fake.requests('/api/connection').length === 3")
    ui.run(f"fake.deferreds.third.resolve(fake.json({json.dumps(CONNECTED)}))")
    expect(page.locator(".connection-banner")).to_have_count(0)
    expect(page.get_by_role("textbox", name="Message")).to_be_focused()
    expect(footer(page)).to_have_text("Databricks connected")


def test_an_unreadable_credential_file_is_a_connection_problem(ui, page):
    # B-T23b. Mutation: drop the status >= 400 mapping in checkConnection.
    message = "macOS denied access to the credential file."
    ui.app(scenario="workspace", setup=CONNECTION_QUEUE + f"""
      fake.connectionAnswers.push(() => fake.json({{ detail: {json.dumps(message)} }}, 400))""")
    expect(footer(page)).to_have_text("Connection needs attention")
    banner = page.locator(".connection-banner")
    expect(banner).to_contain_text(message)
    expect(banner.get_by_role("button", name="Open settings")).to_be_visible()
    expect(banner.get_by_role("button", name="Check again")).to_have_count(0)
    expect(page.locator(".app-error")).to_have_count(0)


@pytest.mark.parametrize("answer, text, recheck", [
    ({"connected": False, "models": [], "error": None}, "Could not check the Databricks connection.", True),
    ({"connected": False, "models": [], "error": "Databricks returned HTTP 401. Check your credentials and access.",
      "error_kind": "http", "http_status": 401}, "Databricks returned HTTP 401. Check your credentials and access.", False),
])
def test_other_connection_problems_show_their_text(ui, page, answer, text, recheck):
    ui.app(scenario="workspace", setup=CONNECTION_QUEUE + f"fake.connectionAnswers.push(() => fake.json({json.dumps(answer)}))")
    banner = page.locator(".connection-banner")
    expect(banner).to_contain_text(text)
    expect(banner.get_by_role("button", name="Check again")).to_have_count(1 if recheck else 0)


def test_both_banners_fit_a_phone(ui, page):
    # B-T24 (the font size is ui-visual's).
    page.set_viewport_size({"width": 360, "height": 740})
    open_chat(ui, setup=CONNECTION_QUEUE + f"""
      fake.settings = {{ ...fake.settings, host: 'https://adb-1234567890123456.7.azuredatabricks.net' }}
      fake.connectionAnswers.push(() => fake.json({json.dumps(NETWORK)}))""")
    page.locator(".connection-banner").get_by_text("Details", exact=True).click()
    expect(page.get_by_role("button", name="Check again")).to_be_visible()
    widths = "[document.documentElement.scrollWidth, document.documentElement.clientWidth]"
    first = ui.js(widths)
    assert first[0] <= first[1], first
    for button in page.locator(".connection-banner button").all():
        box = button.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 360
    ui.run(GO_OFFLINE)
    ui.run("fake.socketFor('a').close()")
    expect(page.locator(".connection-banner")).to_have_text(RECONNECTING)
    second = ui.js(widths)
    assert second[0] <= second[1], second


def failing_create(response_js):
    return f"""const scenarioHandle = fake.handle
      fake.handle = (path, options) => path === '/api/sessions' && options.method === 'POST' ? {response_js} : scenarioHandle(path, options)"""


@pytest.mark.parametrize("response_js, message", [
    ("new Response('<html><body>Bad gateway</body></html>', { status: 502, headers: { 'Content-Type': 'text/html' } })",
     "The local server sent an unexpected response (HTTP 502)."),
    ("fake.json({ detail: [{ type: 'string_too_long', loc: ['body', 'title'], msg: 'String should have at most 160 characters' }] }, 422)",
     "title: String should have at most 160 characters"),
])
def test_unreadable_and_validation_errors_read_as_sentences(ui, page, response_js, message):
    # B-T25. Mutation: restore unconditional response.json().
    ui.app(scenario="workspace", setup=failing_create(response_js))
    box = page.get_by_role("textbox", name="Message")
    box.fill("Start")
    box.press("Enter")
    expect(page.locator(".app-error")).to_have_text(message)
    expect(page.locator(".connection-banner")).to_have_count(0)


def test_an_unexpected_test_request_is_not_mistaken_for_offline(ui, page):
    # B-T25b. Mutation: map every fetch rejection to OFFLINE_MESSAGE.
    ui.app(scenario="workspace", setup=failing_create("undefined"))
    box = page.get_by_role("textbox", name="Message")
    box.fill("Start")
    box.press("Enter")
    expect(page.locator(".app-error")).to_have_text("Unexpected test request: POST /api/sessions")
    expect(page.get_by_text(OFFLINE)).to_have_count(0)
    expect(page.locator(".connection-banner")).to_have_count(0)
    expect(footer(page)).to_have_text("Databricks connected")
