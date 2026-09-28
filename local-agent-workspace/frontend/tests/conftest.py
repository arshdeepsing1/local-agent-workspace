"""Browser tests for the frontend: real Chromium, no Node.js.

Each test loads the UI's own ES modules from frontend/static into a harness page
(tests/support/harness.html). Network access is replaced by an in-page fake
backend (tests/support/fake.js) plus a per-file scenario (tests/support/scenarios).
"""
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from playwright.sync_api import Page, expect, sync_playwright

FRONTEND = Path(__file__).resolve().parents[1]
SUPPORT = FRONTEND / "tests" / "support"
ORIGIN = "http://localhost:8799"  # a secure context, like the real app on 127.0.0.1
CONTENT_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2"}

expect.set_options(timeout=3000)


@pytest.fixture(scope="session")
def browser():
    # UI_TEST_CHROMIUM selects a browser binary; otherwise Playwright's own
    # Chromium is used (install it once with: python -m playwright install chromium).
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("UI_TEST_CHROMIUM") or None)
        yield browser
        browser.close()


def serve(route):
    path = unquote(urlsplit(route.request.url).path)
    file = (FRONTEND / (path.lstrip("/") or "index.html")).resolve()
    if not file.is_relative_to(FRONTEND) or not file.is_file():
        route.fulfill(status=404, body="Not found")
        return
    route.fulfill(status=200, body=file.read_bytes(), content_type=CONTENT_TYPES.get(file.suffix, "application/octet-stream"))


class UI:
    """Starts a harness page and drives the fake backend from Python."""

    def __init__(self, page: Page):
        self.page = page

    def start(self, scenario: str | None = None, setup: str = "", clock: bool = False, query: str = ""):
        self.page.add_init_script(path=SUPPORT / "fake.js")
        if scenario:
            self.page.add_init_script(path=SUPPORT / "scenarios" / f"{scenario}.js")
        if setup:
            self.page.add_init_script(script=setup)
        if clock:
            self.page.clock.install()
        self.page.goto(f"{ORIGIN}/tests/support/harness.html{query}")
        self.page.wait_for_function("window.harnessReady === true")

    def mount(self, module: str, props: str = "{}", name: str = "default", **start):
        """Render one module; `props` is a JavaScript expression evaluated in the page."""
        self.start(**start)
        self.page.evaluate(f"() => mount({json.dumps(module)}, () => ({props}), {json.dumps(name)})")

    def app(self, session: str | None = None, **start):
        """Render the whole app, optionally opened at ?session=<id>."""
        self.mount("App.js", query=f"?session={session}" if session else "", **start)

    def probe(self, module: str, name: str, session: str | None = None, **start):
        """Run a hook alone; its latest result is `hook` in the page."""
        self.start(query=f"?session={session}" if session else "", **start)
        self.page.evaluate(f"() => probe({json.dumps(module)}, {json.dumps(name)})")

    def rerender(self, props: str):
        self.page.evaluate(f"() => rerender(() => ({props}))")

    def js(self, expression: str):
        """Evaluate a JavaScript expression in the page (promises are awaited)."""
        return self.page.evaluate(f"async () => ({expression})")

    def run(self, statements: str):
        """Run JavaScript statements in the page without waiting for anything they start."""
        self.page.evaluate(f"() => {{ {statements} }}")

    def wait(self, expression: str):
        self.page.wait_for_function(f"() => {expression}", timeout=3000)

    def emit(self, session_id: str, data: dict | str):
        payload = data if isinstance(data, str) else json.dumps(data)
        self.run(f"fake.socketFor({json.dumps(session_id)}).emit({payload})")

    def socket_count(self, session_id: str) -> int:
        return self.js(f"fake.socketsFor({json.dumps(session_id)}).length")

    def show_session(self, session_id: str, count: int = 1):
        """Wait for the UI's stream socket, then open it and send the saved snapshot."""
        self.wait(f"fake.socketsFor({json.dumps(session_id)}).length >= {count}")
        self.run(f"const socket = fake.socketFor({json.dumps(session_id)}); socket.open(); "
                 f"socket.emit({{ type: 'snapshot', session: fake.sessions.get({json.dumps(session_id)}) }})")

    def requests(self, path: str, method: str | None = None) -> list[dict]:
        return self.js(f"fake.requests({json.dumps(path)}, {json.dumps(method)})")

    def bodies(self, path: str, method: str | None = None) -> list:
        return self.js(f"fake.bodies({json.dumps(path)}, {json.dumps(method)})")

    def spy(self, name: str) -> list:
        return self.js(f"fake.spies[{json.dumps(name)}] || []")

    def tick(self, ms: int):
        """Advance the faked clock (tests started with clock=True)."""
        self.page.clock.run_for(ms)


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.route(f"{ORIGIN}/**", serve)
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    yield page
    context.close()
    assert not errors, f"Uncaught errors in the page: {errors}"


@pytest.fixture
def ui(page):
    return UI(page)
