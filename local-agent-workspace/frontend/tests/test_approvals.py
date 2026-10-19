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
