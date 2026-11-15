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
    ("move_path", ", input: { path: 'a.txt', destination: 'b.txt' }", "Allow edits in this chat"),
    ("delete_path", ", input: { path: 'a.txt' }", "Allow edits in this chat"),
    ("notebook_edit", ", input: { path: 'n.ipynb', index: 0, source: 'x' }", "Allow edits in this chat"),
    ("mcp__docs__search", ", input: { query: 'x' }", "Allow this tool in this chat"),
    ("hook_before_tool", ", input: { hook_id: 'check', tool: 'write_file' }", "Allow this hook in this chat"),
    ("delegate_task", ", input: { task: 'Review the parser' }", "Allow subagents in this chat"),
    ("stop_job", ", input: { job_id: 'job-1' }", "Allow stopping jobs in this chat"),
    ("remember", ", input: { text: 'Tests run with pytest -q.' }", "Allow saving memory in this chat"),
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
    page.get_by_role("button", name="Ran 2 commands make lint", exact=True).click()  # The two finished commands fold.
    expect(page.get_by_text("Allowed by a rule for this chat", exact=True)).to_have_count(1)
    card = page.locator(".tool-card").filter(has=page.get_by_text("Allowed by a rule for this chat"))
    expect(card.get_by_role("button", name="run command make test")).to_be_visible()


def test_a_checked_read_only_command_says_so(ui, page):
    show(ui, """[{ id: 'checked', type: 'tool', name: 'run_command', state: 'completed', read_only_pipeline: true,
      input: { command: 'grep -n x a.py | head' }, output: '{}' }, { id: 'mixed', type: 'tool', name: 'run_command',
      state: 'completed', read_only_pipeline: true, allowed_by_rule: true, rule_scope: 'project',
      input: { command: 'make test 2>&1 | tail -5' }, output: '{}' }]""")
    page.get_by_role("button", name="Ran 2 commands make test 2>&1 | tail -5", exact=True).click()  # The two finished commands fold.
    expect(page.get_by_text("Ran as a read-only command (checked by the app)", exact=True)).to_have_count(1)
    expect(page.get_by_text("Checked by the app: read-only commands and a rule for this project", exact=True)).to_have_count(1)
    expect(page.get_by_text("Allowed by a rule for this project", exact=True)).to_have_count(0)


def test_a_command_naming_a_secret_file_warns_and_offers_nothing_to_remember(ui, page):
    show(ui, f"[{pending('secret', 'run_command', ", input: { command: 'cat ~/.databrickscfg' }, rememberable: [], secret_files: ['~/.databrickscfg']")}]")
    warning = page.locator(".tool-secret")
    expect(warning).to_have_text("Reads a secret file: ~/.databrickscfg. If you approve, its contents can reach the model; "
                                 "only known token shapes and your credential file's values are hidden. Such a command asks "
                                 "in every mode and cannot be remembered.")
    expect(warning.locator("code")).to_have_text("~/.databrickscfg")
    expect(page.get_by_role("button", name=re.compile("in this chat$"))).to_have_count(0)
    expect(page.get_by_role("button", name="Always allow…")).to_have_count(0)
    page.get_by_role("button", name="Approve").click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui, "secret") == [{"allowed": True}]


def test_a_finished_secret_command_names_the_file_in_its_rule_line(ui, page):
    show(ui, """[{ id: 'done', type: 'tool', name: 'run_command', state: 'completed', approval: 'approved',
      secret_files: ['.env', 'id_rsa'], input: { command: 'cat .env id_rsa' }, output: '{}' }]""")
    expect(page.get_by_text("Named a secret file: .env, id_rsa", exact=True)).to_be_visible()
    expect(page.locator(".tool-secret")).to_have_count(0)


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


# Chat rules ("This chat") in the Permissions menu

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
    return page.get_by_role("group", name="This chat", exact=True)


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


def test_rules_can_be_removed_while_the_chat_is_busy(ui, page):
    rules = open_permissions(ui, page)
    ui.emit("a", {"type": "status", "status": "awaiting_approval"})
    remove = rules.get_by_role("menuitem", name="Remove rule: File edits")
    expect(remove).to_have_attribute("aria-disabled", "false")
    note = page.get_by_text("Changes apply to the agent's next action.", exact=True)
    expect(note).to_be_visible()
    # to_be_visible ignores clipping by the menu's scroll area; the note must sit inside the menu's
    # visible box at any scroll position, including the top.
    menu = page.get_by_role("menu")
    for position in ("0", "m.scrollHeight"):
        assert menu.evaluate(f"m => {{ m.scrollTop = {position}; return m.scrollHeight > m.clientHeight }}")
        area, box = menu.bounding_box(), note.bounding_box()
        assert area["y"] <= box["y"] and box["y"] + box["height"] <= area["y"] + area["height"], (position, area, box)
    remove.click()
    expect(rules.get_by_role("menuitem")).to_have_count(3)
    assert ui.bodies("/api/sessions/a/allow-rules", "DELETE") == [{"tool": "edits"}]


def test_removing_rules_with_the_keyboard_keeps_focus_in_the_menu(ui, page):
    rules = open_permissions(ui, page)
    # End reaches the menu's last items (Add a rule…, Never asks); the last rule is just above them.
    page.keyboard.press("End")
    expect(page.locator(".never-asks>summary")).to_be_focused()
    page.keyboard.press("ArrowUp")
    expect(page.get_by_role("menuitem", name="Add a rule…")).to_be_focused()
    page.keyboard.press("ArrowUp")
    expect(rules.get_by_role("menuitem", name="Remove rule: Hook: check")).to_be_focused()
    for remaining, focused in ((3, "Tool: mcp__docs__search"), (2, "File edits"), (1, "Command: make test")):
        page.keyboard.press("Enter")
        expect(rules.get_by_role("menuitem")).to_have_count(remaining)
        expect(rules.get_by_role("menuitem", name=f"Remove rule: {focused}")).to_be_focused()
    page.keyboard.press("Enter")
    expect(rules).to_have_count(0)
    expect(page.get_by_role("menuitemradio", name=re.compile("^Manual"))).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.get_by_role("menu")).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("^Permissions:"))).to_be_focused()


def test_removing_a_rule_does_not_mark_the_chat_unread_after_leaving(ui, page):
    # The server saves the chat (a newer 'updated') but pushes only a 'permissions' message.
    ui.app(session="a", scenario="workspace", clock=True, setup=RULES.replace(
        "allow_rules: fake.sessions.get('a').allow_rules", "updated: 9, allow_rules: fake.sessions.get('a').allow_rules")
        + "\nfake.sessions.set('b', makeSession('b', 'Second chat'))")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    ui.show_session("a")
    page.get_by_role("button", name=re.compile("^Permissions:")).click()
    page.get_by_role("menuitem", name="Remove rule: File edits").click()
    expect(page.get_by_role("menuitem", name=re.compile("^Remove rule:"))).to_have_count(3)
    page.keyboard.press("Escape")
    page.get_by_role("button", name=re.compile("^Second chat")).click()
    ui.show_session("b")
    polls = len(ui.requests("/api/sessions", "GET"))
    ui.tick(10000)  # The sessions list is polled and now has the newer 'updated'.
    ui.wait(f"fake.requests('/api/sessions', 'GET').length > {polls}")
    expect(page.get_by_role("button", name=re.compile("^Other conversation"))).not_to_contain_text("Unread")
    assert json.loads(ui.js("localStorage.getItem('local.seenConversations')"))["a"] == 9
    # A later change by the agent is still news.
    ui.run("fake.sessions.set('a', { ...fake.sessions.get('a'), updated: 12 })")
    ui.tick(10000)
    expect(page.get_by_role("button", name=re.compile("^Other conversation"))).to_contain_text("Unread")


def test_a_command_rule_shows_its_exact_spacing(ui, page):
    ui.app(session="a", scenario="workspace", setup=RULES + "\nfake.sessions.get('a').allow_rules = [{ tool: 'run_command', command: 'echo  two  spaces' }]")
    ui.show_session("a")
    page.get_by_role("button", name=re.compile("^Permissions:")).click()
    code = page.get_by_role("group", name="This chat", exact=True).locator("code")
    expect(code).to_have_text("echo  two  spaces")
    assert code.evaluate("element => element.innerText") == "echo  two  spaces"


def test_a_chat_without_rules_shows_no_rule_list(ui, page):
    ui.app(session="a", scenario="workspace")
    ui.show_session("a")
    page.get_by_role("button", name=re.compile("^Permissions:")).click()
    expect(page.get_by_role("menuitemradio", name=re.compile("^Manual"))).to_be_visible()
    expect(page.get_by_role("group", name="This chat", exact=True)).to_have_count(0)


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


def open_review(ui, page):
    ui.emit("b", {"type": "changes", "changes": {"files": [{"path": "note.txt", "status": "modified", "added": 2, "removed": 1,
                                                            "current_hash": "hash-note.txt"}], "added": 2, "removed": 1}})
    page.get_by_role("region", name="Changed files").get_by_role("button", name=re.compile("^1 file changed")).click()
    page.get_by_title("Review note.txt").click()
    return page.get_by_role("region", name="Agent changes in note.txt")


def test_undo_in_the_file_review_offers_to_open_the_blocking_chat(ui, page):
    ui.app(session="b", scenario="workspace", setup=BLOCKED)
    ui.show_session("b")
    review = open_review(ui, page)
    review.get_by_role("button", name="Undo change 1").click()
    alert = review.get_by_role("alert")
    expect(alert).to_contain_text("“Other conversation” is waiting for your approval. Answer or stop it before undoing agent changes.")
    expect(page.locator(".app-error")).to_have_count(0)
    # The selected chat itself is not offered; a later error naming another chat is.
    ui.run("blocker = 'b'")
    review.get_by_role("button", name="Undo this file").click()
    expect(alert).to_contain_text("“Selected chat” is waiting for your approval.")
    expect(page.get_by_role("button", name=re.compile("^Open “"))).to_have_count(0)
    ui.run("blocker = 'a'")
    review.get_by_role("button", name="Undo this file").click()
    alert.get_by_role("button", name="Open “Other conversation”").click()
    ui.wait("fake.socketsFor('a').length === 1")
    assert ui.js("location.search") == "?session=a"


RESTORE_BLOCKED = BLOCKED + """
window.checkpoint = { id: 'cp', path: 'src/main.py', status: 'available', created: 1 }
fake.override = path => {
  if (path === '/api/checkpoints?session_id=b') return fake.json([checkpoint])
  if (path === '/api/checkpoints/cp/preview?session_id=b') return fake.json({ ...checkpoint, diff: '-a\\n+b', expected_current_hash: 'h', can_restore: true })
  if (path === '/api/checkpoints/cp/restore?session_id=b') return fake.json({ detail: {
    message: '“Other conversation” is still running. Stop it before restoring checkpoints.', session_id: 'a', title: 'Other conversation' } }, 409)
}"""


def test_a_blocked_recovery_restore_can_open_the_blocking_chat_from_the_dialog(ui, page):
    ui.app(session="b", scenario="workspace", setup=RESTORE_BLOCKED)
    ui.show_session("b")
    page.get_by_role("button", name="Agent tools", exact=True).click()
    dialog = page.get_by_role("dialog", name="Agent tools")
    dialog.get_by_role("button", name="Checkpoints", exact=True).click()
    dialog.get_by_role("button", name="Preview checkpoint src/main.py").click()
    dialog.get_by_role("button", name="Restore checkpoint").click()
    expect(dialog.get_by_role("alert")).to_contain_text("“Other conversation” is still running. Stop it before restoring checkpoints.")
    dialog.get_by_role("button", name="Open “Other conversation”").click()
    expect(dialog).to_have_count(0)
    ui.wait("fake.socketsFor('a').length === 1")
    assert ui.js("location.search") == "?session=a"


# The split "Always allow" button (one control for the chat rule and the wider scopes)

SPLIT_COMMAND = """{ id: 'cmd', type: 'tool', name: 'run_command', state: 'pending', preview: 'make test',
  input: { command: 'make test' }, rememberable: ['chat', 'project', 'user'], editable: ['command'],
  rule_options: { argv: ['make', 'test'], prefixes: [{ prefix: ['make'], flags: [] }], reason: null } }"""
SPLIT_EDIT = """{ id: 'edit', type: 'tool', name: 'edit_file', state: 'pending', preview: 'diff',
  input: { path: 'src/a.py' }, rememberable: ['chat', 'project', 'user'], rule_options: { folder: '/project/src' } }"""


def approval_app(ui, card):
    events = f"[{{ id: 'u', type: 'user', text: 'Run the tests' }}, {card}]"
    ui.app(session="a", scenario="workspace", setup=f"""
      fake.sessions.set('a', {{ ...makeSession('a', 'Tests'), status: 'awaiting_approval', events: {events} }})
      fake.override = path => /^\\/api\\/sessions\\/a\\/approvals\\//.test(path) ? fake.json({{ ok: true }}) : undefined""")
    ui.show_session("a")


def button_rows(buttons):
    tops = sorted(box["y"] for box in (button.bounding_box() for button in buttons))
    rows = []
    for top in tops:
        if not rows or top - rows[-1] > 8:
            rows.append(top)
    return len(rows)


@pytest.mark.parametrize("width, height, max_rows", [(1440, 900, 1), (820, 1180, 1), (390, 844, 2), (360, 740, 2)])
def test_the_approval_buttons_stay_compact_and_keep_their_names(ui, page, width, height, max_rows):
    # B-T27. Mutations: remove the container query (more rows at 360), drop the long button's aria-label
    # (its name changes at 360), or use 'Allow in this chat' as the short text (fails label in name).
    page.set_viewport_size({"width": width, "height": height})
    approval_app(ui, SPLIT_COMMAND)
    card = page.locator(".tool-card.needs-approval")
    allow = card.get_by_role("button", name="Always allow this command in this chat", exact=True)
    expect(allow).to_be_visible()
    buttons = [button for button in card.locator(".approval-buttons").first.locator("button").all() if button.is_visible()]
    assert len(buttons) == 5  # Decline, Edit, the split button's two halves, Approve
    assert button_rows(buttons) <= max_rows, [button.bounding_box() for button in buttons]
    # The short label belongs to narrow cards (560 px and less), as on phones.
    narrow = card.bounding_box()["width"] <= 560
    assert narrow or width > 600
    expected = "Always allow this command" if narrow else "Always allow this command in this chat"
    assert allow.evaluate("e => e.innerText.trim()") == expected
    for button in card.locator("button").all():
        if not button.is_visible():
            continue
        text, name = button.evaluate("e => [e.innerText, e.getAttribute('aria-label') || e.textContent]"
                                     ".map(value => value.replace(/\\s+/g, ' ').trim().toLowerCase())")
        assert text in name, (text, name)
    assert ui.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


def test_the_split_button_approves_for_the_chat_and_its_arrow_opens_the_scopes(ui, page):
    approval_app(ui, SPLIT_COMMAND)
    card = page.locator(".tool-card.needs-approval")
    expect(card.get_by_role("group", name="Always allow")).to_be_visible()
    toggle = card.get_by_role("button", name="Always allow…", exact=True)
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    form = card.get_by_role("group", name="Remember this approval")
    expect(form).to_be_visible()
    # Focus enters the scope choices on the checked one (the default scope), not on the first radio.
    checked = form.get_by_role("radio", checked=True).first
    expect(checked).to_be_focused()
    expect(checked).to_have_accessible_name("This project")
    toggle.click()
    expect(form).to_have_count(0)
    expect(toggle).to_be_focused()
    card.get_by_role("button", name="Always allow this command in this chat", exact=True).click()
    ui.wait("fake.requests('/api/sessions/a/approvals/cmd', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/approvals/cmd", "POST") == [{"allowed": True, "remember": True}]


def test_other_cards_keep_their_full_label_or_their_single_buttons(ui, page):
    page.set_viewport_size({"width": 360, "height": 740})
    approval_app(ui, SPLIT_EDIT)
    allow = page.get_by_role("button", name="Allow edits in this chat", exact=True)
    expect(allow).to_be_visible()
    assert allow.evaluate("e => e.innerText.trim()") == "Allow edits in this chat"
    page.get_by_role("button", name="Always allow…", exact=True).click()
    focused = page.get_by_role("group", name="Remember this approval").get_by_role("radio", checked=True).first
    expect(focused).to_be_focused()
    expect(focused).to_have_accessible_name("This project")
    expect(page.get_by_role("radio", name="This chat")).not_to_be_checked()


def test_cards_without_saved_scopes_keep_the_single_buttons(ui, page):
    # An older card (no rememberable list) and a protected file: no split button.
    show(ui, f"""[{pending('old', 'run_command', ", input: { command: 'make' }")},
      {pending('protected', 'write_file', ", input: { path: '.env' }, protected: '.env', rememberable: ['chat']")}]""")
    expect(page.locator(".split-button")).to_have_count(0)
    old = page.locator(".tool-card").nth(0)
    expect(old.get_by_role("button", name="Always allow this command in this chat", exact=True)).to_have_text(
        "Always allow this command in this chat")
    expect(old.get_by_role("button", name="Always allow…")).to_have_count(0)
    protected = page.locator(".tool-card").nth(1)
    expect(protected.get_by_role("button")).to_have_text(["write file.env", "Decline", "Approve"])


# A waiting card the user scrolled away from (2026-10-16 F1)

LONG_CHAT = json.dumps([event for n in range(12) for event in (
    {"id": f"u{n}", "type": "user", "text": f"Step {n}"},
    {"id": f"r{n}", "type": "assistant", "text": "A long reply.\n\n" + "Some detail about the change. " * 40})])


def long_chat(ui, page):
    page.set_viewport_size({"width": 1280, "height": 700})
    ui.app(session="a", scenario="workspace", setup=f"fake.sessions.set('a', {{ ...makeSession('a', 'Long chat'), events: {LONG_CHAT} }})")
    ui.show_session("a")
    expect(page.locator(".transcript .user-message").first).to_be_attached()
    ui.emit("a", {"type": "status", "status": "running"})


SETTLED = """e => new Promise(done => { let last = -1, still = 0
  const check = () => { still = e.scrollTop === last ? still + 1 : 0; last = e.scrollTop
    if (still >= 3) done(); else requestAnimationFrame(check) }
  requestAnimationFrame(check) })"""


def scroll_to_top(page):
    transcript = page.locator(".transcript")
    transcript.evaluate(SETTLED)  # A smooth scroll still in flight would carry the view back down (slow machines).
    transcript.evaluate("e => { e.style.scrollBehavior = 'auto'; e.scrollTop = 0; e.dispatchEvent(new Event('scroll')) }")
    transcript.evaluate(SETTLED)
    expect(page.get_by_text("Step 0", exact=True)).to_be_in_viewport()


WAITING = {"id": "w", "type": "tool", "name": "write_file", "state": "pending", "input": {"path": "a.txt", "content": "x"},
           "preview": "--- a.txt\n+++ a.txt\n+x"}


def approve_fully_in_view(ui):
    # The hint under the message box comes with the status and makes the transcript shorter: Approve must still be
    # wholly inside it (the transcript scrolls smoothly, so wait for it).
    ui.wait("""(() => { const list = document.querySelector('.transcript').getBoundingClientRect()
      const button = [...document.querySelectorAll('.tool-card.needs-approval button')].find(item => item.textContent.trim() === 'Approve')
      const place = button?.getBoundingClientRect()
      return Boolean(place) && place.top >= list.top - 1 && place.bottom <= list.bottom + 1 })()""")


def test_a_card_that_starts_waiting_comes_into_view_after_scrolling_up(ui, page):
    # Mutations: keep the scroll on follow.current only (the card stays below the fold), or do not scroll again when
    # the status brings the hint (its bottom, with Approve, is cut off).
    long_chat(ui, page)
    scroll_to_top(page)
    ui.emit("a", {"type": "event", "event": {**WAITING, "state": "running"}})
    ui.emit("a", {"type": "event", "event": WAITING})
    ui.emit("a", {"type": "status", "status": "awaiting_approval"})
    card = page.locator(".tool-card.needs-approval")
    expect(card).to_be_in_viewport()
    expect(page.locator(".composer-hint-show")).to_be_visible()
    approve_fully_in_view(ui)
    # Only when it starts waiting: an unrelated update later does not pull the view back.
    scroll_to_top(page)
    ui.emit("a", {"type": "event", "event": {**WAITING, "preview": "--- a.txt\n+++ a.txt\n+y"}})
    ui.emit("a", "{ type: 'context', context_info: contextInfo }")
    expect(page.get_by_text("Step 0", exact=True)).to_be_in_viewport()
    expect(card).not_to_be_in_viewport()


def test_show_in_the_hint_scrolls_to_the_waiting_card_and_focuses_it(ui, page):
    # Mutation: drop the Show button, or focus nothing.
    long_chat(ui, page)
    # The view follows the end.
    page.locator(".transcript").evaluate(
        "e => { e.style.scrollBehavior = 'auto'; e.scrollTop = e.scrollHeight; e.dispatchEvent(new Event('scroll')) }")
    ui.emit("a", {"type": "event", "event": WAITING})
    ui.emit("a", {"type": "status", "status": "awaiting_approval"})
    card = page.locator(".tool-card.needs-approval")
    expect(card).to_be_in_viewport()
    # While the view follows the end, Approve stays in view when the hint appears.
    expect(page.locator(".composer-hint-show")).to_be_visible()
    approve_fully_in_view(ui)
    scroll_to_top(page)
    expect(card).not_to_be_in_viewport()
    hint = page.locator(".composer-hint")
    expect(hint.get_by_role("status")).to_have_text("An action is waiting for your approval above.")
    hint.get_by_role("button", name="Show", exact=True).click()
    expect(card).to_be_in_viewport()
    # Focus lands on the card's heading, which names the action (in view, also on this card a little taller than the
    # transcript); Tab reaches its buttons.
    heading = card.get_by_role("button", name="write file a.txt", exact=True)
    expect(heading).to_be_focused()
    expect(heading).to_be_in_viewport(ratio=1)


def test_a_memory_card_names_the_note_and_edits_it_as_a_note(ui, page):
    show(ui, f"[{pending('note', 'remember', ", input: { text: 'Tests run with pytest -q.' }, editable: ['text'], rememberable: ['chat', 'project', 'user']")}]")
    expect(page.get_by_role("button", name=re.compile("^remember Tests run with pytest -q\\.")).first).to_be_visible()
    page.get_by_role("button", name="Edit", exact=True).click()
    editor = page.get_by_role("textbox", name="Note")
    expect(editor).to_have_value("Tests run with pytest -q.")
    editor.fill("Tests run with pytest -q from backend/.")
    page.get_by_role("button", name="Save edit").click()
    ui.wait("fake.calls.length === 1")
    assert ui.bodies("/api/sessions/parent/approvals/note/edit", "POST") == [{"input": {"text": "Tests run with pytest -q from backend/."}}]


# Web cards (web_fetch.py): one domain per card

WEB_FETCH = ", input: { url: 'https://docs.example.com/guide' }, web_domain: 'docs.example.com', rememberable: ['chat', 'project', 'user'], rule_options: { domain: 'docs.example.com' }"


def test_a_web_fetch_card_offers_allow_once_the_domain_in_this_chat_or_always(ui, page):
    show(ui, f"[{pending('fetch', 'web_fetch', WEB_FETCH)}]")
    card = page.locator(".tool-card.needs-approval")
    expect(card).to_contain_text("Local needs your approval to fetch from docs.example.com.")
    expect(card.get_by_role("button", name=re.compile(r"^web fetch https:\/\/docs\.example\.com\/guide"))).to_be_visible()
    card.get_by_role("button", name="Always allow…", exact=True).click()
    form = card.get_by_role("group", name="Remember this approval")
    expect(form).to_contain_text("Allows fetches from docs.example.com only (not its subdomains).")
    form.get_by_role("radio", name="All projects").check()
    form.get_by_role("button", name="Approve and remember").click()
    ui.wait("fake.calls.length === 1")
    card.get_by_role("button", name="Allow docs.example.com in this chat", exact=True).click()
    ui.wait("fake.calls.length === 2")
    card.get_by_role("button", name="Allow once", exact=True).click()
    ui.wait("fake.calls.length === 3")
    assert decisions(ui, "fetch") == [{"allowed": True, "scope": "user"}, {"allowed": True, "remember": True},
                                      {"allowed": True}]


def test_a_web_search_card_shows_the_query_and_needs_no_chat_button(ui, page):
    show(ui, f"[{pending('search', 'web_search', ", input: { query: 'python asyncio' }, web_provider: 'Brave Search', rememberable: ['chat', 'project', 'user']")}]")
    card = page.locator(".tool-card.needs-approval")
    expect(card).to_contain_text("Local needs your approval to search the web with Brave Search.")
    expect(card.get_by_role("button", name=re.compile("^web search python asyncio"))).to_be_visible()
    expect(card.get_by_role("button", name=re.compile("in this chat$"))).to_have_count(0)
    card.get_by_role("button", name="Approve", exact=True).click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui, "search") == [{"allowed": True}]


# Hidden and direction-changing characters in what you approve are shown as visible escapes

NOTE = "Hidden or direction-changing characters are shown as \\u escapes."


def command_card(ui, command: str, extra: str = ""):
    """A pending command card; `command` is the body of a JavaScript string literal (escapes allowed)."""
    show(ui, f"[{{ id: 'card', type: 'tool', name: 'run_command', state: 'pending', input: {{ command: '{command}' }}, "
             f"preview: '{command}\\n\\nTimeout: 60 seconds'{extra} }}]")


@pytest.mark.parametrize("command, shown, raw", [
    ("echo safe \\u202e txt.sh # rm -rf ~", "echo safe \\u202e txt.sh # rm -rf ~", "‮"),
    ("echo a\\u200bb", "echo a\\u200bb", "​"),
    ("echo a\\rrm -rf ~", "echo a\\x0drm -rf ~", "\r"),
    ("echo \\u001b[2Jhi", "echo \\x1b[2Jhi", "\x1b"),
    ("echo a\\u2028b", "echo a\\u2028b", " "),
    ("echo a\\u{e0041}b", "echo a\\u{e0041}b", "\U000e0041"),
])
def test_a_command_with_hidden_characters_is_shown_with_visible_escapes(ui, page, command, shown, raw):
    command_card(ui, command)
    card = page.locator(".tool-card")
    expect(card.get_by_label("Command", exact=True).first).to_contain_text(shown)
    expect(card.get_by_text(NOTE, exact=True)).to_have_count(1)
    assert raw not in card.inner_text() and raw not in card.text_content(), "the card holds the raw character"
    assert raw not in (card.get_by_role("button").first.get_attribute("aria-label") or "")


def test_the_note_appears_only_when_something_was_escaped(ui, page):
    command_card(ui, 'echo "héllo 日本語 🙂"')
    card = page.locator(".tool-card")
    expect(card.get_by_label("Command", exact=True).first).to_have_text('echo "héllo 日本語 🙂"\n\nTimeout: 60 seconds')
    expect(page.get_by_text("escapes.", exact=False)).to_have_count(0)
    assert "\\u" not in card.inner_text()


def test_approving_a_card_with_hidden_characters_sends_the_original_decision_only(ui, page):
    command_card(ui, "echo safe \\u202e txt.sh # rm -rf ~")
    page.get_by_role("button", name="Approve", exact=True).click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui, "card") == [{"allowed": True}]


def test_editing_a_command_with_hidden_characters_shows_it_escaped_and_sends_what_you_type(ui, page):
    command_card(ui, "echo a\\u202eb", ", editable: ['command']")
    page.get_by_role("button", name="Edit", exact=True).click()
    expect(page.get_by_text(NOTE, exact=True)).to_have_count(1)
    original = page.get_by_label("Original command")
    expect(original).to_have_text("echo a\\u202eb")
    assert "‮" not in original.text_content()
    editor = page.get_by_role("textbox", name="Command")
    assert ui.js("document.querySelector('.approval-edit textarea').value") == "echo a‮b"  # What you edit stays as it is.
    editor.fill("echo ‮ typed")
    page.get_by_role("button", name="Save edit").click()
    ui.wait("fake.calls.length === 1")
    assert ui.bodies("/api/sessions/parent/approvals/card/edit", "POST") == [{"input": {"command": "echo ‮ typed"}}]


def test_options_and_the_approval_preview_escape_hidden_characters(ui, page):
    show(ui, r"""[{ id: 'opts', type: 'tool', name: 'run_command', state: 'pending', input: { command: 'make test', cwd: 'a‮b' } },
      { id: 'prev', type: 'tool', name: 'write_file', state: 'pending', input: { path: 'a.txt', content: 'x' },
        preview: 'Write a‮b.txt\n​new text' }]""")
    expect(page.get_by_label("Options", exact=True)).to_contain_text("a\\u202eb")
    preview = page.get_by_label("Approval preview", exact=True)
    expect(preview).to_contain_text("Write a\\u202eb.txt\n\\u200bnew text")
    assert "‮" not in page.locator(".tool-card").first.text_content() + page.locator(".tool-card").last.text_content()
    assert "​" not in page.locator(".tool-card").last.text_content()
    expect(page.get_by_text(NOTE, exact=True)).to_have_count(2)


def test_the_paths_and_hook_arguments_of_an_approval_card_escape_hidden_characters(ui, page):
    show(ui, r"""[{ id: 'card', type: 'tool', name: 'edit_file', state: 'pending', input: { path: 'a.txt' }, preview: 'p',
      protected: 'a‮txt.sh', hook_change: { hooks: ['fix'], original: { command: 'x​y' } } }]""")
    card = page.locator(".tool-card")
    expect(card.get_by_text("Protected file: a\\u202etxt.sh.", exact=False)).to_have_count(1)
    expect(card.get_by_label("Arguments before the hook")).to_contain_text("x\\u200by")
    text = card.text_content()
    assert "‮" not in text and "​" not in text


def test_visible_text_escapes_only_characters_that_hide_or_reorder_text(ui):
    ui.start()
    result = ui.js("""(async () => {
      const { visibleText } = await import('/static/js/lib.js')
      const out = {}
      for (const [name, text] of Object.entries({
        bidi: '\\u202a\\u202b\\u202c\\u202d\\u202e\\u2066\\u2067\\u2068\\u2069', zeroWidth: '\\u200b\\u200c\\u200d\\u200e\\u200f\\u2060\\u2064\\ufeff\\u061c\\u180e',
        controls: '\\u0000\\u0007\\u000b\\r\\u001b\\u007f\\u0085\\u009f', separators: '\\u2028\\u2029', tags: '\\u{e0000}\\u{e0041}\\u{e007f}',
        plain: 'a\\nb\\tc héllo 日本語 🙂 e\\u0301 👨‍👩‍👧', }))
        out[name] = visibleText(text)
      return out })()""")
    assert result["bidi"] == "\\u202a\\u202b\\u202c\\u202d\\u202e\\u2066\\u2067\\u2068\\u2069"
    assert result["zeroWidth"] == "\\u200b\\u200c\\u200d\\u200e\\u200f\\u2060\\u2064\\ufeff\\u061c\\u180e"
    assert result["controls"] == "\\x00\\x07\\x0b\\x0d\\x1b\\x7f\\x85\\x9f"
    assert result["separators"] == "\\u2028\\u2029"
    assert result["tags"] == "\\u{e0000}\\u{e0041}\\u{e007f}"
    # Newline, tab, accented and CJK letters, emoji, combining marks and the joiners of an emoji sequence stay.
    assert result["plain"] == "a\nb\tc héllo 日本語 🙂 é 👨‍👩‍👧"


# Rules, other zero-width and blank characters, and which escapes are marked

RLO = "‮"


def js_escape(code: int) -> str:
    """A JavaScript string-literal escape for one code point."""
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\u{{{code:x}}}"


def visible_escape(code: int) -> str:
    """What visibleText writes for it."""
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{{{code:x}}}" if code > 0xFFFF else f"\\u{code:04x}"


def remember_card(ui, rule_options: str, extra: str = ""):
    show(ui, "[{ id: 'card', type: 'tool', name: 'run_command', state: 'pending', input: { command: 'echo safe x' }, "
             "preview: 'echo safe x', rememberable: ['chat', 'project', 'user']" + extra + f", rule_options: {rule_options} }}]")


def test_the_always_allow_form_shows_the_rule_with_visible_escapes_and_saves_the_original(ui, page):
    remember_card(ui, "{ argv: ['echo', 'safe', '\\u202e', 'txt.sh'], "
                      "prefixes: [{ prefix: ['echo', 'safe', '\\u202e'], flags: ['--x\\u202e'] }], reason: null }")
    card = page.locator(".tool-card")
    expect(card.get_by_text(NOTE, exact=True)).to_have_count(1)
    page.get_by_role("button", name="Always allow…").click()
    form = page.get_by_role("group", name="Remember this approval")
    expect(form.locator("code").first).to_have_text("echo safe '\\u202e' txt.sh")
    expect(form.locator("code .hidden-char")).to_have_count(1)
    form.get_by_role("radio", name="Commands starting with").check()
    expect(form.get_by_role("combobox", name="Command start").locator("option")).to_have_text(["echo safe '\\u202e'"])
    # The input keeps the real value (it is what is sent); its escaped form is shown beside it, read-only.
    assert ui.js("document.querySelector('.remember-prefix input').value") == f"--x{RLO}"
    expect(form.locator(".hidden-echo code")).to_have_text("--x\\u202e")
    assert RLO not in card.text_content() and RLO not in form.inner_text()
    form.get_by_role("radio", name=re.compile("^Exactly this command")).check()
    form.get_by_role("button", name="Approve and remember").click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui, "card") == [{"allowed": True, "scope": "project", "rule": {
        "tool": "run_command", "behavior": "allow", "argv": ["echo", "safe", RLO, "txt.sh"]}}]


def test_a_prefix_rule_is_saved_with_its_original_prefix_and_flags(ui, page):
    remember_card(ui, "{ argv: ['echo'], prefixes: [{ prefix: ['echo', '\\u202e'], flags: ['--x\\u202e'] }], reason: null }")
    page.get_by_role("button", name="Always allow…").click()
    form = page.get_by_role("group", name="Remember this approval")
    form.get_by_role("radio", name="Commands starting with").check()
    form.get_by_role("button", name="Approve and remember").click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui, "card") == [{"allowed": True, "scope": "project", "rule": {
        "tool": "run_command", "behavior": "allow", "prefix": ["echo", RLO], "flags": [f"--x{RLO}"]}}]


def test_a_domain_and_a_folder_with_hidden_characters_are_shown_escaped(ui, page):
    show(ui, "[{ id: 'fetch', type: 'tool', name: 'web_fetch', state: 'pending', preview: 'p', input: { url: 'https://docs.example.com/' }, "
             "web_domain: 'docs.example.com\\u202e', rememberable: ['chat', 'project', 'user'], rule_options: { domain: 'docs.example.com\\u202e' } }, "
             "{ id: 'edit', type: 'tool', name: 'edit_file', state: 'pending', preview: 'd', input: { path: 'a.py' }, "
             "rememberable: ['chat', 'project', 'user'], rule_options: { folder: '/project/s\\u202erc' } }]")
    fetch, edit = page.locator(".tool-card").first, page.locator(".tool-card").last
    expect(fetch).to_contain_text("Local needs your approval to fetch from docs.example.com\\u202e.")
    expect(fetch.get_by_text(NOTE, exact=True)).to_have_count(1)
    fetch.get_by_role("button", name="Always allow…", exact=True).click()
    expect(fetch.locator(".remember-domain code")).to_have_text("docs.example.com\\u202e")
    expect(fetch.locator(".remember-domain .hidden-char")).to_have_count(1)
    allow = fetch.get_by_role("button", name="Allow docs.example.com\\u202e in this chat", exact=True)
    expect(allow).to_have_count(1)
    assert RLO not in fetch.text_content() and RLO not in (allow.get_attribute("aria-label") or "") + (allow.get_attribute("title") or "")
    allow.click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui, "fetch") == [{"allowed": True, "remember": True}]
    expect(edit.get_by_text(NOTE, exact=True)).to_have_count(1)
    edit.get_by_role("button", name="Always allow…", exact=True).click()
    edit.get_by_role("radio", name="Files under").check()
    assert ui.js("document.querySelector('.remember-folder input').value") == f"/project/s{RLO}rc"
    expect(edit.locator(".hidden-echo code")).to_have_text("/project/s\\u202erc")
    edit.get_by_role("button", name="Approve and remember").click()
    ui.wait("fake.calls.length === 2")
    assert decisions(ui, "edit") == [{"allowed": True, "scope": "project", "rule": {"tool": "edits", "behavior": "allow", "path": f"/project/s{RLO}rc"}}]


def test_a_value_typed_into_the_options_input_is_echoed_escaped(ui, page):
    remember_card(ui, "{ argv: ['echo'], prefixes: [{ prefix: ['echo'], flags: [] }], reason: null }")
    page.get_by_role("button", name="Always allow…").click()
    form = page.get_by_role("group", name="Remember this approval")
    form.get_by_role("radio", name="Commands starting with").check()
    expect(form.get_by_text(NOTE, exact=True)).to_have_count(0)
    form.get_by_role("textbox", name="Allowed options after it").fill(f"--a{RLO}b")
    expect(form.locator(".hidden-echo code")).to_have_text("--a\\u202eb")
    expect(form.get_by_text(NOTE, exact=True)).to_have_count(1)


RULES_MENU = """{ mode: 'manual', onChange: fake.spy('change'), onError: fake.spy('error'), onDone: fake.spy('done'),
  onRemoveRule: fake.spy('remove'), onAddRule: fake.spy('add'), onReset: fake.spy('reset'),
  rules: [{ behavior: 'allow', tool: 'run_command', argv: ['echo', 'safe', '\\u202e', 'txt.sh'] }],
  projectRules: [{ behavior: 'deny', tool: 'run_command', prefix: ['echo', '\\u202e'] },
                 { behavior: 'allow', tool: 'web_fetch', domain: 'docs.example.com\\u202e' }],
  userRules: [{ behavior: 'allow', tool: 'run_command', prefix: ['git'], flags: ['--x\\u202e'] },
              { behavior: 'allow', tool: 'edits', path: '/a\\u202eb' }] }"""


def test_the_permissions_menu_shows_saved_rules_with_visible_escapes_and_removes_the_original(ui, page):
    ui.mount("components/PermissionsMenu.js", RULES_MENU)
    page.get_by_role("button", name=re.compile("^Permissions:")).click()
    menu = page.get_by_role("menu")
    rows = menu.locator(".allow-rule")
    expect(rows).to_have_count(5)
    expect(menu.get_by_role("menuitem", name="Remove rule: Command: echo safe '\\u202e' txt.sh")).to_have_count(1)
    expect(menu.get_by_role("menuitem", name="Remove rule: Commands starting with echo '\\u202e' (any options)")).to_have_count(1)
    expect(menu.get_by_role("menuitem", name="Remove rule: Fetches from docs.example.com\\u202e")).to_have_count(1)
    expect(menu.get_by_role("menuitem", name="Remove rule: Commands starting with git with options '--x\\u202e'")).to_have_count(1)
    expect(menu.get_by_role("menuitem", name="Remove rule: File edits under /a\\u202eb")).to_have_count(1)
    expect(menu.locator(".allow-rule .hidden-char")).to_have_count(5)
    assert RLO not in menu.text_content()
    titles = menu.locator(".allow-rule > span[title]").evaluate_all("nodes => nodes.map(node => node.title).join('')")
    assert RLO not in titles
    expect(menu.get_by_text(NOTE, exact=True)).to_have_count(1)
    # The harness has no stylesheet, so the menu is not laid out in the viewport: click through the DOM.
    menu.get_by_role("menuitem", name="Remove rule: Command: echo safe '\\u202e' txt.sh").dispatch_event("click")
    ui.wait("fake.spies.remove.length === 1")
    assert ui.spy("remove") == [["chat", {"behavior": "allow", "tool": "run_command", "argv": ["echo", "safe", RLO, "txt.sh"]}]]


def test_the_permissions_menu_has_no_note_without_hidden_characters(ui, page):
    ui.mount("components/PermissionsMenu.js", "{ mode: 'manual', onChange: fake.spy('c'), onError: fake.spy('e'), onDone: fake.spy('d'), "
                                              "onRemoveRule: fake.spy('r'), rules: [{ behavior: 'allow', tool: 'run_command', argv: ['echo', 'a b'] }] }")
    page.get_by_role("button", name=re.compile("^Permissions:")).click()
    expect(page.get_by_role("menu").locator(".allow-rule")).to_have_count(1)
    expect(page.get_by_text(NOTE, exact=True)).to_have_count(0)
    expect(page.locator(".hidden-char")).to_have_count(0)


def test_the_add_rule_dialog_echoes_hidden_characters_escaped_and_sends_what_was_typed(ui, page):
    ui.mount("components/PermissionRulesDialog.js", "{ onAdd: fake.spy('add'), onClose: fake.spy('close') }")
    dialog = page.get_by_role("dialog", name="Add a permission rule")
    command = dialog.get_by_role("textbox", name="Command")
    expect(dialog.get_by_text(NOTE, exact=True)).to_have_count(0)
    command.fill(f"echo safe {RLO} txt.sh")
    expect(dialog.get_by_text(NOTE, exact=True)).to_have_count(1)
    expect(dialog.locator(".hidden-echo code")).to_have_text("echo safe \\u202e txt.sh")
    expect(dialog.locator(".hidden-echo .hidden-char")).to_have_count(1)
    assert RLO not in dialog.locator(".hidden-echo").text_content()
    assert command.input_value() == f"echo safe {RLO} txt.sh"
    dialog.get_by_role("button", name="Add rule").click()
    ui.wait("fake.spies.add.length === 1")
    assert ui.spy("add") == [["project", {"tool": "run_command", "behavior": "allow", "text": f"echo safe {RLO} txt.sh", "match": "exact"}]]


def test_the_add_rule_dialog_echoes_a_domain_and_a_folder(ui, page):
    ui.mount("components/PermissionRulesDialog.js", "{ onAdd: fake.spy('add'), onClose: fake.spy('close') }")
    dialog = page.get_by_role("dialog", name="Add a permission rule")
    dialog.get_by_label("Applies to").select_option(label="Web fetches")
    dialog.get_by_role("textbox", name="Domain").fill(f"docs.example.com{RLO}")
    expect(dialog.locator(".hidden-echo code")).to_have_text("docs.example.com\\u202e")
    dialog.get_by_label("Applies to").select_option(label="File edits")
    dialog.get_by_role("textbox", name="Only under this folder").fill(f"/a{RLO}b")
    expect(dialog.locator(".hidden-echo code")).to_have_text("/a\\u202eb")


# Characters Chromium draws as nothing or as blank space

ZERO_WIDTH_OR_BLANK = [
    0x00AD, 0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D, 0x070F, 0xFE00, 0xFE0F, 0xE0100, 0xE01EF, 0x1D173, 0x1D17A,
    0x3164, 0xFFA0, 0x2800, 0x00A0, 0x1680, 0x2000, 0x200A, 0x202F, 0x205F, 0x3000,
    0x0600, 0x0605, 0x06DD, 0x0890, 0x0891, 0x08E2, 0xFFF9, 0xFFFB, 0x110BD, 0x110CD, 0x13430, 0x1343F, 0x1BCA0, 0x1BCA3,
    0x200C, 0x200D,
]


@pytest.mark.parametrize("code", ZERO_WIDTH_OR_BLANK, ids=[f"U+{code:04X}" for code in ZERO_WIDTH_OR_BLANK])
def test_a_pending_command_shows_an_escape_for_each_zero_width_or_blank_character(ui, page, code):
    command_card(ui, f"echo a{js_escape(code)}b")
    card = page.locator(".tool-card")
    expect(card.get_by_label("Command", exact=True).first).to_contain_text(f"echo a{visible_escape(code)}b")
    expect(card.get_by_text(NOTE, exact=True)).to_have_count(1)
    expect(card.get_by_label("Command", exact=True).first.locator(".hidden-char")).to_have_count(1)
    assert chr(code) not in card.text_content(), "the card holds the raw character"
    assert chr(code) not in (card.get_by_role("button").first.get_attribute("aria-label") or "")


def test_emoji_sequences_and_scripts_that_need_joiners_are_shown_unchanged_without_a_note(ui, page):
    text = ("echo \\u{1f468}\\u200d\\u{1f469}\\u200d\\u{1f467} \\u2764\\ufe0f \\u{1f44d}\\u{1f3fd} 1\\ufe0f\\u20e3 h\\u00e9llo \\u65e5\\u672c\\u8a9e "
            "\\u0633\\u0644\\u0627\\u0645\\u200c\\u0639\\u0644\\u06cc\\u06a9\\u0645 \\u0915\\u094d\\u200d\\u0937 \\u0915\\u094d\\u200c\\u0937")
    command_card(ui, text)
    card = page.locator(".tool-card")
    shown = card.get_by_label("Command", exact=True).first
    expect(shown).to_contain_text("echo 👨‍👩‍👧 ❤️ 👍🏽 1️⃣ héllo 日本語 سلام‌علیکم क्‍ष क्‌ष")
    expect(card.get_by_text(NOTE, exact=True)).to_have_count(0)
    expect(card.locator(".hidden-char")).to_have_count(0)


@pytest.mark.parametrize("text, escaped", [
    ("a\\u200db", "a\\u200db"),  # between ASCII letters
    ("a\\u200cb", "a\\u200cb"),
    ("\\u{1f468}\\u200d x", "👨\\u200d x"),  # a joiner that joins nothing
    ("x\\ufe0f", "x\\ufe0f"),  # a variation selector after a plain letter
    ("\\u0633\\u200d1", "س\\u200d1"),  # an Arabic letter and a digit
])
def test_a_joiner_or_selector_that_is_not_part_of_an_emoji_or_a_script_that_needs_it_is_escaped(ui, page, text, escaped):
    command_card(ui, f"echo {text}")
    card = page.locator(".tool-card")
    expect(card.get_by_label("Command", exact=True).first).to_contain_text(f"echo {escaped}")
    expect(card.get_by_text(RTL_NOTE if "\\u0633" in text else NOTE, exact=True)).to_have_count(1)  # An Arabic word is a run.


def test_a_real_escape_is_marked_and_the_same_text_typed_is_not(ui, page):
    command_card(ui, "echo \\u202e and \\\\u202e")
    shown = page.locator(".tool-card").get_by_label("Command", exact=True).first
    expect(shown).to_contain_text("echo \\u202e and \\u202e")
    marks = shown.locator(".hidden-char")
    expect(marks).to_have_count(1)
    expect(marks).to_have_text("\\u202e")
    expect(marks).to_have_attribute("title", "hidden character U+202E")
    assert shown.evaluate("node => node.textContent.split('\\\\u202e').length - 1") == 2  # The real one and the typed one read the same.
    assert ui.js("document.querySelector('.tool-card pre .hidden-char').previousSibling.textContent") == "echo "


def test_helpers_escape_by_category(ui):
    ui.start()
    result = ui.js("""(async () => {
      const { visibleText, visibleParts, hasHidden } = await import('/static/js/lib.js')
      const cases = {
        format: 'a\\u0600b\\u06ddc\\u08e2d', control: 'a\\u000cb', keep: 'a\\nb\\tc d', sep: 'a\\u2028b\\u2029c\\u00a0d\\u3000e',
        lone: 'a\\ud800b\\udc00c', pair: 'a\\u{1f600}b', privateUse: 'a\\ue000b', unassigned: 'a\\u0378b',
        selector: 'x\\ufe0f\\u{e0100}', emoji: '\\u2764\\ufe0f', keycap: '#\\ufe0f\\u20e3', zwj: 'a\\u200db', zwnjArabic: '\\u0633\\u200c\\u0639',
        devanagari: '\\u0915\\u094d\\u200d\\u0937', bengali: '\\u09b0\\u200d\\u09af',
      }
      const out = {}
      for (const [name, text] of Object.entries(cases)) out[name] = [visibleText(text), hasHidden(text)]
      const parts = visibleParts('a\\u202eb\\u202ec')
      out.parts = [parts.length, parts[0], parts[2], parts[4], parts[1].type, parts[1].props.class, parts[1].props.title, parts[1].props.children,
        visibleParts('plain').join('|'), visibleParts('').length]
      return out })()""")
    assert result["format"] == ["a\\u0600b\\u06ddc\\u08e2d", True]
    assert result["control"] == ["a\\x0cb", True]
    assert result["keep"] == ["a\nb\tc d", False]
    assert result["sep"] == ["a\\u2028b\\u2029c\\xa0d\\u3000e", True]
    assert result["lone"] == ["a\\ud800b\\udc00c", True]
    assert result["pair"] == ["a😀b", False]
    assert result["privateUse"] == ["a\\ue000b", True]  # Private use and unassigned code points have no agreed look.
    assert result["unassigned"] == ["a\\u0378b", True]
    assert result["selector"] == ["x\\ufe0f\\u{e0100}", True]
    assert result["emoji"] == ["❤️", False]
    assert result["keycap"] == ["#️⃣", False]
    assert result["zwj"] == ["a\\u200db", True]
    assert result["zwnjArabic"] == ["س‌ع", False]
    assert result["devanagari"] == ["क्‍ष", False]
    assert result["bengali"] == ["র‍য", False]
    count, first, third, fifth, tag, css, title, shown, plain, empty = result["parts"]
    assert (count, first, third, fifth, tag, shown, plain, empty) == (5, "a", "b", "c", "span", "\\u202e", "plain", 0)
    assert "hidden-char" in css.split() and title == "hidden character U+202E"


# Right-to-left words keep the typing order of the command around them

RTL_NOTE = NOTE + " Right-to-left text is kept in typing order."
# Each command with the tokens that must read left to right in this order.
RTL_COMMANDS = [
    ("cat א > 1", ["cat", "א", ">", "1"]),
    ("mv א ב", ["mv", "א", "ב"]),
    ("cp א 1 2", ["cp", "א", "1", "2"]),
    ("rm א && 1", ["rm", "א", "&&", "1"]),
    ("echo א; 1 2", ["echo", "א", ";", "1", "2"]),
    ("scp א 10.0.0.1:/x", ["scp", "א", "10.0.0.1", ":/x"]),
    ("mv ١ ٢", ["mv", "١", "٢"]),
    ("cat ܐ > 1", ["cat", "ܐ", ">", "1"]),
    ("cp ߊ 1 2", ["cp", "ߊ", "1", "2"]),
]
# The left edge of the first character of each token, found one range per character.
LEFT_EDGES = """([selector, tokens]) => {
  const root = document.querySelector(selector)
  const nodes = []
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  while (walker.nextNode()) nodes.push(walker.currentNode)
  const text = nodes.map(node => node.textContent).join('')
  const lefts = []
  for (const node of nodes) for (let i = 0; i < node.textContent.length; i++) {
    const range = document.createRange()
    range.setStart(node, i); range.setEnd(node, i + 1)
    lefts.push(range.getClientRects()[0]?.left ?? null)
  }
  let from = 0
  return tokens.map(token => { const at = text.indexOf(token, from); from = at + token.length; return [token, at, lefts[at]] })
}"""


@pytest.mark.parametrize("command, tokens", RTL_COMMANDS)
def test_right_to_left_words_do_not_reorder_a_command_on_screen(ui, page, command, tokens):
    command_card(ui, command)
    card = page.locator(".tool-card")
    expect(card.get_by_label("Command", exact=True).first).to_contain_text(command)
    for selector in (".tool-card pre[aria-label='Command']", ".tool-card .tool-subject"):
        found = page.evaluate(LEFT_EDGES, [selector, tokens])
        lefts = [left for _, _, left in found]
        assert all(left is not None for left in lefts), found
        assert lefts == sorted(lefts) and len(set(lefts)) == len(lefts), f"{selector}: tokens are not left to right: {found}"
    # An isolated run does not pull the neighbouring ASCII into the right-to-left order.
    assert card.locator("pre[aria-label='Command'] bdi.rtl-run").count() >= 1
    expect(card.get_by_text(RTL_NOTE, exact=True)).to_have_count(1)
    assert card.locator("pre[aria-label='Command']").first.evaluate("node => node.classList.contains('logical-order')")
    page.get_by_role("button", name="Approve", exact=True).click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui, "card") == [{"allowed": True}]


def test_a_card_with_only_ascii_has_no_isolates_and_no_extra_note(ui, page):
    command_card(ui, "cat a > 1")
    card = page.locator(".tool-card")
    expect(card.get_by_label("Command", exact=True).first).to_contain_text("cat a > 1")
    expect(card.locator("bdi")).to_have_count(0)
    expect(card.get_by_text("escapes.", exact=False)).to_have_count(0)
    expect(card.get_by_text("Right-to-left", exact=False)).to_have_count(0)


def test_a_right_to_left_file_name_shows_the_second_note_sentence(ui, page):
    show(ui, "[{ id: 'card', type: 'tool', name: 'edit_file', state: 'pending', input: { path: 'docs/ש.txt' }, preview: 'Edit docs/ש.txt' }]")
    card = page.locator(".tool-card")
    expect(card.get_by_text(RTL_NOTE, exact=True)).to_have_count(1)
    expect(card.locator("bdi.rtl-run").first).to_have_text("ש")


def test_a_right_to_left_text_alongside_an_escape_shows_both_in_one_note(ui, page):
    command_card(ui, "echo א \\u202e")
    card = page.locator(".tool-card")
    expect(card.get_by_text(RTL_NOTE, exact=True)).to_have_count(1)
    expect(card.locator("pre .hidden-char")).to_have_count(1)


# The escape rule is complete: a brute-force sweep of every code point

def test_every_hidden_or_blank_code_point_is_escaped(ui):
    ui.start()
    result = ui.js("""(async () => {
      const { hasHidden, visibleText } = await import('/static/js/lib.js')
      const need = /[\\p{Default_Ignorable_Code_Point}\\p{Cn}\\p{Co}\\p{Cf}\\p{Cc}\\p{Zl}\\p{Zp}\\p{Zs}\\uFFFC]/u
      let scanned = 0, checked = 0, escaped = 0
      const missed = []
      for (let code = 0; code <= 0x10ffff; code++) {
        if (code >= 0xd800 && code <= 0xdfff) continue
        scanned++
        if (code === 0x20 || code === 0x0a || code === 0x09) continue
        const char = String.fromCodePoint(code)
        if (!need.test(char)) continue
        checked++
        // Between two ASCII letters, so a selector or joiner has no emoji or script to belong to.
        if (hasHidden('a' + char + 'b')) escaped++
        else if (missed.length < 20) missed.push(code.toString(16))
      }
      const sample = visibleText('a\\u{e0080}b\\u{fffc}c\\u0e00d')
      return { scanned, checked, escaped, missed, sample } })()""")
    print("brute force:", result)
    assert result["scanned"] == 1114112 - 2048
    assert result["missed"] == [] and result["escaped"] == result["checked"] > 100000
    assert result["sample"] == "a\\u{e0080}b\\ufffcc\\u0e00d"


def test_a_base_with_more_than_two_combining_marks_escapes_the_third_and_later(ui):
    ui.start()
    result = ui.js("""(async () => {
      const { visibleText, hasHidden } = await import('/static/js/lib.js')
      return {
        stacked: visibleText('-\\u0334\\u0335\\u0336\\u20d2y'), two: visibleText('-\\u0334\\u0335y'),
        one: visibleText('e\\u0301'), precomposed: visibleText('\\u00e9'), arabic: visibleText('\\u0628\\u0650\\u0651 \\u0643\\u064e\\u062a\\u064e\\u0628\\u064e'),
        devanagari: visibleText('\\u0915\\u094d\\u0937\\u093f'), next: visibleText('a\\u0301\\u0301\\u0301 b\\u0301'), flag: hasHidden('a\\u0301\\u0301\\u0301') } })()""")
    assert result["stacked"] == "-̴̵\\u0336\\u20d2y"
    assert result["two"] == "-̴̵y"
    assert result["one"] == "é" and result["precomposed"] == "é"
    assert result["arabic"] == "بِّ كَتَبَ"
    assert result["devanagari"] == "क्षि"
    assert result["next"] == "á́\\u0301 b́" and result["flag"] is True
