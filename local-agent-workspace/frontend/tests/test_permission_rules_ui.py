"""Permission rules in the UI: the Always allow form and Edit on approval cards, the Bypass
confirmation, mode changes while busy, rule scopes in the Permissions menu, the add-rule dialog,
turned-off tools, and the outcome lines of cards that ran or stopped without asking."""
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

ALL_SCOPES = "rememberable: ['chat', 'project', 'user']"
GIT_STATUS = f"""{{ id: 'card', type: 'tool', name: 'run_command', state: 'pending', preview: 'git status --short',
  input: {{ command: 'git status --short' }}, {ALL_SCOPES}, editable: ['command'],
  rule_options: {{ argv: ['git', 'status', '--short'], prefixes: [{{ prefix: ['git', 'status'], flags: ['--short'] }}], reason: null }} }}"""


def show(ui, events: str):
    ui.mount("components/Conversation.js", f"{{ session: makeSession({events}), onError: fake.spy('onError') }}", setup=SETUP)


def decisions(ui, event_id="card"):
    return ui.bodies(f"/api/sessions/parent/approvals/{event_id}", "POST")


def remember_form(page):
    page.get_by_role("button", name="Always allow…").click()
    return page.get_by_role("group", name="Remember this approval")


# The Always allow form

def test_always_allow_defaults_to_the_exact_command_in_this_project(ui, page):
    show(ui, f"[{GIT_STATUS}]")
    form = remember_form(page)
    expect(form.get_by_role("radio", name="This project")).to_be_checked()
    expect(form.get_by_role("radio", name="This chat")).not_to_be_checked()
    expect(form.get_by_role("radio", name="All projects")).not_to_be_checked()
    exact = form.get_by_role("radio", name=re.compile("^Exactly this command"))
    expect(exact).to_be_checked()
    expect(form.locator("code")).to_have_text("git status --short")
    form.get_by_role("button", name="Approve and remember").click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui) == [{"allowed": True, "scope": "project",
                              "rule": {"tool": "run_command", "behavior": "allow", "argv": ["git", "status", "--short"]}}]


def test_the_exact_command_is_shown_quoted(ui, page):
    show(ui, f"[{GIT_STATUS.replace("argv: ['git', 'status', '--short']", "argv: ['git', 'commit', '-m', 'fix it']")}]")
    expect(remember_form(page).locator("code")).to_have_text("git commit -m 'fix it'")


def test_a_server_offered_prefix_sends_prefix_and_flags(ui, page):
    show(ui, f"[{GIT_STATUS}]")
    form = remember_form(page)
    form.get_by_role("radio", name="All projects").check()
    form.get_by_role("radio", name="Commands starting with").check()
    start = form.get_by_role("combobox", name="Command start")
    # Only the server's prefixes are offered, never ones the page builds from the command.
    assert start.locator("option").all_inner_texts() == ["git status"]
    options = form.get_by_role("textbox", name="Allowed options after it")
    expect(options).to_have_value("--short")
    expect(form.get_by_text("Separate options with spaces. Use --name= to allow any value. Nothing else may follow.")).to_be_visible()
    form.get_by_role("button", name="Approve and remember").click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui) == [{"allowed": True, "scope": "user", "rule": {
        "tool": "run_command", "behavior": "allow", "prefix": ["git", "status"], "flags": ["--short"]}}]


def test_the_options_input_is_split_on_spaces_and_follows_the_chosen_prefix(ui, page):
    show(ui, f"""[{GIT_STATUS.replace("prefixes: [{ prefix: ['git', 'status'], flags: ['--short'] }]",
                                      "prefixes: [{ prefix: ['git'], flags: ['status', '--short'] }, { prefix: ['git', 'status'], flags: ['--short'] }]")}]""")
    form = remember_form(page)
    form.get_by_role("radio", name="Commands starting with").check()
    options = form.get_by_role("textbox", name="Allowed options after it")
    expect(options).to_have_value("status --short")
    form.get_by_role("combobox", name="Command start").select_option(label="git status")
    expect(options).to_have_value("--short")
    options.fill("  --short   -s ")
    form.get_by_role("button", name="Approve and remember").click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui)[0]["rule"] == {"tool": "run_command", "behavior": "allow", "prefix": ["git", "status"], "flags": ["--short", "-s"]}


def test_a_command_without_an_exact_argv_can_only_be_remembered_as_text_in_this_chat(ui, page):
    show(ui, """[{ id: 'card', type: 'tool', name: 'run_command', state: 'pending', preview: 'npm test | tee out',
      input: { command: 'npm test | tee out' }, rememberable: ['chat'],
      rule_options: { argv: null, prefixes: [], reason: 'uses shell syntax (|).' } }]""")
    # The form could only repeat the chat button, so the card offers that button alone and says why in its title.
    # Mutation: keep the split arrow when the form has nothing more to offer.
    expect(page.get_by_role("button", name="Always allow…")).to_have_count(0)
    expect(page.get_by_role("group", name="Always allow")).to_have_count(0)
    allow = page.get_by_role("button", name="Always allow this command in this chat", exact=True)
    expect(allow).to_have_attribute("title", "Always allow this command in this chat. uses shell syntax (|). Only this "
                                             "exact text can be allowed, in this chat.")
    allow.click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui) == [{"allowed": True, "remember": True}]


@pytest.mark.parametrize("name, extra", [
    ("mcp__docs__search", "input: {}, rememberable: ['chat']"),
    ("run_command", "input: { command: 'git status' }, rememberable: ['chat'], rule_options: { argv: ['git', 'status'], prefixes: [] }"),
])
def test_a_chat_only_card_keeps_the_arrow_only_when_the_form_offers_more(ui, page, name, extra):
    # An MCP tool for this chat only: nothing more to choose. A command with an exact argv: its form can match it.
    show(ui, f"[{{ id: 'card', type: 'tool', name: '{name}', state: 'pending', preview: 'p', {extra} }}]")
    toggle = page.get_by_role("button", name="Always allow…")
    if name == "run_command":
        expect(toggle).to_have_attribute("title", "Always allow…")
        expect(remember_form(page).get_by_role("radio", name="Exactly this command")).to_be_visible()
    else:
        expect(toggle).to_have_count(0)
        expect(page.get_by_role("button", name="Allow this tool in this chat", exact=True)).to_be_visible()


def test_edits_can_be_remembered_for_a_folder_or_for_all_files(ui, page):
    card = f"""{{ id: 'ID', type: 'tool', name: 'write_file', state: 'pending', preview: 'diff',
      input: {{ path: 'src/a.py', content: 'x' }}, {ALL_SCOPES}, rule_options: {{ folder: '/project/src' }} }}"""
    show(ui, f"[{card.replace('ID', 'one')}, {card.replace('ID', 'two')}]")
    page.get_by_role("button", name="Always allow…").first.click()
    form = page.get_by_role("group", name="Remember this approval")
    expect(form.get_by_role("radio", name="All file edits")).to_be_checked()
    form.get_by_role("radio", name="Files under").check()
    expect(form.get_by_role("textbox", name="Folder")).to_have_value("/project/src")
    form.get_by_role("button", name="Approve and remember").click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui, "one") == [{"allowed": True, "scope": "project", "rule": {"tool": "edits", "behavior": "allow", "path": "/project/src"}}]
    page.get_by_role("button", name="Always allow…").last.click()
    page.get_by_role("group", name="Remember this approval").last.get_by_role("radio", name="This chat").check()
    page.get_by_role("button", name="Approve and remember").last.click()
    ui.wait("fake.calls.length === 2")
    assert decisions(ui, "two") == [{"allowed": True, "scope": "chat", "rule": {"tool": "edits", "behavior": "allow"}}]


@pytest.mark.parametrize("name", ["mcp__docs__search", "hook_before_tool", "delegate_task", "stop_job"])
def test_other_cards_remember_a_scope_without_a_rule(ui, page, name):
    show(ui, f"[{{ id: 'card', type: 'tool', name: '{name}', state: 'pending', preview: 'p', input: {{}}, {ALL_SCOPES} }}]")
    form = remember_form(page)
    expect(form.get_by_role("radio", name="Exactly this command")).to_have_count(0)
    form.get_by_role("button", name="Approve and remember").click()
    ui.wait("fake.calls.length === 1")
    assert decisions(ui) == [{"allowed": True, "scope": "project"}]


def test_always_allow_offers_only_the_scopes_the_server_lists(ui, page):
    show(ui, f"[{GIT_STATUS.replace(ALL_SCOPES, "rememberable: ['chat', 'user']")}]")
    form = remember_form(page)
    expect(form.get_by_role("radio", name="This project")).to_have_count(0)
    expect(form.get_by_role("radio", name="This chat")).to_be_checked()
    show_none = GIT_STATUS.replace(ALL_SCOPES, "rememberable: []")
    ui.rerender(f"{{ session: makeSession([{show_none}]), onError: fake.spy('onError') }}")
    expect(page.get_by_role("button", name="Always allow…")).to_have_count(0)


def test_protected_cards_show_the_note_and_cannot_be_remembered(ui, page):
    # Even if a rememberable list arrived, a protected card offers neither form nor chat button.
    show(ui, f"""[{{ id: 'card', type: 'tool', name: 'write_file', state: 'pending', preview: 'diff',
      input: {{ path: '.vscode/tasks.json', content: '{{}}' }}, protected: 'editor and repository configuration (.vscode)',
      {ALL_SCOPES}, rule_options: {{ folder: '/project/.vscode' }} }}]""")
    expect(page.get_by_text("Protected file: editor and repository configuration (.vscode). Edits to it always need your approval.",
                            exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Approve")).to_be_visible()
    expect(page.get_by_role("button", name="Always allow…")).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("in this chat$"))).to_have_count(0)


def test_uncertain_commands_say_why_they_ask(ui, page):
    # The server offers nothing to remember on these cards, so neither the form nor the chat button shows.
    show(ui, f"[{GIT_STATUS.replace("editable: ['command']", 'uncertain: true').replace(ALL_SCOPES, 'rememberable: []')}]")
    expect(page.get_by_text("This command could not be checked exactly against your deny and ask rules, so it needs your approval.",
                            exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Always allow…")).to_have_count(0)
    expect(page.get_by_role("button", name="Always allow this command in this chat")).to_have_count(0)


@pytest.mark.parametrize("card", [
    "{ id: 'card', type: 'tool', name: 'run_command', state: 'pending', preview: 'npm test', input: { command: 'npm test' }, rememberable: [] }",
    "{ id: 'card', type: 'tool', name: 'write_file', state: 'pending', preview: 'diff', input: { path: 'a.txt', content: 'x' }, rememberable: [] }",
    "{ id: 'card', type: 'tool', name: 'mcp__docs__search', state: 'pending', preview: 'p', input: {}, rememberable: [] }",
], ids=["command", "edits", "mcp"])
def test_cards_that_offer_nothing_to_remember_have_no_chat_button(ui, page, card):
    # An Always ask rule, or a command that names a credential file: the server refuses to remember them.
    show(ui, f"[{card}]")
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("in this chat$"))).to_have_count(0)
    expect(page.get_by_role("button", name="Always allow…")).to_have_count(0)


# Edit before approving

EDIT_CARD = f"""{{ id: 'card', type: 'tool', name: 'run_command', state: 'pending', preview: 'npm test',
  input: {{ command: 'npm test' }}, editable: ['command'], {ALL_SCOPES},
  rule_options: {{ argv: ['npm', 'test'], prefixes: [], reason: null }} }}"""
EDITED_CARD = EDIT_CARD.replace("preview: 'npm test'", "preview: 'npm run lint', edited: true").replace(
    "input: { command: 'npm test' }", "input: { command: 'npm run lint' }")


def app_with_card(ui, card, extra=""):
    setup = f"""fake.sessions.set('a', {{ ...makeSession('a', 'Other conversation'), status: 'awaiting_approval', events: [{card}] }})
    fake.override = (path, options) => {{
      if (path === '/api/sessions/a/approvals/card/edit') return window.editResponse || fake.json({{ ok: true }})
      if (path === '/api/sessions/a/approvals/card') return fake.json({{ ok: true }})
    }}
    {extra}"""
    ui.app(session="a", scenario="workspace", setup=setup)
    ui.show_session("a")


def test_edit_sends_the_new_command_and_the_card_shows_the_servers_preview(ui, page):
    app_with_card(ui, EDIT_CARD)
    page.get_by_role("button", name="Edit", exact=True).click()
    command = page.get_by_role("textbox", name="Command", exact=True)
    expect(command).to_have_value("npm test")
    for name in ("Approve", "Decline", "Always allow…", "Always allow this command in this chat"):
        expect(page.get_by_role("button", name=name, exact=True)).to_be_disabled()
    command.fill("npm run lint")
    page.get_by_role("button", name="Save edit").click()
    expect(command).to_have_count(0)
    assert ui.bodies("/api/sessions/a/approvals/card/edit", "POST") == [{"input": {"command": "npm run lint"}}]
    ui.emit("a", f"{{ type: 'event', event: {EDITED_CARD} }}")
    expect(page.get_by_label("Command", exact=True)).to_have_text("npm run lint")
    expect(page.get_by_text("Edited by you", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_enabled()


def test_a_rejected_edit_keeps_the_editor_and_its_text(ui, page):
    app_with_card(ui, EDIT_CARD, "window.editResponse = fake.json({ detail: 'The edited action is blocked: git push is denied.' }, 400)")
    page.get_by_role("button", name="Edit", exact=True).click()
    command = page.get_by_role("textbox", name="Command", exact=True)
    command.fill("git push")
    page.get_by_role("button", name="Save edit").click()
    expect(page.get_by_role("alert")).to_contain_text("The edited action is blocked: git push is denied.")
    expect(command).to_have_value("git push")
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_disabled()
    page.get_by_role("button", name="Cancel edit").click()
    expect(command).to_have_count(0)
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_enabled()
    assert ui.requests("/api/sessions/a/approvals/card", "POST") == []


def test_save_edit_waits_for_a_change(ui, page):
    show(ui, f"[{EDIT_CARD}]")
    page.get_by_role("button", name="Edit", exact=True).click()
    command = page.get_by_role("textbox", name="Command", exact=True)
    save = page.get_by_role("button", name="Save edit")
    expect(save).to_be_disabled()
    command.fill("npm test -- --ci")
    expect(save).to_be_enabled()
    command.fill("npm test")
    expect(save).to_be_disabled()
    assert ui.requests("/api/sessions/parent/approvals/card/edit", "POST") == []


@pytest.mark.parametrize("name, field, label", [("write_file", "content", "File content"), ("edit_file", "new_text", "Replacement text")])
def test_file_cards_edit_their_own_field(ui, page, name, field, label):
    show(ui, f"""[{{ id: 'card', type: 'tool', name: '{name}', state: 'pending', preview: 'diff',
      input: {{ path: 'a.txt', {field}: 'old text' }}, editable: ['{field}'] }}]""")
    page.get_by_role("button", name="Edit", exact=True).click()
    box = page.get_by_role("textbox", name=label)
    expect(box).to_have_value("old text")
    box.fill("new text")
    page.get_by_role("button", name="Save edit").click()
    ui.wait("fake.calls.length === 1")
    assert ui.bodies("/api/sessions/parent/approvals/card/edit", "POST") == [{"input": {field: "new text"}}]


def test_cards_without_editable_fields_have_no_edit_button(ui, page):
    show(ui, f"[{EDIT_CARD.replace("editable: ['command']", 'editable: []')}]")
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Edit", exact=True)).to_have_count(0)


def focused(ui):
    return ui.js("[document.activeElement.tagName, document.activeElement.getAttribute('aria-label') || document.activeElement.textContent.trim()]")


def test_keyboard_focus_follows_the_editor(ui, page):
    app_with_card(ui, EDIT_CARD)
    page.get_by_role("button", name="Edit", exact=True).focus()
    page.keyboard.press("Enter")
    command = page.get_by_role("textbox", name="Command", exact=True)
    expect(command).to_be_focused()
    page.get_by_role("button", name="Cancel edit").focus()
    page.keyboard.press("Enter")
    expect(command).to_have_count(0)
    expect(page.get_by_role("button", name="Edit", exact=True)).to_be_focused()
    page.keyboard.press("Enter")
    expect(command).to_be_focused()
    command.fill("npm run lint")
    page.get_by_role("button", name="Save edit").focus()
    page.keyboard.press("Enter")
    expect(command).to_have_count(0)
    expect(page.get_by_role("button", name="Edit", exact=True)).to_be_focused(), focused(ui)


# Modes: Bypass confirmation, changes while busy, Don't ask, Never asks

MENU = re.compile("^Permissions:")


def open_menu(ui, page, session="a", setup=""):
    ui.app(session=session, scenario="workspace", setup=setup)
    if session:
        ui.show_session(session)
    page.get_by_role("button", name=MENU).click()
    return page.get_by_role("menu")


def mode_item(page, label):
    return page.get_by_role("menuitemradio", name=re.compile(f"^{re.escape(label)}"))


BYPASS_TEXT = ("Switch this chat to Bypass permissions?\n\nEvery action will run without asking, including commands and edits "
               "outside the project, and any approval cards waiting now will be approved. Deny rules, ask rules, "
               "credential exclusions, cards for commands that name credential files and approvals a hook asks for "
               "still apply.")


def test_bypass_needs_confirmation_and_sends_confirm(ui, page):
    open_menu(ui, page, setup="fake.confirmAnswer = false")
    mode_item(page, "Bypass permissions").click()
    assert ui.js("fake.confirms") == [BYPASS_TEXT]
    assert ui.requests("/api/sessions/a/permissions", "PUT") == []
    expect(mode_item(page, "Manual")).to_have_attribute("aria-checked", "true")
    ui.run("fake.confirmAnswer = true")
    mode_item(page, "Bypass permissions").click()
    expect(page.get_by_role("button", name="Permissions: Bypass permissions")).to_be_visible()
    assert ui.bodies("/api/sessions/a/permissions", "PUT") == [{"permission_mode": "bypassPermissions", "confirm": True}]
    assert len(ui.js("fake.confirms")) == 2


def test_other_modes_need_no_confirmation(ui, page):
    open_menu(ui, page)
    mode_item(page, "Plan").click()
    expect(page.get_by_role("button", name="Permissions: Plan")).to_be_visible()
    assert ui.bodies("/api/sessions/a/permissions", "PUT") == [{"permission_mode": "plan"}]
    assert ui.js("fake.confirms") == []


def test_a_new_chat_confirms_bypass_without_a_request(ui, page):
    open_menu(ui, page, session=None, setup="fake.confirmAnswer = false")
    mode_item(page, "Bypass permissions").click()
    expect(page.get_by_role("button", name="Permissions: Manual")).to_be_visible()
    ui.run("fake.confirmAnswer = true")
    mode_item(page, "Bypass permissions").click()
    expect(page.get_by_role("button", name="Permissions: Bypass permissions")).to_be_visible()
    assert ui.js("fake.confirms") == [BYPASS_TEXT, BYPASS_TEXT]
    assert not ui.js("fake.calls.some(call => call.path.endsWith('/permissions'))")


@pytest.mark.parametrize("status", ["running", "awaiting_approval"])
def test_modes_can_change_while_the_chat_is_busy(ui, page, status):
    open_menu(ui, page)
    ui.emit("a", {"type": "status", "status": status})
    expect(page.get_by_text("Changes apply to the agent's next action.", exact=True)).to_be_visible()
    for label in ("Auto", "Manual", "Accept edits", "Don't ask", "Plan", "Bypass permissions"):
        expect(mode_item(page, label)).to_have_attribute("aria-disabled", "false")
    mode_item(page, "Accept edits").click()
    expect(page.get_by_role("button", name="Permissions: Accept edits")).to_be_visible()
    assert ui.bodies("/api/sessions/a/permissions", "PUT") == [{"permission_mode": "acceptEdits"}]


def test_modes_stay_locked_while_a_model_change_saves(ui, page):
    setup = "const saving = fake.defer('model'); fake.override = path => path === '/api/sessions/a/model' ? saving.promise : undefined"
    ui.app(session="a", scenario="workspace", setup=setup)
    ui.show_session("a")
    page.get_by_role("combobox", name="Model").select_option("other-model")
    page.get_by_role("button", name=MENU).click()
    expect(mode_item(page, "Plan")).to_have_attribute("aria-disabled", "true")
    mode_item(page, "Plan").click(force=True)
    expect(page.get_by_role("button", name="Permissions: Manual")).to_be_visible()
    assert ui.requests("/api/sessions/a/permissions", "PUT") == []


def test_dont_ask_is_listed_and_selectable(ui, page):
    open_menu(ui, page)
    item = mode_item(page, "Don't ask")
    expect(item).to_contain_text("Run only what rules allow; block everything else without asking")
    item.click()
    expect(page.get_by_role("button", name="Permissions: Don't ask")).to_be_visible()
    assert ui.bodies("/api/sessions/a/permissions", "PUT") == [{"permission_mode": "dontAsk"}]


def test_never_asks_lists_read_only_commands_in_auto_and_plan(ui, page):
    read_only = ("Read-only commands: pwd, ls and read-only git, and commands or pipelines of read-only programs the "
                 "app checks (cat, head, grep, rg, find, sed -n, wc …) on paths in approved folders; anything else still "
                 "asks")
    menu = open_menu(ui, page)
    menu.locator(".never-asks>summary").click()
    for text in ("Reading, listing and searching files in the project and granted folders", "Listing jobs and reading their output",
                 "Tasks and skills", "Questions and plan reviews from the agent"):
        expect(menu.get_by_text(text, exact=True)).to_be_visible()
    expect(menu.get_by_text(read_only)).to_have_count(0)
    for mode, shown in (("Auto", True), ("Accept edits", False), ("Plan", True)):
        mode_item(page, mode).click()
        page.get_by_role("button", name=MENU).click()
        page.locator(".never-asks>summary").click()
        expect(page.get_by_text(read_only, exact=True)).to_have_count(1 if shown else 0)


def test_opening_never_asks_scrolls_its_list_into_the_menu(ui, page):
    menu = open_menu(ui, page)
    menu.locator(".never-asks>summary").click()
    expect(menu.locator(".never-asks li").last).to_have_text("Questions and plan reviews from the agent")
    # Every item sits inside the scrolling menu and above the note that sticks to its bottom. The list
    # scrolls in on the details' toggle event, which runs after the click, so wait for it (3 s at most).
    hidden = """(() => {
      const menu = document.querySelector('.permissions-menu').getBoundingClientRect()
      const note = document.querySelector('.permissions-note').getBoundingClientRect()
      return Array.from(document.querySelectorAll('.never-asks li')).map(li => li.getBoundingClientRect())
        .filter(box => box.top < menu.top || box.bottom > Math.min(menu.bottom, note.top) + 0.5).length
    })()"""
    ui.wait(f"{hidden} === 0")
    assert ui.js(hidden) == 0


# Rules in the Permissions menu

SCOPED = """fake.sessions.set('a', { ...makeSession('a', 'Other conversation'), allow_rules: [
  { tool: 'run_command', command: 'make test' }, { behavior: 'allow', tool: 'run_command', argv: ['echo', 'two words'] }] })
fake.projectRules = [{ behavior: 'deny', tool: 'run_command', prefix: ['git', 'push'] }, { behavior: 'allow', tool: 'edits', path: '/project/src' }]
fake.userRules = [{ behavior: 'ask', tool: 'mcp__docs__*' }, { behavior: 'allow', tool: 'run_command', prefix: ['git', 'status'], flags: ['-s', '--short'] }]
fake.override = (path, options) => {
  if (path === '/api/permission-rules' && options.method === 'DELETE') {
    const { scope, rule } = JSON.parse(options.body)
    const keep = item => JSON.stringify(item) !== JSON.stringify(rule)
    if (scope === 'project') fake.projectRules = fake.projectRules.filter(keep)
    if (scope === 'user') fake.userRules = fake.userRules.filter(keep)
    if (scope === 'chat') fake.sessions.set('a', { ...fake.sessions.get('a'), allow_rules: fake.sessions.get('a').allow_rules.filter(keep) })
    return fake.json(ruleSets('a'))
  }
  if (path === '/api/sessions/a/allow-rules' && options.method === 'DELETE') {
    const removed = options.body
    const updated = { ...fake.sessions.get('a'), allow_rules: fake.sessions.get('a').allow_rules.filter(rule => JSON.stringify(rule) !== removed) }
    fake.sessions.set('a', updated)
    return fake.json(updated)
  }
}"""


def test_the_menu_lists_rules_by_scope_with_badges(ui, page):
    menu = open_menu(ui, page, setup=SCOPED)
    chat = menu.get_by_role("group", name="This chat", exact=True)
    project = menu.get_by_role("group", name="This project", exact=True)
    user = menu.get_by_role("group", name="All projects", exact=True)
    expect(chat.get_by_role("menuitem", name="Remove rule: Command: make test")).to_be_visible()
    expect(chat.get_by_role("menuitem", name="Remove rule: Command: echo 'two words'")).to_be_visible()
    expect(project.get_by_role("menuitem", name="Remove rule: Commands starting with git push (any options)")).to_be_visible()
    expect(project.get_by_role("menuitem", name="Remove rule: File edits under /project/src")).to_be_visible()
    expect(user.get_by_role("menuitem", name="Remove rule: All tools of MCP server docs")).to_be_visible()
    expect(user.get_by_role("menuitem", name="Remove rule: Commands starting with git status with options -s --short")).to_be_visible()
    expect(project.locator(".rule-badge")).to_have_text(["Deny"])
    expect(user.locator(".rule-badge")).to_have_text(["Always ask"])
    expect(chat.locator(".rule-badge")).to_have_count(0)
    assert ui.requests("/api/permission-rules?session_id=a", "GET")
    # Saved rules changed elsewhere arrive with the permissions message.
    ui.emit("a", {"type": "permissions", "permission_mode": "manual", "allowed_directories": [], "allow_rules": [],
                  "disabled_tools": [], "project_rules": [], "user_rules": [{"behavior": "allow", "tool": "delegate_task"}],
                  "rules_problem": None})
    expect(chat).to_have_count(0)
    expect(project).to_have_count(0)
    expect(user.get_by_text("Subagents", exact=True)).to_be_visible()


def test_groups_without_rules_are_hidden(ui, page):
    menu = open_menu(ui, page, setup="fake.userRules = [{ behavior: 'deny', tool: 'stop_job' }]")
    expect(menu.get_by_role("group", name="All projects", exact=True).get_by_text("Stopping jobs", exact=True)).to_be_visible()
    expect(menu.get_by_role("group", name="This chat", exact=True)).to_have_count(0)
    expect(menu.get_by_role("group", name="This project", exact=True)).to_have_count(0)


def test_removing_rules_uses_the_scope_route_or_the_chat_route(ui, page):
    menu = open_menu(ui, page, setup=SCOPED)
    menu.get_by_role("menuitem", name="Remove rule: Commands starting with git push (any options)").click()
    expect(menu.get_by_role("menuitem", name=re.compile("git push"))).to_have_count(0)
    menu.get_by_role("menuitem", name="Remove rule: Command: make test").click()
    expect(menu.get_by_role("menuitem", name="Remove rule: Command: make test")).to_have_count(0)
    menu.get_by_role("menuitem", name="Remove rule: Command: echo 'two words'").click()
    expect(menu.get_by_role("group", name="This chat", exact=True)).to_have_count(0)
    assert ui.bodies("/api/permission-rules", "DELETE") == [
        {"scope": "project", "session_id": "a", "rule": {"behavior": "deny", "tool": "run_command", "prefix": ["git", "push"]}},
        {"scope": "chat", "session_id": "a", "rule": {"behavior": "allow", "tool": "run_command", "argv": ["echo", "two words"]}},
    ]
    assert ui.bodies("/api/sessions/a/allow-rules", "DELETE") == [{"tool": "run_command", "command": "make test"}]


NEW_CHAT_RULES = """fake.projectRules = [{ behavior: 'deny', tool: 'run_command', prefix: ['git', 'push'] }]
fake.override = (path, options) => {
  if (path === '/api/permission-rules' && ['POST', 'DELETE'].includes(options.method)) {
    const { scope, session_id, rule } = JSON.parse(options.body)
    // Like the server: chat and project rules need a conversation.
    if (scope !== 'user' && !session_id) return fake.json({ detail: 'Choose a conversation for a rule in this chat or project.' }, 400)
    const keep = item => JSON.stringify(item) !== JSON.stringify(rule)
    if (options.method === 'DELETE') fake.projectRules = fake.projectRules.filter(keep)
    else fake.projectRules = [...fake.projectRules, { tool: rule.tool, behavior: rule.behavior, argv: rule.text.split(' ') }]
    return fake.json(ruleSets(session_id))
  }
}"""
# Changes only: the reasoning preview (POST /api/settings/reasoning) reads.
POSTS = ("fake.calls.filter(call => call.method !== 'GET' && call.path !== '/api/settings/reasoning')"
         ".map(call => [call.method, call.path])")


def test_a_new_chat_removes_a_project_rule_through_the_chat_it_creates(ui, page):
    menu = open_menu(ui, page, session=None, setup=NEW_CHAT_RULES)
    project = menu.get_by_role("group", name="This project", exact=True)
    project.get_by_role("menuitem", name="Remove rule: Commands starting with git push (any options)").click()
    ui.wait("fake.projectRules.length === 0")
    assert ui.js(POSTS) == [["POST", "/api/sessions"], ["DELETE", "/api/permission-rules"]]
    assert ui.bodies("/api/permission-rules", "DELETE") == [
        {"scope": "project", "session_id": "created", "rule": {"behavior": "deny", "tool": "run_command", "prefix": ["git", "push"]}}]
    expect(page.locator(".app-error")).to_have_count(0)


def test_a_new_chat_adds_a_project_rule_through_the_chat_it_creates(ui, page):
    menu = open_menu(ui, page, session=None, setup=NEW_CHAT_RULES)
    menu.get_by_role("menuitem", name="Add a rule…").click()
    dialog = page.get_by_role("dialog", name="Add a permission rule")
    dialog.get_by_role("textbox", name="Command").fill("npm test")
    dialog.get_by_role("button", name="Add rule").click()
    expect(dialog).to_have_count(0)
    assert ui.js(POSTS) == [["POST", "/api/sessions"], ["POST", "/api/permission-rules"]]
    assert ui.bodies("/api/permission-rules", "POST") == [{"scope": "project", "session_id": "created", "rule": {
        "tool": "run_command", "behavior": "allow", "text": "npm test", "match": "exact"}}]
    expect(page.locator(".app-error")).to_have_count(0)


def test_badges_stay_small_so_long_rules_are_not_cut_off(ui, page):
    page.set_viewport_size({"width": 390, "height": 844})
    prefix = "['docker', 'compose', 'down', '--volumes', '--remove-orphans']"
    menu = open_menu(ui, page, setup=f"fake.projectRules = [{{ behavior: 'deny', tool: 'run_command', prefix: {prefix} }}]")
    row = menu.get_by_role("group", name="This project", exact=True).locator(".allow-rule")
    label, badge = row.locator("span").first, row.locator(".rule-badge")
    expect(badge).to_have_text("Deny")
    assert badge.bounding_box()["width"] < 60, badge.bounding_box()
    assert label.evaluate("node => node.scrollHeight <= node.clientHeight"), label.evaluate("node => [node.scrollHeight, node.clientHeight]")


def test_rule_labels(ui):
    ui.start()
    labels = ui.js("""import('/static/js/components/PermissionsMenu.js').then(({ ruleLabel }) => [
      { tool: 'run_command', command: 'make  test' }, { tool: 'run_command', command: 'make', cwd: '/srv/other' },
      { behavior: 'allow', tool: 'run_command', argv: ['git', 'commit', '-m', "it's"] },
      { behavior: 'allow', tool: 'run_command', prefix: ['git', 'status'], flags: ['-s', '--short'] },
      { behavior: 'allow', tool: 'run_command', prefix: ['git', 'status'], flags: [] },
      { behavior: 'ask', tool: 'run_command', prefix: ['rm', '-rf'] },
      { behavior: 'deny', tool: 'run_command' },
      { tool: 'edits' }, { behavior: 'allow', tool: 'edits', path: '/srv/x' },
      { behavior: 'deny', tool: 'mcp__docs__*' }, { tool: 'mcp__docs__search' },
      { tool: 'hook', hook: 'check' }, { behavior: 'allow', tool: 'delegate_task' }, { behavior: 'allow', tool: 'stop_job' },
      { behavior: 'deny', tool: 'write_file' },
    ].map(ruleLabel))""")
    assert labels == [
        "Command: make  test", "Command: make in /srv/other", "Command: git commit -m 'it'\\''s'",
        "Commands starting with git status with options -s --short", "Commands starting with git status with no other arguments",
        "Commands starting with rm -rf (any options)", "Every command", "File edits", "File edits under /srv/x",
        "All tools of MCP server docs", "Tool: mcp__docs__search", "Hook: check", "Subagents", "Stopping jobs", "Tool: write_file",
    ]


def test_an_unreadable_rules_file_shows_the_problem_and_reset_posts(ui, page):
    setup = """fake.rulesProblem = 'Saved permission rules could not be read: invalid JSON.'
    fake.override = (path, options) => {
      if (path === '/api/permission-rules/reset?session_id=a' && options.method === 'POST') { fake.rulesProblem = null; return fake.json(ruleSets('a')) }
    }"""
    menu = open_menu(ui, page, setup=setup)
    alert = menu.get_by_role("alert")
    expect(alert).to_have_text("Saved permission rules could not be read: invalid JSON. They are ignored until you reset them.")
    menu.get_by_role("menuitem", name="Reset saved rules").focus()
    page.keyboard.press("Enter")
    expect(menu.get_by_role("alert")).to_have_count(0)
    assert len(ui.requests("/api/permission-rules/reset?session_id=a", "POST")) == 1
    # The focused button is gone: focus moves to the selected mode, so the keyboard still works the menu.
    expect(mode_item(page, "Manual")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.get_by_role("menu")).to_have_count(0)


# The add-rule dialog

ADD_RULE = """window.addAnswers = []
fake.override = (path, options) => {
  if (path === '/api/permission-rules' && options.method === 'POST') {
    const answer = addAnswers.shift()
    if (answer) return fake.json({ detail: answer }, 400)
    const { scope, rule } = JSON.parse(options.body)
    if (scope === 'project' && rule.text) fake.projectRules = [...fake.projectRules, { tool: rule.tool, behavior: rule.behavior, prefix: rule.text.split(' ') }]
    return fake.json(ruleSets('a'))
  }
}"""


def open_dialog(ui, page, setup=ADD_RULE):
    menu = open_menu(ui, page, setup=setup)
    menu.get_by_role("menuitem", name="Add a rule…").click()
    dialog = page.get_by_role("dialog", name="Add a permission rule")
    expect(dialog).to_be_visible()
    return dialog


def test_the_dialog_posts_a_deny_prefix_draft_and_shows_errors_inline(ui, page):
    dialog = open_dialog(ui, page, ADD_RULE + "\naddAnswers.push('A deny or ask rule can have at most 16 words.')")
    dialog.get_by_role("combobox", name="Behavior").select_option(label="Deny")
    expect(dialog.get_by_role("radio", name="This project")).to_be_checked()
    expect(dialog.get_by_role("combobox", name="Applies to")).to_have_value("command")
    dialog.get_by_role("radio", name="Commands starting with").check()
    expect(dialog.get_by_role("textbox", name="Allowed options after it")).to_have_count(0)
    dialog.get_by_role("textbox", name="Command").fill("git push")
    dialog.get_by_role("button", name="Add rule").click()
    expect(dialog.get_by_role("alert")).to_have_text("A deny or ask rule can have at most 16 words.")
    expect(dialog).to_be_visible()
    expect(page.locator(".app-error")).to_have_count(0)
    dialog.get_by_role("button", name="Add rule").click()
    expect(dialog).to_have_count(0)
    draft = {"tool": "run_command", "behavior": "deny", "text": "git push", "match": "prefix"}
    assert ui.bodies("/api/permission-rules", "POST") == [{"scope": "project", "session_id": "a", "rule": draft}] * 2
    page.get_by_role("button", name=MENU).click()
    expect(page.get_by_role("group", name="This project", exact=True)).to_contain_text("Deny")


def test_an_allow_prefix_rule_sends_its_options(ui, page):
    dialog = open_dialog(ui, page)
    dialog.get_by_role("radio", name="All projects").check()
    dialog.get_by_role("radio", name="Commands starting with").check()
    dialog.get_by_role("textbox", name="Command").fill(" git log ")
    dialog.get_by_role("textbox", name="Allowed options after it").fill("--oneline -n")
    expect(dialog.get_by_text("Separate options with spaces. Use --name= to allow any value. Nothing else may follow.")).to_be_visible()
    dialog.get_by_role("button", name="Add rule").click()
    expect(dialog).to_have_count(0)
    assert ui.bodies("/api/permission-rules", "POST") == [{"scope": "user", "session_id": "a", "rule": {
        "tool": "run_command", "behavior": "allow", "text": "git log", "match": "prefix", "options": "--oneline -n"}}]


def test_edit_and_tool_rules(ui, page):
    dialog = open_dialog(ui, page)
    dialog.get_by_role("radio", name="This chat").check()
    dialog.get_by_role("combobox", name="Applies to").select_option(label="File edits")
    dialog.get_by_role("textbox", name="Only under this folder").fill("/srv/x/src")
    dialog.get_by_role("button", name="Add rule").click()
    expect(dialog).to_have_count(0)
    page.get_by_role("button", name=MENU).click()
    page.get_by_role("menuitem", name="Add a rule…").click()
    dialog.get_by_role("combobox", name="Behavior").select_option(label="Always ask")
    dialog.get_by_role("combobox", name="Applies to").select_option(label="A tool")
    dialog.get_by_role("textbox", name="Tool name").fill("mcp__docs__*")
    dialog.get_by_role("button", name="Add rule").click()
    expect(dialog).to_have_count(0)
    assert ui.bodies("/api/permission-rules", "POST") == [
        {"scope": "chat", "session_id": "a", "rule": {"tool": "edits", "behavior": "allow", "path": "/srv/x/src"}},
        {"scope": "project", "session_id": "a", "rule": {"tool": "mcp__docs__*", "behavior": "ask"}},
    ]


def test_web_fetch_rules_name_a_domain(ui, page):
    dialog = open_dialog(ui, page)
    dialog.get_by_role("combobox", name="Applies to").select_option(label="Web fetches")
    domain = dialog.get_by_role("textbox", name="Domain")
    expect(dialog.get_by_text("Allow names one host exactly; other hosts, also its subdomains, still ask.")).to_be_visible()
    expect(dialog.get_by_role("button", name="Add rule")).to_be_disabled()
    domain.fill(" docs.python.org ")
    dialog.get_by_role("button", name="Add rule").click()
    expect(dialog).to_have_count(0)
    page.get_by_role("button", name=MENU).click()
    page.get_by_role("menuitem", name="Add a rule…").click()
    dialog.get_by_role("combobox", name="Behavior").select_option(label="Deny")
    dialog.get_by_role("combobox", name="Applies to").select_option(label="Web fetches")
    expect(dialog.get_by_text("Deny and Always ask cover the domain and its subdomains; leave it empty for every web fetch.")).to_be_visible()
    dialog.get_by_role("button", name="Add rule").click()
    expect(dialog).to_have_count(0)
    assert ui.bodies("/api/permission-rules", "POST") == [
        {"scope": "project", "session_id": "a", "rule": {"tool": "web_fetch", "behavior": "allow", "domain": "docs.python.org"}},
        {"scope": "project", "session_id": "a", "rule": {"tool": "web_fetch", "behavior": "deny"}},
    ]


def test_web_rules_have_plain_labels_in_the_menu(ui, page):
    menu = open_menu(ui, page, setup="""fake.userRules = [{ behavior: 'allow', tool: 'web_fetch', domain: 'docs.python.org' },
      { behavior: 'deny', tool: 'web_fetch', domain: 'example.com' }, { behavior: 'allow', tool: 'web_search' }]""")
    user = menu.get_by_role("group", name="All projects", exact=True)
    expect(user.get_by_role("menuitem", name="Remove rule: Fetches from docs.python.org", exact=True)).to_be_visible()
    expect(user.get_by_role("menuitem", name="Remove rule: Fetches from example.com and its subdomains")).to_be_visible()
    expect(user.get_by_role("menuitem", name="Remove rule: Web searches")).to_be_visible()


def test_cancel_and_escape_close_the_dialog(ui, page):
    dialog = open_dialog(ui, page)
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_have_count(0)
    page.get_by_role("button", name=MENU).click()
    page.get_by_role("menuitem", name="Add a rule…").click()
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    assert ui.requests("/api/permission-rules", "POST") == []


def test_closing_the_dialog_returns_focus_to_the_message_box(ui, page):
    dialog = open_dialog(ui, page)
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    expect(page.get_by_role("textbox", name="Message")).to_be_focused()
    page.get_by_role("button", name=MENU).click()
    page.get_by_role("menuitem", name="Add a rule…").click()
    dialog.get_by_role("textbox", name="Command").fill("make test")
    dialog.get_by_role("button", name="Add rule").click()
    expect(dialog).to_have_count(0)
    expect(page.get_by_role("textbox", name="Message")).to_be_focused()


def test_the_dialog_fits_a_phone(ui, page):
    page.set_viewport_size({"width": 390, "height": 844})
    dialog = open_dialog(ui, page)
    dialog.get_by_role("radio", name="Commands starting with").check()
    box = dialog.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390, box
    assert dialog.evaluate("d => d.scrollWidth <= d.clientWidth")
    for name in ("Command", "Allowed options after it"):
        field = dialog.get_by_role("textbox", name=name).bounding_box()
        assert field["x"] + field["width"] <= box["x"] + box["width"], (name, field)


# Turned-off tools (Agent tools -> Tools)

TOOLS = """fake.override = (path, options) => {
  if (path === '/api/tools?session_id=a') return fake.json({ tools: [
    { name: 'read_file', description: 'Read a text file.' }, { name: 'run_command', description: 'Run a command.' }],
    disabled: ['run_command', 'mcp__docs__search'] })
  if (path === '/api/sessions/a/disabled-tools' && options.method === 'PUT') return fake.json({ ...session(), disabled_tools: JSON.parse(options.body).tools })
}"""


def open_tools(ui, page, selected="session()"):
    ui.mount("components/AgentToolsDialog.js", f"dialogProps({selected})", scenario="agent_tools", setup=TOOLS)
    page.get_by_role("button", name="Tools", exact=True).click()
    return page.get_by_role("region", name="Tools")


def test_the_tools_tab_loads_toggles_and_saves(ui, page):
    tools = open_tools(ui, page)
    tabs = page.get_by_role("navigation", name="Agent tools sections").get_by_role("button").all_inner_texts()
    assert tabs == ["Overview", "Tasks", "Subagents", "Tools", "Usage", "Export", "Checkpoints", "Verify", "Memory", "Worktrees",
                    "Extensions"]
    expect(tools).to_contain_text("Turned-off tools are not offered to the agent in this chat, and calls to them are refused. "
                                  "Changes apply from the agent's next step. Subagents, forks and imports keep this setting.")
    # Each checkbox is named by its tool alone; the description describes it.
    read = tools.get_by_role("checkbox", name="read_file", exact=True)
    run = tools.get_by_role("checkbox", name="run_command", exact=True)
    expect(read).to_have_accessible_description("Read a text file.")
    expect(read).to_be_checked()
    expect(run).not_to_be_checked()
    expect(tools.get_by_text("Read a text file.")).to_be_visible()
    chips = tools.get_by_role("list", name="Turned-off MCP tools")
    expect(chips.get_by_role("listitem")).to_have_text(["mcp__docs__search"])
    read.uncheck()
    run.check()
    name = tools.get_by_role("textbox", name="Turn off an MCP tool by name")
    name.fill("mcp__git")
    tools.get_by_role("button", name="Add", exact=True).click()
    expect(tools.get_by_role("alert")).to_have_text("Enter an MCP tool name such as mcp__docs__search.")
    expect(chips.get_by_role("listitem")).to_have_count(1)
    name.fill("mcp__git__push")
    name.press("Enter")
    expect(tools.get_by_role("alert")).to_have_count(0)
    tools.get_by_role("button", name="Turn on mcp__docs__search").click()
    expect(chips.get_by_role("listitem")).to_have_text(["mcp__git__push"])
    tools.get_by_role("button", name="Save tools").click()
    expect(tools.get_by_role("status")).to_have_text("Tools saved for this chat.")
    assert ui.bodies("/api/sessions/a/disabled-tools", "PUT") == [{"tools": ["read_file", "mcp__git__push"]}]


def test_the_app_saves_turned_off_tools_for_the_open_chat(ui, page):
    setup = """window.session = () => fake.sessions.get('a')
    """ + TOOLS.replace("fake.override = (path, options) => {", """fake.override = (path, options) => {
  if (path === '/api/sessions/a/disabled-tools' && options.method === 'PUT') {
    fake.sessions.set('a', { ...fake.sessions.get('a'), disabled_tools: JSON.parse(options.body).tools })
  }""")
    ui.app(session="a", scenario="workspace", setup=setup)
    ui.show_session("a")
    page.get_by_role("button", name="Agent tools", exact=True).click()
    page.get_by_role("button", name="Tools", exact=True).click()
    tools = page.get_by_role("region", name="Tools")
    tools.get_by_role("checkbox", name="read_file", exact=True).uncheck()
    tools.get_by_role("button", name="Save tools").click()
    expect(tools.get_by_role("status")).to_have_text("Tools saved for this chat.")
    assert ui.bodies("/api/sessions/a/disabled-tools", "PUT") == [{"tools": ["run_command", "mcp__docs__search", "read_file"]}]
    expect(tools.get_by_role("list", name="Turned-off MCP tools").get_by_role("listitem")).to_have_text(["mcp__docs__search"])


def test_the_tools_tab_needs_a_conversation(ui, page):
    tools = open_tools(ui, page, "null")
    expect(tools).to_have_text("Start a conversation to choose its tools.")
    assert not ui.js("fake.calls.some(call => call.path.startsWith('/api/tools'))")


# Outcome lines

def test_outcome_lines_name_the_rule_mode_or_block(ui, page):
    def done(event_id, extra):
        return f"{{ id: '{event_id}', type: 'tool', name: 'run_command', state: 'completed', input: {{ command: 'ls' }}, output: '{{}}'{extra} }}"
    show(ui, "[" + ", ".join([
        done("chat", ", allowed_by_rule: true, rule_scope: 'chat'"),
        done("project", ", allowed_by_rule: true, rule_scope: 'project', without_shell: true"),
        done("user", ", allowed_by_rule: true, rule_scope: 'user'"),
        done("readonly", ", read_only_command: true, without_shell: true"),
        done("mode", ", approval: 'approved', approved_by_mode: 'acceptEdits'"),
        done("edited", ", approval: 'approved', edited: true"),
        """{ id: 'blocked', type: 'tool', name: 'read_file', state: 'rejected', approval: 'blocked', input: { path: '/srv/x' },
          blocked_reason: "Not run: this path is outside the project and folder access needs your approval, which Don't ask mode does not show.",
          output: 'ignored' }""",
        """{ id: 'denied', type: 'tool', name: 'run_command', state: 'rejected', approval: 'blocked', input: { command: 'git push' },
          output: 'Denied by a rule for this project: Commands starting with git push.' }""",
        # Don't ask refuses before any card, so there is no approval field.
        """{ id: 'dontask', type: 'tool', name: 'delegate_task', state: 'rejected', input: { task: 'x' },
          output: "Not run: Don't ask mode blocks actions that no rule allows." }""",
        """{ id: 'declined', type: 'tool', name: 'run_command', state: 'rejected', approval: 'declined', edited: true, input: { command: 'ls' } }""",
        """{ id: 'cancelled', type: 'tool', name: 'run_command', state: 'cancelled', edited: true, input: { command: 'ls' } }""",
    ]) + "]")
    for text in ("Allowed by a rule for this chat", "Allowed by a rule for this project · ran without a shell",
                 "Allowed by a rule for all projects", "Ran as a read-only command · ran without a shell",
                 "Approved when you switched to Accept edits", "Edited by you before approval",
                 "Not run: this path is outside the project and folder access needs your approval, which Don't ask mode does not show.",
                 "Not run: Denied by a rule for this project: Commands starting with git push.",
                 "Not run: Don't ask mode blocks actions that no rule allows."):
        expect(page.get_by_text(text, exact=True)).to_have_count(1)
    # Cards edited and then declined or cancelled were never approved.
    expect(page.get_by_text("Edited by you", exact=True)).to_have_count(2)
    expect(page.get_by_text(re.compile("Not run: Not run"))).to_have_count(0)


# Phones

def test_a_busy_card_with_edit_and_always_allow_open_fits_a_phone(ui, page):
    page.set_viewport_size({"width": 390, "height": 844})
    long_argv = "['npm', 'run', 'lint:all-packages-with-a-very-long-script-name', '--', '--max-warnings=0', '--format=stylish']"
    first = EDIT_CARD.replace("argv: ['npm', 'test'], prefixes: []", f"argv: {long_argv}, prefixes: [{{ prefix: ['npm', 'run'], flags: ['--max-warnings=0', '--format=stylish', '--cache-location=/srv/x/a/very/long/folder/name'] }}]")
    second = first.replace("id: 'card'", "id: 'other'")
    app_with_card(ui, f"{first}, {second}")
    page.get_by_role("button", name="Always allow…").first.click()
    page.get_by_role("radio", name="Commands starting with").check()
    page.get_by_role("button", name="Edit", exact=True).last.click()
    expect(page.get_by_role("textbox", name="Command", exact=True)).to_be_visible()
    expect(page.get_by_role("textbox", name="Allowed options after it")).to_be_visible()
    assert ui.js("document.scrollingElement.scrollWidth <= innerWidth"), ui.js("[document.scrollingElement.scrollWidth, innerWidth]")
    for button in page.locator(".needs-approval button").all():
        box = button.bounding_box()
        if box:
            assert box["x"] + box["width"] <= 390, box


@pytest.mark.parametrize("width,height", [(1440, 900), (360, 740)])
def test_menu_shows_every_mode(ui, page, width, height):
    page.set_viewport_size({"width": width, "height": height})
    menu = open_menu(ui, page)
    expect(menu).to_be_visible()
    expect(mode_item(page, "Bypass permissions")).to_be_in_viewport(ratio=1)
    expect(page.get_by_role("menuitem", name="Add a rule…")).to_be_in_viewport(ratio=1)
    assert menu.bounding_box()["y"] >= ui.js("document.querySelector('.topbar').getBoundingClientRect().bottom")


@pytest.mark.parametrize("width,height,lines", [(1366, 625, 1), (844, 390, 1), (1024, 768, 8), (1280, 720, 12), (360, 740, 6)],
                         ids=["1366x625", "844x390", "1024x768-tall-draft", "1280x720-tall-draft", "360x740-tall-draft"])
def test_menu_stays_below_the_top_bar(ui, page, width, height, lines):
    """On a short window, or above a tall draft, the menu is shorter and scrolls; its heading stays on screen."""
    page.set_viewport_size({"width": width, "height": height})
    ui.app(session="a", scenario="workspace")
    ui.show_session("a")
    if lines > 1:
        page.get_by_role("textbox", name="Message").fill("\n".join(f"Line {n}" for n in range(lines)))
    page.get_by_role("button", name=MENU).click()
    menu = page.get_by_role("menu")
    expect(menu).to_be_visible()
    assert menu.bounding_box()["y"] >= ui.js("document.querySelector('.topbar').getBoundingClientRect().bottom")
    expect(page.get_by_role("button", name="Close permissions")).to_be_in_viewport(ratio=1)
    expect(mode_item(page, "Manual")).to_be_focused()
    bypass = mode_item(page, "Bypass permissions")
    bypass.evaluate("e => e.scrollIntoView({ block: 'center' })")  # The menu scrolls when it is shorter than its items.
    expect(bypass).to_be_in_viewport(ratio=1)
