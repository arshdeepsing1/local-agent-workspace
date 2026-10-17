"""Project settings files (M5f3): the trust prompt when a project has .agents/settings.json, and Settings naming each
value a trusted file sets and where it comes from."""
import json

from playwright.sync_api import expect

SHA = "a" * 64
FILE = {"layer": "project", "name": ".agents/settings.json", "sha256": "b" * 64,
        "values": {"max_agent_steps": 12, "model": "databricks-claude-opus-5-5", "auto_keep_changes": True},
        "problems": ["spending_limit_dbu: A project file can lower your spending limit (5 DBU), never raise or remove it."],
        "ignored": ["env_file", "hooks"]}
PENDING = {"workspace": "/project", "status": "pending", "changed": False, "decision": None, "sha256": SHA, "files": [FILE],
           "values": {}, "sources": {}, "followed": [], "notes": []}
TRUSTED = {**PENDING, "status": "trusted", "decision": "trust",
           "values": {"max_agent_steps": 12, "model": "databricks-claude-opus-5-5", "auto_keep_changes": True,
                      "context_window": 200000},
           "sources": {"max_agent_steps": "project", "model": "project", "auto_keep_changes": "project",
                       "context_window": "project"}, "followed": ["context_window"]}


def setup(report, answer=None):
    """fake.projectLayers starts as report; POST /trust answers answer (default: the report trusted)."""
    return f"""
fake.projectLayers = {json.dumps(report)}
fake.settings = {{ ...fake.settings, project_layers: fake.projectLayers }}
fake.override = (path, options) => {{
  if (path.startsWith('/api/settings/project/trust') && options.method === 'POST') {{
    const answer = {json.dumps(answer)} || {{ ...fake.projectLayers, status: JSON.parse(options.body).decision === 'trust' ? 'trusted' : 'ignored',
      decision: JSON.parse(options.body).decision }}
    if (answer.status === 409) return fake.json({{ detail: answer.detail }}, 409)
    fake.projectLayers = answer
    fake.settings = {{ ...fake.settings, project_layers: answer }}
    return fake.json(answer)
  }}
}}
"""


def dialog(page):
    return page.get_by_role("dialog", name="Project settings files")


def start(ui, page, report, answer=None, model="test-model"):
    ui.app(scenario="workspace", setup=setup(report, answer))
    expect(page.get_by_role("combobox", name="Model")).to_have_value(model)


def test_a_project_with_settings_files_asks_once_and_trust_applies_them(ui, page):
    start(ui, page, PENDING)
    expect(dialog(page)).to_be_visible()
    expect(dialog(page).get_by_text("This project has settings in .agents/settings.json. They apply to its chats only if you trust them.")).to_be_visible()
    file = dialog(page).get_by_role("region", name=".agents/settings.json")
    expect(file.get_by_text("Agent steps per message")).to_be_visible()
    expect(file.get_by_text("12", exact=True)).to_be_visible()
    expect(file.get_by_text("Keep changes automatically")).to_be_visible()
    expect(file.get_by_text("on", exact=True)).to_be_visible()
    expect(file.get_by_text("Not used: spending_limit_dbu: A project file can lower your spending limit")).to_be_visible()
    expect(file.get_by_text("Ignored (only Settings can set them): env_file, hooks")).to_be_visible()
    dialog(page).get_by_role("button", name="Trust").click()
    expect(dialog(page)).to_have_count(0)
    assert ui.bodies("/api/settings/project/trust", "POST") == [{"decision": "trust", "sha256": SHA}]
    ui.wait("fake.requests('/api/settings').length >= 1")  # Settings are read again, with the new sources.


def test_decide_later_asks_nothing_until_the_files_change(ui, page):
    start(ui, page, PENDING)
    dialog(page).get_by_role("button", name="Decide later").click()
    expect(dialog(page)).to_have_count(0)
    assert ui.requests("/api/settings/project/trust", "POST") == []
    # Escape also closes it without a decision.
    ui.run("fake.projectLayers = { ...fake.projectLayers, sha256: 'c'.repeat(64) }; fake.settings = { ...fake.settings, project_layers: fake.projectLayers }")
    page.get_by_role("button", name="Settings", exact=True).first.click()
    page.get_by_role("dialog", name="Settings").get_by_role("button", name="Save settings").click()
    expect(dialog(page)).to_be_visible()  # Changed files ask again.
    page.keyboard.press("Escape")
    expect(dialog(page)).to_have_count(0)
    assert ui.requests("/api/settings/project/trust", "POST") == []


def test_files_that_changed_while_you_read_them_are_shown_again(ui, page):
    newer = {**PENDING, "sha256": "d" * 64, "files": [{**FILE, "values": {"max_agent_steps": 64}, "problems": [], "ignored": []}]}
    start(ui, page, PENDING, {"status": 409, "detail": {"message": "The project's settings files changed while you reviewed them. Review them again.", "report": newer}})
    dialog(page).get_by_role("button", name="Trust").click()
    expect(dialog(page).get_by_role("alert")).to_have_text("The project's settings files changed while you reviewed them. Review them again.")
    expect(dialog(page).get_by_text("64", exact=True)).to_be_visible()
    expect(dialog(page).get_by_text("Keep changes automatically")).to_have_count(0)


def test_settings_names_each_value_a_trusted_file_sets(ui, page):
    start(ui, page, TRUSTED, model="databricks-claude-opus-5-5")  # A new chat shows the file's model.
    expect(dialog(page)).to_have_count(0)  # Trusted: nothing to ask.
    page.get_by_role("button", name="Settings", exact=True).first.click()
    settings = page.get_by_role("dialog", name="Settings")
    expect(settings.get_by_text("In this project: 12 from .agents/settings.json.")).to_be_visible()
    expect(settings.get_by_text("In this project: databricks-claude-opus-5-5 from .agents/settings.json.")).to_be_visible()
    expect(settings.get_by_text("In this project: 200,000, the default of the endpoint .agents/settings.json sets.")).to_be_visible()
    expect(settings.get_by_text("In this project: on from .agents/settings.json.")).to_be_visible()
    expect(settings.get_by_text("You trust .agents/settings.json: chats in this project use the values it sets")).to_be_visible()
    # Your own value stays in the form and is what Save sends.
    expect(settings.get_by_role("spinbutton", name="Agent steps per message")).to_have_value("32")
    settings.get_by_role("button", name="Review settings files…").click()
    expect(dialog(page)).to_be_visible()
    dialog(page).get_by_role("button", name="Ignore").click()
    expect(dialog(page)).to_have_count(0)
    assert ui.bodies("/api/settings/project/trust", "POST") == [{"decision": "ignore", "sha256": SHA}]
    expect(settings.get_by_text("You chose to ignore .agents/settings.json: chats in this project use your Settings.")).to_be_visible()
    expect(settings.get_by_text("In this project: 12 from .agents/settings.json.")).to_have_count(0)
    settings.get_by_role("button", name="Save settings").click()
    sent = ui.bodies("/api/settings", "PUT")[-1]
    assert sent["max_agent_steps"] == 32 and "project_layers" not in sent


def test_a_chats_project_is_asked_about_by_its_chat(ui, page):
    setup_chat = setup(PENDING) + "fake.sessions.set('w', { ...makeSession('w', 'Worktree chat'), workspace: '/state/worktrees/1', worktree: { branch: 'chat/x', source_workspace: '/project' } })"
    ui.app(session="w", scenario="workspace", setup=setup_chat)
    ui.show_session("w")
    expect(dialog(page)).to_be_visible()
    assert ui.requests("/api/settings/project?session_id=w")
    dialog(page).get_by_role("button", name="Trust").click()
    expect(dialog(page)).to_have_count(0)
    assert len(ui.requests("/api/settings/project/trust?session_id=w", "POST")) == 1


def test_a_new_chat_starts_with_the_files_model_and_effort_and_keeps_your_choice(ui, page):
    trusted = {**TRUSTED, "values": {**TRUSTED["values"], "reasoning_effort": "high"},
               "sources": {**TRUSTED["sources"], "reasoning_effort": "project"}}
    start(ui, page, trusted, model="databricks-claude-opus-5-5")
    expect(page.get_by_role("combobox", name="Reasoning effort")).to_have_value("high")
    # Another model for this new chat is sent with it: saving it in Settings would lose to the file's model.
    page.get_by_role("combobox", name="Model").select_option("other-model")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("other-model")
    assert ui.requests("/api/settings", "PUT") == []
    page.get_by_role("textbox", name="Message").fill("Hello")
    page.get_by_role("textbox", name="Message").press("Enter")
    ui.wait("fake.requests('/api/sessions', 'POST').length === 1")
    assert ui.bodies("/api/sessions", "POST") == [{"permission_mode": "manual", "model": "other-model"}]
