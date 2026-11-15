#!/usr/bin/env python3
"""Write PNG screenshots of a fixed list of screens of the real frontend, with the UI tests' fake backend.

Usage (from the local-agent-workspace folder; needs Playwright and its Chromium, as the UI tests do):
    python3 scripts/ui_screenshots.py --out /tmp/ui/before
    python3 scripts/ui_screenshots.py --out /tmp/ui/b --tokens design/mockups/b.css
    python3 scripts/ui_screenshots.py --out /tmp/ui/b --only chat --only approval --width 1280
    python3 scripts/ui_screenshots.py --tokens design/mockups/b.css --contrast

Each screen is the whole app (static/js/App.js) on the test harness page (frontend/tests/support/harness.html).
The network is the tests' in-page fake backend (tests/support/fake.js and its scenarios), filled with the generic
demo project of scripts/_ui_sample_data.py. No server runs and nothing is sent anywhere.

One file per screen and width: <name>-<width>.png. The picture is the viewport. `chat` and `workflow` grow the viewport so the
whole transcript shows. `approval`, `plan` and `settings` grow it by a fixed limit so the waiting card, or more of
the dialog, shows. The script prints one line per file. It exits
with 1 when a screen did not render, the page is wider than the viewport or the page logged an uncaught error.

--tokens injects a CSS file after the app's stylesheet, so its :root tokens and override rules win. It also forces
the light look. --contrast prints the contrast ratios of the main colour pairs of that file and writes no picture.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ui_sample_data as sample  # noqa: E402

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
SUPPORT = FRONTEND / "tests" / "support"
ORIGIN = "http://localhost:8799"  # The origin frontend/tests/conftest.py uses: a secure context.
CONTENT_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2"}
HEIGHTS = {390: 844}  # Phone height; every other width is 800 high.
TIMEOUT = 8000
MAX_GROWTH = 6000  # The most a picture grows past the viewport, in CSS pixels.
CARD_GROWTH = 900  # The same for a screen whose last card must show whole.

# Colour pairs --contrast reports: (text or line token, background token, the least ratio it needs).
PAIRS = [("ink", "canvas", 4.5), ("ink", "surface", 4.5), ("ink", "sidebar", 4.5), ("ink-2", "surface-2", 4.5),
         ("muted", "canvas", 4.5), ("muted", "sidebar", 4.5), ("muted", "surface-2", 4.5), ("accent-text", "surface", 4.5),
         ("accent-text", "accent-soft", 4.5), ("on-accent", "accent", 4.5), ("on-primary", "primary", 4.5),
         ("danger", "danger-soft", 4.5), ("success", "success-soft", 4.5), ("warning", "warning-soft", 4.5),
         ("info", "info-soft", 4.5), ("terminal-ink", "terminal-bg", 4.5), ("code-keyword", "surface-2", 4.5),
         ("code-string", "surface-2", 4.5), ("border-strong", "surface", 3), ("border-strong", "canvas", 3),
         ("focus", "canvas", 3), ("focus", "sidebar", 3)]


def serve(route):
    """Answer a request from the frontend folder, as the UI tests do."""
    path = unquote(urlsplit(route.request.url).path)
    file = (FRONTEND / (path.lstrip("/") or "index.html")).resolve()
    if not file.is_relative_to(FRONTEND) or not file.is_file():
        route.fulfill(status=404, body="Not found")
        return
    route.fulfill(status=200, body=file.read_bytes(), content_type=CONTENT_TYPES.get(file.suffix, "application/octet-stream"))


class Screen:
    """One page with the app mounted on the harness and the fake backend filled with the sample data."""

    def __init__(self, page, tokens):
        self.page = page
        self.tokens = tokens

    def open(self, session=None):
        page = self.page
        page.add_init_script(path=SUPPORT / "fake.js")
        page.add_init_script(path=SUPPORT / "scenarios" / "workspace.js")
        page.add_init_script(script="window.sampleScenarioHandlers = [fake.handle]")
        page.add_init_script(path=SUPPORT / "scenarios" / "agent_tools.js")
        page.add_init_script(script="window.sampleScenarioHandlers.push(fake.handle)")
        page.add_init_script(script=sample.setup_js())
        page.goto(f"{ORIGIN}/tests/support/harness.html" + (f"?session={session}" if session else ""))
        page.wait_for_function("window.harnessReady === true")
        if self.tokens:
            page.add_style_tag(content=self.tokens)
        page.evaluate("() => preload('App.js', 'default')")
        page.evaluate("() => { mount('App.js', () => ({}), 'default') }")
        self.see("select[aria-label='Model']")
        if session:
            page.wait_for_function(f"fake.socketsFor({json.dumps(session)}).length >= 1")
            page.evaluate(f"() => {{ const socket = fake.socketFor({json.dumps(session)}); socket.open(); "
                          f"socket.emit({{ type: 'snapshot', session: fake.sessions.get({json.dumps(session)}) }}) }}")
            self.session = session

    def emit(self, data):
        self.page.evaluate(f"() => {{ fake.socketFor({json.dumps(self.session)}).emit({json.dumps(data)}) }}")

    def see(self, selector, count=1):
        """Wait until at least `count` elements match and the first one is visible."""
        self.page.wait_for_function("([selector, count]) => document.querySelectorAll(selector).length >= count", arg=[selector, count])
        self.page.locator(selector).first.wait_for(state="visible")

    def press(self, selector):
        """Click from the page, so a control in a closed drawer (the phone's sidebar) works too."""
        self.see_attached(selector)
        self.page.evaluate("selector => document.querySelector(selector).click()", selector)

    def see_attached(self, selector):
        self.page.locator(selector).first.wait_for(state="attached")

    def grow(self, selector, limit=MAX_GROWTH, end=False):
        """Make the viewport taller until the scrolling element `selector` shows all of its content (or `limit` more).

        The element then shows its start, or its end (a card that waits is the last thing in a transcript)."""
        size = self.page.viewport_size
        for _ in range(3):
            extra = self.page.evaluate("selector => { const item = document.querySelector(selector); return item.scrollHeight - item.clientHeight }", selector)
            if extra <= 0 or size["height"] >= self.page.viewport_size["height"] + limit:
                break
            self.page.set_viewport_size({"width": size["width"], "height": min(self.page.viewport_size["height"] + extra, size["height"] + limit)})
        self.page.evaluate("([selector, end]) => { const item = document.querySelector(selector); item.style.scrollBehavior = 'auto'; "
                           "item.scrollTop = end ? item.scrollHeight : 0 }", [selector, end])


def empty(screen):
    screen.open()
    screen.see(".welcome h1")
    screen.see_attached(".conversation-row:nth-of-type(3)")
    screen.see(".composer textarea")


def chat(screen):
    screen.open("chat")
    screen.see(".markdown pre code")
    screen.see(".tool-group-head")
    screen.see(".context-meter")
    screen.press("[data-event-id='cmd-1'] .tool-heading")  # The finished command, with its output.
    screen.see("[data-event-id='cmd-1'] .tool-body pre")
    screen.grow(".transcript")


def approval(screen):
    screen.open("approval")
    screen.see("[data-event-id='ask-1'] .approval-buttons")
    screen.page.locator("[data-event-id='ask-1']").get_by_role("button", name=re.compile("^Always allow…")).click()
    screen.see(".remember-options")
    screen.grow(".transcript", CARD_GROWTH, end=True)


def composer(screen):
    screen.open("busy")
    screen.see(".queue-bar")
    screen.emit({"type": "changes", "changes": sample.CHANGES})
    screen.emit({"type": "tasks", "tasks": sample.TASKS})
    screen.see(".status-chip")  # The status row shows the queue and the changes, and a chip for the tasks.
    screen.page.locator(".chat-composer input[type=file]").set_input_files(
        {"name": "notes.md", "mimeType": "text/markdown", "buffer": b"# Notes\n\nRetry three times.\n"})
    screen.see(".upload-chip")
    screen.page.get_by_role("textbox", name="Message").fill(sample.DRAFT)
    screen.page.wait_for_function("text => document.querySelector('.composer textarea').value === text", arg=sample.DRAFT)


def workspace(screen):
    screen.open("chat")
    screen.see(".markdown pre code")
    screen.emit({"type": "changes", "changes": sample.CHANGES})
    screen.see(".changes-bar")
    screen.press(".changes-toggle")
    screen.press(".changes-open")
    screen.see(".workspace-panel .diff-review .diff-line")


def settings(screen):
    screen.open()
    screen.see(".welcome h1")
    screen.press(".sidebar-footer button")
    screen.see(".app-settings #settings-appearance")
    size = screen.page.viewport_size
    screen.page.set_viewport_size({"width": size["width"], "height": size["height"] + 500})
    screen.page.evaluate("() => document.querySelector('#settings-appearance').scrollIntoView({ block: 'start' })")


def agent_tools(screen):
    screen.open("chat")
    screen.see(".markdown pre code")
    screen.press(".agent-tools-toggle")
    screen.see(".agent-tools-dialog [aria-label='Overview']")


def context(screen):
    screen.open("chat")
    screen.see(".markdown pre code")
    screen.press("details.context-meter > summary")
    screen.see(".context-legend li")


def workflow(screen):
    screen.open("workflow")
    screen.see(".run-head")
    screen.see(".run-batch-approval .approval-buttons")
    screen.see(".tool-card.run-member", 3)
    screen.grow(".transcript")


def plan(screen):
    screen.open("plan")
    screen.see(".plan-card .plan-body")
    screen.see(".plan-card button")
    screen.grow(".transcript", CARD_GROWTH, end=True)


SCREENS = {"empty": empty, "chat": chat, "approval": approval, "composer": composer, "workspace": workspace,
           "settings": settings, "agent-tools": agent_tools, "context": context, "workflow": workflow, "plan": plan}


def shoot(browser, name, width, scale, tokens, out):
    """Render one screen at one width. Returns an error text, or None when the file was written."""
    phone = width < 600
    context = browser.new_context(viewport={"width": width, "height": HEIGHTS.get(width, 800)}, device_scale_factor=scale,
                                  is_mobile=phone, has_touch=phone, color_scheme="light" if tokens else None)
    context.set_default_timeout(TIMEOUT)
    page = context.new_page()
    page.route(f"{ORIGIN}/**", serve)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        SCREENS[name](Screen(page, tokens))
        page.evaluate("() => document.fonts.ready.then(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))))")
        if page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth"):
            errors.append("the page is wider than the viewport")
        page.screenshot(path=out / f"{name}-{width}.png")
    except Exception as error:  # A selector that never came, or a page that did not load.
        errors.append(str(error).splitlines()[0])
    finally:
        context.close()
    return "; ".join(errors) or None


def luminance(colour):
    channels = [int(colour[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    red, green, blue = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first, second):
    """The WCAG contrast ratio of two #rrggbb colours."""
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def solid_tokens(css, selector):
    """The solid #rrggbb custom properties of the first `selector { ... }` block of a CSS text."""
    block = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return {name: value.lower() for name, value in re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b", block.group(1) if block else "")}


def report_contrast(css):
    """Print the ratio of each pair in PAIRS from the file's :root tokens. Returns 1 when one is too low.

    A file that gives the sidebar its own values (a `.sidebar { ... }` block) is checked with them on the sidebar."""
    root = solid_tokens(css, ":root")
    rail = {**root, **solid_tokens(css, ".sidebar")}
    pairs = PAIRS + ([(name, "sidebar", 4.5) for name in ("ink-2", "accent-text", "danger")]
                     + [("on-accent", "accent", 4.5), ("border-strong", "sidebar", 3)] if rail != root else [])
    low = 0
    for index, (text, background, least) in enumerate(pairs):
        tokens = rail if background == "sidebar" or index >= len(PAIRS) else root
        if text not in tokens or background not in tokens:
            print(f"{text} on {background}: not a solid #rrggbb colour, not checked")
            continue
        ratio = contrast(tokens[text], tokens[background])
        low += ratio < least
        where = " (sidebar values)" if tokens is rail and rail != root else ""
        print(f"{text} on {background}{where}: {ratio:.2f} (needs {least}){'' if ratio >= least else '  TOO LOW'}")
    return 1 if low else 0


def main(argv):
    parser = argparse.ArgumentParser(description="Write PNG screenshots of the app's main screens with the UI tests' fake backend.")
    parser.add_argument("--out", type=Path, help="folder for the PNG files (created when missing)")
    parser.add_argument("--tokens", type=Path, help="a CSS file injected after the app's stylesheet (a mock-up direction)")
    parser.add_argument("--only", action="append", choices=list(SCREENS), help="render only this screen (repeatable)")
    parser.add_argument("--width", default="1280,390", help="viewport widths, comma-separated (default 1280,390)")
    parser.add_argument("--scale", type=float, default=2, help="device scale factor (default 2)")
    parser.add_argument("--contrast", action="store_true", help="print the contrast ratios of the --tokens file and stop")
    args = parser.parse_args(argv[1:])
    tokens = args.tokens.read_text(encoding="utf-8") if args.tokens else None
    if args.contrast:
        if tokens is None:
            parser.error("--contrast needs --tokens")
        return report_contrast(tokens)
    if args.out is None:
        parser.error("--out is required")
    try:
        widths = [int(value) for value in args.width.split(",") if value.strip()]
    except ValueError:
        parser.error("--width takes whole numbers, for example 1280,390")
    from playwright.sync_api import sync_playwright  # Imported here so --help and --contrast need no browser.

    args.out.mkdir(parents=True, exist_ok=True)
    failed = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for name in args.only or list(SCREENS):
            for width in widths:
                error = shoot(browser, name, width, args.scale, tokens, args.out)
                failed += bool(error)
                print(f"{'FAILED' if error else 'wrote '} {args.out / f'{name}-{width}.png'}{f': {error}' if error else ''}")
        browser.close()
    if failed:
        print(f"{failed} screen(s) failed.", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
