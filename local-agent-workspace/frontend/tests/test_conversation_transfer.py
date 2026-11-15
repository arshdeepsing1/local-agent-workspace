import json
import re

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


BUNDLE = '{"format":"local-agent-workspace.conversations","version":1}'


def exported(omitted=()):
    omitted = list(omitted)
    return ("fake.handle = () => fake.json({ bundle: { format: 'local-agent-workspace.conversations', version: 1 }, "
            f"children_omitted: {{ count: {len(omitted)}, children: {json.dumps(omitted)} }} }}); fake.captureDownloads()")


def test_export_includes_the_subagent_conversations_by_default_and_downloads_the_bundle_alone(ui, page):
    show(ui, exported())
    expect(page.get_by_role("checkbox", name=re.compile("^Export this chat only"))).not_to_be_checked()
    page.get_by_role("button", name="Export conversation").click()
    expect(page.get_by_role("status")).to_have_text("Conversation file exported.")
    assert ui.js("fake.calls.map(call => call.path)") == ["/api/sessions/source/export?include_children=true&report=true"]
    download = ui.js("(async () => { const d = fake.downloads[0]; return { count: fake.downloads.length, name: d.download, revoked: d.revoked, text: await d.blob.text() } })()")
    assert download == {"count": 1, "name": "conversation-source.json", "revoked": True, "text": BUNDLE}
    assert ui.js("document.querySelector('a[download]')") is None
    assert ui.spy("select") == []


def test_export_this_chat_only_leaves_the_subagent_conversations_out(ui, page):
    show(ui, exported())
    page.get_by_role("checkbox", name=re.compile("^Export this chat only")).check()
    page.get_by_role("button", name="Export conversation").click()
    expect(page.get_by_role("status")).to_have_text("Conversation file exported.")
    assert ui.js("fake.calls.map(call => call.path)") == ["/api/sessions/source/export?include_children=false&report=true"]
    page.get_by_role("checkbox", name=re.compile("^Export with reasoning")).check()
    page.get_by_role("button", name="Export conversation").click()
    ui.wait("fake.calls.length === 2")
    assert ui.js("fake.calls[1].path") == "/api/sessions/source/export?include_children=false&report=true&with_reasoning=true"


def test_this_chat_only_is_labelled_as_an_export_option_and_a_fork_does_not_send_it(ui, page):
    show(ui, "fake.handle = () => fake.json({ id: 'fork-id' })")
    box = page.get_by_role("checkbox", name=re.compile("^Export this chat only"))
    expect(box).to_have_accessible_name(re.compile(r"Fork always copies the subagent conversations that have ended\.$"))
    box.check()
    page.get_by_role("button", name="Fork completed conversation").click()
    ui.wait("fake.spies.select.length === 1")
    call = ui.js("fake.calls[0]")
    assert (call["path"], call["method"], call.get("body")) == ("/api/sessions/source/fork", "POST", None)


def test_an_export_that_left_subagent_conversations_out_says_how_many_and_why(ui, page):
    left = [{"id": "a", "title": "One", "reason": "limit"}, {"id": "b", "title": "Two", "reason": "limit"},
            {"id": "c", "title": "Three", "reason": "busy"}]
    show(ui, exported(left))
    page.get_by_role("button", name="Export conversation").click()
    expect(page.get_by_role("status")).to_have_text(
        "Conversation file exported. 3 subagent conversations were not exported (more than fit in 32 conversations, still running).")
    assert ui.js("fake.downloads.length") == 1


def test_a_fork_that_left_subagent_conversations_out_says_so(ui, page):
    show(ui, "fake.handle = () => fake.json({ id: 'fork-id', children_copied: 31, children_omitted: [{ id: 'a', title: 'One', reason: 'limit' }] })")
    page.get_by_role("button", name="Fork completed conversation").click()
    ui.wait("fake.spies.select.length === 1")
    assert ui.spy("select") == [["fork-id"]]
    expect(page.get_by_role("status")).to_have_text(
        "Fork created. 1 subagent conversation was not copied (more than fit in 32 conversations).")


def test_a_fork_that_copied_everything_shows_no_message(ui, page):
    show(ui, "fake.handle = () => fake.json({ id: 'fork-id', children_copied: 2, children_omitted: [] })")
    page.get_by_role("button", name="Fork completed conversation").click()
    ui.wait("fake.spies.select.length === 1")
    expect(page.get_by_role("status")).to_have_count(0)


def test_the_markdown_transcript_can_include_the_subagents(ui, page):
    show(ui, TRANSCRIPT + "; fake.captureDownloads()")
    page.get_by_role("button", name="Export Markdown").click()
    expect(page.get_by_role("status")).to_have_text("Markdown transcript exported.")
    page.get_by_role("checkbox", name=re.compile("^Include subagents")).check()
    page.get_by_role("button", name="Copy conversation").click()
    ui.wait("fake.calls.length === 2")
    assert ui.js("fake.calls.map(call => call.path)") == ["/api/sessions/source/transcript.md",
                                                           "/api/sessions/source/transcript.md?subagents=1"]


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


MARKDOWN = "# Transcript: Fix the export\\n\\n**User:**\\n\\nPlease fix it. Token: [REDACTED]\\n"
TRANSCRIPT = ("fake.handle = path => path.split('?')[0] === '/api/sessions/source/transcript.md' ? new Response('" + MARKDOWN + "', { status: 200, headers: {"
              " 'Content-Type': 'text/markdown; charset=utf-8', 'Content-Disposition': 'attachment; filename=\"2026-01-01-fix-the-export-source.md\"' } })"
              " : fake.json({ detail: 'Conversation not found.' }, 404)")


def test_export_markdown_downloads_the_server_transcript(ui, page):
    show(ui, TRANSCRIPT + "; fake.captureDownloads()")
    page.get_by_role("button", name="Export Markdown").click()
    expect(page.get_by_role("status")).to_have_text("Markdown transcript exported.")
    assert ui.js("fake.calls.map(call => [call.path, call.method, call.headers['X-Local-Token']])") == [
        ["/api/sessions/source/transcript.md", "GET", "local-token"]]
    download = ui.js("(async () => { const d = fake.downloads[0]; return { count: fake.downloads.length, name: d.download, revoked: d.revoked, type: d.blob.type, text: await d.blob.text() } })()")
    assert download == {"count": 1, "name": "2026-01-01-fix-the-export-source.md", "revoked": True, "type": "text/markdown",
                        "text": "# Transcript: Fix the export\n\n**User:**\n\nPlease fix it. Token: [REDACTED]\n"}


def test_copy_conversation_copies_the_transcript_even_while_busy(ui, page):
    show(ui, TRANSCRIPT, props=PROPS.replace("busy: false", "busy: true"))
    expect(page.get_by_role("button", name="Export conversation")).to_be_disabled()
    page.get_by_role("button", name="Copy conversation").click()
    expect(page.get_by_role("status")).to_have_text("Conversation copied as Markdown.")
    assert ui.js("fake.clipboard") == ["# Transcript: Fix the export\n\n**User:**\n\nPlease fix it. Token: [REDACTED]\n"]


def test_copy_conversation_starts_the_clipboard_write_during_the_click(ui, page):
    # Where the browser offers ClipboardItem, the write starts at once (Safari allows it only
    # during the click) and gets the text when the transcript arrives.
    show(ui, TRANSCRIPT + """; const respond = fake.handle, held = fake.defer('transcript')
      fake.handle = (...args) => held.promise.then(() => respond(...args))
      window.writeStarted = false
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
      write: async items => { window.writeStarted = true; fake.clipboard.push(await (await items[0].getType('text/plain')).text()) } } })""")
    page.get_by_role("button", name="Copy conversation").click()
    ui.wait("fake.calls.length === 1")
    assert ui.js("window.writeStarted") is True
    assert ui.js("fake.clipboard") == []
    ui.run("fake.deferreds.transcript.resolve()")
    expect(page.get_by_role("status")).to_have_text("Conversation copied as Markdown.")
    assert ui.js("fake.clipboard") == ["# Transcript: Fix the export\n\n**User:**\n\nPlease fix it. Token: [REDACTED]\n"]


def test_transcript_messages_appear_under_the_transcript_buttons(ui, page):
    # The section is long; a message at its end would be out of view in the dialog.
    follows = "document.querySelector('[role={0}]').previousElementSibling.textContent"
    show(ui, TRANSCRIPT)
    page.get_by_role("button", name="Copy conversation").click()
    expect(page.get_by_role("status")).to_have_text("Conversation copied as Markdown.")
    assert ui.js(follows.format("status")) == "Export MarkdownCopy conversation"
    show(ui, TRANSCRIPT, props=PROPS.replace("'source'", "'gone'"))
    page.get_by_role("button", name="Export Markdown").click()
    expect(page.get_by_role("alert")).to_have_text("Conversation not found.")
    assert ui.js(follows.format("alert")) == "Export MarkdownCopy conversation"
    # Messages of the other actions stay at the end, after the import form.
    show(ui, "fake.handle = () => fake.json({ detail: 'Export failed.' }, 500)")
    page.get_by_role("button", name="Export conversation").click()
    expect(page.get_by_role("alert")).to_have_text("Export failed.")
    assert ui.js("document.querySelector('[role=alert]').previousElementSibling.tagName") == "FORM"


def test_transcript_errors_are_shown_and_need_a_conversation(ui, page):
    show(ui, TRANSCRIPT, props=PROPS.replace("'source'", "'gone'"))
    page.get_by_role("button", name="Copy conversation").click()
    expect(page.get_by_role("alert")).to_have_text("Conversation not found.")
    assert ui.js("fake.clipboard") == []
    ui.rerender("{ workspace: '/default', onSelectSession: fake.spy('select'), onError: fake.spy('onError') }")
    expect(page.get_by_role("button", name="Export Markdown")).to_be_disabled()
    expect(page.get_by_role("button", name="Copy conversation")).to_be_disabled()


# The delete dialog: one question for a chat with subagent chats ------------------------------------

RLO = "\u202e"
DELETE_PROPS = ("{ session: { id: 'p', title: %s }, children: 2, onDelete: fake.spy('delete'), "
                "onClose: fake.spy('close') }")


def delete_dialog(ui, children, title="'Parent task'"):
    setup = f"fake.handle = path => path === '/api/sessions/p/children' ? fake.json({json.dumps(children)}) : fake.json({{}}, 404)"
    ui.mount("components/DeleteConversationDialog.js", DELETE_PROPS % title, setup=setup)
    return ui.page.get_by_role("dialog", name="Delete conversation")


def ended(*names):
    return [{"id": name, "title": f"Task {name}", "status": "idle", "subagent": {"task": f"Do {name}"}} for name in names]


def test_the_delete_dialog_counts_the_subagent_chats_and_says_none_run(ui, page):
    dialog = delete_dialog(ui, ended("a", "b"))
    expect(dialog).to_contain_text("Delete “Parent task”? It has 2 subagent conversations. Your project files will remain.")
    expect(dialog.get_by_role("status")).to_have_text("None of them is running.")
    expect(dialog.get_by_role("checkbox")).to_have_count(0)
    expect(dialog.get_by_role("button", name="Delete with subagents")).to_have_class("primary")
    expect(dialog.get_by_role("button", name="Delete with subagents")).to_be_enabled()
    assert ui.js("fake.calls.map(call => [call.method, call.path])") == [["GET", "/api/sessions/p/children"]]


def test_the_main_button_deletes_the_chat_with_its_subagents_and_the_other_keeps_them(ui, page):
    dialog = delete_dialog(ui, ended("a", "b"))
    dialog.get_by_role("button", name="Delete with subagents").click()
    ui.wait("fake.spies.close.length === 1")
    assert ui.spy("delete") == [["delete"]]
    dialog = delete_dialog(ui, ended("a", "b"))
    dialog.get_by_role("button", name="Delete, keep subagents").click()
    ui.wait("fake.spies.close.length === 1")
    assert ui.spy("delete") == [["keep"]]
    dialog = delete_dialog(ui, ended("a", "b"))
    dialog.get_by_role("button", name="Cancel").click()
    assert ui.spy("delete") == [] and len(ui.spy("close")) == 1


def test_a_running_subagent_is_named_and_nothing_is_deleted_until_you_confirm_stopping_it(ui, page):
    running = [*ended("a"), {"id": "b", "title": f"Run{RLO}ning", "status": "running", "subagent": {"task": f"Check{RLO}it"}}]
    dialog = delete_dialog(ui, running, title=f"'Plan{RLO}x'")
    expect(dialog.get_by_role("status")).to_have_text("1 of them is still running.")
    expect(dialog.get_by_role("list", name="Running subagents")).to_have_text("Check\\u202eit")
    expect(dialog.locator("p.logical-order").first).to_contain_text("Delete “Plan\\u202ex”?")
    assert RLO not in dialog.text_content()
    primary, keep = dialog.get_by_role("button", name="Delete with subagents"), dialog.get_by_role("button", name="Delete, keep subagents")
    expect(primary).to_be_disabled()
    expect(keep).to_be_disabled()
    expect(dialog.get_by_role("button", name="Cancel")).to_be_enabled()
    primary.click(force=True)
    assert ui.spy("delete") == []
    dialog.get_by_role("checkbox", name=re.compile("^Stop it first")).check()
    expect(primary).to_be_enabled()
    primary.click()
    ui.wait("fake.spies.close.length === 1")
    assert ui.spy("delete") == [["delete"]]


def test_the_delete_dialog_still_asks_when_the_list_of_subagents_cannot_be_read(ui, page):
    ui.mount("components/DeleteConversationDialog.js", DELETE_PROPS % "'Parent task'",
             setup="fake.handle = () => fake.json({ detail: 'Not found.' }, 404)")
    dialog = page.get_by_role("dialog", name="Delete conversation")
    expect(dialog).to_contain_text("It has 2 subagent conversations.")
    expect(dialog.get_by_role("status")).to_have_count(0)
    expect(dialog.get_by_role("button", name="Delete with subagents")).to_be_enabled()


# Fork from here: the dialog tells what was left out before it opens the fork ---------------------

def fork_dialog(ui, reply):
    ui.mount("components/ForkDialog.js", "{ session: { id: 'source', title: 'Source' }, beforeEventId: 'u2', "
             "onSelectSession: fake.spy('select'), onClose: fake.spy('close') }",
             setup=f"fake.handle = () => fake.json({json.dumps(reply)})")
    return ui.page.get_by_role("dialog", name="Fork conversation")


def test_fork_from_here_closes_at_once_when_every_subagent_chat_was_copied(ui, page):
    dialog = fork_dialog(ui, {"id": "fork-id", "children_copied": 1, "children_omitted": []})
    expect(dialog).to_contain_text("with the subagent chats it started up to here")
    dialog.get_by_role("button", name="Fork", exact=True).click()
    ui.wait("fake.spies.select.length === 1")
    assert ui.spy("select") == [["fork-id"]] and len(ui.spy("close")) == 1
    assert json.loads(ui.js("fake.calls[0].body")) == {"before_event_id": "u2", "title": "Source (fork)"}


def test_fork_from_here_shows_the_left_out_subagent_chats_before_it_opens_the_fork(ui, page):
    reply = {"id": "fork-id", "children_copied": 31, "children_omitted": [{"id": "a", "title": "One", "reason": "limit"}]}
    dialog = fork_dialog(ui, reply)
    dialog.get_by_role("button", name="Fork", exact=True).click()
    expect(dialog.get_by_role("status")).to_have_text("Fork created. 1 subagent conversation was not copied (more than fit in 32 conversations).")
    assert ui.spy("select") == [] and ui.spy("close") == []
    dialog.locator("input").first.press("Enter")  # A second submit does not make a second fork.
    assert ui.js("fake.calls.length") == 1
    dialog.get_by_role("button", name="Open the fork").click()
    assert ui.spy("select") == [["fork-id"]] and len(ui.spy("close")) == 1
