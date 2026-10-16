"""Settings for M2: custom instructions, file write limit, environment variables for
commands, and continuing after rate limits."""
import re

import pytest
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


def test_prompt_caching_is_chosen_in_the_model_group_and_saved(ui, page):
    dialog = open_settings(ui, page, setup="fake.settings = { ...fake.settings, prompt_caching: 'auto' }")
    group = dialog.get_by_role("group", name="Model and context")
    caching = group.get_by_role("combobox", name="Prompt caching")
    expect(caching).to_have_value("auto")
    expect(caching.locator("option")).to_have_text(["Automatic (Claude endpoints)", "Documented forms only", "Off"])
    expect(group.get_by_text(re.compile(r"^Marks the stable start of each agent request so Databricks can bill repeated "
                                        r"input at the cache-read rate \(about 5% of the input price\)\. Writes cost 25% "
                                        r"more; entries expire after 5 minutes\. Documented forms only marks just your "
                                        r"last message or the last tool call\.$"))).to_be_visible()
    caching.select_option("off")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["prompt_caching"] == "off"


def test_documented_forms_only_is_chosen_and_saved(ui, page):
    dialog = open_settings(ui, page)
    caching = dialog.get_by_role("combobox", name="Prompt caching")
    expect(caching).to_have_value("auto")
    caching.select_option(label="Documented forms only")
    expect(caching).to_have_value("messages")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["prompt_caching"] == "messages"
    # Saved, it is shown again when Settings open next.
    page.get_by_role("button", name="Settings", exact=True).click()
    expect(page.get_by_role("dialog").get_by_role("combobox", name="Prompt caching")).to_have_value("messages")


def test_a_saved_prompt_caching_choice_is_shown_and_kept(ui, page):
    dialog = open_settings(ui, page, setup="fake.settings = { ...fake.settings, prompt_caching: 'off' }")
    expect(dialog.get_by_role("combobox", name="Prompt caching")).to_have_value("off")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["prompt_caching"] == "off"


def test_the_fallback_endpoint_is_chosen_in_the_model_group_and_saved(ui, page):
    dialog = open_settings(ui, page)
    group = dialog.get_by_role("group", name="Model and context")
    fallback = group.get_by_role("combobox", name="Fallback endpoint")
    expect(fallback).to_have_value("")
    # The endpoints the app knows, never the model endpoint itself, and a typed name for one a chat uses.
    expect(fallback.locator("option")).to_have_text([
        "None", "Claude Opus 5.5 · databricks-claude-opus-5-5", "Claude Opus 4.8 · databricks-claude-opus-4-8",
        "GPT OSS 120B · databricks-gpt-oss-120b", "GPT OSS 20B · databricks-gpt-oss-20b", "Other endpoint…"])
    expect(group.get_by_text(re.compile(r"^When the model endpoint fails before a reply starts .* The next request tries "
                                        r"the model endpoint again\.$"))).to_be_visible()
    fallback.select_option("databricks-gpt-oss-120b")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["fallback_model"] == "databricks-gpt-oss-120b"
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.get_by_role("dialog")
    fallback = dialog.get_by_role("combobox", name="Fallback endpoint")
    expect(fallback).to_have_value("databricks-gpt-oss-120b")
    fallback.select_option("Other endpoint…")
    name = dialog.get_by_role("textbox", name="Fallback endpoint name")
    expect(name).to_be_focused()
    name.fill(" team-endpoint ")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[1]["fallback_model"] == "team-endpoint"


def test_a_fallback_equal_to_the_model_endpoint_is_explained(ui, page):
    dialog = open_settings(ui, page, setup="fake.settings = { ...fake.settings, fallback_model: 'other-model' }")
    fallback = dialog.get_by_role("combobox", name="Fallback endpoint")
    expect(fallback).to_have_value("other-model")
    note = dialog.get_by_text("This is the model endpoint: chats on it have no fallback.")
    expect(note).to_have_count(0)
    dialog.get_by_role("combobox", name="Databricks model endpoint").select_option("other-model")
    expect(note).to_be_visible()
    expect(fallback).to_have_value("other-model")  # Still listed while the form has it.
    fallback.select_option("")
    expect(note).to_have_count(0)


def test_a_rejected_name_shows_the_server_message(ui, page):
    dialog = open_settings(ui, page, setup="""fake.override = (path, options) => path === '/api/settings' && options.method === 'PUT'
      ? fake.json({ detail: 'LD_PRELOAD changes how programs start and cannot be passed to commands.' }, 400) : undefined""")
    dialog.get_by_role("textbox", name="Environment variables for commands").fill("LD_PRELOAD")
    dialog.get_by_role("button", name="Save settings").click()
    expect(dialog.get_by_role("alert")).to_have_text("LD_PRELOAD changes how programs start and cannot be passed to commands.")
    assert ui.bodies("/api/settings", "PUT")[0]["command_env"] == ["LD_PRELOAD"]


def test_more_than_32_names_or_repeated_names_need_no_server_round_trip(ui, page):
    # Reported: 33 names showed the server's raw validation JSON as the error.
    dialog = open_settings(ui, page)
    names = dialog.get_by_role("textbox", name="Environment variables for commands")
    names.fill(" ".join(f"VAR{index}" for index in range(33)))
    dialog.get_by_role("button", name="Save settings").click()
    expect(dialog.get_by_role("alert")).to_have_text("List at most 32 environment variable names.")
    assert ui.bodies("/api/settings", "PUT") == []
    # A repeated name is sent once.
    names.fill("JAVA_HOME JAVA_HOME, NVM_DIR JAVA_HOME")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["command_env"] == ["JAVA_HOME", "NVM_DIR"]


def test_the_instructions_counter_describes_the_field_without_announcing_each_key(ui, page):
    dialog = open_settings(ui, page)
    instructions = dialog.get_by_role("textbox", name="Custom instructions")
    instructions.fill("Be brief.")
    expect(instructions).to_have_accessible_description(re.compile(r"^9 / 8,000"))
    expect(dialog.locator("[aria-live]").filter(has_text="8,000")).to_have_count(0)


def test_a_cleared_file_write_limit_stays_empty_and_is_not_saved(ui, page):
    # Reported: clearing the field put a 0 back into it.
    dialog = open_settings(ui, page)
    limit = dialog.get_by_role("spinbutton", name="File write limit (KB)")
    limit.select_text()
    limit.press("Backspace")
    expect(limit).to_have_value("")
    limit.press_sequentially("64")
    expect(limit).to_have_value("64")
    limit.fill("")
    dialog.get_by_role("button", name="Save settings").click()
    expect(dialog).to_be_visible()
    assert ui.bodies("/api/settings", "PUT") == []


def test_the_auto_compact_field_explains_the_token_threshold_and_is_saved(ui, page):
    dialog = open_settings(ui, page)
    field = dialog.get_by_role("spinbutton", name="Auto-compact at (% of input budget)")
    expect(field).to_have_value("90")
    expect(field).to_have_attribute("min", "50")
    expect(field).to_have_attribute("max", "100")
    expect(dialog.get_by_text(re.compile(r"With these settings that is about 108,748 tokens\. Lower values compact "
                                         r"sooner"))).to_be_visible()
    expect(dialog.get_by_text(re.compile(r"plus up to 8,192 for thinking on models that always think, such as "
                                         r"Claude Opus 5\.5"))).to_be_visible()
    field.fill("60")
    expect(dialog.get_by_text(re.compile(r"that is about 72,499 tokens"))).to_be_visible()
    field.fill("40")
    expect(dialog.get_by_text(re.compile(r"Enter a whole number from 50 to 100\."))).to_be_visible()
    expect(dialog.get_by_text(re.compile(r"that is about 48,"))).to_have_count(0)
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    assert ui.bodies("/api/settings", "PUT") == []
    # Clearing the field leaves it empty (no 0 put back) and claims no token count.
    field.select_text()
    field.press("Backspace")
    expect(field).to_have_value("")
    expect(dialog.get_by_text(re.compile(r"Enter a whole number from 50 to 100\."))).to_be_visible()
    expect(dialog.get_by_text(re.compile(r"about 0 tokens"))).to_have_count(0)
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    assert ui.bodies("/api/settings", "PUT") == []
    field.fill("101")
    dialog.get_by_role("button", name="Save settings").click()
    assert ui.bodies("/api/settings", "PUT") == []
    field.fill("75")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["auto_compact_percent"] == 75


def test_a_saved_auto_compact_value_is_shown(ui, page):
    dialog = open_settings(ui, page, setup="fake.settings = { ...fake.settings, auto_compact_percent: 100 }")
    expect(dialog.get_by_role("spinbutton", name="Auto-compact at (% of input budget)")).to_have_value("100")
    expect(dialog.get_by_text(re.compile(r"that is about 120,832 tokens"))).to_be_visible()


# Layout: a named dialog, grouped fields and a Save button that stays in view

LAYOUT_VIEWPORTS = [(1280, 900), (390, 844)]


def open_settings_at(ui, page, width, height, setup=""):
    page.set_viewport_size({"width": width, "height": height})
    ui.app(scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    if width <= 600:
        page.get_by_role("button", name="Open sidebar").click()
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.get_by_role("dialog", name="Settings")
    expect(dialog).to_be_visible()
    return dialog


def footer_top(ui):
    return ui.js("document.querySelector('.settings-dialog form > footer').getBoundingClientRect().top")


@pytest.mark.parametrize("width,height", LAYOUT_VIEWPORTS)
def test_save_is_always_visible(ui, page, width, height):
    dialog = open_settings_at(ui, page, width, height)
    expect(dialog).to_have_accessible_description("Configure your local workspace.")
    save = dialog.get_by_role("button", name="Save settings")
    expect(save).to_be_in_viewport(ratio=1)
    for top in ("dialog.scrollHeight", "0"):
        ui.run(f"const dialog = document.querySelector('.settings-dialog'); dialog.scrollTop = {top}")
        expect(save).to_be_in_viewport(ratio=1)
        expect(dialog.get_by_role("button", name="Cancel", exact=True)).to_be_in_viewport(ratio=1)
    assert ui.js("(d => d.scrollHeight > d.clientHeight)(document.querySelector('.settings-dialog'))")  # It does scroll.
    expect(dialog.get_by_role("textbox", name="Custom instructions")).to_have_css("border-radius", "8px")
    expect(dialog.get_by_role("group", name="Model and context").get_by_role("spinbutton", name="Context budget (tokens)")).to_be_visible()
    expect(dialog.get_by_role("group", name="Project").get_by_role("textbox", name="Project folder")).to_be_visible()
    groups = dialog.locator("fieldset.settings-group")
    expect(groups).to_have_count(8)
    assert [groups.nth(n).locator("legend").inner_text() for n in range(8)] == [
        "Project", "Model and context", "Reasoning", "Agent", "Subagents", "Commands and files", "Notifications", "Credentials"]
    for n in range(8):
        expect(dialog.get_by_role("group", name=groups.nth(n).locator("legend").inner_text(), exact=True)).to_have_count(1)
    # The footer spans the dialog's padding without making it scroll sideways (phones have less padding).
    fits = ui.js("""(d => { const box = d.getBoundingClientRect(), footer = d.querySelector('form > footer').getBoundingClientRect()
      return { sideways: d.scrollWidth - d.clientWidth, inside: footer.left >= box.left && footer.right <= box.right } })(document.querySelector('.settings-dialog'))""")
    assert fits == {"sideways": 0, "inside": True}, fits


SAVE_FAILS = """fake.override = (path, options) => path === '/api/settings' && options.method === 'PUT'
  ? fake.json({ detail: 'The project folder does not exist: /srv/missing' }, 400) : undefined"""


@pytest.mark.parametrize("scrolled", ["top", "bottom"])
@pytest.mark.parametrize("width,height", LAYOUT_VIEWPORTS)
def test_a_failed_save_shows_its_error_above_the_buttons(ui, page, width, height, scrolled):
    dialog = open_settings_at(ui, page, width, height, setup=SAVE_FAILS)
    ui.run(f"const dialog = document.querySelector('.settings-dialog'); dialog.scrollTop = {'0' if scrolled == 'top' else 'dialog.scrollHeight'}")
    dialog.get_by_role("button", name="Save settings").click()
    alert = dialog.get_by_role("alert").filter(has_text="The project folder does not exist")
    expect(alert).to_be_in_viewport(ratio=1)
    expect(dialog.get_by_role("button", name="Save settings")).to_be_enabled()
    # Nothing covers it: the sticky footer holds it.
    assert alert.evaluate("e => { const r = e.getBoundingClientRect(); return e.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) }")
    assert ui.js("document.querySelector('.settings-dialog form > footer').contains(document.querySelector('form .form-error[role=alert]'))")


@pytest.mark.parametrize("width,height", LAYOUT_VIEWPORTS)
def test_focused_fields_stay_above_the_footer(ui, page, width, height):
    dialog = open_settings_at(ui, page, width, height)
    dialog.get_by_role("textbox", name="Project folder").focus()
    hidden = []
    for _ in range(60):
        state = ui.js("""(a => ({ name: a.getAttribute('aria-label') || a.labels?.[0]?.textContent.trim().slice(0, 40) || a.textContent.trim(),
          inFooter: !!a.closest('form > footer'), top: a.getBoundingClientRect().top, bottom: a.getBoundingClientRect().bottom,
          height: a.getBoundingClientRect().height }))(document.activeElement)""")
        if state["inFooter"]:
            break
        footer = footer_top(ui)
        # A control taller than the room above the footer (Custom instructions on a phone) must at least start above it.
        if state["bottom"] > footer + 0.5 and (state["height"] < 150 or state["top"] >= footer):
            hidden.append((state["name"], state["top"], state["bottom"], footer))
        page.keyboard.press("Tab")
    else:
        raise AssertionError("Tab never reached the footer")
    assert hidden == []


@pytest.mark.parametrize("width,height", LAYOUT_VIEWPORTS)
def test_an_invalid_field_is_shown_above_the_footer(ui, page, width, height):
    dialog = open_settings_at(ui, page, width, height)
    limit = dialog.get_by_role("spinbutton", name="File write limit (KB)")
    limit.fill("5")
    ui.run("document.querySelector('.settings-dialog').scrollTop = 0")
    dialog.get_by_role("button", name="Save settings").click()
    expect(limit).to_be_focused()
    assert limit.bounding_box()["y"] + limit.bounding_box()["height"] <= footer_top(ui) + 0.5
    assert ui.bodies("/api/settings", "PUT") == []


# The user's instruction files, the output style and project memory (Settings → Agent)

def test_instruction_files_output_style_and_memory_show_their_defaults_and_save(ui, page):
    dialog = open_settings(ui, page, setup="""fake.override = path => path === '/api/output-styles' ? fake.json({ styles: [
      { id: 'default', name: 'Default', source: 'built-in' }, { id: 'concise', name: 'Concise', source: 'built-in' },
      { id: 'explanatory', name: 'Explanatory', source: 'built-in' },
      { id: 'teacher', name: 'Teacher', source: 'project', path: '.agents/output-styles/teacher.md' }], errors: [] }) : undefined""")
    group = dialog.get_by_role("group", name="Agent")
    agents = group.get_by_role("checkbox", name="Load my instructions file ~/.agents/AGENTS.md")
    claude = group.get_by_role("checkbox", name="Also load ~/.claude/CLAUDE.md")
    memory = group.get_by_role("checkbox", name="Project memory")
    style = group.get_by_role("combobox", name="Output style")
    expect(agents).to_be_checked()
    expect(claude).not_to_be_checked()
    expect(memory).to_be_checked()
    expect(style).to_have_value("default")
    expect(style.locator("option")).to_have_text(["Default", "Concise", "Explanatory", "Teacher"])
    expect(group.get_by_text(re.compile(r"^Your own instruction files for every project, added after Custom instructions"))).to_be_visible()
    expect(group.get_by_text(re.compile(r"Review, edit and delete them in Agent tools → Memory\.$"))).to_be_visible()
    claude.check()
    agents.uncheck()
    memory.uncheck()
    style.select_option("teacher")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    body = ui.bodies("/api/settings", "PUT")[0]
    assert (body["user_agents_file"], body["user_claude_file"], body["project_memory"], body["output_style"]) == (
        False, True, False, "teacher")


def test_a_saved_style_without_its_file_stays_selected_and_says_so(ui, page):
    dialog = open_settings(ui, page, setup="fake.settings = { ...fake.settings, output_style: 'gone' }")
    style = dialog.get_by_role("combobox", name="Output style")
    expect(style).to_have_value("gone")
    expect(style.locator("option:checked")).to_have_text("gone (no such style file here)")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["output_style"] == "gone"


def test_the_built_in_styles_are_offered_when_the_style_list_fails(ui, page):
    dialog = open_settings(ui, page, setup="""fake.override = path => path === '/api/output-styles'
      ? fake.json({ detail: 'Broken' }, 500) : undefined""")
    style = dialog.get_by_role("combobox", name="Output style")
    expect(style.locator("option")).to_have_text(["Default", "Concise", "Explanatory"])
