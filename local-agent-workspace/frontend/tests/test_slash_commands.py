"""Built-in / commands in the message box (M5a), custom commands in the / menu, plain-text sends and
Settings → slash commands off (M5b). Commands run in the page: never queued, never sent to /messages."""
import json

from playwright.sync_api import expect

REPLY = "The parser is fixed: **two** files changed."
ROUTES = """fake.override = (path, options) => {
  if (path === '/api/sessions/a/compact') return fake.json({ ok: true }, 202)
  if (path === '/api/sessions/a/fork') { fake.sessions.set('forked', makeSession('forked', 'Fork of Work')); return fake.json(fake.sessions.get('forked')) }
  if (path === '/api/sessions/a/transcript.md') return new Response('# Work\\n\\nThe transcript.', { headers: {
    'Content-Type': 'text/markdown', 'Content-Disposition': 'attachment; filename="work.md"' } })
  if (path === '/api/sessions/a/metrics') return new Promise(() => {})
  if (path === '/api/sessions/a/stop') return fake.json({ ok: true })
}"""


def start_app(ui, page, status="idle", setup="", **fields):
    session = {"status": status, "permission_mode": "acceptEdits", "reasoning_effort": "high", "model": "other-model",
               "events": [{"id": "u", "type": "user", "text": "Fix the parser"},
                          {"id": "r", "type": "assistant", "text": REPLY}], **fields}
    chats = f"fake.sessions.set('a', {{ ...makeSession('a', 'Work'), context_info: contextInfo, ...{json.dumps(session)} }}); "
    ui.app(session="a", scenario="workspace", setup=chats + ROUTES + ";" + setup)
    ui.show_session("a")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("other-model")
    expect(page.locator(".user-message")).not_to_have_count(0)


def message(page):
    return page.get_by_role("textbox", name="Message")


def run(page, text):
    """Type a command after closing the / menu, as when it is typed with its arguments, and press Enter."""
    box = message(page)
    box.fill(text)
    box.press("Enter")


def no_messages(ui):
    assert not ui.requests("/api/sessions/a/messages", "POST")


def error(page):
    return page.locator(".app-error")


def test_rename_compact_and_model_call_their_routes(ui, page):
    start_app(ui, page)
    run(page, "/rename   The   parser fix ")
    ui.wait("fake.requests('/api/sessions/a/title', 'PUT').length === 1")
    assert ui.bodies("/api/sessions/a/title", "PUT") == [{"title": "The parser fix"}]
    expect(page.locator(".conversation-title")).to_contain_text("The parser fix")
    expect(message(page)).to_have_value("")
    run(page, "/compact keep the parser decisions")
    ui.wait("fake.requests('/api/sessions/a/compact', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/compact", "POST") == [{"preservation_note": "keep the parser decisions"}]
    run(page, "/model Latest Model")
    ui.wait("fake.requests('/api/sessions/a/model', 'PUT').length === 1")
    assert ui.bodies("/api/sessions/a/model", "PUT") == [{"model": "latest-model"}]
    expect(page.get_by_role("combobox", name="Model")).to_have_value("latest-model")
    # An unknown model keeps the command in the box and says which ones exist.
    run(page, "/model gpt-9")
    expect(error(page)).to_contain_text("No model named “gpt-9”. Choose one of: Latest model, Test model, Other model.")
    expect(message(page)).to_have_value("/model gpt-9")
    assert len(ui.requests("/api/sessions/a/model", "PUT")) == 1
    # /model alone opens the list: focus moves to it.
    run(page, "/model")
    expect(page.get_by_role("combobox", name="Model")).to_be_focused()
    no_messages(ui)


def test_compact_is_refused_when_there_is_nothing_to_compact(ui, page):
    start_app(ui, page, setup="fake.sessions.get('a').context_info = { ...contextInfo, compactable: false }")
    run(page, "/compact")
    expect(error(page)).to_contain_text("Nothing to compact yet: earlier turns are already summarized")
    expect(message(page)).to_have_value("/compact")
    assert not ui.requests("/api/sessions/a/compact", "POST")
    no_messages(ui)


def test_mode_switches_and_bypass_asks_first(ui, page):
    start_app(ui, page)
    run(page, "/mode plan")
    ui.wait("fake.requests('/api/sessions/a/permissions', 'PUT').length === 1")
    expect(page.get_by_role("button", name="Permissions: Plan")).to_be_visible()
    ui.run("fake.confirmAnswer = false")
    run(page, "/mode bypass")
    ui.wait("fake.confirms.length === 1")
    assert ui.js("fake.confirms[0]").startswith("Switch this chat to Bypass permissions?")
    assert len(ui.requests("/api/sessions/a/permissions", "PUT")) == 1
    ui.run("fake.confirmAnswer = true")
    run(page, "/mode bypass")
    ui.wait("fake.requests('/api/sessions/a/permissions', 'PUT').length === 2")
    assert ui.bodies("/api/sessions/a/permissions", "PUT") == [
        {"permission_mode": "plan"}, {"permission_mode": "bypassPermissions", "confirm": True}]
    run(page, "/mode accept-edits")
    ui.wait("fake.requests('/api/sessions/a/permissions', 'PUT').length === 3")
    assert ui.bodies("/api/sessions/a/permissions", "PUT")[-1] == {"permission_mode": "acceptEdits"}
    run(page, "/mode careful")
    expect(error(page)).to_contain_text("No permission mode named “careful”.")
    # /mode alone opens the permission menu.
    run(page, "/mode")
    expect(page.get_by_role("menu", name="Permission mode")).to_be_visible()
    no_messages(ui)


def test_context_usage_export_and_help_open_their_views(ui, page):
    start_app(ui, page)
    run(page, "/context")
    meter = page.locator("details.context-meter")
    expect(meter).to_have_attribute("open", "")
    expect(meter.locator("summary")).to_be_focused()
    meter.locator("summary").press("Escape")
    run(page, "/usage")
    dialog = page.get_by_role("dialog", name="Agent tools")
    expect(dialog.get_by_role("button", name="Usage", exact=True)).to_have_attribute("aria-pressed", "true")
    dialog.get_by_role("button", name="Close agent tools").click()
    run(page, "/export")
    expect(dialog.get_by_role("button", name="Export", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(dialog.get_by_role("button", name="Export Markdown")).to_be_visible()
    dialog.get_by_role("button", name="Close agent tools").click()
    run(page, "/help")
    help = page.get_by_role("region", name="Commands")
    expect(help).to_contain_text("/rename <title>")
    expect(help).to_contain_text("/btw <question>")
    expect(help.get_by_role("button", name="Close commands")).to_be_focused()
    help.get_by_role("button", name="Close commands").click()
    expect(help).to_have_count(0)
    expect(message(page)).to_be_focused()
    no_messages(ui)


def test_copy_copies_the_last_reply_or_the_transcript(ui, page):
    start_app(ui, page)
    run(page, "/copy")
    ui.wait("fake.clipboard.length === 1")
    assert ui.js("fake.clipboard") == [REPLY]
    expect(page.locator(".command-notice")).to_have_text("Last reply copied.")
    run(page, "/copy all")
    ui.wait("fake.clipboard.length === 2")
    assert ui.js("fake.clipboard[1]") == "# Work\n\nThe transcript."
    expect(page.locator(".command-notice")).to_have_text("Conversation copied as Markdown.")
    run(page, "/copy everything")
    expect(error(page)).to_contain_text("Use /copy for the last reply or /copy all for the conversation.")
    no_messages(ui)


def test_fork_new_and_clear(ui, page):
    start_app(ui, page)
    run(page, "/fork")
    ui.wait("fake.requests('/api/sessions/a/fork', 'POST').length === 1")
    ui.wait("new URL(location.href).searchParams.get('session') === 'forked'")
    page.get_by_role("button", name="Work").first.click()
    ui.show_session("a", 2)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("other-model")
    expect(message(page)).to_be_enabled()
    run(page, "/new")
    ui.wait("new URL(location.href).searchParams.get('session') === null")
    expect(page.locator(".conversation-title")).to_contain_text("New conversation")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    expect(page.get_by_role("button", name="Permissions: Manual")).to_be_visible()


def test_clear_starts_a_new_chat_with_the_same_model_mode_and_effort(ui, page):
    start_app(ui, page)
    run(page, "/clear")
    ui.wait("new URL(location.href).searchParams.get('session') === null")
    expect(page.locator(".conversation-title")).to_contain_text("New conversation")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("other-model")
    expect(page.get_by_role("button", name="Permissions: Accept edits")).to_be_visible()
    # The old chat is kept, and changing the model here changes only the new chat, not Settings.
    assert ui.js("fake.sessions.has('a')")
    page.get_by_role("combobox", name="Model").select_option("latest-model")
    assert not ui.requests("/api/settings", "PUT")
    message(page).fill("Start again")
    message(page).press("Enter")
    ui.wait("fake.requests('/api/sessions/created/messages', 'POST').length === 1")
    assert ui.bodies("/api/sessions", "POST") == [{"permission_mode": "acceptEdits", "reasoning_effort": "high",
                                                   "model": "latest-model"}]


def test_clear_from_bypass_asks_before_the_new_chat_bypasses(ui, page):
    start_app(ui, page, permission_mode="bypassPermissions", setup="fake.confirmAnswer = false")
    run(page, "/clear")
    ui.wait("fake.confirms.length === 1")
    expect(page.get_by_role("button", name="Permissions: Manual")).to_be_visible()


def test_paths_and_unknown_words_are_messages_and_commands_are_never_queued(ui, page):
    start_app(ui, page, status="running")
    run(page, "/compact")
    expect(error(page)).to_contain_text("Stop the response first: /compact runs when the conversation is idle.")
    expect(message(page)).to_have_value("/compact")
    # Allowed while busy: it runs at once.
    run(page, "/rename Busy work")
    ui.wait("fake.requests('/api/sessions/a/title', 'PUT').length === 1")
    no_messages(ui)
    for text in ("/tmp/output.log is empty", "/unknown do it", "/help me with the parser"):
        run(page, text)
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 3")
    assert [body["text"] for body in ui.bodies("/api/sessions/a/messages", "POST")] == [
        "/tmp/output.log is empty", "/unknown do it", "/help me with the parser"]


def test_commands_in_a_new_conversation(ui, page):
    ui.app(scenario="workspace")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    run(page, "/rename A title")
    expect(error(page)).to_contain_text("Start a conversation first")
    assert not ui.requests("/api/sessions", "POST")


def test_the_menu_runs_commands_inserts_arguments_and_lists_custom_commands(ui, page):
    start_app(ui, page, setup="""fake.commands = [{ name: 'deploy', description: 'Deploy the app', argument_hint: '<environment>',
        path: '.agents/commands/deploy.md', source: 'project', ignored: [], notes: [] }]
      fake.handle = (base => (path, options) => path === '/api/skills?session_id=a'
        ? fake.json([...skills, { id: 'compact', name: 'compact', description: 'Project compaction notes', path: '.agents/skills/compact/SKILL.md', reserved: true }])
        : base(path, options))(fake.handle)""")
    box = message(page)
    box.press_sequentially("/dep")
    menu = page.get_by_role("listbox", name="Commands and skills")
    custom = menu.get_by_role("group", name="Custom commands").get_by_role("option")
    expect(custom).to_have_count(1)
    expect(custom).to_contain_text("/deploy <environment>")
    expect(custom).to_contain_text("Deploy the app")
    box.press("Enter")
    expect(box).to_have_value("/deploy ")
    # A skill whose ID a command owns is inserted as /skill <id>.
    box.fill("")
    box.press_sequentially("/comp")
    skill = menu.get_by_role("group", name="Skills").get_by_role("option")
    expect(skill).to_contain_text("/skill compact")
    skill.click()
    expect(box).to_have_value("/skill compact ")
    # A command that takes nothing runs when chosen.
    box.fill("")
    box.press_sequentially("/hel")
    box.press("Enter")
    expect(page.get_by_role("region", name="Commands")).to_be_visible()
    expect(box).to_have_value("")
    # Typed in full with Enter, a command with optional arguments runs too; Tab only inserts.
    box.press_sequentially("/copy")
    box.press("Tab")
    expect(box).to_have_value("/copy ")
    box.fill("")
    box.press_sequentially("/copy")
    box.press("Enter")
    ui.wait("fake.clipboard.length === 1")
    no_messages(ui)


def test_plain_text_sends_exactly_what_was_typed(ui, page):
    start_app(ui, page)
    toggle = page.get_by_role("button", name="Send as plain text")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true")
    box = message(page)
    expect(box).to_be_focused()
    box.press_sequentially("/clear @note.txt")
    expect(page.locator(".slash-menu")).to_have_count(0)
    expect(page.locator(".mention-chip")).to_have_count(0)
    expect(page.get_by_role("status").filter(has_text="Plain text: sent exactly as typed")).to_be_visible()
    box.press("Enter")
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/messages", "POST") == [{"text": "/clear @note.txt", "verbatim": True}]
    expect(toggle).to_have_attribute("aria-pressed", "false")
    # Shift+Alt+Enter sends one message as plain text, also a ! line or a /btw question.
    for text in ("/help", "!ls", "/btw what changed?"):
        box.fill(text)
        box.press("Shift+Alt+Enter")
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 4")
    assert ui.bodies("/api/sessions/a/messages", "POST")[1:] == [
        {"text": "/help", "verbatim": True}, {"text": "!ls", "verbatim": True}, {"text": "/btw what changed?", "verbatim": True}]
    assert not ui.requests("/api/sessions/a/shell", "POST") and not ui.requests("/api/sessions/a/side-question", "POST")


def test_slash_commands_off_sends_typed_commands_and_runs_chosen_ones(ui, page):
    start_app(ui, page, setup="fake.settings = { ...fake.settings, slash_commands: false }")
    box = message(page)
    run(page, "/rename Typed title")
    ui.wait("fake.requests('/api/sessions/a/messages', 'POST').length === 1")
    assert ui.bodies("/api/sessions/a/messages", "POST") == [{"text": "/rename Typed title"}]
    assert not ui.requests("/api/sessions/a/title", "PUT")
    box.press_sequentially("/ren")
    menu = page.get_by_role("listbox", name="Commands and skills")
    expect(menu.get_by_role("group", name="Skills")).to_have_count(0)
    expect(page.locator(".slash-menu")).to_contain_text("Slash commands are off in Settings")
    box.press("Enter")
    expect(box).to_have_value("/rename ")
    box.press_sequentially("Chosen title")
    box.press("Enter")
    ui.wait("fake.requests('/api/sessions/a/title', 'PUT').length === 1")
    assert ui.bodies("/api/sessions/a/title", "PUT") == [{"title": "Chosen title"}]
    assert len(ui.requests("/api/sessions/a/messages", "POST")) == 1


def test_messages_show_the_commands_skills_and_plain_text_they_carried(ui, page):
    events = [{"id": "u1", "type": "user", "text": "/review src/a.py", "commands": [{"name": "review", "path": ".agents/commands/review.md"}]},
              {"id": "u2", "type": "user", "text": "Use /handoff", "skills": ["handoff"]},
              {"id": "u3", "type": "user", "text": "/clear", "verbatim": True}]
    start_app(ui, page, events=events)
    expect(page.locator(".user-message")).to_have_count(3)
    chips = page.get_by_role("list", name="Sent with this message")
    expect(chips.nth(0)).to_have_text("Command /review · .agents/commands/review.md")
    expect(chips.nth(1)).to_have_text("Skill /handoff")
    expect(chips.nth(2)).to_have_text("Sent as plain text")
