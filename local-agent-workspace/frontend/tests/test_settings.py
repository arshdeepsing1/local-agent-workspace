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
      write_limit_kb: 120, command_env: ['JAVA_HOME', 'NVM_DIR'], secret_file_patterns: ['*.tfvars'], rate_limit_auto_continue: true }""")
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
    secrets = dialog.get_by_role("textbox", name="Secret file names for commands")
    expect(secrets).to_have_value("*.tfvars")
    secrets.fill("*.tfvars, prod-*.yaml  *.tfvars")
    dialog.get_by_role("checkbox", name="Continue automatically after rate limits").click()
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    body = ui.bodies("/api/settings", "PUT")[0]
    assert body["secret_file_patterns"] == ["*.tfvars", "prod-*.yaml"]
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
    for top in ("body.scrollHeight", "0"):  # The dialog's body scrolls (DialogShell); its header and footer stay.
        ui.run(f"const body = document.querySelector('.settings-dialog .dialog-body'); body.scrollTop = {top}")
        expect(save).to_be_in_viewport(ratio=1)
        expect(dialog.get_by_role("button", name="Cancel", exact=True)).to_be_in_viewport(ratio=1)
    assert ui.js("(d => d.scrollHeight > d.clientHeight)(document.querySelector('.settings-dialog .dialog-body'))")  # It does scroll.
    expect(dialog.get_by_role("textbox", name="Custom instructions")).to_have_css("border-radius", "8px")
    expect(dialog.get_by_role("group", name="Model and context").get_by_role("spinbutton", name="Context budget (tokens)")).to_be_visible()
    expect(dialog.get_by_role("group", name="Project").get_by_role("textbox", name="Project folder")).to_be_visible()
    groups = dialog.locator("fieldset.settings-group")
    expect(groups).to_have_count(11)
    assert [groups.nth(n).locator("legend").inner_text() for n in range(11)] == [
        "Project", "Model and context", "Reasoning", "Agent", "Subagents", "Commands and files", "Web access", "Browsing",
        "Appearance", "Notifications", "Credentials"]
    for n in range(11):
        expect(dialog.get_by_role("group", name=groups.nth(n).locator("legend").inner_text(), exact=True)).to_have_count(1)
    # The footer spans the dialog's width without making it scroll sideways.
    fits = ui.js("""(d => { const box = d.getBoundingClientRect(), footer = d.querySelector('form > footer').getBoundingClientRect()
      return { sideways: d.scrollWidth - d.clientWidth, inside: footer.left >= box.left && footer.right <= box.right } })(document.querySelector('.settings-dialog'))""")
    assert fits == {"sideways": 0, "inside": True}, fits


SAVE_FAILS = """fake.override = (path, options) => path === '/api/settings' && options.method === 'PUT'
  ? fake.json({ detail: 'The project folder does not exist: /srv/missing' }, 400) : undefined"""


@pytest.mark.parametrize("scrolled", ["top", "bottom"])
@pytest.mark.parametrize("width,height", LAYOUT_VIEWPORTS)
def test_a_failed_save_shows_its_error_above_the_buttons(ui, page, width, height, scrolled):
    dialog = open_settings_at(ui, page, width, height, setup=SAVE_FAILS)
    ui.run(f"const body = document.querySelector('.settings-dialog .dialog-body'); body.scrollTop = {'0' if scrolled == 'top' else 'body.scrollHeight'}")
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
    for _ in range(120):  # Every focusable control of the dialog, then the footer.
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
    ui.run("document.querySelector('.settings-dialog .dialog-body').scrollTop = 0")
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


# Settings → Web access

def test_web_access_turns_fetch_off_and_names_a_search_provider_without_its_key(ui, page):
    dialog = open_settings(ui, page)
    group = dialog.get_by_role("group", name="Web access")
    fetch = group.get_by_role("checkbox", name="Let the agent fetch web pages (web_fetch)")
    provider = group.get_by_role("combobox", name="Web search provider")
    expect(fetch).to_be_checked()
    expect(provider).to_have_value("")
    expect(group.get_by_role("textbox", name="SearXNG address")).to_have_count(0)
    provider.select_option("brave")
    key = group.get_by_role("textbox", name="API key entry in your credential file")
    expect(key).to_have_attribute("placeholder", "BRAVE_API_KEY")
    expect(group.get_by_text("The name of the entry, for example BRAVE_API_KEY=… in your credential file.", exact=False)).to_be_visible()
    provider.select_option("searxng")
    expect(key).to_have_count(0)
    group.get_by_role("textbox", name="SearXNG address").fill("http://127.0.0.1:8888")
    fetch.uncheck()
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    body = ui.bodies("/api/settings", "PUT")[0]
    assert (body["web_fetch"], body["web_search_provider"], body["web_search_url"]) == (False, "searxng", "http://127.0.0.1:8888")


def test_web_access_shows_what_the_saved_provider_still_needs(ui, page):
    dialog = open_settings(ui, page, setup="""fake.settings = { ...fake.settings, web_search_provider: 'tavily', web_search_key: '',
      web_search_status: { provider: 'Tavily', ready: false, key_name: 'TAVILY_API_KEY',
        problem: 'Add TAVILY_API_KEY=… to your credential file: web search with Tavily reads its API key there.' } }""")
    group = dialog.get_by_role("group", name="Web access")
    expect(group.get_by_role("status")).to_have_text(
        "Add TAVILY_API_KEY=… to your credential file: web search with Tavily reads its API key there.")
    group.get_by_role("textbox", name="API key entry in your credential file").fill("MY_TAVILY")
    expect(group.get_by_role("status")).to_have_count(0)  # Checked again when saved.


# Settings → Browsing

def test_browsing_follows_web_access_and_loads_its_defaults(ui, page):
    dialog = open_settings(ui, page)
    groups = dialog.locator("fieldset.settings-group legend")
    titles = [groups.nth(n).inner_text() for n in range(groups.count())]
    assert titles.index("Browsing") == titles.index("Web access") + 1
    expect(dialog.get_by_role("navigation", name="Settings sections").get_by_role("button", name="Browsing")).to_be_visible()
    group = dialog.get_by_role("group", name="Browsing")
    expect(group.get_by_role("checkbox", name="Let the agent use a browser")).not_to_be_checked()
    window = group.get_by_role("combobox", name="Browser window")
    expect(window).to_have_value("auto")
    expect(window.locator("option")).to_have_text(["Automatic", "Show the window", "Hidden"])
    limit = group.get_by_role("spinbutton", name="Time limit per action (seconds)")
    expect(limit).to_have_value("30")
    expect(limit).to_have_attribute("min", "5")
    expect(limit).to_have_attribute("max", "120")
    expect(limit).to_have_attribute("step", "1")
    expect(group.get_by_role("status")).to_have_count(0)  # No browser_status: no status line.
    expect(group.get_by_text(re.compile("asks before it opens a site it has not opened before in this chat"))).to_be_visible()
    expect(group.get_by_text(re.compile("never types into password, card or code fields on a real site"))).to_be_visible()
    expect(group.get_by_text(re.compile("32-tool request limit"))).to_be_visible()


def test_browsing_saved_values_load_and_changed_values_save(ui, page):
    dialog = open_settings(ui, page, setup="""fake.settings = { ...fake.settings, browser_tools: true,
      browser_headless: 'headless', browser_action_timeout: 45 }""")
    group = dialog.get_by_role("group", name="Browsing")
    tools = group.get_by_role("checkbox", name="Let the agent use a browser")
    window = group.get_by_role("combobox", name="Browser window")
    limit = group.get_by_role("spinbutton", name="Time limit per action (seconds)")
    expect(tools).to_be_checked()
    expect(window).to_have_value("headless")
    expect(limit).to_have_value("45")
    tools.uncheck()
    window.select_option("window")
    limit.fill("90")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    body = ui.bodies("/api/settings", "PUT")[0]
    assert (body["browser_tools"], body["browser_headless"], body["browser_action_timeout"]) == (False, "window", 90)


def test_browsing_cancel_discards_changes(ui, page):
    dialog = open_settings(ui, page)
    dialog.get_by_role("checkbox", name="Let the agent use a browser").check()
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT") == []


def test_the_browser_time_limit_must_stay_in_range(ui, page):
    dialog = open_settings(ui, page)
    limit = dialog.get_by_role("spinbutton", name="Time limit per action (seconds)")
    for bad in ("4", "121", ""):
        limit.fill(bad)
        dialog.get_by_role("button", name="Save settings").click()
        expect(dialog).to_be_visible()
        expect(limit).to_be_focused()
        assert ui.bodies("/api/settings", "PUT") == []
    limit.fill("5")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["browser_action_timeout"] == 5


def test_the_browser_status_line_says_ready_or_names_the_problem(ui, page):
    dialog = open_settings(ui, page, setup="fake.settings = { ...fake.settings, browser_status: { ready: true } }")
    expect(dialog.get_by_role("group", name="Browsing").get_by_role("status")).to_have_text("Ready.")
    problem = "The browser is not installed. Run: python -m playwright install chromium"
    page.get_by_role("button", name="Cancel", exact=True).click()
    ui.app(scenario="workspace", setup=f"fake.settings = {{ ...fake.settings, browser_status: {{ ready: false, problem: '{problem}' }} }}")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    page.get_by_role("button", name="Settings", exact=True).click()
    expect(page.get_by_role("dialog").get_by_role("group", name="Browsing").get_by_role("status")).to_have_text(problem)


def test_the_browser_proxy_field_defaults_to_auto_and_explains_its_values(ui, page):
    dialog = open_settings(ui, page)
    group = dialog.get_by_role("group", name="Browsing")
    field = group.get_by_role("textbox", name="Proxy for the browser")
    expect(field).to_have_value("auto")
    expect(group.get_by_text(re.compile("auto uses the proxy your computer is set to"))).to_be_visible()
    expect(group.get_by_text(re.compile("none connects directly"))).to_be_visible()
    expect(group.get_by_text(re.compile("Zscaler in local proxy mode"))).to_be_visible()
    expect(group.get_by_text(re.compile("shown as <redacted> afterwards"))).to_be_visible()
    expect(group.get_by_text(re.compile(r"socks5://127\.0\.0\.1:1080 for a SOCKS5 proxy"))).to_be_visible()
    expect(group.get_by_text(re.compile("uses a system SOCKS proxy only when it finds no HTTP or HTTPS proxy"))).to_be_visible()


def test_the_browser_proxy_saved_value_loads_and_a_changed_value_saves(ui, page):
    dialog = open_settings(ui, page, setup="""fake.settings = { ...fake.settings, browser_upstream_proxy: 'http://agent:<redacted>@proxy.test:8080' }""")
    field = dialog.get_by_role("group", name="Browsing").get_by_role("textbox", name="Proxy for the browser")
    expect(field).to_have_value("http://agent:<redacted>@proxy.test:8080")
    field.fill("  none ")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["browser_upstream_proxy"] == "none"


def test_an_unchanged_redacted_proxy_address_is_sent_back_as_shown(ui, page):
    dialog = open_settings(ui, page, setup="""fake.settings = { ...fake.settings, browser_upstream_proxy: 'http://agent:<redacted>@proxy.test:8080' }""")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["browser_upstream_proxy"] == "http://agent:<redacted>@proxy.test:8080"


def test_the_browser_proxy_field_cannot_be_empty(ui, page):
    dialog = open_settings(ui, page)
    field = dialog.get_by_role("textbox", name="Proxy for the browser")
    field.fill("")
    dialog.get_by_role("button", name="Save settings").click()
    expect(dialog).to_be_visible()
    expect(field).to_be_focused()
    assert ui.bodies("/api/settings", "PUT") == []


def test_the_status_line_names_the_connection_mode_the_app_found(ui, page):
    dialog = open_settings(ui, page, setup="""fake.settings = { ...fake.settings,
      browser_status: { ready: true, proxy: 'system proxy 127.0.0.1:9000' } }""")
    expect(dialog.get_by_role("group", name="Browsing").get_by_role("status")).to_have_text(
        "Ready. Connection: system proxy 127.0.0.1:9000.")


def test_a_pac_file_gets_a_sentence_that_says_to_enter_the_proxy_address(ui, page):
    sentence = "This computer finds its proxy through a PAC file. Enter the proxy address in Settings → Browsing."
    dialog = open_settings(ui, page, setup=f"""fake.settings = {{ ...fake.settings, browser_status: {{ ready: true,
      proxy: 'PAC file found: enter the proxy address', proxy_problem: '{sentence}' }} }}""")
    group = dialog.get_by_role("group", name="Browsing")
    expect(group.get_by_role("status")).to_have_text("Ready. Connection: PAC file found: enter the proxy address.")
    expect(group.get_by_text(sentence)).to_be_visible()


# Settings → Model and context: the trim marks in effect

def test_the_trim_hint_shows_the_marks_in_effect(ui, page):
    dialog = open_settings(ui, page, setup="fake.settings = { ...fake.settings, context_trim_effective: { start: 65, target: 50 } }")
    group = dialog.get_by_role("group", name="Model and context")
    expect(group.get_by_text("In effect: trimming starts at 65% and stops at 50%.")).to_be_visible()
    expect(group.get_by_text("Trimming is off for this threshold.")).to_have_count(0)


def test_the_trim_hint_says_trimming_is_off_when_the_threshold_leaves_no_room(ui, page):
    dialog = open_settings(ui, page, setup="fake.settings = { ...fake.settings, context_trim: true, context_trim_effective: null }")
    group = dialog.get_by_role("group", name="Model and context")
    expect(group.get_by_text("Trimming is off for this threshold.")).to_be_visible()
    expect(group.get_by_text(re.compile("In effect"))).to_have_count(0)
    group.get_by_role("checkbox", name="Trim old tool results from requests").uncheck()
    expect(group.get_by_text("Trimming is off for this threshold.")).to_have_count(0)


# Settings → Appearance → Text size (textSize.js): every font size at once, kept in this browser only.

def test_text_size_scales_all_text_at_once_and_is_kept_in_this_browser(ui, page):
    dialog = open_settings(ui, page)
    size = dialog.get_by_role("group", name="Appearance").get_by_role("combobox", name="Text size")
    expect(size).to_have_value("100")
    legend = "parseFloat(getComputedStyle(document.querySelector('.settings-group legend')).fontSize)"
    base = ui.js(legend)
    size.select_option("125")
    assert ui.js(legend) == pytest.approx(base * 1.25)
    assert ui.js("localStorage.getItem('local.textSize')") == "125"
    assert ui.js("document.documentElement.scrollWidth <= innerWidth")  # No sideways scroll.
    ui.app(scenario="workspace")  # The page again: the size comes back from this browser's storage.
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    assert ui.js("getComputedStyle(document.documentElement).getPropertyValue('--text-scale').trim()") == "1.25"
    page.get_by_role("button", name="Settings", exact=True).click()
    size = page.get_by_role("dialog").get_by_role("combobox", name="Text size")
    expect(size).to_have_value("125")
    size.select_option("100")
    assert ui.js(legend) == pytest.approx(base)
    assert ui.js("localStorage.getItem('local.textSize')") is None
    assert ui.bodies("/api/settings", "PUT") == []  # Never saved with Settings.


# The Style and Theme choices are gone (one light style): tests/test_one_style.py.


@pytest.mark.parametrize("width,height", LAYOUT_VIEWPORTS)
def test_the_largest_text_size_keeps_the_workspace_within_the_window(ui, page, width, height):
    page.set_viewport_size({"width": width, "height": height})
    ui.app(scenario="workspace", setup="localStorage.setItem('local.textSize', '150')")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    assert ui.js("getComputedStyle(document.documentElement).getPropertyValue('--text-scale').trim()") == "1.5"
    assert ui.js("document.documentElement.scrollWidth <= innerWidth")
    expect(page.get_by_role("textbox", name="Message")).to_be_in_viewport()


def test_every_settings_checkbox_keeps_its_size_at_320_px(ui, page):
    page.set_viewport_size({"width": 320, "height": 700})
    ui.app(scenario="workspace")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    page.get_by_role("button", name="Open sidebar").click()  # On a phone the sidebar is a drawer.
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.get_by_role("dialog")
    boxes = dialog.locator("input[type=checkbox]")
    count = boxes.count()
    assert count >= 5, count
    narrow = boxes.evaluate_all("els => els.map(e => [e.closest('label')?.textContent.trim().slice(0, 40), e.getBoundingClientRect().width, e.getBoundingClientRect().height]).filter(([, w, h]) => w < 16 || h < 16)")
    assert narrow == [], narrow


def test_close_and_cancel_wait_while_settings_are_saving(ui, page):
    dialog = open_settings(ui, page)
    ui.run("const base = fake.override; fake.override = (path, options) => path === '/api/settings' && options.method === 'PUT' "
           "? new Promise(() => {}) : base(path, options)")
    dialog.get_by_role("button", name="Save settings").click()
    expect(dialog.get_by_role("button", name="Saving…")).to_be_visible()
    expect(dialog.get_by_role("button", name="Close settings")).to_be_disabled()
    expect(dialog.get_by_role("button", name="Cancel", exact=True)).to_be_disabled()
    page.keyboard.press("Escape")
    page.mouse.click(4, 4)
    expect(dialog).to_be_visible()
