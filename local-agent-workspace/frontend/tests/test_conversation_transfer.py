from playwright.sync_api import expect

SETUP = """
fake.handle = () => fake.json({ session: { id: 'new-copy' }, imported_count: 1 })
"""
PROPS = "{ sessionId: 'source', workspace: '/chosen/workspace', busy: false, onSelectSession: fake.spy('select'), onError: fake.spy('onError') }"


def show(ui, setup="", props=PROPS):
    ui.mount("components/ConversationTransfer.js", props, setup=SETUP + setup)
    ui.js("import('/static/js/api.js').then(api => api.setToken('local-token'))")


def choose_file(ui, text='{"format":"bundle"}', size=None):
    size_arg = "undefined" if size is None else str(size)
    ui.run(f"window.chosen = fake.chooseFile(document.querySelector('input[type=file]'), {text!r}, 'conversation.json', {size_arg})")


def test_imports_exact_json_with_token_into_selected_workspace(ui, page):
    show(ui)
    destination = page.get_by_role("textbox", name="Import destination workspace")
    expect(destination).to_have_value("/chosen/workspace")
    text = '{"source_workspace":"/old/machine","version":1,"version":2,"large":9007199254740993}'
    choose_file(ui, text)
    destination.fill("/new/project")
    page.get_by_role("button", name="Import as new conversation").click()
    ui.wait("fake.spies.select.length === 1")
    assert ui.spy("select") == [["new-copy"]]
    assert ui.js("fake.calls") == [{"path": "/api/sessions/import?workspace=%2Fnew%2Fproject", "method": "POST", "body": text,
                                    "headers": {"Content-Type": "application/json", "X-Local-Token": "local-token"}}]
    expect(page.get_by_text("manual permissions, no folder grants, and no active skills", exact=False)).to_be_visible()


def test_oversized_file_is_rejected_before_reading_or_sending(ui, page):
    show(ui)
    choose_file(ui, size=16 * 1024 * 1024 + 1)
    page.get_by_role("button", name="Import as new conversation").click()
    expect(page.get_by_role("alert")).to_have_text("Conversation file exceeds the 16 MiB limit.")
    assert ui.js("chosen.reads") == 0
    assert ui.js("fake.calls.length") == 0
    assert ui.spy("select") == []


def test_rejected_import_keeps_file_and_workspace(ui, page):
    show(ui, "fake.handle = () => fake.json({ detail: 'Conversation bundle contains duplicate JSON keys.' }, 400)")
    choose_file(ui, '{"version":1,"version":2}')
    page.get_by_role("button", name="Import as new conversation").click()
    expect(page.get_by_role("alert")).to_have_text("Conversation bundle contains duplicate JSON keys.")
    assert ui.spy("onError") == [["Conversation bundle contains duplicate JSON keys."]]
    assert ui.spy("select") == []
    expect(page.get_by_role("button", name="Import as new conversation")).to_be_enabled()
    expect(page.get_by_role("textbox")).to_have_value("/chosen/workspace")


def test_fork_selects_only_the_new_id(ui, page):
    show(ui, "fake.handle = () => fake.json({ id: 'fork-id' })")
    page.get_by_role("button", name="Fork completed conversation").click()
    ui.wait("fake.spies.select.length === 1")
    assert ui.spy("select") == [["fork-id"]]
    call = ui.js("fake.calls[0]")
    assert (call["path"], call["method"], call.get("body")) == ("/api/sessions/source/fork", "POST", None)


def test_export_uses_child_scope_and_downloads_returned_json(ui, page):
    show(ui, "fake.handle = () => fake.json({ format: 'local-agent-workspace.conversations', version: 1 }); fake.captureDownloads()")
    page.get_by_role("checkbox").check()
    page.get_by_role("button", name="Export conversation").click()
    expect(page.get_by_role("status")).to_have_text("Conversation file exported.")
    assert ui.js("fake.calls.map(call => call.path)") == ["/api/sessions/source/export?include_children=true"]
    download = ui.js("(async () => { const d = fake.downloads[0]; return { count: fake.downloads.length, name: d.download, revoked: d.revoked, text: await d.blob.text() } })()")
    assert download == {"count": 1, "name": "conversation-source.json", "revoked": True,
                        "text": '{"format":"local-agent-workspace.conversations","version":1}'}
    assert ui.js("document.querySelector('a[download]')") is None
    assert ui.spy("select") == []


def test_duplicate_requests_are_prevented_and_busy_disables_actions(ui, page):
    show(ui, "const pending = fake.defer('fork'); fake.handle = () => pending.promise")
    fork = page.get_by_role("button", name="Fork completed conversation")
    fork.click()
    fork.click(force=True)
    assert ui.js("fake.calls.length") == 1
    expect(fork).to_be_disabled()
    ui.run("fake.deferreds.fork.resolve(fake.json({ id: 'new-id' }))")
    ui.wait("fake.spies.select.length === 1")
    ui.rerender(PROPS.replace("busy: false", "busy: true"))
    expect(fork).to_be_disabled()
    expect(page.get_by_role("button", name="Export conversation")).to_be_disabled()


def test_import_allowed_without_conversation_but_export_and_fork_disabled(ui, page):
    show(ui, props="{ workspace: '/default', onSelectSession: fake.spy('select'), onError: fake.spy('onError') }")
    choose_file(ui)
    expect(page.get_by_role("button", name="Import as new conversation")).to_be_enabled()
    expect(page.get_by_role("button", name="Export conversation")).to_be_disabled()
    expect(page.get_by_role("button", name="Fork completed conversation")).to_be_disabled()


def test_import_completion_after_unmount_is_ignored(ui, page):
    show(ui, "const pending = fake.defer('import'); fake.handle = () => pending.promise")
    choose_file(ui)
    page.get_by_role("button", name="Import as new conversation").click()
    ui.wait("fake.calls.length === 1")
    ui.run("unmount()")
    ui.js("(async () => { fake.deferreds.import.resolve(fake.json({ session: { id: 'created-after-close' }, imported_count: 1 })); await new Promise(done => setTimeout(done, 20)) })()")
    assert ui.spy("select") == []
