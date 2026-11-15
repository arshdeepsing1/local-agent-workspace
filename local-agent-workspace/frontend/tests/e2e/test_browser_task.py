"""The browser tool through the real page, app and simulator: the simulator's "browse the form <address>" turn opens a form
served on loopback, types a name, clicks Send order, reads the result page and takes a screenshot. The first host asks on a
card, answered in the page; Auto then runs the other four actions without a card, and the screenshot shows as a thumbnail.

The pages are served by this test on 127.0.0.1; nothing leaves the computer. The test skips, with the reason, when the
Playwright package or its Chromium is missing (the browser tool needs both)."""
import html
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from .helpers import send

FORM = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Order form</title></head><body>
<h1>Order form</h1><form action="result.html" method="get"><label for="name">Name</label>
<input id="name" name="name" type="text"><button type="submit">Send order</button></form></body></html>"""
RESULT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Order received</title></head><body>
<h1>Order received</h1><p>Thank you, {name}.</p></body></html>"""


@pytest.fixture
def form_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parts = urlsplit(self.path)
            requests.append(self.path)
            if parts.path == "/form.html":
                body = FORM
            elif parts.path == "/result.html":
                body = RESULT.format(name=html.escape((parse_qs(parts.query).get("name") or [""])[0]))
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.requests, server.port = requests, server.server_address[1]
    yield server
    server.shutdown()
    server.server_close()


def need_browser_tool():
    from local_agent.browser import browser_status
    status = browser_status()
    if not status["ready"]:
        pytest.skip(f"The browser tool cannot run here: {status['problem']}")


def test_the_five_step_browse_task_runs_from_the_page_with_one_card_and_shows_its_screenshot(app_page, stack, form_server):
    need_browser_tool()
    page = app_page
    stack.manager.settings.values.update(browser_tools=True, browser_headless="headless")
    key = f"localhost:{form_server.port}"
    page.get_by_role("button", name=re.compile("^Permissions:")).click()
    page.get_by_role("menuitemradio", name=re.compile("^Auto")).click()
    send(page, f"Please browse the form http://{key}/form.html")
    # The host is new, so the first action asks, before any request leaves; the answer allows the host for this chat.
    card = page.locator(".tool-card.needs-approval")
    expect(card).to_have_count(1, timeout=30_000)
    expect(card.locator(".tool-name")).to_have_text(f"Browse {key}")
    assert form_server.requests == []
    card.get_by_role("button", name=f"Allow {key} in this chat", exact=True).click()
    expect(page.get_by_role("img", name=f"Screenshot of {key}")).to_be_visible(timeout=90_000)
    expect(page.locator("details.turn-summary > summary")).to_have_count(1, timeout=60_000)
    # Five browser cards in order, each with its action and host; none waits for an answer.
    expect(page.locator(".tool-card .tool-name")).to_have_text(
        [f"Browse {key}", f"Type on {key}", f"Click on {key}", f"Read {key}", f"Screenshot of {key}"])
    expect(page.locator(".tool-card.needs-approval")).to_have_count(0)
    # The server side agrees: five completed browser calls, one approval, the form sent with the typed name.
    [listed] = stack.manager.store.list()
    events = stack.manager.get(listed["id"])["events"]
    calls = [event for event in events if event.get("type") == "tool"]
    assert [event["name"] for event in calls] == ["browser"] * 5 and [event["state"] for event in calls] == ["completed"] * 5
    assert [event.get("approval") for event in calls] == ["approved", None, None, None, None]
    assert "Order received" in calls[3]["output"] and "Ada Lovelace" in calls[3]["output"]
    assert any(request.startswith("/result.html?name=Ada+Lovelace") for request in form_server.requests)
    assert calls[4]["browser"]["image"]["upload"]
    # Opening the screenshot shows it full size.
    page.get_by_role("button", name=f"Open the screenshot of {key} full size").click()
    expect(page.get_by_role("img", name=f"Screenshot of {key}")).not_to_have_class("browser-thumb")
