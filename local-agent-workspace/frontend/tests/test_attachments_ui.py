"""Files attached to a message in the composer: Attach files, drop and paste, the chips and their warnings, the
uploads a message sends, the chips of a sent message, and Edit or Retry sending them again."""
import json

from playwright.sync_api import expect

FILES = """fake.override = path => path.includes('mention-files?') ? fake.json([]) : undefined"""
SENT = [
    {"id": "u", "type": "user", "text": "Compare them", "in_wire": True, "attachments": [
        {"kind": "document", "name": "report.pdf", "upload": "1".zfill(64), "format": "pdf", "chars": 1200,
         "truncated": True, "units": "page", "count": 3},
        {"kind": "image", "upload": "2".zfill(64), "name": "shot.png", "media_type": "image/png", "width": 640,
         "height": 480, "bytes": 2048},
        {"kind": "document", "name": "paper.pdf", "upload": "3".zfill(64), "format": "pdf", "chars": 0,
         "truncated": False, "as_path": True}]},
    {"id": "r", "type": "assistant", "text": "They differ.", "request_info": {"model": "test-model", "status": "completed",
                                                                         "finish_reason": "stop"}},
]


def start(ui, page, setup=FILES, events=None):
    sessions = f"fake.sessions.set('a', {{ ...makeSession('a', 'Files'), events: {json.dumps(events or [])}, terminal_reason: 'completed' }})"
    ui.app(session="a", scenario="workspace", setup=f"{setup}\n{sessions}")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    ui.show_session("a")


def choose(page, *files):
    """Choose files with Attach files (the browser's file chooser)."""
    with page.expect_file_chooser() as chooser:
        page.get_by_role("button", name="Attach files").click()
    chooser.value.set_files([{"name": name, "mimeType": kind, "buffer": data} for name, kind, data in files])


def chips(page):
    return page.locator(".upload-chip")


def test_attach_files_uploads_each_file_and_the_message_sends_their_ids(ui, page):
    start(ui, page)
    choose(page, ("report.pdf", "application/pdf", b"%PDF-1.7 report"), ("shot.png", "image/png", b"\x89PNG fake"))
    expect(chips(page)).to_have_count(2)
    expect(chips(page).nth(0)).to_contain_text("report.pdf")
    expect(chips(page).nth(0)).to_contain_text("PDF · 3 pages · ~300 tokens")
    expect(chips(page).nth(1)).to_contain_text("PNG · 640×480 · ~410 tokens")
    uploads = ui.requests("/api/sessions/a/uploads", "POST")
    assert [call["headers"]["X-File-Name"] for call in uploads] == ["report.pdf", "shot.png"]
    assert all(call["headers"]["Content-Type"] == "application/octet-stream" for call in uploads)
    assert ui.js("fake.uploads.map(item => item.file instanceof File && item.file.size)") == [15, 9]
    box = page.get_by_role("textbox", name="Message")
    box.fill("Compare them")
    box.press("Enter")
    expect(chips(page)).to_have_count(0)
    assert ui.bodies("/api/sessions/a/messages", "POST") == [
        {"text": "Compare them", "uploads": [{"id": "1".zfill(64)}, {"id": "2".zfill(64)}]}]


def test_drop_and_paste_attach_files_but_copied_text_pastes_as_text(ui, page):
    start(ui, page)
    ui.run("""const transfer = new DataTransfer()
      transfer.items.add(new File(['a,b\\n1,2'], 'sales.csv', { type: 'text/csv' }))
      const block = document.querySelector('.composer-block')
      block.dispatchEvent(new DragEvent('dragover', { dataTransfer: transfer, bubbles: true, cancelable: true }))""")
    expect(page.locator(".composer-block.dragging-files")).to_have_count(1)
    ui.run("""const transfer = new DataTransfer()
      transfer.items.add(new File(['a,b\\n1,2'], 'sales.csv', { type: 'text/csv' }))
      document.querySelector('.composer-block').dispatchEvent(new DragEvent('drop', { dataTransfer: transfer, bubbles: true, cancelable: true }))""")
    expect(chips(page)).to_have_count(1)
    expect(page.locator(".composer-block.dragging-files")).to_have_count(0)
    expect(chips(page)).to_contain_text("sales.csv")
    # A pasted screenshot attaches; text copied with a picture of it (a spreadsheet) pastes as text.
    ui.run("""const transfer = new DataTransfer()
      transfer.items.add(new File(['png'], 'image.png', { type: 'image/png' }))
      document.querySelector('textarea[aria-label="Message"]').dispatchEvent(new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true, cancelable: true }))""")
    expect(chips(page)).to_have_count(2)
    ui.run("""const transfer = new DataTransfer()
      transfer.setData('text/plain', 'Region\\tTotal')
      transfer.items.add(new File(['png'], 'cells.png', { type: 'image/png' }))
      document.querySelector('textarea[aria-label="Message"]').dispatchEvent(new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true, cancelable: true }))""")
    page.wait_for_timeout(100)
    expect(chips(page)).to_have_count(2)
    assert len(ui.requests("/api/sessions/a/uploads", "POST")) == 2
    page.get_by_role("button", name="Remove sales.csv").click()
    expect(chips(page)).to_have_count(1)
    expect(page.get_by_role("textbox", name="Message")).to_be_focused()


def test_files_the_app_does_not_take_are_refused_before_uploading(ui, page):
    start(ui, page)
    choose(page, ("tool.exe", "application/octet-stream", b"MZ"))
    expect(page.get_by_role("alert")).to_contain_text("tool.exe cannot be attached: attach PDF, Word (.docx)")
    choose(page, ("big.png", "image/png", b"x" * 2_500_001))
    expect(page.get_by_role("alert")).to_contain_text("big.png is larger than 2.5 MB, the most an image can be. The app does not shrink images")
    assert ui.requests("/api/sessions/a/uploads", "POST") == []
    # A refusal from the server (a damaged document) leaves no chip.
    ui.run("""fake.override = (path, options) => path === '/api/sessions/a/uploads'
      ? fake.json({ detail: 'notes.docx: This file is not a valid Office document (not a ZIP archive).' }, 400) : undefined""")
    choose(page, ("notes.docx", "application/octet-stream", b"PK broken"))
    expect(page.get_by_role("alert")).to_contain_text("notes.docx: This file is not a valid Office document")
    expect(chips(page)).to_have_count(0)


def test_a_message_waits_for_its_files_to_upload(ui, page):
    start(ui, page)
    ui.run("""const pending = fake.defer('upload')
      fake.override = (path, options) => path === '/api/sessions/a/uploads' ? pending.promise : undefined""")
    choose(page, ("plan.md", "text/markdown", b"# Plan"))
    expect(chips(page)).to_contain_text("Uploading…")
    box = page.get_by_role("textbox", name="Message")
    box.fill("Use the plan")
    expect(page.get_by_role("button", name="Send message")).to_be_disabled()
    expect(page.get_by_role("status").filter(has_text="Uploading the attached files…")).to_be_visible()
    box.press("Enter")
    assert ui.requests("/api/sessions/a/messages", "POST") == []
    ui.run("fake.deferreds.upload.resolve(fake.json(fake.uploadRecord('plan.md', null)))")
    expect(chips(page)).to_contain_text("Markdown · ~300 tokens")
    page.get_by_role("button", name="Send message").click()
    expect(chips(page)).to_have_count(0)
    assert ui.bodies("/api/sessions/a/messages", "POST")[0]["uploads"] == [{"id": "0".zfill(64)}]


def test_warnings_for_an_image_the_endpoint_does_not_take_and_a_pdf_that_goes_as_a_path(ui, page):
    start(ui, page)
    ui.run("""const record = fake.uploadRecord
      fake.uploadRecord = (name, file) => name.endsWith('.png')
        ? { ...record(name, file), vision_refusal: 'test-model does not take images, so images cannot be sent in this conversation.' }
        : { ...record(name, file), estimated_tokens: 0, unreadable: 'Reading PDF text needs the app\\'s optional documents extra.' }""")
    choose(page, ("shot.png", "image/png", b"png"), ("paper.pdf", "application/pdf", b"%PDF"))
    warnings = page.locator(".attachment-warning")
    expect(warnings).to_have_count(2)
    expect(warnings.nth(0)).to_have_text("shot.png: test-model does not take images, so images cannot be sent in this conversation.")
    expect(warnings.nth(1)).to_contain_text("paper.pdf: Reading PDF text needs the app's optional documents extra.")
    expect(chips(page).nth(1)).to_contain_text("PDF · 3 pages · text not read")
    page.get_by_role("button", name="Attach as path").click()
    expect(page.locator(".attachment-note")).to_contain_text("paper.pdf goes as a path: the agent gets where the file is saved")
    expect(chips(page).nth(1)).to_contain_text("· as a path")
    page.get_by_role("button", name="Remove shot.png").click()
    box = page.get_by_role("textbox", name="Message")
    box.fill("Read the paper")
    box.press("Enter")
    expect(chips(page)).to_have_count(0)
    assert ui.bodies("/api/sessions/a/messages", "POST")[0]["uploads"] == [{"id": "2".zfill(64), "as_path": True}]


def test_a_new_conversation_is_created_for_its_first_attached_file(ui, page):
    ui.app(scenario="workspace", setup=FILES)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    choose(page, ("notes.txt", "text/plain", b"notes"))
    expect(chips(page)).to_contain_text("notes.txt")
    assert len(ui.requests("/api/sessions", "POST")) == 1
    assert [call["path"] for call in ui.requests("/api/sessions/created/uploads", "POST")] == ["/api/sessions/created/uploads"]


def test_a_sent_message_names_its_files_and_edit_and_retry_send_them_again(ui, page):
    start(ui, page, events=SENT)
    message = page.locator(".user-message")
    expect(message.get_by_role("list", name="Attachments").get_by_role("listitem")).to_have_text([
        "report.pdf · 3 pages · shortened", "shot.png · 640×480", "paper.pdf · as a path"])
    ui.run("""fake.override = (path, options) => path === '/api/sessions/a/rewind'
      ? fake.json({ session: fake.sessions.get('a'), backup_session_id: 'b', files: [], resent: true }) : undefined""")
    page.get_by_role("button", name="Retry response").click()
    ui.wait("fake.requests('/api/sessions/a/rewind', 'POST').length === 1")
    uploads = [{"id": "1".zfill(64)}, {"id": "2".zfill(64)}, {"id": "3".zfill(64), "as_path": True}]
    assert ui.bodies("/api/sessions/a/rewind", "POST")[0]["resend_uploads"] == uploads
    message.hover()
    page.get_by_role("button", name="Edit message").click()
    expect(page.locator(".edit-message-files")).to_have_text(
        "Sent again with: report.pdf · 3 pages · shortened; shot.png · 640×480; paper.pdf · as a path")
    # A file can stay behind (for example an image after a switch to an endpoint without image input).
    page.get_by_role("button", name="Do not send shot.png again").click()
    expect(page.locator(".edit-message-files")).to_have_text(
        "Sent again with: report.pdf · 3 pages · shortened; paper.pdf · as a path")
    page.get_by_role("button", name="Send edited message").click()
    ui.wait("fake.requests('/api/sessions/a/rewind', 'POST').length === 2")
    assert ui.bodies("/api/sessions/a/rewind", "POST")[1]["resend_uploads"] == [uploads[0], uploads[2]]


def test_an_imported_message_names_its_files_and_sends_none_again(ui, page):
    """An imported chat has no attached files (their events keep the names, no upload ids)."""
    imported = json.loads(json.dumps(SENT))
    for item in imported[0]["attachments"]:
        del item["upload"]
    start(ui, page, events=imported)
    message = page.locator(".user-message")
    expect(message.get_by_role("list", name="Attachments").get_by_role("listitem")).to_have_count(3)
    ui.run("""fake.override = (path, options) => path === '/api/sessions/a/rewind'
      ? fake.json({ session: fake.sessions.get('a'), backup_session_id: 'b', files: [], resent: true }) : undefined""")
    page.get_by_role("button", name="Retry response").click()
    ui.wait("fake.requests('/api/sessions/a/rewind', 'POST').length === 1")
    assert "resend_uploads" not in ui.bodies("/api/sessions/a/rewind", "POST")[0]
    message.hover()
    page.get_by_role("button", name="Edit message").click()
    expect(page.get_by_role("textbox", name="Edit message")).to_be_visible()
    expect(page.locator(".edit-message-files")).to_have_count(0)


def test_editing_a_queued_message_brings_its_files_back(ui, page):
    queued = [{"id": "q1", "text": "Then read this", "created": 1, "paused": True,
               "uploads": [{"id": "5".zfill(64), "name": "spec.docx", "kind": "document"},
                           {"id": "6".zfill(64), "name": "shot.png", "kind": "image"}]}]
    setup = FILES + f"\nfake.sessions.set('a', {{ ...makeSession('a', 'Files'), queued_messages: {json.dumps(queued)} }})"
    ui.app(session="a", scenario="workspace", setup=setup)
    ui.show_session("a")
    ui.run("""fake.override = (path, options) => path === '/api/sessions/a/queue/q1' && options.method === 'DELETE'
      ? fake.json({ queued_messages: [] }) : path.includes('mention-files?') ? fake.json([]) : undefined""")
    bar = page.get_by_role("group", name="Queued messages")
    expect(bar.locator(".queue-tag")).to_have_text(["spec.docx", "shot.png"])
    bar.get_by_role("button", name="Edit queued message: Then read this").click()
    expect(page.get_by_role("textbox", name="Message")).to_have_value("Then read this")
    expect(chips(page)).to_have_count(2)
    expect(chips(page).nth(0)).to_contain_text("spec.docx")
    # Only its name and kind come back: no pixel size to show.
    expect(chips(page).nth(1).locator(".upload-detail")).to_have_text("Image")
    page.get_by_role("button", name="Send message").click()
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/messages", "POST")[0]["uploads"] == [{"id": "5".zfill(64)}, {"id": "6".zfill(64)}]


def test_long_file_names_fit_a_phone(coarse_ui, coarse_page):
    ui, page = coarse_ui, coarse_page
    start(ui, page)
    choose(page, ("a-very-long-quarterly-report-name-that-goes-on-and-on-for-the-board-meeting.pdf", "application/pdf", b"%PDF"),
           ("shot.png", "image/png", b"png"))
    expect(chips(page)).to_have_count(2)
    assert ui.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    widths = ui.js("[...document.querySelectorAll('.upload-chip')].map(chip => chip.getBoundingClientRect().right <= document.querySelector('.composer').getBoundingClientRect().right + 1)")
    assert widths == [True, True]
    button = page.get_by_role("button", name="Remove shot.png").bounding_box()
    assert button["width"] >= 23.5 and button["height"] >= 23.5
    attach = page.get_by_role("button", name="Attach files").bounding_box()
    assert attach["width"] >= 23.5 and attach["height"] >= 23.5
