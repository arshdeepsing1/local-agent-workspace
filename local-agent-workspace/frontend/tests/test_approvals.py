"""Approval quick wins: "Allow for this chat", decline notes, and errors that name the blocking chat."""
import json
import re

import pytest
from playwright.sync_api import expect

SETUP = """
window.makeSession = (events, overrides = {}) => ({
  id: 'parent', title: 'Parent task', workspace: '/project', model: 'test-model', status: 'idle',
  events, updated: 1, permission_mode: 'manual', allowed_directories: [], ...overrides,
})
fake.handle = () => fake.json({ ok: true })
"""


def show(ui, events: str):
    ui.mount("components/Conversation.js", f"{{ session: makeSession({events}), onError: fake.spy('onError') }}", setup=SETUP)


def pending(event_id, name, extra=""):
    return f"{{ id: '{event_id}', type: 'tool', name: '{name}', state: 'pending', preview: 'preview'{extra} }}"


def decisions(ui, event_id):
    return [json.loads(call["body"]) for call in ui.requests(f"/api/sessions/parent/approvals/{event_id}", "POST")]


# "Allow for this chat" on approval cards

@pytest.mark.parametrize("name, extra, label", [
    ("run_command", ", input: { command: 'make test' }", "Always allow this command in this chat"),
    ("write_file", ", input: { path: 'a.txt', content: 'x' }", "Allow edits in this chat"),
    ("edit_file", ", input: { path: 'a.txt' }", "Allow edits in this chat"),
    ("insert_activity_log", ", input: { path: 'log.md' }", "Allow edits in this chat"),
    ("mcp__docs__search", ", input: { query: 'x' }", "Allow this tool in this chat"),
    ("hook_before_tool", ", input: { hook_id: 'check', tool: 'write_file' }", "Allow this hook in this chat"),
])
def test_each_kind_of_card_offers_its_own_allow_for_this_chat(ui, page, name, extra, label):
    show(ui, f"[{pending('card', name, extra)}]")
    page.get_by_role("button", name=label, exact=True).click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui, "card") == [{"allowed": True, "remember": True}]
    expect(page.get_by_role("button", name=re.compile("in this chat$"))).to_have_count(1)


def test_folder_access_cards_have_no_allow_for_this_chat(ui, page):
    show(ui, f"[{pending('access', 'access_directory', ", input: { path: '/srv/data' }")}]")
    expect(page.get_by_role("button", name="Approve")).to_be_visible()
    expect(page.get_by_role("button", name="Decline")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("in this chat$"))).to_have_count(0)


def test_cards_ran_by_a_rule_say_so_without_expanding(ui, page):
    show(ui, """[{ id: 'ran', type: 'tool', name: 'run_command', state: 'completed', allowed_by_rule: true,
      input: { command: 'make test' }, output: '{}' }, { id: 'asked', type: 'tool', name: 'run_command',
      state: 'completed', approval: 'approved', input: { command: 'make lint' }, output: '{}' }]""")
    expect(page.get_by_text("Allowed by a rule for this chat", exact=True)).to_have_count(1)
    card = page.locator(".tool-card").filter(has=page.get_by_text("Allowed by a rule for this chat"))
    expect(card.get_by_role("button", name="run command make test")).to_be_visible()


# Decline with a note

def test_decline_sends_the_note_and_approve_does_not(ui, page):
    show(ui, f"[{pending('one', 'write_file', ", input: { path: 'a.txt' }")}, "
             f"{pending('two', 'run_command', ", input: { command: 'make' }")}]")
    notes = page.get_by_role("textbox", name="Tell the agent why (optional)")
    expect(notes).to_have_count(2)
    assert notes.first.get_attribute("maxlength") == "2000"
    notes.first.fill("  Use the parser module instead.  ")
    page.get_by_role("button", name="Decline").first.click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui, "one") == [{"allowed": False, "note": "Use the parser module instead."}]
    notes.last.fill("A note is only for Decline.")
    page.get_by_role("button", name="Approve").last.click()
    ui.wait("fake.calls.length === 2")
    assert decisions(ui, "two") == [{"allowed": True}]


def test_an_empty_note_sends_a_plain_decline(ui, page):
    show(ui, f"[{pending('one', 'write_file', ", input: { path: 'a.txt' }")}]")
    page.get_by_role("textbox", name="Tell the agent why (optional)").fill("   ")
    page.get_by_role("button", name="Decline").click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui, "one") == [{"allowed": False}]


def test_a_declined_card_shows_the_clipped_note(ui, page):
    long_note = "Keep the old API. " * 20
    show(ui, f"""[{{ id: 'declined', type: 'tool', name: 'write_file', state: 'rejected', approval: 'declined',
      decline_note: 'Use the parser module instead.', input: {{ path: 'a.txt' }}, output: 'User declined this action.' }},
      {{ id: 'long', type: 'tool', name: 'run_command', state: 'rejected', approval: 'declined',
      decline_note: {json.dumps(long_note)}, input: {{ command: 'make' }} }},
      {{ id: 'plain', type: 'tool', name: 'run_command', state: 'rejected', approval: 'declined', input: {{ command: 'ls' }} }}]""")
    expect(page.get_by_text("Action declined: Use the parser module instead.", exact=True)).to_be_visible()
    clipped = page.get_by_text(re.compile("^Action declined: Keep the old API"))
    expect(clipped).to_have_text("Action declined: " + long_note.strip()[:240] + "…")
    expect(page.get_by_text("Action declined", exact=True)).to_have_count(1)


# "Allowed in this chat" in the Permissions menu

RULES = """window.rules = [{ tool: 'run_command', command: 'make test' }, { tool: 'edits' },
  { tool: 'mcp__docs__search' }, { tool: 'hook', hook: 'check' }]
fake.sessions.set('a', { ...makeSession('a', 'Other conversation'), allow_rules: rules })
fake.override = (path, options) => {
  if (path === '/api/sessions/a/allow-rules' && options.method === 'DELETE') {
    const removed = JSON.stringify(JSON.parse(options.body))
    const updated = { ...fake.sessions.get('a'), allow_rules: fake.sessions.get('a').allow_rules.filter(rule => JSON.stringify(rule) !== removed) }
    fake.sessions.set('a', updated)
    return fake.json(updated)
  }
}"""


def open_permissions(ui, page):
    ui.app(session="a", scenario="workspace", setup=RULES)
    ui.show_session("a")
    page.get_by_role("button", name=re.compile("^Permissions:")).click()
    return page.get_by_role("group", name="Allowed in this chat")


def test_permissions_menu_lists_and_removes_rules(ui, page):
    rules = open_permissions(ui, page)
    expect(rules.get_by_role("menuitem")).to_have_count(4)
    for text in ("Command: make test", "File edits", "Tool: mcp__docs__search", "Hook: check"):
        expect(rules.get_by_text(text, exact=True)).to_be_visible()
    rules.get_by_role("menuitem", name="Remove rule: Command: make test").click()
    expect(rules.get_by_role("menuitem")).to_have_count(3)
    assert ui.bodies("/api/sessions/a/allow-rules", "DELETE") == [{"tool": "run_command", "command": "make test"}]
    expect(rules.get_by_text("Command: make test")).to_have_count(0)
    # A rule added by an approval elsewhere arrives with the permissions message.
    ui.emit("a", {"type": "permissions", "permission_mode": "manual", "allowed_directories": [],
                  "allow_rules": [{"tool": "edits"}, {"tool": "run_command", "command": "npm run lint"}]})
    expect(rules.get_by_text("Command: npm run lint", exact=True)).to_be_visible()
    expect(rules.get_by_role("menuitem")).to_have_count(2)


def test_rules_cannot_be_removed_while_the_chat_is_busy(ui, page):
    rules = open_permissions(ui, page)
    ui.emit("a", {"type": "status", "status": "awaiting_approval"})
    remove = rules.get_by_role("menuitem", name="Remove rule: File edits")
    expect(remove).to_have_attribute("aria-disabled", "true")
    remove.click(force=True)
    expect(page.get_by_text("Stop the response before changing modes.")).to_be_visible()
    assert ui.requests("/api/sessions/a/allow-rules", "DELETE") == []


def test_a_chat_without_rules_shows_no_rule_list(ui, page):
    ui.app(session="a", scenario="workspace")
    ui.show_session("a")
    page.get_by_role("button", name=re.compile("^Permissions:")).click()
    expect(page.get_by_role("menuitemradio", name=re.compile("^Manual"))).to_be_visible()
    expect(page.get_by_role("group", name="Allowed in this chat")).to_have_count(0)


# 409 errors that name the blocking chat

BLOCKED = """fake.sessions.set('b', makeSession('b', 'Selected chat'))
window.conflict = session_id => fake.json({ detail: {
  message: `“${fake.sessions.get(session_id).title}” is waiting for your approval. Answer or stop it before undoing agent changes.`,
  session_id, title: fake.sessions.get(session_id).title } }, 409)
window.blocker = 'a'
fake.override = path => path === '/api/sessions/b/changes/undo' ? conflict(blocker) : undefined"""


def undo_all(ui, page):
    ui.emit("b", {"type": "changes", "changes": {"files": [{"path": "a.txt", "status": "modified", "added": 1,
                                                            "removed": 1, "current_hash": "h"}], "added": 1, "removed": 1}})
    page.get_by_role("button", name="Undo all changes").click()


def test_an_error_naming_another_chat_offers_to_open_it(ui, page):
    ui.app(session="b", scenario="workspace", setup=BLOCKED)
    ui.show_session("b")
    undo_all(ui, page)
    alert = page.get_by_role("alert")
    expect(alert).to_contain_text("“Other conversation” is waiting for your approval. Answer or stop it before undoing agent changes.")
    alert.get_by_role("button", name="Open “Other conversation”").click()
    ui.wait("fake.socketsFor('a').length === 1")
    assert ui.js("location.search") == "?session=a"
    expect(page.get_by_role("alert")).to_have_count(0)


def test_an_error_naming_the_selected_chat_or_a_plain_error_has_no_open_button(ui, page):
    ui.app(session="b", scenario="workspace", setup=BLOCKED)
    ui.show_session("b")
    ui.run("blocker = 'b'")
    undo_all(ui, page)
    expect(page.get_by_role("alert")).to_contain_text("“Selected chat” is waiting for your approval.")
    expect(page.get_by_role("button", name=re.compile("^Open “"))).to_have_count(0)
    ui.run("fake.override = () => fake.json({ detail: 'Stop workspace commands before undoing agent changes.' }, 409)")
    page.get_by_role("button", name="Undo all changes").click()
    expect(page.get_by_role("alert")).to_contain_text("Stop workspace commands before undoing agent changes.")
    expect(page.get_by_role("button", name=re.compile("^Open “"))).to_have_count(0)


def test_api_errors_keep_string_details_and_expose_object_details(ui):
    ui.start()
    result = ui.js("""(async () => {
      const client = await import('/static/js/api.js')
      const detail = { message: '“Fix the parser” is still running. Stop it first.', session_id: 'x', title: 'Fix the parser' }
      fake.handle = path => path === '/api/object' ? fake.json({ detail }, 409) : fake.json({ detail: 'Plain words.' }, 409)
      const object = await client.api('/object').catch(error => error)
      const plain = await client.api('/plain').catch(error => error)
      const text = await client.apiText('/object').catch(error => error)
      return { object: [object.message, object.status, object.detail], plain: [plain.message, plain.status, 'detail' in plain],
               text: [text.message, text.detail?.session_id], lookup: client.blockingChat(detail.message)?.session_id,
               unknown: client.blockingChat('Plain words.') }
    })()""")
    message = "“Fix the parser” is still running. Stop it first."
    assert result["object"] == [message, 409, {"message": message, "session_id": "x", "title": "Fix the parser"}]
    assert result["plain"] == ["Plain words.", 409, False]
    assert result["text"] == [message, "x"]
    assert result["lookup"] == "x" and result["unknown"] is None
