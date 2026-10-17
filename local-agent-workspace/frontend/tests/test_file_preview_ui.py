"""The Workspace preview and Download (M8g2): images and PDF from their bytes, Markdown rendered without loading
anything from the web, HTML in a sandboxed frame with scripts off until allowed, and files that are not text."""
import base64
import json
import struct
import zlib

from playwright.sync_api import expect


def png(width, height):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"\xa3\x5a\x40\xff" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


PNG = png(2, 1)
FILES = [{"path": name, "name": name, "directory": False}
         for name in ("logo.png", "paper.pdf", "README.md", "page.html", "data.bin")]
TEXT = {"README.md": "# Guide\n\nSee ![logo](logo.png) and ![remote](https://example.com/track.png).\n\n"
                     "[Notes](docs/notes.md) · [Site](https://example.com/)\n",
        "page.html": "<!doctype html><title>x</title><p id=t>Static page</p><script>document.getElementById('t').textContent='Ran'</script>",
        "docs/notes.md": "# Notes\n"}
SETUP = f"""
const png = Uint8Array.from(atob({json.dumps(base64.b64encode(PNG).decode())}), c => c.charCodeAt(0))
const texts = {json.dumps(TEXT)}
fake.captureDownloads()
fake.override = path => {{
  const params = new URLSearchParams(path.split('?')[1] || '')
  if (path.startsWith('/api/files?')) return fake.json({json.dumps(FILES)})
  if (path.startsWith('/api/file/raw?')) {{
    const name = params.get('path')
    if (name === 'missing.png') return fake.json({{ detail: 'missing.png does not exist in /project.' }}, 400)
    const body = name.endsWith('.png') ? png : name.endsWith('.pdf') ? new TextEncoder().encode('%PDF-1.4\\n%%EOF\\n') : new Uint8Array([1, 2, 3])
    return new Response(body, {{ headers: {{ 'Content-Type': params.get('download') ? 'application/octet-stream' : 'image/png' }} }})
  }}
  if (path.startsWith('/api/file?')) {{
    const name = params.get('path')
    if (name in texts) return fake.json({{ path: name, content: texts[name], absolute: `/project/${{name}}` }})
    return fake.json({{ detail: {{ message: 'This file is not UTF-8 text, so the text editor cannot open it.', code: 'not_text' }} }}, 400)
  }}
}}
"""


def start(ui, page):
    ui.app(session="a", scenario="workspace", setup=SETUP)
    ui.show_session("a")
    page.get_by_role("button", name="Workspace", exact=True).click()
    expect(page.locator(".file-row", has_text="logo.png")).to_be_visible()


def panel(page):
    return page.get_by_role("complementary", name="Workspace panel")


def test_an_image_opens_in_the_preview_and_downloads_as_a_file(ui, page):
    start(ui, page)
    page.locator(".file-row", has_text="logo.png").click()
    image = panel(page).get_by_role("img", name="logo.png")
    expect(image).to_be_visible()
    assert image.get_attribute("src").startswith("blob:")
    expect(panel(page).get_by_text(f"2 × 1 px · {len(PNG)} bytes")).to_be_visible()
    # Read from its bytes, never as text; no Save for a file without text.
    assert ui.requests("/api/file?path=logo.png&session_id=a") == []
    assert len(ui.requests("/api/file/raw?path=logo.png&session_id=a")) == 1
    expect(panel(page).get_by_role("button", name="Save file")).to_have_count(0)
    panel(page).get_by_role("button", name="Download file").click()
    ui.wait("fake.downloads.length === 1")
    download = ui.js("({ ...fake.downloads[0], size: fake.downloads[0].blob.size })")
    assert download["download"] == "logo.png" and download["size"] == len(PNG)
    assert download["connected"] and download["revoked"]
    assert len(ui.requests("/api/file/raw?path=logo.png&download=1&session_id=a")) == 1


def test_each_file_row_has_a_download_button(ui, page):
    start(ui, page)
    row = page.locator(".file-entry", has_text="data.bin")
    row.hover()
    row.get_by_role("button", name="Download", exact=True).click()
    expect(row.get_by_role("button", name="Download", exact=True)).to_have_accessible_description("data.bin")
    ui.wait("fake.downloads.length === 1")
    assert ui.js("fake.downloads[0].download") == "data.bin"
    # Folders have none, and choosing Download does not open the file.
    expect(panel(page).locator(".file-editor")).to_have_count(0)


def test_a_pdf_shows_in_a_frame_from_a_typed_blob(ui, page):
    start(ui, page)
    page.locator(".file-row", has_text="paper.pdf").click()
    frame = panel(page).locator("iframe.file-preview-frame.pdf")
    expect(frame).to_be_attached()
    assert frame.get_attribute("src").startswith("blob:")
    assert frame.get_attribute("title") == "Preview of paper.pdf"


def test_markdown_previews_project_images_but_nothing_from_the_web(ui, page):
    start(ui, page)
    page.locator(".file-row", has_text="README.md").click()
    expect(panel(page).get_by_role("textbox", name="Edit README.md")).to_be_visible()
    panel(page).get_by_role("button", name="Preview", exact=True).click()
    preview = panel(page).locator(".file-preview-markdown")
    expect(preview.get_by_role("heading", name="Guide")).to_be_visible()
    ui.wait("document.querySelector('.file-preview-markdown img')?.src.startsWith('blob:')")
    expect(preview.get_by_text("Image from the web, not loaded: remote")).to_be_visible()
    assert not any("example.com" in call["path"] for call in ui.js("fake.calls"))
    assert ui.js("[...document.querySelectorAll('.file-preview-markdown img')].map(img => img.getAttribute('src') || '').some(src => src.includes('example.com'))") is False
    expect(preview.get_by_role("link", name="Site")).to_have_attribute("target", "_blank")
    expect(preview.get_by_role("link", name="Site")).to_have_attribute("rel", "noopener noreferrer")
    # Preview shows the editor's text, unsaved edits included; Edit goes back to it.
    panel(page).get_by_role("button", name="Edit", exact=True).click()
    panel(page).get_by_role("textbox", name="Edit README.md").fill("# Draft title\n")
    panel(page).get_by_role("button", name="Preview", exact=True).click()
    expect(preview.get_by_role("heading", name="Draft title")).to_be_visible()
    expect(panel(page).get_by_text("Unsaved changes")).to_be_visible()


def test_a_markdown_link_to_a_project_file_opens_it(ui, page):
    start(ui, page)
    page.locator(".file-row", has_text="README.md").click()
    panel(page).get_by_role("button", name="Preview", exact=True).click()
    panel(page).locator(".file-preview-markdown").get_by_role("link", name="Notes").click()
    expect(panel(page).get_by_role("textbox", name="Edit docs/notes.md")).to_have_value("# Notes\n")


def test_html_previews_in_a_sandbox_without_scripts_until_allowed(ui, page):
    start(ui, page)
    page.locator(".file-row", has_text="page.html").click()
    panel(page).get_by_role("button", name="Preview", exact=True).click()
    frame = panel(page).locator("iframe.file-preview-frame")
    expect(frame).to_have_attribute("sandbox", "")
    srcdoc = frame.get_attribute("srcdoc")
    # The page's own policy comes right after its doctype: no requests, inline styles and scripts only.
    assert srcdoc.startswith("<!doctype html><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none';")
    assert "form-action 'none'" in srcdoc and "connect-src" not in srcdoc
    content = page.frame_locator(".file-preview-frame")
    expect(content.get_by_text("Static page")).to_be_visible()
    expect(panel(page).get_by_text("Scripts are off. Nothing is loaded from the network.")).to_be_visible()
    panel(page).get_by_role("button", name="Run scripts").click()
    expect(panel(page).locator("iframe.file-preview-frame")).to_have_attribute("sandbox", "allow-scripts")
    expect(page.frame_locator(".file-preview-frame").get_by_text("Ran")).to_be_visible()
    # The frame has an origin of its own: it cannot reach the app's page or storage.
    assert page.frame_locator(".file-preview-frame").locator("body").evaluate(
        "() => { try { return String(window.parent.document.title) } catch { return 'blocked' } }") == "blocked"
    # Remembered for that file until the page is reloaded.
    panel(page).get_by_role("button", name="Back to files").click()
    page.locator(".file-row", has_text="page.html").click()
    panel(page).get_by_role("button", name="Preview", exact=True).click()
    expect(panel(page).get_by_role("button", name="Stop scripts")).to_have_attribute("aria-pressed", "true")
    panel(page).get_by_role("button", name="Stop scripts").click()
    expect(panel(page).locator("iframe.file-preview-frame")).to_have_attribute("sandbox", "")


def test_a_file_that_is_not_text_offers_download(ui, page):
    start(ui, page)
    page.locator(".file-row", has_text="data.bin").click()
    expect(panel(page).get_by_text("This file is not UTF-8 text, so the text editor cannot open it.")).to_be_visible()
    expect(panel(page).get_by_role("alert")).to_have_count(0)
    panel(page).locator(".file-preview-none").get_by_role("button", name="Download").click()
    ui.wait("fake.downloads.length === 1")
    assert ui.js("fake.downloads[0].download") == "data.bin"
