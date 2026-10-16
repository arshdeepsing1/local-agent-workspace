"""Reasoning effort and thinking in the UI: the composer's effort picker, Settings → Reasoning and its preview, the
endpoint chooser, the Settings section list, the Thinking block of a reply and the effort in Request details."""
import json
import re

from playwright.sync_api import expect

OPUS = "databricks-claude-opus-5-5"
GPT = "databricks-gpt-oss-120b"


def start_chat(ui, page, model=OPUS, events=None, setup="", **start):
    chat = f"fake.sessions.set('a', {{ ...makeSession('a', 'Effort chat'), model: {json.dumps(model)}, events: {json.dumps(events or [])} }})"
    ui.app(session="a", scenario="workspace", setup=chat + ";" + setup, **start)
    ui.show_session("a")
    expect(page.get_by_role("combobox", name="Model")).to_have_value(model)
    return page.get_by_role("combobox", name="Reasoning effort")


# The composer's effort picker

def test_the_picker_shows_the_level_in_use_and_saves_a_choice(ui, page):
    picker = start_chat(ui, page)
    expect(picker).to_have_value("default")
    expect(picker.locator("option:checked")).to_have_text("Default (Medium)")
    expect(picker.locator("option")).to_have_text(["Default (Medium)", "Fast", "Medium", "High", "Extra high", "Max"])
    picker.select_option("high")
    ui.wait("fake.requests('/api/sessions/a/reasoning-effort', 'PUT').length === 1")
    assert ui.bodies("/api/sessions/a/reasoning-effort", "PUT") == [{"reasoning_effort": "high"}]
    expect(picker).to_have_value("high")
    assert ui.js("fake.sessions.get('a').reasoning_effort") == "high"


def test_a_saved_level_shows_when_the_chat_opens_and_follows_pushed_changes(ui, page):
    picker = start_chat(ui, page, setup="fake.sessions.get('a').reasoning_effort = 'xhigh'")
    expect(picker).to_have_value("xhigh")
    ui.emit("a", {"type": "reasoning_effort", "reasoning_effort": "low"})
    expect(picker).to_have_value("low")


def test_the_picker_works_from_the_keyboard_and_while_the_agent_works(ui, page):
    picker = start_chat(ui, page)
    ui.emit("a", {"type": "status", "status": "running"})
    expect(page.get_by_role("button", name="Stop response")).to_be_visible()
    expect(picker).to_be_enabled()
    expect(picker).to_have_attribute("title", re.compile("applies from the agent's next model request"))
    picker.focus()
    # Typing a level's first letter chooses it on every platform (on macOS the arrow keys open the
    # menu of a closed select instead of stepping through it).
    page.keyboard.press("f")
    ui.wait("fake.requests('/api/sessions/a/reasoning-effort', 'PUT').length === 1")
    assert ui.bodies("/api/sessions/a/reasoning-effort", "PUT") == [{"reasoning_effort": "low"}]
    expect(picker).to_have_value("low")
    expect(picker).to_be_focused()  # Still on the picker: the next key press chooses again.
    page.wait_for_timeout(1100)  # Past the browser's type-ahead window, so "m" starts a new search.
    page.keyboard.press("m")
    ui.wait("fake.requests('/api/sessions/a/reasoning-effort', 'PUT').length === 2")
    expect(picker).to_have_value("medium")


def test_choices_made_while_one_saves_are_sent_in_turn(ui, page):
    picker = start_chat(ui, page, setup="""const first = fake.defer('first')
      fake.override = (path, options) => path === '/api/sessions/a/reasoning-effort' && fake.requests(path, 'PUT').length === 1
        ? first.promise.then(() => fake.json({ ...fake.sessions.get('a'), reasoning_effort: 'low' })) : undefined""")
    picker.select_option("low")
    ui.wait("fake.requests('/api/sessions/a/reasoning-effort', 'PUT').length === 1")
    picker.select_option("max")
    expect(picker).to_have_value("max")
    ui.run("fake.deferreds.first.resolve()")
    ui.wait("fake.requests('/api/sessions/a/reasoning-effort', 'PUT').length === 2")
    assert ui.bodies("/api/sessions/a/reasoning-effort", "PUT") == [{"reasoning_effort": "low"}, {"reasoning_effort": "max"}]
    expect(picker).to_have_value("max")


def test_a_failed_save_shows_the_saved_level_again(ui, page):
    picker = start_chat(ui, page, setup="""fake.override = (path, options) => path === '/api/sessions/a/reasoning-effort'
      ? fake.json({ detail: 'The conversation is being deleted.' }, 400) : undefined""")
    picker.select_option("max")
    expect(page.locator(".app-error")).to_contain_text("The conversation is being deleted.")
    expect(picker).to_have_value("default")


def test_a_new_conversation_is_created_with_its_chosen_level(ui, page):
    ui.app(scenario="workspace", setup=f"fake.settings = {{ ...fake.settings, model: '{OPUS}', reasoning_effort: 'medium' }}")
    picker = page.get_by_role("combobox", name="Reasoning effort")
    expect(picker).to_have_value("medium")  # The Settings default.
    picker.select_option("max")
    expect(picker).to_have_value("max")
    assert ui.requests("/api/sessions", "POST") == [] and ui.requests("/api/settings", "PUT") == []
    page.get_by_role("textbox", name="Message").fill("Plan the migration")
    page.get_by_role("textbox", name="Message").press("Enter")
    ui.wait("fake.requests('/api/sessions', 'POST').length === 1")
    assert ui.bodies("/api/sessions", "POST") == [{"permission_mode": "manual", "reasoning_effort": "max"}]


def test_gpt_oss_levels_say_what_is_sent_and_an_unknown_endpoint_is_unverified(ui, page):
    picker = start_chat(ui, page, model=GPT)
    expect(picker.locator("option")).to_have_text(["Default (Medium)", "Fast", "Medium", "High", "Extra high (sent as High)",
                                                    "Max (sent as High)"])
    ui.run("fake.socketFor('a').emit({ type: 'model', model: 'test-model' })")
    expect(picker.locator("option").first).to_have_text("Default")
    expect(picker).to_have_attribute("title", re.compile(r"This endpoint gets no reasoning fields \(Settings → Reasoning → Request format\)"))


def test_the_picker_fits_the_toolbar_at_360(ui, page):
    page.set_viewport_size({"width": 360, "height": 740})
    picker = start_chat(ui, page)
    expect(picker.locator("option:checked")).to_have_text("Default (Medium)")
    tops = page.locator(".composer-toolbar > :not(.composer-spacer)").evaluate_all(
        "els => els.map(el => Math.round(el.getBoundingClientRect().top))")
    assert max(tops) - min(tops) <= 4, tops
    assert ui.js("document.documentElement.scrollWidth <= innerWidth")
    box = picker.bounding_box()
    assert box["width"] >= 40 and box["x"] + box["width"] <= 360


# Settings → Reasoning

def open_settings(ui, page, setup=""):
    ui.app(scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_be_visible()
    page.get_by_role("button", name="Settings", exact=True).click()
    return page.get_by_role("dialog", name="Settings")


def preview(dialog):
    return dialog.locator(".settings-preview")


def test_the_reasoning_group_previews_the_fields_and_saves(ui, page):
    dialog = open_settings(ui, page, f"fake.settings = {{ ...fake.settings, model: '{OPUS}' }}")
    group = dialog.get_by_role("group", name="Reasoning", exact=True)
    effort = group.get_by_role("combobox", name="Default effort for new chats")
    expect(effort.locator("option:checked")).to_have_text("Default (Medium)")
    expect(preview(dialog)).to_contain_text("Added to agent requests to Claude Opus 5.5 at Default (Medium)")
    expect(preview(dialog).locator("pre")).to_have_text(json.dumps({"thinking": {"type": "adaptive", "display": "summarized"}}, indent=2))
    effort.select_option("high")
    expect(preview(dialog).locator("pre")).to_have_text(json.dumps(
        {"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": "high"}}, indent=2))
    group.get_by_role("checkbox", name="Show thinking summaries").click()
    expect(preview(dialog).locator("pre")).to_have_text(json.dumps(
        {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}, indent=2))
    request_format = group.get_by_role("combobox", name="Request format")
    expect(request_format.locator("option")).to_have_text([
        "Automatic (per endpoint)", "Anthropic fields (thinking + output_config.effort)", "OpenAI field (reasoning_effort)",
        "Don't send reasoning fields"])
    request_format.select_option("openai")
    expect(preview(dialog).locator("pre")).to_have_text(json.dumps({"reasoning_effort": "high"}, indent=2))
    request_format.select_option("none")
    expect(preview(dialog)).to_contain_text("No reasoning fields.")
    expect(preview(dialog).locator("pre")).to_have_count(0)
    # The server computes the preview: every change asked it with the form's values.
    assert ui.bodies("/api/settings/reasoning")[-1] == {"model": OPUS, "reasoning_format": "none", "thinking_summaries": False,
                                                        "reasoning_effort": "high"}
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    body = ui.bodies("/api/settings", "PUT")[0]
    assert (body["reasoning_effort"], body["reasoning_format"], body["thinking_summaries"]) == ("high", "none", False)


def test_the_preview_follows_the_endpoint_and_marks_an_unknown_one_unverified(ui, page):
    dialog = open_settings(ui, page)  # test-model: no profile.
    expect(preview(dialog)).to_contain_text("No reasoning profile for test-model")
    expect(preview(dialog)).to_contain_text("No reasoning fields.")
    dialog.get_by_label("Databricks model endpoint").select_option(GPT)
    dialog.get_by_role("combobox", name="Default effort for new chats").select_option("max")
    expect(preview(dialog).locator("pre")).to_have_text(json.dumps({"reasoning_effort": "high"}, indent=2))
    expect(dialog.get_by_role("combobox", name="Default effort for new chats").locator("option:checked")).to_have_text(
        "Max (sent as High)")


def test_other_endpoints_stay_choosable_after_choosing_one(ui, page):
    # Reported: once an endpoint was chosen, the list showed only that one (a filtered datalist).
    dialog = open_settings(ui, page)
    chooser = dialog.get_by_label("Databricks model endpoint")
    expect(chooser).to_have_js_property("tagName", "SELECT")
    options = ["Claude Opus 5.5 · databricks-claude-opus-5-5", "Claude Opus 4.8 · databricks-claude-opus-4-8",
               "GPT OSS 120B · databricks-gpt-oss-120b", "GPT OSS 20B · databricks-gpt-oss-20b", "Test model · test-model",
               "Other model · other-model", "Latest model · latest-model", "Other endpoint…"]
    expect(chooser.locator("option")).to_have_text(options)
    chooser.select_option(OPUS)
    expect(chooser.locator("option")).to_have_text(options)
    chooser.select_option("other-model")
    expect(chooser).to_have_value("other-model")
    expect(dialog.get_by_role("textbox", name="Endpoint name")).to_have_count(0)
    chooser.select_option(label="Other endpoint…")
    name = dialog.get_by_role("textbox", name="Endpoint name")
    expect(name).to_be_focused()
    name.fill("databricks-claude-opus-5-5")  # Typing a listed name keeps the typed field open.
    expect(name).to_be_visible()
    name.fill("my-endpoint")
    dialog.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert ui.bodies("/api/settings", "PUT")[0]["model"] == "my-endpoint"


def test_a_saved_endpoint_without_a_profile_or_listing_shows_in_the_list(ui, page):
    dialog = open_settings(ui, page, "fake.settings = { ...fake.settings, model: 'custom-endpoint' }")
    chooser = dialog.get_by_label("Databricks model endpoint")
    expect(chooser).to_have_value("custom-endpoint")
    expect(chooser.locator("option:checked")).to_have_text("Custom endpoint · custom-endpoint")


def test_the_section_list_jumps_to_a_section(ui, page):
    dialog = open_settings(ui, page)
    nav = dialog.get_by_role("navigation", name="Settings sections")
    expect(nav.get_by_role("button")).to_have_text(["Project", "Model and context", "Reasoning", "Agent", "Subagents",
                                                    "Commands and files", "Notifications", "Credentials"])
    expect(nav.get_by_role("button", name="Project")).to_have_attribute("aria-current", "true")
    nav.get_by_role("button", name="Reasoning").click()
    expect(nav.get_by_role("button", name="Reasoning")).to_have_attribute("aria-current", "true")
    expect(nav.get_by_role("button", name="Project")).not_to_have_attribute("aria-current", "true")
    legend = dialog.get_by_role("group", name="Reasoning", exact=True).locator("legend")
    expect(legend).to_be_in_viewport()
    assert legend.bounding_box()["y"] - dialog.bounding_box()["y"] < 120  # At the top of the dialog.
    expect(nav).to_be_in_viewport()  # The list stays beside the sections.
    expect(dialog.get_by_role("group", name="Reasoning", exact=True)).to_be_focused()
    page.keyboard.press("Tab")
    expect(dialog.get_by_role("combobox", name="Default effort for new chats")).to_be_focused()
    # Scrolling back to the top marks Project again.
    page.wait_for_timeout(350)
    ui.run("document.querySelector('.settings-dialog').scrollTop = 0")
    expect(nav.get_by_role("button", name="Project")).to_have_attribute("aria-current", "true")


def test_a_phone_shows_one_column_without_the_section_list(ui, page):
    page.set_viewport_size({"width": 390, "height": 844})
    ui.app(scenario="workspace")
    page.get_by_role("button", name="Open sidebar").click()
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.get_by_role("dialog", name="Settings")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("navigation", name="Settings sections")).to_be_hidden()
    assert ui.js("(d => d.scrollWidth - d.clientWidth)(document.querySelector('.settings-dialog'))") == 0
    field = dialog.get_by_role("textbox", name="Project folder")
    assert field.bounding_box()["width"] > 300


def test_settings_type_scale(ui, page):
    expect(open_settings(ui, page)).to_be_visible()
    sizes = ui.js("""(d => ({
      title: getComputedStyle(d.querySelector('h2')).fontSize, family: getComputedStyle(d.querySelector('h2')).fontFamily,
      legend: getComputedStyle(d.querySelector('fieldset > legend')).fontSize,
      label: getComputedStyle(d.querySelector('fieldset label')).fontSize,
      input: getComputedStyle(d.querySelector('fieldset input')).fontSize,
      help: getComputedStyle(d.querySelector('fieldset .field-help')).fontSize,
      helpLine: getComputedStyle(d.querySelector('fieldset .field-help')).lineHeight,
      helpColor: getComputedStyle(d.querySelector('fieldset .field-help')).color,
      width: d.getBoundingClientRect().width }))(document.querySelector('.settings-dialog'))""")
    assert sizes["family"].startswith('"DM Sans"'), sizes
    assert 20 <= float(sizes["title"][:-2]) <= 22 and 14 <= float(sizes["legend"][:-2]) <= 15, sizes
    assert 13 <= float(sizes["label"][:-2]) <= 14 and 13 <= float(sizes["input"][:-2]) <= 14, sizes
    assert 12 <= float(sizes["help"][:-2]) <= 13 and abs(float(sizes["helpLine"][:-2]) / float(sizes["help"][:-2]) - 1.45) < .06
    assert sizes["helpColor"] == "rgb(102, 102, 97)"  # --muted
    assert 760 <= sizes["width"] <= 880, sizes


def test_long_help_waits_behind_more(ui, page):
    dialog = open_settings(ui, page)
    detail = dialog.get_by_text(re.compile("^It uses notify-send on Linux and osascript on macOS"))
    expect(detail).to_be_hidden()
    more = dialog.get_by_role("group", name="Notifications", exact=True).locator("summary")
    expect(more).to_have_text("More")
    expect(more).to_have_accessible_name("More about desktop notifications")
    more.click()
    expect(detail).to_be_visible()


# The Thinking block

def thinking(page):
    return page.locator("details.thinking")


def test_thinking_opens_while_the_model_thinks_and_collapses_when_the_answer_starts(ui, page):
    start_chat(ui, page, events=[{"id": "u", "type": "user", "text": "Explain the parser"}],
               setup="fake.sessions.get('a').status = 'running'", clock=True)
    ui.run("""fake.socketFor('a').emit({ type: 'event', event: { id: 'r', type: 'assistant', text: '',
      request_info: { model: 'databricks-claude-opus-5-5', status: 'running' } } })""")
    expect(page.locator(".thinking, .thinking-line")).to_have_count(0)  # No reasoning yet: nothing invented.
    ui.run("""window.started = Date.now() / 1000 - 12
      fake.socketFor('a').emit({ type: 'event', event: { id: 'r', type: 'assistant', text: '', reasoning_started: started,
        reasoning_summary: 'Reading **parser.py** first.', request_info: { model: 'databricks-claude-opus-5-5', status: 'running' } } })""")
    block = thinking(page)
    expect(block).to_have_attribute("open", "")
    expect(block.locator("summary")).to_have_text("Thinking… 12 s")
    expect(block.locator(".markdown strong")).to_have_text("parser.py")
    ui.tick(3000)
    expect(block.locator("summary")).to_have_text("Thinking… 15 s")
    # The server ends the thinking time when the answer starts; then the text streams in.
    ui.run("""fake.socketFor('a').emit({ type: 'event', event: { id: 'r', type: 'assistant', text: '', reasoning_started: started,
      reasoning_seconds: 15.2, reasoning_summary: 'Reading **parser.py** first.',
      request_info: { model: 'databricks-claude-opus-5-5', status: 'running' } } })""")
    ui.emit("a", {"type": "delta", "id": "r", "text": "The parser reads tokens."})
    expect(block).not_to_have_attribute("open", "")
    expect(block.locator("summary")).to_have_text("Thought for 15 s")
    expect(page.get_by_text("The parser reads tokens.")).to_be_visible()
    ui.tick(5000)
    expect(block.locator("summary")).to_have_text("Thought for 15 s")


def test_text_without_a_server_duration_collapses_too(ui, page):
    start_chat(ui, page, events=[{"id": "u", "type": "user", "text": "Go"}], setup="fake.sessions.get('a').status = 'running'",
               clock=True)
    ui.run("""fake.socketFor('a').emit({ type: 'event', event: { id: 'r', type: 'assistant', text: '',
      reasoning_started: Date.now() / 1000 - 4, reasoning_summary: 'Plan.', request_info: { model: 'm', status: 'running' } } })""")
    expect(thinking(page)).to_have_attribute("open", "")
    ui.emit("a", {"type": "delta", "id": "r", "text": "Done."})
    expect(thinking(page)).not_to_have_attribute("open", "")
    expect(thinking(page).locator("summary")).to_have_text("Thought for 4 s")


def test_the_users_choice_is_kept(ui, page):
    start_chat(ui, page, events=[{"id": "u", "type": "user", "text": "Go"}], setup="fake.sessions.get('a').status = 'running'")
    event = {"id": "r", "type": "assistant", "text": "", "reasoning_started": 1_700_000_000, "reasoning_summary": "First idea.",
             "request_info": {"model": OPUS, "status": "running"}}
    ui.emit("a", {"type": "event", "event": event})
    block = thinking(page)
    expect(block).to_have_attribute("open", "")
    block.locator("summary").click()  # Closed while it thinks: more thinking does not reopen it.
    expect(block).not_to_have_attribute("open", "")
    ui.emit("a", {"type": "event", "event": {**event, "reasoning_summary": "First idea. Second idea."}})
    expect(block).not_to_have_attribute("open", "")
    block.locator("summary").focus()  # Opened from the keyboard after the answer: it stays open.
    ui.emit("a", {"type": "event", "event": {**event, "reasoning_seconds": 3, "reasoning_summary": "First idea. Second idea."}})
    ui.emit("a", {"type": "delta", "id": "r", "text": "Answer."})
    page.keyboard.press("Enter")
    expect(block).to_have_attribute("open", "")
    ui.emit("a", {"type": "event", "event": {**event, "text": "Answer.", "reasoning_seconds": 3,
                                             "reasoning_summary": "First idea. Second idea.",
                                             "request_info": {"model": OPUS, "status": "completed", "finish_reason": "stop"}}})
    expect(block).to_have_attribute("open", "")
    expect(block.get_by_text("First idea. Second idea.")).to_be_visible()


def test_a_saved_reply_shows_how_long_it_thought(ui, page):
    events = [{"id": "u", "type": "user", "text": "Go"},
              {"id": "r", "type": "assistant", "text": "Answer.", "reasoning_summary": "Checked the **tests**.",
               "reasoning_started": 1_700_000_000.0, "reasoning_seconds": 12.4, "reasoning_wait_seconds": 9,
               "request_info": {"model": OPUS, "status": "completed", "finish_reason": "stop"}}]
    start_chat(ui, page, events=events)
    block = thinking(page)
    expect(block).not_to_have_attribute("open", "")
    expect(block.locator("summary")).to_have_text("Thought for 12 s")  # From the first thinking text, as before.
    # Request details say when that text arrived, after the request was sent.
    page.get_by_text("Request details · completed").click()
    expect(page.locator('xpath=//dt[normalize-space()="Thinking text began"]/following-sibling::dd[1]')).to_have_text(
        "after 9.0 s")
    expect(block.locator(".markdown")).to_be_hidden()
    block.locator("summary").click()
    expect(block.locator(".markdown strong")).to_have_text("tests")
    # Thinking text is smaller and muted, the answer is not.
    styles = page.evaluate("""() => [document.querySelector('.thinking .markdown'), document.querySelector('.reply-content > .markdown')]
      .map(el => [getComputedStyle(el).fontSize, getComputedStyle(el).color])""")
    assert styles[0] == ["13px", "rgb(102, 102, 97)"] and float(styles[1][0][:-2]) > 13, styles


def test_thinking_without_a_summary_is_one_quiet_line(ui, page):
    events = [{"id": "u", "type": "user", "text": "Go"},
              {"id": "r", "type": "assistant", "text": "Answer.", "reasoning_seconds": 4.2,
               "request_info": {"model": OPUS, "status": "completed", "usage": {"output_tokens": 300, "reasoning_tokens": 200}}},
              {"id": "u2", "type": "user", "text": "Again"},
              {"id": "r2", "type": "assistant", "text": "Answer two.",
               "request_info": {"model": OPUS, "status": "completed", "usage": {"output_tokens": 30, "reasoning_tokens": 20}}},
              {"id": "u3", "type": "user", "text": "Plain"},
              {"id": "r3", "type": "assistant", "text": "No thinking.", "request_info": {"model": OPUS, "status": "completed"}}]
    start_chat(ui, page, events=events)
    expect(thinking(page)).to_have_count(0)
    expect(page.locator(".thinking-line")).to_have_text(["Thought for 4 s · the endpoint did not return a summary",
                                                         "Thought · the endpoint did not return a summary"])


def test_request_details_name_the_effort_and_the_fields(ui, page):
    reasoning = {"effort": "high", "format": "anthropic", "sent_effort": "high", "effort_field": "output_config.effort",
                 "fields": {"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": "high"}}}
    rejected = {"effort": "max", "format": "anthropic", "fields": {}, "skipped": "rejected", "rejected_field": "output_config.effort"}
    mapped = {"effort": "xhigh", "format": "openai", "sent_effort": "high", "effort_field": "reasoning_effort",
              "fields": {"reasoning_effort": "high"}}
    events = [{"id": f"r{n}", "type": "assistant", "text": f"Answer {n}.",
               "request_info": {"model": OPUS, "status": "completed", "reasoning": value}}
              for n, value in enumerate([reasoning, rejected, mapped])]
    start_chat(ui, page, events=events)
    for summary in page.get_by_text("Request details · completed").all():
        summary.click()
    rows = page.locator('xpath=//dt[normalize-space()="Effort"]/following-sibling::dd[1]')
    expect(rows).to_have_text(["High (output_config.effort)",
                               "Max (not sent: Databricks rejected output_config.effort, so the request was sent again "
                               "without reasoning fields)", "Extra high (reasoning_effort as high)"])
    fields = page.locator('xpath=//dt[normalize-space()="Reasoning fields"]/following-sibling::dd[1]')
    expect(fields).to_have_text([json.dumps(reasoning["fields"], separators=(",", ":")),
                                 json.dumps(mapped["fields"], separators=(",", ":"))])
    # No thinking was timed on these replies: no row about when it began.
    expect(page.locator("dt", has_text=re.compile("^Thinking"))).to_have_count(0)
