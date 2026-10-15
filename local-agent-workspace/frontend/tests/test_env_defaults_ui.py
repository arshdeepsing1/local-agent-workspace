"""The default code folder and Python environment in Settings, Folder access, Files and the Terminal;
live endpoint limit warnings; and Claude Opus 5.5 as the default model."""
import re

from playwright.sync_api import expect

OPUS = "databricks-claude-opus-5-5"


def status(state, path, message, source="settings", **extra):
    return {"path": path, "state": state, "message": message, "source": source, **extra}


def settings_setup(**values):
    """Setup that adds values (a JavaScript object literal body) to fake.settings."""
    body = ", ".join(f"{key}: {value}" for key, value in values.items())
    return f"fake.settings = {{ ...fake.settings, {body} }}"


def choose_endpoint(dialog, name):
    """Choose an endpoint in Settings: from the list, or typed after Other endpoint…."""
    chooser = dialog.get_by_label("Databricks model endpoint")
    if chooser.locator(f'option[value="{name}"]').count():
        chooser.select_option(name)
    else:
        chooser.select_option(label="Other endpoint…")
        dialog.get_by_role("textbox", name="Endpoint name").fill(name)


def open_settings(ui, page, setup="", clock=False):
    ui.app(scenario="workspace", setup=setup, clock=clock)
    expect(page.get_by_role("combobox", name="Model")).to_be_visible()
    page.get_by_role("button", name="Settings", exact=True).click()
    return page.get_by_role("dialog")


READY_VENV = ("{ path: '/srv/venvs/v', state: 'ready', message: 'Ready: commands use /srv/venvs/v/bin/python (Python 3.12.4).', "
              "python: '/srv/venvs/v/bin/python', version: '3.12.4', source: 'env' }")


def test_the_two_folders_render_and_are_saved(ui, page):
    dialog = open_settings(ui, page, settings_setup(
        coding_dir="'/srv/code'", python_venv="'/srv/venvs/v'", python_venv_status=READY_VENV,
        coding_dir_status="{ path: '/srv/code', state: 'ready', message: 'Ready: every conversation can use /srv/code.', source: 'settings' }"))
    coding = dialog.get_by_role("textbox", name="Default folder for new code")
    venv = dialog.get_by_role("textbox", name="Python virtual environment")
    expect(coding).to_have_value("/srv/code")
    expect(coding).to_have_attribute("placeholder", "~/code (optional)")
    expect(venv).to_have_value("/srv/venvs/v")
    expect(venv).to_have_attribute("placeholder", "~/.venvs/project (optional)")
    expect(dialog.get_by_text(re.compile("the agent creates it here; work on the open project stays in the project"))).to_be_visible()
    expect(dialog.get_by_text(re.compile("VIRTUAL_ENV is set and its bin/ comes first on PATH"))).to_be_visible()
    coding.fill("/srv/new-code")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    body = ui.bodies("/api/settings", "PUT")[0]
    assert body["coding_dir"] == "/srv/new-code" and body["python_venv"] == "/srv/venvs/v"


def test_folder_statuses(ui, page):
    dialog = open_settings(ui, page, settings_setup(
        coding_dir="'/srv/state/x'", python_venv="'/srv/venvs/v'", python_venv_status=READY_VENV,
        coding_dir_status="{ path: '/srv/state/x', state: 'invalid', message: \"Not used: This is the app's state folder or inside it, which holds conversations and settings.\", source: 'settings' }"))
    expect(dialog.get_by_role("alert")).to_have_text(re.compile("^Not used: This is the app's state folder"))
    expect(dialog.get_by_text("Ready: commands use /srv/venvs/v/bin/python (Python 3.12.4). From .env.")).to_be_visible()
    coding = dialog.get_by_role("textbox", name="Default folder for new code")
    coding.fill("/srv/other")
    expect(dialog.get_by_text("Checked when you save.")).to_be_visible()
    expect(dialog.get_by_role("alert")).to_have_count(0)
    coding.fill("/srv/state/x")
    expect(dialog.get_by_role("alert")).to_be_visible()


def test_a_missing_folder_and_statuses_refreshed_when_the_dialog_opens(ui, page):
    missing = "{ path: '/srv/code', state: 'missing', message: \"/srv/code does not exist yet; the agent's first file there creates it.\", source: 'env' }"
    ready = "{ path: '/srv/code', state: 'ready', message: 'Ready: every conversation can use /srv/code.', source: 'env' }"
    # The page loaded while the folder was missing; the agent created it since.
    setup = (settings_setup(coding_dir="'/srv/code'", coding_dir_status=missing)
             + "; fake.override = (path, options) => path === '/api/settings' && (options.method || 'GET') === 'GET'"
             " ? fake.json({ ...fake.settings, coding_dir_status: window.nextStatus || fake.settings.coding_dir_status }) : undefined")
    dialog = open_settings(ui, page, setup)
    expect(dialog.get_by_role("status").filter(has_text="does not exist yet")).to_be_visible()
    dialog.get_by_role("button", name="Cancel").click()
    ui.run(f"window.nextStatus = {ready}")
    page.get_by_role("button", name="Settings", exact=True).click()
    expect(page.get_by_role("dialog").get_by_text("Ready: every conversation can use /srv/code. From .env.")).to_be_visible()


def test_live_limits_follow_the_latest_values(ui, page):
    setup = """const first = fake.defer('first')
      fake.override = (path, options) => {
        if (path !== '/api/settings/limits') return undefined
        const body = JSON.parse(options.body)
        if (body.model === 'databricks-claude-opus-5-5' && body.context_window === 131072) return first.promise
      }"""
    dialog = open_settings(ui, page, setup, clock=True)
    ui.tick(300)
    ui.wait("fake.requests('/api/settings/limits').length === 1")
    expect(dialog.get_by_text("No built-in limits for this endpoint.", exact=False)).to_be_visible()
    budget = dialog.get_by_label("Context budget (tokens)")
    output = dialog.get_by_label("Max output tokens per request")
    choose_endpoint(dialog, OPUS)
    ui.tick(300)
    ui.wait("fake.requests('/api/settings/limits').length === 2")  # Held: answered later.
    budget.fill("300000")
    output.fill("30000")
    ui.tick(100)
    assert len(ui.requests("/api/settings/limits")) == 2  # Still waiting for the pause.
    ui.tick(200)
    ui.wait("fake.requests('/api/settings/limits').length === 3")
    assert ui.bodies("/api/settings/limits")[-1] == {"model": OPUS, "context_window": 300000, "max_output_tokens": 30000}
    expect(dialog.get_by_role("status").filter(has_text="Above 20,000, four requests at once")).to_be_visible()
    expect(dialog.get_by_text("Above 250,000 tokens", exact=False)).to_be_visible()
    # The earlier request answers late: it does not replace the later answer.
    ui.run("fake.deferreds.first.resolve(fake.json(limitsCheck({ model: 'databricks-gpt-oss-120b', context_window: 131072, max_output_tokens: 8192 })))")
    page.wait_for_timeout(100)
    expect(dialog.get_by_text("Above 250,000 tokens", exact=False)).to_be_visible()
    expect(dialog.get_by_text("No built-in limits for this endpoint.", exact=False)).to_have_count(0)
    # Values outside the Settings ranges are not sent, and the warnings go away.
    budget.fill("1")
    ui.tick(400)
    assert len(ui.requests("/api/settings/limits")) == 3
    expect(dialog.get_by_text("Above 250,000 tokens", exact=False)).to_have_count(0)
    output.fill("299000")
    budget.fill("300000")
    ui.tick(400)
    assert len(ui.requests("/api/settings/limits")) == 3  # Output not below the context budget minus 2,048.


def test_the_recommended_defaults_button(ui, page):
    # Reported: the button went away as soon as it was clicked, with no feedback. It stays, confirmed, and comes
    # back when the values differ again.
    dialog = open_settings(ui, page, settings_setup(model=f"'{OPUS}'"))
    button = dialog.locator(".settings-recommended")
    expect(button).to_have_accessible_name("Use Claude Opus 5.5 defaults (200,000 context, 16,384 output)")
    expect(dialog.get_by_text(re.compile("^Limits for Claude Opus 5.5 are Databricks's documented"))).to_be_visible()
    button.focus()
    page.keyboard.press("Enter")
    expect(dialog.get_by_label("Context budget (tokens)")).to_have_value("200000")
    expect(dialog.get_by_label("Max output tokens per request")).to_have_value("16384")
    expect(button).to_have_accessible_name("Using Claude Opus 5.5 defaults (200,000 context, 16,384 output)")
    expect(button).to_have_attribute("aria-disabled", "true")
    expect(button).to_be_visible()
    expect(button).to_be_focused()  # Still where you pressed it.
    page.keyboard.press("Enter")  # Pressed again: nothing changes.
    expect(dialog.get_by_label("Context budget (tokens)")).to_have_value("200000")
    page.wait_for_timeout(400)  # The limits check after the change keeps it.
    expect(button).to_have_accessible_name("Using Claude Opus 5.5 defaults (200,000 context, 16,384 output)")
    dialog.get_by_label("Context budget (tokens)").fill("250000")
    expect(button).to_have_accessible_name("Use Claude Opus 5.5 defaults (200,000 context, 16,384 output)")
    expect(button).not_to_have_attribute("aria-disabled", "true")


def test_budgets_at_the_defaults_follow_the_endpoint_field_as_save_does(ui, page):
    dialog = open_settings(ui, page)  # test-model at its defaults, 131,072 / 8,192.
    budget = dialog.get_by_label("Context budget (tokens)")
    output = dialog.get_by_label("Max output tokens per request")
    choose_endpoint(dialog, OPUS)
    expect(budget).to_have_value("200000")
    expect(output).to_have_value("16384")
    expect(dialog.get_by_role("button", name=re.compile("^Use Claude Opus 5.5 defaults"))).to_have_count(0)
    expect(dialog.get_by_role("button", name=re.compile("^Using Claude Opus 5.5 defaults"))).to_be_visible()
    expect(dialog.get_by_text("Current estimated input budget: 181,568 tokens.", exact=False)).to_be_visible()
    choose_endpoint(dialog, "test-model")  # The saved endpoint again: the saved budgets.
    expect(budget).to_have_value("131072")
    expect(output).to_have_value("8192")
    choose_endpoint(dialog, OPUS)
    expect(output).to_have_value("16384")
    output.fill("12000")  # Edited: it stays; the context budget still follows.
    choose_endpoint(dialog, "other-endpoint")
    expect(budget).to_have_value("131072")
    page.wait_for_timeout(400)
    expect(output).to_have_value("12000")
    choose_endpoint(dialog, OPUS)
    expect(budget).to_have_value("200000")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    body = ui.bodies("/api/settings", "PUT")[0]
    assert (body["model"], body["context_window"], body["max_output_tokens"]) == (OPUS, 200000, 12000)


def test_custom_budgets_do_not_follow_the_endpoint_field(ui, page):
    dialog = open_settings(ui, page, settings_setup(context_window="65536"))
    choose_endpoint(dialog, OPUS)
    ui.wait("fake.requests('/api/settings/limits').some(request => JSON.parse(request.body).model === 'databricks-claude-opus-5-5')")
    expect(dialog.get_by_role("button", name=re.compile("^Use Claude Opus 5.5 defaults"))).to_be_visible()
    expect(dialog.get_by_label("Context budget (tokens)")).to_have_value("65536")
    expect(dialog.get_by_label("Max output tokens per request")).to_have_value("16384")  # At its default: follows.


def test_the_input_budget_names_what_a_capped_endpoint_gets(ui, page):
    dialog = open_settings(ui, page, settings_setup(model=f"'{OPUS}'"))
    dialog.get_by_label("Context budget (tokens)").fill("1048576")
    dialog.get_by_label("Max output tokens per request").fill("131072")
    expect(dialog.get_by_text("Current estimated input budget: 915,456 tokens (869,952 for requests to this endpoint, "
                              "which accepts less).", exact=False)).to_be_visible()
    dialog.get_by_label("Context budget (tokens)").fill("300000")
    expect(dialog.get_by_text("Current estimated input budget: 166,880 tokens (169,952 for requests to this endpoint, "
                              "which accepts less).", exact=False)).to_be_visible()  # Output capped at 128,000.
    dialog.get_by_label("Max output tokens per request").fill("16384")
    expect(dialog.get_by_text("Current estimated input budget: 281,568 tokens.", exact=False)).to_be_visible()


def test_the_auto_compact_hint_names_what_a_capped_endpoint_gets(ui, page):
    dialog = open_settings(ui, page, settings_setup(model=f"'{OPUS}'"))
    dialog.get_by_label("Context budget (tokens)").fill("1048576")
    dialog.get_by_label("Max output tokens per request").fill("16384")
    expect(dialog.get_by_text("With these settings that is about 927,129 tokens (883,411 for requests to this "
                              "endpoint).", exact=False)).to_be_visible()
    dialog.get_by_label("Max output tokens per request").fill("131072")
    expect(dialog.get_by_text("that is about 823,910 tokens (782,956 for requests to this endpoint).",
                              exact=False)).to_be_visible()
    dialog.get_by_label("Context budget (tokens)").fill("300000")
    expect(dialog.get_by_text("that is about 150,192 tokens (152,956 for requests to this endpoint).",
                              exact=False)).to_be_visible()  # Output capped at 128,000.
    dialog.get_by_label("Max output tokens per request").fill("16384")
    expect(dialog.get_by_text("With these settings that is about 253,411 tokens. Lower", exact=False)).to_be_visible()


def test_the_migration_note(ui, page):
    dialog = open_settings(ui, page, settings_setup(model=f"'{OPUS}'", model_migrated_from="'databricks-gpt-oss-120b'"))
    expect(dialog.get_by_role("status").filter(has_text="The default model changed")).to_have_text(
        "The default model changed from GPT OSS 120B to Claude Opus 5.5, the app's new default. Claude Opus 5.5 costs "
        "more per token. To switch back, choose databricks-gpt-oss-120b here and save, or in the model picker of a new "
        "conversation (in an open conversation the picker changes only that conversation).")
    chooser = dialog.get_by_label("Databricks model endpoint")
    expect(chooser).to_have_value(OPUS)
    expect(chooser.locator("option:checked")).to_have_text(f"Claude Opus 5.5 · {OPUS}")


def test_no_migration_note_without_a_migration(ui, page):
    dialog = open_settings(ui, page)
    expect(dialog.get_by_text("The default model changed", exact=False)).to_have_count(0)


def test_folder_access_lists_the_default_code_folder(ui, page):
    setup = settings_setup(coding_dir="'/srv/code'", coding_dir_status=(
        "{ path: '/srv/code', state: 'missing', message: '/srv/code does not exist yet.', source: 'settings' }"))
    setup += "; fake.override = path => path === '/api/folder-access/check' ? fake.json({ accessible: true, path: '/srv/code/tool' }) : undefined"
    ui.app(session="a", scenario="workspace", setup=setup)
    ui.show_session("a")
    page.get_by_role("button", name="Folder access").click()
    dialog = page.get_by_role("dialog")
    expect(dialog.locator(".folder-grant").nth(1)).to_have_text(
        "/srv/codeDefault code folder · all conversations · created on first write")
    expect(dialog.get_by_role("button", name="Remove access to /srv/code")).to_have_count(0)
    expect(dialog.get_by_text("The default code folder comes from Settings; change it there.", exact=False)).to_be_visible()
    assert ui.requests("/api/settings", "GET")  # Refreshed on open.
    dialog.get_by_role("textbox").first.fill("/srv/code/tool")
    dialog.get_by_role("button", name="Check access").click()
    expect(dialog.get_by_text("Allowed for this conversation.")).to_be_visible()
    expect(dialog.get_by_role("button", name="Allowed")).to_be_disabled()


def test_folder_access_hides_a_default_folder_inside_the_project(ui, page):
    setup = settings_setup(coding_dir_status="{ path: '/project/tools', state: 'ready', message: 'Ready.', source: 'settings' }")
    ui.app(session="a", scenario="workspace", setup=setup)
    ui.show_session("a")
    page.get_by_role("button", name="Folder access").click()
    expect(page.get_by_role("dialog").get_by_text("Default code folder", exact=False)).to_have_count(0)


def test_files_shows_the_default_folder_and_its_first_write(ui, page):
    setup = settings_setup(coding_dir="'/srv/code'", coding_dir_status=(
        "{ path: '/srv/code', state: 'missing', message: '/srv/code does not exist yet.', source: 'settings' }"))
    ui.app(session="a", scenario="workspace", setup=setup)
    ui.show_session("a")
    page.get_by_role("button", name=re.compile("^Workspace")).click()
    group = page.get_by_role("group", name="Folders in Files")
    root = group.locator('.file-roots-root[data-root="/srv/code"]')
    expect(root).to_have_accessible_name("Default code folder: /srv/code, Created on first write")
    expect(root).to_be_disabled()
    expect(root).to_contain_text("Created on first write")
    expect(group.locator(".file-roots-remove")).to_have_count(0)
    ui.emit("a", "{ type: 'changes', changes: makeChanges(changedFile('/srv/code/tool/main.py', 3, 0, 'added')) }")
    expect(root).to_have_accessible_name("Default code folder: /srv/code, 1 changed file")
    expect(root).to_be_enabled()
    expect(root).to_contain_text("Default code folder")
    expect(page.get_by_text(re.compile("outside these folders"))).to_have_count(0)


def test_the_default_folder_stays_browsable_after_its_first_changes_are_kept(ui, page):
    missing = "{ path: '/srv/code', state: 'missing', message: '/srv/code does not exist yet.', source: 'settings' }"
    ready = "{ path: '/srv/code', state: 'ready', message: 'Ready: every conversation can use /srv/code.', source: 'settings' }"
    ui.app(session="a", scenario="workspace", setup=settings_setup(coding_dir="'/srv/code'", coding_dir_status=missing))
    ui.show_session("a")
    page.get_by_role("button", name=re.compile("^Workspace")).click()
    root = page.get_by_role("group", name="Folders in Files").locator('.file-roots-root[data-root="/srv/code"]')
    expect(root).to_be_disabled()
    ui.run(f"fake.settings = {{ ...fake.settings, coding_dir_status: {ready} }}")  # The agent's write creates it.
    ui.emit("a", "{ type: 'changes', changes: makeChanges(changedFile('/srv/code/tool/main.py', 3, 0, 'added')) }")
    expect(root).to_be_enabled()
    ui.emit("a", "{ type: 'changes', changes: makeChanges() }")  # Kept (or Automatic Keep).
    expect(root).to_have_accessible_name("Default code folder: /srv/code")
    expect(root).to_be_enabled()


def test_a_reply_that_creates_the_default_folder_with_a_command_makes_it_browsable(ui, page):
    missing = "{ path: '/srv/code', state: 'missing', message: '/srv/code does not exist yet.', source: 'settings' }"
    ready = "{ path: '/srv/code', state: 'ready', message: 'Ready: every conversation can use /srv/code.', source: 'settings' }"
    ui.app(session="a", scenario="workspace", setup=settings_setup(coding_dir="'/srv/code'", coding_dir_status=missing))
    ui.show_session("a")
    page.get_by_role("button", name=re.compile("^Workspace")).click()
    root = page.get_by_role("group", name="Folders in Files").locator('.file-roots-root[data-root="/srv/code"]')
    expect(root).to_be_disabled()
    ui.emit("a", "{ type: 'status', status: 'running' }")
    ui.run(f"fake.settings = {{ ...fake.settings, coding_dir_status: {ready} }}")  # mkdir by a command: no change row.
    ui.emit("a", "{ type: 'status', status: 'idle' }")
    expect(root).to_be_enabled()


def test_the_default_folder_root_can_be_browsed(ui, page):
    ready = "{ path: '/srv/code', state: 'ready', message: 'Ready: every conversation can use /srv/code.', source: 'settings' }"
    ui.app(session="a", scenario="workspace", setup=settings_setup(coding_dir="'/srv/code'", coding_dir_status=ready))
    ui.show_session("a")
    page.get_by_role("button", name=re.compile("^Workspace")).click()
    root = page.get_by_role("group", name="Folders in Files").locator('.file-roots-root[data-root="/srv/code"]')
    expect(root).to_have_accessible_name("Default code folder: /srv/code")
    root.click()
    expect(root).to_have_attribute("aria-pressed", "true")
    ui.wait("fake.calls.some(call => call.path.startsWith('/api/files?path=%2Fsrv%2Fcode&'))")
    page.wait_for_timeout(200)
    expect(root).to_have_attribute("aria-pressed", "true")  # Not reset to the project.


def test_the_terminal_names_the_python_environment(ui, page):
    ui.mount("components/TerminalPanel.js", "{ sessionId: 'a', workspace: '/project', pythonVenv: { state: 'ready', path: '/srv/venvs/v' } }",
             scenario="terminal", clock=True)
    expect(page.get_by_text("Commands use the Python environment /srv/venvs/v.")).to_be_visible()
    ui.rerender("{ sessionId: 'a', workspace: '/project', pythonVenv: { state: 'invalid', path: '/srv/venvs/v', "
                "message: 'Not used: No bin/python here; choose the environment\\'s folder.' } }")
    expect(page.get_by_text("The Python environment in Settings is not used: No bin/python here; choose the environment's folder.")).to_be_visible()
    ui.rerender("{ sessionId: 'a', workspace: '/project', pythonVenv: { state: 'off' } }")
    expect(page.get_by_text(re.compile("Python environment"))).to_have_count(0)


def test_the_composer_names_opus_5_5(ui, page):
    ui.app(scenario="workspace", setup=settings_setup(model=f"'{OPUS}'"))
    model = page.get_by_role("combobox", name="Model")
    expect(model).to_have_value(OPUS)
    expect(model.locator("option:checked")).to_have_text("Claude Opus 5.5")


def test_the_composer_falls_back_to_the_default_model(ui, page):
    ui.app(scenario="workspace", setup=settings_setup(model="''"))
    model = page.get_by_role("combobox", name="Model")
    expect(model).to_have_value(OPUS)
    expect(model.locator("option:checked")).to_have_text("Claude Opus 5.5")
    assert ui.js("import('/static/js/types.js').then(m => [m.modelLabel('databricks-gpt-oss-120b'), m.modelLabel('databricks-claude-opus-4-8'), m.modelLabel('databricks-meta-llama-3-1-70b-instruct')])") == [
        "GPT OSS 120B", "Claude Opus 4.8", "Meta Llama 3.1 70B instruct"]
