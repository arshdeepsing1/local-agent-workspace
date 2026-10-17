"""The Workspace preview and Download, and a project's settings file, through the real page and app (M8g2, M5f3):
the server's own headers and the page's own Content-Security-Policy apply, which the in-page fake backend of the
other UI tests cannot show."""
import json
import struct
import zlib

import pytest
from playwright.sync_api import expect

from .helpers import send


def png(width, height):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"\xa3\x5a\x40\xff" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@pytest.fixture
def files(stack):
    """Files to preview, written before the page opens."""
    (stack.project / "logo.png").write_bytes(png(3, 2))
    (stack.project / "page.html").write_text(
        "<!doctype html><title>x</title><p id=t>Static page</p>"
        "<script>document.getElementById('t').textContent = 'Ran'</script>")
    (stack.project / "data.bin").write_bytes(bytes(range(256)))
    # An SVG whose script tries to read the app's token when the image is opened as a page of its own.
    (stack.project / "logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="3"><rect width="4" height="3" fill="teal"/><script>'
        f'fetch({json.dumps(stack.url.rstrip("/") + "/api/bootstrap")}).then(r => r.json()).then(d => '
        'document.documentElement.setAttribute("data-token", d.token ? "read" : "none"), () => '
        'document.documentElement.setAttribute("data-token", "blocked"))</script></svg>')
    return stack.project


def panel(page):
    return page.get_by_role("complementary", name="Workspace panel")


def open_workspace(page):
    page.get_by_role("button", name="Workspace", exact=True).click()
    expect(panel(page).locator(".file-row", has_text="logo.png")).to_be_visible()


def test_an_image_previews_and_any_file_downloads_from_the_server(files, app_page):
    page = app_page
    open_workspace(page)
    panel(page).locator(".file-row", has_text="logo.png").click()
    image = panel(page).get_by_role("img", name="logo.png")
    expect(image).to_be_visible()
    assert image.evaluate("img => [img.naturalWidth, img.naturalHeight]") == [3, 2]
    expect(panel(page).get_by_text("3 × 2 px")).to_be_visible()
    with page.expect_download() as download:
        panel(page).get_by_role("button", name="Download file").click()
    assert download.value.suggested_filename == "logo.png"
    assert open(download.value.path(), "rb").read() == (files / "logo.png").read_bytes()
    # A file that is not text has no editor, only Download.
    panel(page).get_by_role("button", name="Back to files").click()
    panel(page).locator(".file-row", has_text="data.bin").click()
    expect(panel(page).get_by_text("This file is not UTF-8 text, so the text editor cannot open it.")).to_be_visible()
    with page.expect_download() as download:
        panel(page).locator(".file-preview-none").get_by_role("button", name="Download").click()
    assert open(download.value.path(), "rb").read() == bytes(range(256))


def test_an_svg_opened_in_a_tab_of_its_own_cannot_reach_the_app(files, app_page):
    page = app_page
    open_workspace(page)
    panel(page).locator(".file-row", has_text="logo.svg").click()
    image = panel(page).get_by_role("img", name="logo.svg")
    expect(image).to_be_visible()
    # What Open image in new tab does: the image's address as a page, in the same browser profile.
    other = page.context.new_page()
    other.goto(image.get_attribute("src"))
    other.wait_for_function("document.documentElement.getAttribute('data-token') !== null")
    assert other.evaluate("[location.origin, document.documentElement.getAttribute('data-token')]") == ["null", "blocked"]
    other.close()


def test_an_html_file_previews_in_a_sandbox_under_the_pages_policy(files, app_page):
    page = app_page
    open_workspace(page)
    panel(page).locator(".file-row", has_text="page.html").click()
    panel(page).get_by_role("button", name="Preview", exact=True).click()
    frame = page.frame_locator(".file-preview-frame")
    expect(frame.get_by_text("Static page")).to_be_visible()
    panel(page).get_by_role("button", name="Run scripts").click()
    expect(page.frame_locator(".file-preview-frame").get_by_text("Ran")).to_be_visible()
    # Its own origin: neither the app's page nor its token can be reached from it.
    reached = page.frame_locator(".file-preview-frame").locator("body").evaluate(
        "() => { try { return window.parent.document.title } catch { return 'blocked' } }")
    assert reached == "blocked"


@pytest.fixture
def project_settings(stack):
    """A shared settings file in the project, written before the page opens: one Agent step per message."""
    (stack.project / ".agents").mkdir()
    (stack.project / ".agents" / "settings.json").write_text(json.dumps({"max_agent_steps": 1, "env_file": "/srv/x"}))
    return stack.project


def test_a_trusted_project_file_sets_what_its_chats_run_with(project_settings, app_page, stack):
    page = app_page
    dialog = page.get_by_role("dialog", name="Project settings files")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_text("Agent steps per message")).to_be_visible()
    expect(dialog.get_by_text("Ignored (only Settings can set it): env_file")).to_be_visible()
    dialog.get_by_role("button", name="Trust").click()
    expect(dialog).to_have_count(0)
    # A chat in the project runs with one step: the read turn stops after its first tool call.
    send(page, "Please read the project")
    stop = page.get_by_role("status").filter(has_text="The agent reached its step limit.")
    expect(stop).to_be_visible(timeout=15_000)
    # The notice names the file that sets the limit, not Settings.
    expect(page.get_by_text("This project's .agents/settings.json sets this limit, not Settings")).to_be_visible()
    expect(page.get_by_text("Increase Agent steps in Settings")).to_have_count(0)
    # Settings name the value and its file; your own value is unchanged.
    page.get_by_role("button", name="Settings", exact=True).click()
    settings = page.get_by_role("dialog", name="Settings")
    expect(settings.get_by_text("In this project: 1 from .agents/settings.json.")).to_be_visible()
    expect(settings.get_by_role("spinbutton", name="Agent steps per message")).to_have_value("32")
    assert stack.manager.settings.values["max_agent_steps"] == 32
