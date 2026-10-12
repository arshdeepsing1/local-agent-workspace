"""Settings for M2: custom instructions, file write limit, environment variables for
commands, and continuing after rate limits."""
import re

from playwright.sync_api import expect


def open_settings(ui, page, setup=""):
    ui.app(scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    page.get_by_role("button", name="Settings", exact=True).click()
    return page.get_by_role("dialog")


def test_the_new_fields_render_with_their_defaults_and_help(ui, page):
    dialog = open_settings(ui, page)
    instructions = dialog.get_by_role("textbox", name="Custom instructions")
    expect(instructions).to_have_value("")
    expect(instructions).to_have_attribute("maxlength", "8000")
    expect(dialog.get_by_text("0 / 8,000")).to_be_visible()
    expect(dialog.get_by_text(re.compile(r"before project instructions \(AGENTS\.md, CLAUDE\.md\), which cannot override them"))).to_be_visible()
    limit = dialog.get_by_role("spinbutton", name="File write limit (KB)")
    expect(limit).to_have_value("80")
    expect(limit).to_have_attribute("min", "16")
    expect(limit).to_have_attribute("max", "1000")
    expect(dialog.get_by_text(re.compile("One write_file call carries at most about 500 KB"))).to_be_visible()
    names = dialog.get_by_role("textbox", name="Environment variables for commands")
    expect(names).to_have_value("")
    expect(names).to_have_attribute("placeholder", "JAVA_HOME NVM_DIR CONDA_PREFIX")
    expect(dialog.get_by_text(re.compile(r"the Databricks token and variables that change how programs start \(LD_\*"))).to_be_visible()
    auto = dialog.get_by_role("checkbox", name="Continue automatically after rate limits")
    expect(auto).to_be_checked()
    expect(dialog.get_by_text(re.compile("wait for Databricks's rate limit to reset .* Stop cancels the wait"))).to_be_visible()


def test_saved_values_show_and_the_put_carries_the_new_fields(ui, page):
    dialog = open_settings(ui, page, setup="""fake.settings = { ...fake.settings, user_instructions: 'Be brief.',
      write_limit_kb: 120, command_env: ['JAVA_HOME', 'NVM_DIR'], rate_limit_auto_continue: true }""")
    instructions = dialog.get_by_role("textbox", name="Custom instructions")
    expect(instructions).to_have_value("Be brief.")
    expect(dialog.get_by_text("9 / 8,000")).to_be_visible()
    names = dialog.get_by_role("textbox", name="Environment variables for commands")
    expect(names).to_have_value("JAVA_HOME NVM_DIR")
    instructions.fill("Answer in short paragraphs.\nPrefer standard library modules.")
    expect(dialog.get_by_text("60 / 8,000")).to_be_visible()
    dialog.get_by_role("spinbutton", name="File write limit (KB)").fill("200")
    names.fill("JAVA_HOME,  CONDA_PREFIX\tGOPATH, ")
    expect(names).to_have_value("JAVA_HOME,  CONDA_PREFIX\tGOPATH, ")
    dialog.get_by_role("checkbox", name="Continue automatically after rate limits").click()
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    body = ui.bodies("/api/settings", "PUT")[0]
    assert body["user_instructions"] == "Answer in short paragraphs.\nPrefer standard library modules."
    assert body["write_limit_kb"] == 200
    assert body["command_env"] == ["JAVA_HOME", "CONDA_PREFIX", "GOPATH"]
    assert body["rate_limit_auto_continue"] is False


def test_a_rejected_name_shows_the_server_message(ui, page):
    dialog = open_settings(ui, page, setup="""fake.override = (path, options) => path === '/api/settings' && options.method === 'PUT'
      ? fake.json({ detail: 'LD_PRELOAD changes how programs start and cannot be passed to commands.' }, 400) : undefined""")
    dialog.get_by_role("textbox", name="Environment variables for commands").fill("LD_PRELOAD")
    dialog.get_by_role("button", name="Save settings").click()
    expect(dialog.get_by_role("alert")).to_have_text("LD_PRELOAD changes how programs start and cannot be passed to commands.")
    assert ui.bodies("/api/settings", "PUT")[0]["command_env"] == ["LD_PRELOAD"]
