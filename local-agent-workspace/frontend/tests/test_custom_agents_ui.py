"""Custom agents and per-agent model and effort in the UI: Agent tools → Subagents lists the agent
types (built-in defaults, custom agent files and files that cannot be used), subagent cards and rows
name the agent with its effort and model, and Settings → Subagents saves the built-in defaults."""
import re

from playwright.sync_api import expect


def open_subagents(ui, page, setup="", selected="session()"):
    ui.mount("components/AgentToolsDialog.js", f"{{ ...dialogProps({selected}) }}", scenario="agent_tools", setup=setup)
    expect(page.get_by_role("region", name="Overview")).to_be_visible()
    page.get_by_role("button", name="Subagents", exact=True).click()
    return page.get_by_role("region", name="Subagents")


def test_the_subagents_section_lists_the_agent_types_and_unusable_files(ui, page):
    section = open_subagents(ui, page)
    types = section.get_by_role("list", name="Agent types")
    rows = types.get_by_role("listitem")
    expect(rows).to_have_count(5)
    expect(rows.nth(0)).to_contain_text("ExploreBuilt-in")
    expect(rows.nth(0)).to_contain_text("Read-only files · Fast effort · Same model as the chat")
    expect(rows.nth(1)).to_contain_text("Inherit permitted tools · Same effort as the chat · Same model as the chat")
    expect(rows.nth(2)).to_contain_text("Read and edit files · High effort · GPT OSS 120B")
    custom = rows.nth(3)
    expect(custom.locator("strong")).to_have_text("security-auditor")
    expect(custom).to_contain_text("Custom")
    expect(custom).to_contain_text("Reviews code for injection and secrets handling.")
    expect(custom).to_contain_text("Read-only files · Tools: read_file, search_files · Fast effort · Claude Sonnet 4.5 · "
                                   "8 steps by default · Skills: review")
    expect(custom.locator("code")).to_have_text(".agents/agents/security-auditor.md")
    expect(custom.locator(".agent-type-note")).to_have_text("Ignored unknown tools: browser_open.")
    broken = rows.nth(4)
    expect(broken.locator("code")).to_have_text(".claude/agents/broken.md")
    expect(broken).to_contain_text("Not used")
    expect(broken.locator(".form-error")).to_have_text(re.compile("^description is required"))
    assert ui.requests("/api/agents?session_id=a")
    # Refresh reads the files again.
    section.get_by_role("button", name="Refresh agent types").click()
    page.wait_for_function("fake.requests('/api/agents?session_id=a').length === 2")
    assert ui.spy("onError") == []


def test_without_custom_agents_the_list_says_so_and_a_subagent_chat_has_none(ui, page):
    section = open_subagents(ui, page, "window.agentTypes = { ...agentTypes, custom: [], errors: [] }")
    expect(section.get_by_role("list", name="Agent types").get_by_role("listitem")).to_have_count(3)
    expect(section.get_by_text("No custom agents in this project.")).to_be_visible()
    ui.rerender("dialogProps({ ...session(), is_subagent: true, tool_profile: 'read_only' })")
    expect(page.get_by_text("This child cannot widen its inherited tool access.")).to_be_visible()
    expect(page.get_by_role("list", name="Agent types")).to_have_count(0)


def test_a_failed_agent_list_shows_the_error_in_the_section(ui, page):
    section = open_subagents(ui, page, """fake.override = path => path.startsWith('/api/agents')
      ? fake.json({ detail: 'Cannot read the project.' }, 400) : undefined""")
    expect(section.get_by_role("alert")).to_have_text("Cannot read the project.")
    expect(section.locator(".subagent-row")).to_have_count(1)  # The children still list.


def test_a_subagent_row_names_its_agent_effort_and_model(ui, page):
    section = open_subagents(ui, page, """window.child = { ...child, agent_type: 'security-auditor', model: 'databricks-claude-sonnet-4-5',
      delegation: { ...child.delegation, agent_type: 'security-auditor', model: 'databricks-claude-sonnet-4-5', effort: 'low' },
      subagent: { ...child.subagent, agent_type: 'security-auditor', model: 'databricks-claude-sonnet-4-5', effort: 'low' } }""")
    row = section.locator(".subagent-row")
    expect(row).to_contain_text("security-auditor · Waiting for approval · Read-only files · Fast · Claude Sonnet 4.5")
    expect(row.locator("code")).to_have_text("conversations/child.jsonl")


SETUP = """
window.makeSession = (events, overrides = {}) => ({
  id: 'parent', title: 'Parent task', workspace: '/project', model: 'test-model', status: 'idle',
  events, updated: 1, permission_mode: 'manual', allowed_directories: [], ...overrides,
})
"""


def test_a_subagent_card_shows_the_agent_with_its_effort_and_model(ui, page):
    events = """[{ id: 'd1', type: 'tool', name: 'delegate_task', call_id: 'c1', state: 'running', child_session_id: 'child-1',
      input: { task: 'Audit the parser', agent_type: 'security-auditor' },
      delegation: { status: 'running', completed_tools: 1, agent_type: 'security-auditor', model: 'databricks-claude-sonnet-4-5',
        effort: 'low', started: Date.now() / 1000 - 5 } },
      { id: 'd2', type: 'tool', name: 'delegate_task', call_id: 'c2', state: 'running', input: { task: 'Old' },
        delegation: { status: 'running', completed_tools: 0, agent_type: 'explore' } }]"""
    ui.mount("components/Conversation.js", f"{{ session: makeSession({events}), onError: fake.spy('onError') }}", setup=SETUP)
    cards = page.get_by_label("Subagent progress")
    expect(cards).to_have_count(2)
    expect(cards.nth(0).locator(".delegation-type")).to_have_text("security-auditor")
    agent = cards.nth(0).locator(".delegation-agent")
    expect(agent).to_have_text("Fast · Claude Sonnet 4.5")
    expect(agent).to_have_attribute("title", "Effort and model (databricks-claude-sonnet-4-5)")
    # A card from before agents recorded their model and effort shows only the type.
    expect(cards.nth(1).locator(".delegation-type")).to_have_text("Explore")
    expect(cards.nth(1).locator(".delegation-agent")).to_have_count(0)


def open_settings(ui, page, setup=""):
    ui.app(scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    page.get_by_role("button", name="Settings", exact=True).click()
    return page.get_by_role("dialog")


def test_settings_subagents_show_the_defaults_and_save_changes(ui, page):
    dialog = open_settings(ui, page, """fake.settings = { ...fake.settings, subagent_defaults: {
      explore: { model: '', effort: 'low' }, general: { model: '', effort: '' }, editor: { model: 'my-endpoint', effort: 'max' } } }""")
    group = dialog.get_by_role("group", name="Subagents", exact=True)
    dialog.get_by_role("navigation", name="Settings sections").get_by_role("button", name="Subagents").click()
    explore_model = group.get_by_role("combobox", name="Explore model")
    expect(explore_model).to_have_value("")
    expect(explore_model.locator("option:checked")).to_have_text("Same as the chat")
    expect(group.get_by_role("combobox", name="Explore effort")).to_have_value("low")
    expect(group.get_by_role("combobox", name="Explore effort").locator("option:checked")).to_have_text("Fast")
    expect(group.get_by_role("combobox", name="General effort")).to_have_value("")
    # A saved endpoint that is not listed opens Other endpoint… with its name.
    expect(group.get_by_role("textbox", name="Editor endpoint name")).to_have_value("my-endpoint")
    expect(group.get_by_role("combobox", name="Editor effort")).to_have_value("max")
    explore_model.select_option("databricks-gpt-oss-120b")
    group.get_by_role("combobox", name="General effort").select_option("high")
    group.get_by_role("combobox", name="General model").select_option(label="Other endpoint…")
    name = group.get_by_role("textbox", name="General endpoint name")
    expect(name).to_be_focused()
    name.fill("databricks-claude-sonnet-4-5")
    group.get_by_role("combobox", name="Editor model").select_option("")
    expect(group.get_by_role("textbox", name="Editor endpoint name")).to_have_count(0)
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["subagent_defaults"] == {
        "explore": {"model": "databricks-gpt-oss-120b", "effort": "low"},
        "general": {"model": "databricks-claude-sonnet-4-5", "effort": "high"},
        "editor": {"model": "", "effort": "max"}}


def test_settings_without_saved_defaults_show_explore_at_fast_and_send_nothing_unchanged(ui, page):
    dialog = open_settings(ui, page)
    group = dialog.get_by_role("group", name="Subagents", exact=True)
    expect(group.get_by_role("combobox", name="Explore effort")).to_have_value("low")
    expect(group.get_by_role("combobox", name="Editor model")).to_have_value("")
    expect(group.get_by_text(re.compile("Explore starts at Fast"))).to_be_visible()
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert "subagent_defaults" not in ui.bodies("/api/settings", "PUT")[0]
