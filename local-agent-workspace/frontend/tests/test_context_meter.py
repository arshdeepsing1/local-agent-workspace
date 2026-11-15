import re

import pytest
from playwright.sync_api import expect

INFO = """{ estimated_tokens: 6000, input_budget: 22768, context_window: 33008, reply_reserve: 8192,
  compactions: 1, summarized_messages: 4, estimate_method: 'weighted_utf8', instruction_files: ['AGENTS.md'], warnings: [] }"""
CONTEXT = """{ estimated_tokens: 6000, input_budget: 120832, context_window: 131072, reply_reserve: 8192,
  compactions: 1, summarized_messages: 6, estimate_method: 'weighted_utf8', instruction_files: [], warnings: [] }"""


def definition(scope, label):
    return scope.locator(f'xpath=.//dt[normalize-space()="{label}"]/following-sibling::dd[1]')


def open_details(page):
    """With a breakdown the usage card leads and the explanations sit under a collapsed Details."""
    page.locator(".context-more > summary").click()
    expect(page.locator(".context-more")).to_have_attribute("open", "")


def legend(page):
    """{label: (tokens, share)} of the usage card's legend."""
    rows = page.locator(".context-legend li")
    expect(rows.first).to_be_visible()
    return {row.locator(".context-legend-name").inner_text().split(" · ")[0]:
            (row.locator(".context-legend-tokens").inner_text(), row.locator(".context-legend-share").inner_text())
            for row in rows.all()}


def test_shows_breakdown_and_compaction_threshold(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{CONTEXT}, breakdown: {{
        system_instructions: 1000, tool_definitions: 2000, messages_and_results: 2500, summary: 400, request_overhead: 100 }} }} }}""")
    details = page.locator("details.context-meter")
    expect(details).not_to_have_attribute("open", "")
    page.get_by_text("Last model input · ~5%").click()
    open_details(page)
    expect(page.get_by_text("Approximate input breakdown")).to_be_visible()
    for label, value in [("System instructions", "~1,000 tokens"), ("Tool definitions", "~2,000 tokens"),
                         ("Messages and tool results", "~2,500 tokens"), ("Conversation summary", "~400 tokens"),
                         ("Request overhead", "~100 tokens")]:
        expect(definition(page, label)).to_have_text(value)
    expect(page.get_by_text("Approximately 6,000 of 120,832 input tokens used.")).to_be_visible()
    expect(page.get_by_text("Automatic compaction threshold: approximately 120,832 input tokens.")).to_be_visible()
    expect(page.get_by_text("Compactions: 1. Messages summarized: 6.")).to_be_visible()
    # Escape inside the open details closes them and returns focus to the summary.
    expect(details).to_have_attribute("open", "")
    page.locator("details.context-meter > summary").focus()
    page.keyboard.press("Escape")
    expect(details).not_to_have_attribute("open", "")
    expect(page.locator("details.context-meter > summary")).to_be_focused()


def test_zero_summary_and_no_invented_breakdown(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{CONTEXT}, breakdown: {{
        system_instructions: 1000, tool_definitions: 2000, messages_and_results: 2900, summary: 0, request_overhead: 100 }} }} }}""")
    page.get_by_text("Last model input · ~5%").click()
    open_details(page)
    expect(definition(page, "Conversation summary")).to_have_text("~0 tokens")
    assert "Conversation summary" not in legend(page)  # Nothing summarized: no card row.
    ui.rerender(f"{{ info: {CONTEXT} }}")
    expect(page.get_by_text("Approximate input breakdown")).to_have_count(0)
    expect(page.get_by_text("Automatic compaction threshold: approximately 120,832 input tokens.")).to_be_visible()


def test_legacy_byte_estimate_is_marked_outdated(ui, page):
    ui.mount("components/ContextMeter.js", f"{{ info: {{ ...{CONTEXT}, estimate_method: 'conservative_utf8' }} }}")
    page.get_by_text("Last model input · estimate outdated").click()
    expect(page.get_by_role("progressbar")).to_have_count(0)
    expect(page.get_by_text("Approximate input breakdown")).to_have_count(0)
    expect(page.get_by_text(re.compile("The saved meter counted bytes as tokens"))).to_be_visible()


def test_instruction_sources_scopes_costs_and_reasons(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{INFO}, instruction_sources: [
        {{ path: 'AGENTS.md', scope: '.', status: 'loaded', estimated_tokens: 54 }},
        {{ path: 'nested/CLAUDE.md', scope: 'nested', status: 'omitted', estimated_tokens: 0, reason: 'Instruction limit exceeded.' }},
    ] }} }}""")
    page.get_by_text(re.compile("Last model input")).click()
    items = page.locator(".context-details li")
    expect(items.filter(has_text="AGENTS.md").first).to_contain_text("scope: . and descendants · ~54 tokens")
    expect(items.filter(has_text="nested/CLAUDE.md")).to_contain_text("scope: nested and descendants · Omitted · 0 tokens loaded")
    expect(page.get_by_text("Instruction limit exceeded.")).to_be_visible()
    expect(page.get_by_text(re.compile("File costs are estimates included in system instructions above"))).to_be_visible()
    expect(page.get_by_text("Approximately 6,000 of 22,768 input tokens used.")).to_be_visible()


def test_trimmed_files_rules_user_files_and_memory_are_listed_with_their_scope(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{INFO}, instruction_sources: [
        {{ path: 'AGENTS.md', scope: '.', status: 'trimmed', estimated_tokens: 5900,
           reason: 'Trimmed to fit the 24,000-byte limit for project instructions: 23,507 of 33,000 bytes loaded.' }},
        {{ path: '.agents/rules/python.md', scope: 'files matching src/**/*.py', status: 'loaded', estimated_tokens: 40 }},
        {{ path: '~/.agents/AGENTS.md', scope: 'all projects', status: 'loaded', estimated_tokens: 30 }},
        {{ path: 'Project memory', scope: 'this project', status: 'loaded', estimated_tokens: 62 }},
    ] }} }}""")
    page.get_by_text(re.compile("Last model input")).click()
    items = page.locator(".context-details li")
    expect(items.filter(has_text="AGENTS.md").first).to_contain_text("scope: . and descendants · Trimmed · ~5,900 tokens loaded")
    expect(page.get_by_text(re.compile("^Trimmed to fit the 24,000-byte limit"))).to_be_visible()
    expect(items.filter(has_text="python.md")).to_contain_text(".agents/rules/python.md · applies to files matching src/**/*.py · ~40 tokens")
    expect(items.filter(has_text="~/.agents/AGENTS.md")).to_contain_text("~/.agents/AGENTS.md · applies to all projects · ~30 tokens")
    expect(items.filter(has_text="Project memory")).to_contain_text("Project memory · applies to this project · ~62 tokens")


def test_submits_note_once_and_prevents_duplicates(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {INFO},
        onCompact: fake.spy('compact', () => fake.defer('compact').promise) }}""")
    page.get_by_text(re.compile("Last model input")).click()
    page.get_by_role("textbox", name="Preservation note (optional)").fill("  Keep migration decisions  ")
    page.get_by_role("button", name="Compact now").click()
    expect(page.get_by_role("button", name="Starting compaction…")).to_be_disabled()
    page.get_by_role("button", name="Starting compaction…").click(force=True)
    assert ui.spy("compact") == [["Keep migration decisions"]]
    ui.run("fake.deferreds.compact.resolve()")
    expect(page.get_by_role("button", name="Compact now")).to_be_enabled()


def test_rejected_compaction_keeps_the_note(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {INFO},
        onCompact: fake.spy('compact', () => Promise.reject(new Error('No earlier turns to compact.'))) }}""")
    page.get_by_text(re.compile("Last model input")).click()
    page.get_by_role("textbox").fill("Keep decisions")
    page.get_by_role("button", name="Compact now").click()
    expect(page.get_by_role("alert")).to_have_text("No earlier turns to compact.")
    expect(page.get_by_role("textbox")).to_have_value("Keep decisions")


def test_compact_now_is_unavailable_when_there_is_nothing_to_compact(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{INFO}, compactable: false }},
        onCompact: fake.spy('compact') }}""")
    page.get_by_text(re.compile("Last model input")).click()
    button = page.get_by_role("button", name="Compact now")
    expect(button).to_be_disabled()
    expect(page.get_by_role("textbox", name="Preservation note (optional)")).to_be_disabled()
    note = ("Nothing to compact yet: earlier turns are already summarized and the latest turn is kept whole. "
            "Compact now is available again after your next message.")
    expect(page.get_by_text(note)).to_be_visible()
    expect(button).to_have_accessible_description(note)
    expect(page.get_by_text(re.compile("Summarizes earlier turns"))).to_have_count(0)
    button.click(force=True)
    assert ui.spy("compact") == []
    # Before any compaction: a conversation with one turn.
    ui.rerender(f"{{ info: {{ ...{INFO}, compactions: 0, compactable: false }}, onCompact: fake.spy('compact') }}")
    expect(page.get_by_text(re.compile(r"^Nothing to compact yet: this conversation has one turn"))).to_be_visible()
    # A new turn makes it available again, as does an older saved meter without the flag.
    for flag in ("compactable: true, ", ""):
        ui.rerender(f"{{ info: {{ ...{INFO}, {flag}}}, onCompact: fake.spy('compact') }}")
        expect(button).to_be_enabled()
        expect(page.get_by_text(re.compile("Summarizes earlier turns"))).to_be_visible()
        expect(page.get_by_text(re.compile("Nothing to compact yet"))).to_have_count(0)


def test_open_details_close_when_the_chat_waits_for_approval(ui, page):
    # The details float over the transcript; a new approval card must not sit under them.
    ui.mount("components/ContextMeter.js", f"{{ info: {INFO}, busy: true, status: 'running' }}")
    details = page.locator("details.context-meter")
    page.get_by_text(re.compile("Last model input")).click()
    expect(details).to_have_attribute("open", "")
    ui.rerender(f"{{ info: {INFO}, busy: true, status: 'awaiting_approval' }}")
    expect(details).not_to_have_attribute("open", "")
    # They can be opened again while the approval waits, and other statuses leave them open.
    page.get_by_text(re.compile("Last model input")).click()
    expect(details).to_have_attribute("open", "")
    ui.rerender(f"{{ info: {INFO}, busy: true, status: 'running' }}")
    expect(details).to_have_attribute("open", "")


def test_busy_disables_compaction_and_preview_is_labelled(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{INFO}, prepared_for_next_turn: true }}, busy: true,
        onCompact: fake.spy('compact') }}""")
    page.get_by_text(re.compile("Context preview")).click()
    expect(page.get_by_role("button", name="Compact now")).to_be_disabled()
    expect(page.get_by_role("textbox")).to_be_disabled()
    expect(page.get_by_text(re.compile("Preview after compaction using saved tool definitions"))).to_be_visible()
    expect(page.get_by_text(re.compile("Last model input"))).to_have_count(0)
    page.get_by_role("button", name="Compact now").click(force=True)
    assert ui.spy("compact") == []


def test_calibrated_estimate_and_summary_adjustments(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{CONTEXT}, estimated_tokens: 12650, estimate_scale: 1.25,
        summary_adjustment: 'condensed' }} }}""")
    page.get_by_text("Last model input · ~10%").click()
    expect(page.get_by_text(re.compile(r"scaled ×1\.25 to match the input tokens Databricks reported"))).to_be_visible()
    expect(page.get_by_text(re.compile("Heuristic text-size estimate"))).to_have_count(0)
    expect(page.get_by_text(re.compile(r"condensed by a short extra request\. Full history is preserved\."))).to_be_visible()
    ui.rerender(f"{{ info: {{ ...{CONTEXT}, estimated_tokens: 12650, estimate_scale: 1.3, estimate_prior: true }} }}")
    expect(page.get_by_text(re.compile(r"scaled ×1\.30, this model's starting ratio until Databricks reports input "
                                       r"tokens for it"))).to_be_visible()
    ui.rerender(f"{{ info: {{ ...{CONTEXT}, summary_adjustment: 'trimmed' }} }}")
    expect(page.get_by_text(re.compile("Heuristic text-size estimate"))).to_be_visible()
    expect(page.get_by_text(re.compile(r"part of it was omitted\. Full history is preserved\."))).to_be_visible()
    # Condensed, but still too long: the app shortened its least important sections (kept in the handoff).
    handoff = "handoff_path: 'handoffs/auto/h.md'"
    ui.rerender(f"{{ info: {{ ...{CONTEXT}, summary_adjustment: 'condensed_trimmed', {handoff} }} }}")
    expect(page.get_by_text("The condensed conversation summary was still over its size limit, so the app shortened "
                            "its least important sections; the saved handoff has them in full. Full history is "
                            "preserved.")).to_be_visible()
    expect(page.get_by_text(re.compile("part of it was omitted"))).to_have_count(0)
    ui.rerender(f"{{ info: {{ ...{CONTEXT}, summary_adjustment: 'condensed_trimmed' }} }}")
    expect(page.get_by_text(re.compile(r"least important sections\. Full history is preserved\.$"))).to_be_visible()
    ui.rerender(f"{{ info: {{ ...{CONTEXT}, summary_fitted_to_budget: 'condensed_trimmed' }} }}")
    expect(page.get_by_text(re.compile("condensed the summary and the app shortened its least important sections to "
                                       "fit"))).to_be_visible()
    # Within its own limit, but the request after compaction was over the input budget.
    ui.rerender(f"{{ info: {{ ...{CONTEXT}, summary_fitted_to_budget: 'trimmed' }} }}")
    expect(page.get_by_text(re.compile(r"^The request after the latest compaction was over the input budget .*so part "
                                       r"of the summary was omitted to fit\. Full history is preserved\.$"))).to_be_visible()
    expect(page.get_by_text(re.compile("over its size limit"))).to_have_count(0)
    ui.rerender(f"{{ info: {{ ...{CONTEXT}, summary_fitted_to_budget: 'condensed' }} }}")
    expect(page.get_by_text(re.compile("a short extra request condensed the summary to fit"))).to_be_visible()
    ui.rerender(f"{{ info: {CONTEXT} }}")
    expect(page.get_by_text(re.compile("over its size limit"))).to_have_count(0)
    expect(page.get_by_text(re.compile("over the input budget"))).to_have_count(0)


def test_custom_instructions_and_skill_index_apply_beyond_a_folder(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{INFO}, instruction_sources: [
        {{ path: 'AGENTS.md', scope: '.', status: 'loaded', estimated_tokens: 54 }},
        {{ path: 'Custom instructions (Settings)', scope: 'all projects', status: 'loaded', estimated_tokens: 31 }},
        {{ path: 'Skill index', scope: 'this workspace', status: 'loaded', estimated_tokens: 120 }},
    ] }} }}""")
    page.get_by_text(re.compile("Last model input")).click()
    expect(page.get_by_text("Loaded instructions", exact=True)).to_be_visible()
    items = page.locator(".context-details li")
    expect(items).to_have_text([
        re.compile(r"AGENTS\.md · scope: \. and descendants · ~54 tokens"),
        re.compile(r"^\s*Custom instructions \(Settings\) · applies to all projects · ~31 tokens\s*$"),
        re.compile(r"^\s*Skill index · applies to this workspace · ~120 tokens\s*$"),
    ])
    expect(items.nth(1).locator("code")).to_have_count(0)


# Reported input ("Last model input" from Databricks' counts) next to the app's estimate.
REPORT = """{ call_id: 'c1', model: 'databricks-claude-opus-5-5', status: 'completed', finish_reason: 'stop', partial: false,
  input_tokens: 10000, uncached_input_tokens: 9000, cache_read_input_tokens: 1000, cache_creation_input_tokens: 0,
  output_tokens: 50, reasoning_tokens: 20, context_window: 131072, input_budget: 120832, auto_compact_threshold: 108748,
  auto_compact_percent: 90, compactions: 1, percent_of_input_budget: 8.3, percent_of_context_budget: 7.6,
  percent_of_threshold: 9.2, model_window: 1000000, percent_of_model_window: 1, estimated_tokens: 8000,
  estimate_scale: 1.3, next_estimate_scale: 1.25, estimate_gap_percent: 25 }"""
REPORTED = f"""{{ ...{CONTEXT}, estimated_tokens: 8000, auto_compact_threshold: 108748, auto_compact_percent: 90,
  last_request: {REPORT} }}"""


def test_reported_input_is_shown_with_percentages_and_the_threshold_mark(ui, page):
    ui.mount("components/ContextMeter.js", f"{{ info: {REPORTED} }}")
    summary = page.get_by_text("Last model input · 10,000 tokens · 8%")
    expect(summary).to_be_visible()
    bar = page.get_by_role("progressbar", name="Reported model input usage")
    expect(bar).to_have_attribute("aria-valuetext", "8% of context budget: 10,000 of 131,072 tokens reported by "
                                  "Databricks; 1% of the model's 1,000,000-token window")
    expect(page.get_by_role("progressbar", name="Estimated model input usage")).to_have_count(0)
    expect(page.locator(".context-bar-mark")).to_have_attribute("style", re.compile("left: ?83%"))  # 108,748 of 131,072.
    expect(page.locator(".context-bar")).not_to_have_class(re.compile("over"))
    summary.click()
    expect(page.get_by_text("Last request · reported by Databricks")).to_be_visible()
    reported = page.locator(".context-reported")
    expect(definition(reported, "Input tokens")).to_have_text("10,000 = 9,000 uncached + 1,000 cache read")
    expect(definition(reported, "Output tokens")).to_have_text("50 (reasoning 20)")
    expect(definition(reported, "Input budget")).to_have_text("8.3% of 120,832 (auto-compaction at 90%)")
    expect(definition(reported, "Context budget")).to_have_text("7.6% of 131,072")
    expect(definition(reported, "Model window")).to_have_text("1% of 1,000,000 for databricks-claude-opus-5-5")
    expect(definition(reported, "Status")).to_have_text("completed · stop")
    expect(page.get_by_text(re.compile(
        r"^Databricks counted 25% more input tokens than the app's estimate for this request \(~8,000\)\. .*"
        r"Later estimates are scaled from these reports \(×1\.25\)\. Cached tokens still occupy the context window; "
        r"they are cheaper, not smaller\.$"))).to_be_visible()
    # The scale this request used (×1.30) is not the one later estimates use.
    expect(page.get_by_text(re.compile("×1\\.30"))).to_have_count(0)
    expect(page.get_by_text("App estimate", exact=True)).to_be_visible()
    expect(page.get_by_text("Approximately 8,000 of 120,832 input tokens used.")).to_be_visible()
    expect(page.get_by_text("Automatic compaction at 90% of the input budget: about 108,748 input tokens "
                            "(Settings → Auto-compact at).")).to_be_visible()


def test_a_threshold_set_by_a_project_settings_file_names_the_file(ui, page):
    ui.mount("components/ContextMeter.js", f"{{ info: {{ ...{REPORTED}, auto_compact_percent: 70, auto_compact_source: 'local' }} }}")
    page.get_by_text("Last model input · 10,000 tokens · 8%").click()
    expect(page.get_by_text(re.compile(r"^Automatic compaction at 70% of the input budget: about 108,748 input tokens "
                                       r"\(set by this project's \.agents/settings\.local\.json\)\.$"))).to_be_visible()
    expect(page.get_by_text("Settings → Auto-compact at", exact=False)).to_have_count(0)


def test_a_report_from_before_a_compaction_is_labelled_and_the_estimate_leads(ui, page):
    ui.mount("components/ContextMeter.js", f"{{ info: {{ ...{REPORTED}, compactions: 2, prepared_for_next_turn: true }} }}")
    summary = page.get_by_text("Context preview · ~6%")
    expect(summary).to_be_visible()
    expect(page.get_by_role("progressbar", name="Estimated model input usage")).to_have_attribute(
        "aria-valuetext", "Approximately 6% of context budget")
    summary.click()
    expect(page.get_by_text("Before compaction · reported by Databricks")).to_be_visible()
    expect(page.get_by_text(re.compile("Databricks counted"))).to_have_count(0)
    # Not a preview, but a compaction ran since the report: still history.
    ui.rerender(f"{{ info: {{ ...{REPORTED}, compactions: 2 }} }}")
    expect(page.get_by_text("Last model input · ~6%")).to_be_visible()
    expect(page.get_by_text("Before compaction · reported by Databricks")).to_be_visible()


def test_a_report_from_another_budget_or_model_is_not_shown_as_current(ui, page):
    # The input budget changed in Settings since the report: its share is of the old budget.
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{REPORTED}, input_budget: 989760, context_window: 1000000,
        estimated_tokens: 99000 }}, model: 'databricks-claude-opus-5-5' }}""")
    summary = page.get_by_text("Last model input · ~10%")
    expect(summary).to_be_visible()
    expect(page.get_by_role("progressbar", name="Reported model input usage")).to_have_count(0)
    summary.click()
    expect(page.get_by_text("Earlier request · reported by Databricks")).to_be_visible()
    expect(page.get_by_text(re.compile("Databricks counted"))).to_have_count(0)
    # The conversation switched models since the report, at the same budget. Until a request to the new model is
    # prepared, the info and the report describe the same request, so the reported value stays.
    ui.rerender(f"{{ info: {REPORTED}, model: 'databricks-gpt-oss-120b' }}")
    expect(page.get_by_text("Last model input · 10,000 tokens · 8%")).to_be_visible()
    expect(page.get_by_text("Last request · reported by Databricks")).to_be_visible()
    # A request to the new model was prepared (streaming, or it failed): the report is history.
    ui.rerender(f"{{ info: {{ ...{REPORTED}, estimated_tokens: 8800 }}, model: 'databricks-gpt-oss-120b' }}")
    expect(page.get_by_text("Last model input · ~7%")).to_be_visible()
    expect(page.get_by_text("Earlier request · reported by Databricks")).to_be_visible()
    ui.rerender(f"{{ info: {REPORTED}, model: 'databricks-claude-opus-5-5' }}")
    expect(page.get_by_text("Last model input · 10,000 tokens · 8%")).to_be_visible()
    expect(page.get_by_text("Last request · reported by Databricks")).to_be_visible()


# After one Opus 5.5 turn at default Settings (report 138,403 tokens, 76.2% of 181,568), the picker switched
# the chat to GPT OSS: the backend moved the budgets and rescaled the next-request estimate, but the last
# request's estimate (139,084 at ×1.3) is still Opus-scaled.
SWITCHED = """{ estimated_tokens: 139084, estimate_scale: 1.3, input_budget: 120832, context_window: 131072,
  reply_reserve: 8192, compactions: 0, summarized_messages: 0, estimate_method: 'weighted_utf8',
  instruction_files: [], warnings: [], auto_compact_percent: 90, auto_compact_threshold: 108748,
  next_estimate: { tokens: 107003, scale: 1, percent_of_input_budget: 88.6, percent_of_threshold: 98.4 },
  last_request: { call_id: 'c1', model: 'databricks-claude-opus-5-5', status: 'completed', finish_reason: 'stop',
    partial: false, input_tokens: 138403, uncached_input_tokens: 138403, cache_read_input_tokens: 0,
    cache_creation_input_tokens: 0, output_tokens: 50, context_window: 200000, input_budget: 181568,
    auto_compact_threshold: 163411, auto_compact_percent: 90, compactions: 0, percent_of_input_budget: 76.2,
    percent_of_context_budget: 69.2, model_window: 1000000, percent_of_model_window: 13.8, estimated_tokens: 139084,
    estimate_scale: 1.3, next_estimate_scale: 1.3, estimate_gap_percent: -0.5 } }"""


def test_a_switch_to_a_model_with_another_budget_shows_the_next_requests_share(ui, page):
    ui.mount("components/ContextMeter.js", f"{{ info: {SWITCHED}, model: 'databricks-gpt-oss-120b' }}")
    # Not the Opus-scaled estimate over the GPT OSS budget (~115%, marked over the threshold).
    summary = page.get_by_text("Next request · ~82%")
    expect(summary).to_be_visible()
    expect(page.get_by_text(re.compile("115%"))).to_have_count(0)
    expect(page.locator(".context-bar")).not_to_have_class(re.compile("over"))
    expect(page.get_by_role("progressbar", name="Estimated next request input usage")).to_have_attribute(
        "aria-valuetext", "Approximately 82% of context budget")
    summary.click()
    expect(page.get_by_text("Earlier request · reported by Databricks")).to_be_visible()
    expect(page.get_by_text(re.compile(r"^Next request: ~107,003 tokens \(88\.6% of the input budget\)"))).to_be_visible()
    # Once a request to the new model is prepared, the meter shows that request's own estimate again.
    ui.rerender(f"{{ info: {{ ...{SWITCHED}, estimated_tokens: 107500, estimate_scale: undefined }}, "
                "model: 'databricks-gpt-oss-120b' }")
    expect(page.get_by_text("Last model input · ~82%")).to_be_visible()


FIRST_OPUS = f"""{{ ...{CONTEXT}, estimated_tokens: 9722, estimate_scale: 1.3, estimate_prior: true,
  last_request: {{ ...{REPORT}, input_tokens: 7211, uncached_input_tokens: 7211, cache_read_input_tokens: 0,
  percent_of_input_budget: 6, estimated_tokens: 9722, estimate_scale: 1.3, next_estimate_scale: 1,
  estimate_gap_percent: -25.8 }} }}"""


def test_a_first_report_that_replaces_the_starting_ratio_says_so(ui, page):
    # A new Opus 5.5 chat's first request: estimated at the ×1.30 starting ratio, reported below the plain
    # text-size heuristic. The report replaces the starting ratio, so later estimates are unscaled.
    ui.mount("components/ContextMeter.js", f"{{ info: {FIRST_OPUS} }}")
    page.get_by_text("Last model input · 7,211 tokens · 6%").click()
    expect(page.get_by_text(re.compile(r"^Databricks counted 25\.8% fewer input tokens .*\(~9,722\)\. That estimate "
                                       r"was scaled ×1\.30; later estimates use the unscaled text-size heuristic "
                                       r"\(×1\.00\)\. The app never scales its estimate below the text-size "
                                       r"heuristic, so compaction may start slightly early\.$"))).to_be_visible()
    expect(page.get_by_text(re.compile(r"scaled ×1\.30, this model's starting ratio before Databricks reported input "
                                       r"tokens for it; later estimates use ×1\.00\."))).to_be_visible()
    expect(page.get_by_text(re.compile("until Databricks reports"))).to_have_count(0)
    # A cancelled request is no calibration sample: the starting ratio still applies.
    ui.rerender(f"""{{ info: {{ ...{FIRST_OPUS}, last_request: {{ ...{FIRST_OPUS}.last_request, status: 'cancelled',
        partial: true, finish_reason: undefined, next_estimate_scale: 1.3 }} }} }}""")
    expect(page.get_by_text(re.compile(r"scaled ×1\.30, this model's starting ratio until Databricks reports input "
                                       r"tokens for it"))).to_be_visible()
    # A calibrated scale (no starting ratio) that falls to 1: no "starting ratio" wording.
    ui.rerender(f"""{{ info: {{ ...{FIRST_OPUS}, estimate_scale: 1.1, estimate_prior: false, last_request: {{
        ...{FIRST_OPUS}.last_request, estimate_scale: 1.1, estimate_gap_percent: -9.1 }} }} }}""")
    expect(page.get_by_text(re.compile(r"That estimate was scaled ×1\.10; later estimates use the unscaled "
                                       r"text-size heuristic \(×1\.00\)\."))).to_be_visible()
    expect(page.get_by_text(re.compile("starting ratio"))).to_have_count(0)


def test_partial_fewer_and_matching_reports(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{REPORTED}, last_request: {{ ...{REPORT}, partial: true,
        status: 'cancelled', finish_reason: undefined, estimate_gap_percent: -4.5, cache_read_input_tokens: 0,
        uncached_input_tokens: 10000, model_window: null, percent_of_model_window: null }} }} }}""")
    summary = page.get_by_text("Last model input · 10,000 tokens · 8% (partial)")
    expect(summary).to_be_visible()
    expect(page.get_by_role("progressbar", name="Reported model input usage")).to_have_attribute(
        "aria-valuetext", "8% of context budget: 10,000 of 131,072 tokens reported by Databricks")
    summary.click()
    reported = page.locator(".context-reported")
    expect(definition(reported, "Input tokens")).to_have_text("10,000")
    expect(definition(reported, "Model window")).to_have_text("Not known for this endpoint")
    expect(definition(reported, "Status")).to_have_text(re.compile("^cancelled · partial"))
    # Below the scaled estimate but above the text-size heuristic: later estimates are scaled down.
    expect(page.get_by_text(re.compile(r"^Databricks counted 4\.5% fewer input tokens .*\(~8,000\)\. Later estimates "
                                       r"are scaled from these reports \(×1\.25\)\.$"))).to_be_visible()
    expect(page.get_by_text(re.compile("Cached tokens still occupy"))).to_have_count(0)
    ui.rerender(f"""{{ info: {{ ...{REPORTED}, last_request: {{ ...{REPORT}, estimate_gap_percent: -4.5,
        estimate_scale: 1, next_estimate_scale: 1 }} }} }}""")
    expect(page.get_by_text(re.compile(r"^Databricks counted 4\.5% fewer input tokens .* The app never scales its "
                                       r"estimate below the text-size heuristic, so compaction may start slightly "
                                       r"early\."))).to_be_visible()
    # A report saved before the next scale was recorded names no number.
    ui.rerender(f"{{ info: {{ ...{REPORTED}, last_request: {{ ...{REPORT}, next_estimate_scale: undefined }} }} }}")
    expect(page.get_by_text(re.compile(r"Later estimates are scaled from these reports\. Cached"))).to_be_visible()
    ui.rerender(f"{{ info: {{ ...{REPORTED}, last_request: {{ ...{REPORT}, estimate_gap_percent: 1.5 }} }} }}")
    expect(page.get_by_text(re.compile(r"^The app's estimate matched the reported count within 2%\."))).to_be_visible()


def test_the_bar_turns_over_at_the_threshold(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{REPORTED}, last_request: {{ ...{REPORT},
        input_tokens: 115000, percent_of_input_budget: 95.2 }} }} }}""")
    expect(page.locator(".context-bar")).to_have_class(re.compile(r"\bover\b"))
    ui.rerender(f"{{ info: {{ ...{CONTEXT}, estimated_tokens: 110000, auto_compact_percent: 90, auto_compact_threshold: 108748 }} }}")
    expect(page.get_by_text("Last model input · ~84%")).to_be_visible()
    expect(page.locator(".context-bar")).to_have_class(re.compile(r"\bover\b"))


def test_next_estimate_and_deferred_compaction_are_explained(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{CONTEXT}, auto_compact_percent: 90, auto_compact_threshold: 108748,
        compaction_deferred: true, next_estimate: {{ tokens: 7200, percent_of_input_budget: 6, percent_of_threshold: 6.6 }} }} }}""")
    page.get_by_text("Last model input · ~5%").click()
    expect(page.get_by_text("Next request: ~7,200 tokens (6% of the input budget) before your next message. "
                            "Automatic compaction runs when it passes ~108,748 tokens.")).to_be_visible()
    expect(page.get_by_text("The estimate passed the auto-compact threshold, but summarizing earlier turns would not "
                            "bring it under the threshold, so the request was sent as it is. Compaction runs when it "
                            "would, or when the request no longer fits the input budget.")).to_be_visible()
    expect(page.get_by_text(re.compile("reported by Databricks"))).to_have_count(0)


def test_the_collapsed_meter_wraps_at_phone_width(ui, page):
    page.set_viewport_size({"width": 360, "height": 700})
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{REPORTED}, compactions: 1, warnings: ['One', 'Two'],
        last_request: {{ ...{REPORT}, partial: true }} }} }}""")
    expect(page.get_by_text(re.compile("8% \\(partial\\)"))).to_be_visible()
    assert ui.js("document.documentElement.scrollWidth") <= 360


def test_request_details_add_the_input_including_cache(ui, page):
    ui.mount("components/RequestDetails.js", """{ info: { model: 'm', status: 'completed', finish_reason: 'stop',
        usage: { input_tokens: 9000, output_tokens: 50, cache_read_input_tokens: 1000, cache_creation_input_tokens: 200 } } }""")
    page.get_by_text("Request details · completed").click()
    expect(definition(page, "Input incl. cache")).to_have_text("10,200")
    ui.rerender("{ info: { model: 'm', status: 'error', usage: { output_tokens: 5 } } }")
    expect(definition(page, "Input incl. cache")).to_have_text("Unavailable")


@pytest.mark.parametrize("size, shown", [(1_234_567, "1.2 MB"), (245_300, "245 KB"), (120, "1 KB")])
def test_the_request_size_is_shown_against_the_databricks_limit(ui, page, size, shown):
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{CONTEXT}, payload_bytes: {size},
        payload_limit_bytes: 4000000 }} }}""")
    page.get_by_text(re.compile("Last model input")).click()
    expect(page.get_by_text(f"Request size: {shown} of Databricks' 4 MB limit.", exact=True)).to_be_visible()


def test_no_request_size_without_a_measured_body(ui, page):
    ui.mount("components/ContextMeter.js", f"{{ info: {CONTEXT} }}")
    page.get_by_text(re.compile("Last model input")).click()
    expect(page.get_by_text(re.compile("Request size"))).to_have_count(0)


def test_request_details_show_the_rate_limit_of_a_final_429(ui, page):
    ui.mount("components/RequestDetails.js", """{ info: { model: 'm', status: 'error', http_status: 429,
        error_kind: 'rate_limit', rate_limit: { limit_type: 'input_tokens_per_minute', limit: 1000000, current: 1200000,
        retry_after: 12 } } }""")
    page.get_by_text("Request details · error").click()
    expect(definition(page, "Rate limit")).to_have_text(
        "input tokens per minute: limit 1,000,000, current 1,200,000, retry after 12 s")
    expect(page.locator("dt", has_text="Databricks message")).to_have_count(0)
    ui.rerender("{ info: { model: 'm', status: 'error', http_status: 429, error_kind: 'rate_limit' } }")
    expect(page.locator("dt", has_text="Rate limit")).to_have_count(0)


def test_request_details_show_what_a_429_without_a_limit_type_said(ui, page):
    ui.mount("components/RequestDetails.js", """{ info: { model: 'm', status: 'error', http_status: 429,
        error_kind: 'rate_limit', rate_limit: { code: 'REQUEST_LIMIT_EXCEEDED', message: 'Exceeded workspace rate limit',
        retry_after: 7, retry_after_header: '7' } } }""")
    page.get_by_text("Request details · error").click()
    expect(definition(page, "Rate limit")).to_have_text("type not named, retry after 7 s (Retry-After header)")
    expect(definition(page, "Databricks message")).to_have_text("REQUEST_LIMIT_EXCEEDED: Exceeded workspace rate limit")
    ui.rerender("""{ info: { model: 'm', status: 'error', http_status: 429, error_kind: 'rate_limit',
        rate_limit: { message: 'Too Many Requests' } } }""")
    expect(definition(page, "Rate limit")).to_have_text("type not named")
    expect(definition(page, "Databricks message")).to_have_text("Too Many Requests")


def test_an_anchored_estimate_says_so(ui, page):
    # The estimate is the input Databricks reported for the previous request plus what was added since.
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{CONTEXT}, estimate_anchor: {{ reported: 5000, added: 800 }},
        auto_compact_percent: 90, auto_compact_threshold: 108748,
        next_estimate: {{ tokens: 6100, scale: 1.25, anchor: {{ model: 'm', reported: 5800, added: 240, raw: 7000 }},
                         percent_of_input_budget: 5, percent_of_threshold: 5.6 }} }} }}""")
    page.get_by_text("Last model input · ~5%").click()
    expect(page.locator(".context-anchor")).to_have_text(
        "Anchored on the last reported count: 5,000 tokens Databricks reported for the previous request + ~1,000 tokens "
        "added since. Only what changed is estimated from text size.")
    expect(page.get_by_text("Next request: ~6,100 tokens (5% of the input budget, anchored on the last reported count) "
                            "before your next message. Automatic compaction runs when it passes ~108,748 tokens.")).to_be_visible()
    # A request that shrank since (a shorter system message, fewer tools).
    ui.rerender(f"{{ info: {{ ...{CONTEXT}, estimated_tokens: 4800, estimate_anchor: {{ reported: 5000, added: -200 }} }} }}")
    expect(page.locator(".context-anchor")).to_contain_text("for the previous request − ~200 tokens removed since.")
    # Without an anchor: no such line.
    ui.rerender(f"{{ info: {CONTEXT} }}")
    expect(page.locator(".context-anchor")).to_have_count(0)
    expect(page.get_by_text(re.compile("anchored on the last reported count"))).to_have_count(0)


def test_tool_definitions_are_split_per_mcp_server(ui, page):
    breakdown = """breakdown: { system_instructions: 1000, tool_definitions: 2000, messages_and_results: 2500, summary: 0,
        request_overhead: 500 }"""
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{CONTEXT}, {breakdown},
        tool_groups: {{ 'Built-in tools': 1400, 'MCP docs': 450, 'MCP git_hub': 150 }} }} }}""")
    page.get_by_text("Last model input · ~5%").click()
    rows = legend(page)
    assert rows["Built-in tools"][0] == "1.4k" and rows["MCP tools"][0] == "600" and "Tool definitions" not in rows
    expect(page.locator(".context-legend li").filter(has_text="MCP tools")).to_contain_text("MCP tools · 2")
    open_details(page)
    tools = definition(page, "Tool definitions")
    expect(tools).to_contain_text("~2,000 tokens")
    expect(tools.locator("li")).to_have_text(["Built-in tools · ~1,400 tokens", "MCP docs · ~450 tokens",
                                              "MCP git_hub · ~150 tokens"])
    # Only built-in tools: the total alone.
    ui.rerender(f"""{{ info: {{ ...{CONTEXT}, {breakdown}, tool_groups: {{ 'Built-in tools': 2000 }} }} }}""")
    expect(definition(page, "Tool definitions")).to_have_text("~2,000 tokens")
    assert legend(page)["Tool definitions"][0] == "2.0k" and "MCP tools" not in legend(page)


def test_selected_skills_are_listed_one_by_one(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{INFO}, instruction_sources: [
        {{ path: 'Skill index', scope: 'this workspace', status: 'loaded', estimated_tokens: 120 }},
        {{ path: 'Skill /style', scope: 'this chat', status: 'loaded', estimated_tokens: 40 }},
        {{ path: 'Skill /review', scope: 'this message', status: 'loaded', estimated_tokens: 310 }},
    ] }} }}""")
    page.get_by_text(re.compile("Last model input")).click()
    items = page.locator(".context-details li")
    expect(items).to_have_text([
        re.compile(r"^\s*Skill index · applies to this workspace · ~120 tokens\s*$"),
        re.compile(r"^\s*Skill /style · applies to this chat · ~40 tokens\s*$"),
        re.compile(r"^\s*Skill /review · applies to this message · ~310 tokens\s*$"),
    ])
    expect(page.get_by_text(re.compile("A skill your message selected counts in messages and tool results instead."))).to_be_visible()


def test_the_usage_card_shows_where_the_context_budget_goes(ui, page):
    # Like Claude's context panel: what the request holds, then the free space up to the auto-compact threshold,
    # the buffer up to the input budget and what is kept for the reply; the parts add up to the context budget.
    info = """{ estimated_tokens: 60000, input_budget: 181568, context_window: 200000, reply_reserve: 16384,
      auto_compact_threshold: 163411, auto_compact_percent: 90, estimate_scale: 1.0, compactions: 1, summarized_messages: 9,
      estimate_method: 'weighted_utf8', instruction_files: ['AGENTS.md'], warnings: ['Large tool result trimmed.'],
      breakdown: { system_instructions: 5000, tool_definitions: 8000, messages_and_results: 44000, summary: 2600, request_overhead: 400 },
      tool_groups: { 'Built-in tools': 6000, 'MCP docs': 2000 },
      instruction_sources: [{ path: 'AGENTS.md', scope: '.', status: 'loaded', estimated_tokens: 1500 },
        { path: 'Project memory', scope: 'this project', status: 'loaded', estimated_tokens: 500 },
        { path: 'Skill /review', scope: 'this message', status: 'loaded', estimated_tokens: 900 },
        { path: 'big/AGENTS.md', scope: 'big', status: 'omitted', estimated_tokens: 0 }] }"""
    ui.mount("components/ContextMeter.js", f"{{ info: {info}, onCompact: fake.spy('compact') }}")
    page.get_by_text("Last model input · ~30%").click()
    card = page.get_by_role("region", name="Context usage")
    # No percent_used (an older server): the page's fallback shows the card's own figures (60k of 200k) in the meter too.
    expect(card.locator(".context-usage-head")).to_have_text(re.compile(r"30% used\s*~60\.0k / 200k"))
    assert legend(page) == {
        "Messages and tool results": ("44.0k", "22.0%"), "Conversation summary": ("2.6k", "1.3%"),
        "System instructions": ("3.0k", "1.5%"), "Instruction files and memory": ("2.0k", "1.0%"),
        "Built-in tools": ("6.0k", "3.0%"), "MCP tools": ("2.0k", "1.0%"), "Request overhead": ("400", "0.2%"),
        "Free space": ("103k", "51.7%"), "Autocompact buffer": ("18.2k", "9.1%"), "Reserved for the reply": ("18.4k", "9.2%")}
    expect(card.locator(".context-legend li").filter(has_text="Instruction files")).to_contain_text("· 2")
    expect(card.get_by_role("img")).to_have_attribute("aria-label", re.compile(r"^60\.0k of the 200k-token context budget used: "
                                                                              r"Messages and tool results 44\.0k, "))
    expect(card.locator(".context-stack-mark")).to_have_attribute("style", re.compile(r"left: ?81\.7"))
    expect(card.locator(".context-usage-note")).to_have_text("Estimated by the app · percent of the context budget, compacts at 90%.")
    # Warnings and Compact now stay in view; the explanations wait under Details, which stays as it was left.
    expect(page.get_by_text("Large tool result trimmed.")).to_be_visible()
    expect(page.get_by_role("button", name="Compact now")).to_be_visible()
    expect(page.get_by_text("Approximately 60,000 of 181,568 input tokens used.")).to_be_hidden()
    open_details(page)
    expect(page.get_by_text("Approximately 60,000 of 181,568 input tokens used.")).to_be_visible()
    ui.rerender(f"{{ info: {{ ...{info}, estimated_tokens: 61000 }} }}")
    expect(page.locator(".context-more")).to_have_attribute("open", "")


def test_the_usage_card_follows_the_reported_count_and_names_the_model_window(ui, page):
    # Databricks reported 170,000 for the request the app estimated at 100,000: the parts scale to the report, the
    # threshold is passed (no free space), and the model's larger window is named next to the budget.
    report = """{ call_id: 'c1', model: 'databricks-claude-opus-5-5', status: 'completed', finish_reason: 'stop', partial: false,
      input_tokens: 170000, uncached_input_tokens: 2000, cache_read_input_tokens: 168000, cache_creation_input_tokens: 0,
      context_window: 200000, input_budget: 181568, auto_compact_threshold: 163411, auto_compact_percent: 90, compactions: 0,
      percent_of_input_budget: 93.6, percent_of_context_budget: 85, model_window: 1000000, percent_of_model_window: 17,
      estimated_tokens: 100000, estimate_scale: 1.3 }"""
    info = f"""{{ estimated_tokens: 100000, input_budget: 181568, context_window: 200000, reply_reserve: 16384,
      auto_compact_threshold: 163411, auto_compact_percent: 90, compactions: 0, summarized_messages: 0,
      estimate_method: 'weighted_utf8', instruction_files: [], warnings: [],
      breakdown: {{ system_instructions: 10000, tool_definitions: 10000, messages_and_results: 80000, summary: 0, request_overhead: 0 }},
      last_request: {report} }}"""
    ui.mount("components/ContextMeter.js", f"{{ info: {info} }}")
    page.get_by_text("Last model input · 170,000 tokens · 85%").click()
    expect(page.locator(".context-usage-head")).to_have_text(re.compile(r"85% used\s*170k / 200k"))
    rows = legend(page)
    assert rows["Messages and tool results"] == ("136k", "68.0%") and rows["System instructions"] == ("17.0k", "8.5%")
    assert rows["Free space"] == ("0", "0.0%") and rows["Autocompact buffer"] == ("11.6k", "5.8%")
    assert rows["Reserved for the reply"] == ("18.4k", "9.2%")
    expect(page.locator(".context-usage-note")).to_have_text(
        "Reported by Databricks for the last request · percent of the context budget, compacts at 90%; parts are the "
        "app's estimate. The model's window is 1M; this chat uses a 200k context budget (Settings).")


def test_short_token_counts_round_before_choosing_the_unit(ui, page):
    ui.mount("components/ContextMeter.js", f"{{ info: {CONTEXT} }}")
    shown = page.evaluate("""async () => [999, 999.6, 12345, 99949, 99950, 999499, 999960, 1000001, 1200000, NaN]
                               .map((await import('/static/js/components/ContextMeter.js')).shortTokens)""")
    assert shown == ["999", "1.0k", "12.3k", "99.9k", "100k", "999k", "1M", "1M", "1.2M", "—"]


def test_mcp_servers_are_counted_without_a_built_in_group(ui, page):
    breakdown = """breakdown: { system_instructions: 1000, tool_definitions: 2000, messages_and_results: 2500, summary: 0,
        request_overhead: 500 }"""
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{CONTEXT}, {breakdown},
        tool_groups: {{ 'MCP docs': 1500, 'MCP git_hub': 500 }} }} }}""")
    page.get_by_text("Last model input · ~5%").click()
    expect(page.locator(".context-legend li").filter(has_text="MCP tools")).to_contain_text("MCP tools · 2")
    assert legend(page)["MCP tools"][0] == "2.0k" and "Built-in tools" not in legend(page)


# One percentage (the server's percent_used): the collapsed meter, the usage card and Progress show the same number.

BREAKDOWN = "{ system_instructions: 5000, tool_definitions: 8000, messages_and_results: 44000, summary: 2600, request_overhead: 400 }"
# Old formulas give 30% (card) and 33% (meter) for these tokens; every state below sends another percent_used.
BASE = f"""estimated_tokens: 60000, input_budget: 181568, context_window: 200000, reply_reserve: 16384,
  auto_compact_threshold: 163411, auto_compact_percent: 90, estimate_scale: 1.0, compactions: 0, summarized_messages: 0,
  estimate_method: 'weighted_utf8', instruction_files: [], warnings: [], breakdown: {BREAKDOWN}"""
SERVER = "limit_name: 'context budget', limit_tokens: 200000, compact_at_percent: 82"
CURRENT = """last_request: { call_id: 'c1', model: 'test-model', status: 'completed', partial: false, input_tokens: 122000,
  uncached_input_tokens: 2000, cache_read_input_tokens: 118400, cache_creation_input_tokens: 1600, context_window: 200000,
  input_budget: 181568, compactions: 0, percent_of_input_budget: 67.2, percent_of_context_budget: 61, estimated_tokens: 60000 }"""
STATES = {
    "fresh": (f"{BASE}, {SERVER}, percent_used: 41, used_tokens: 82000, is_estimate: true, last_request_cached: 0", 41, True, 0),
    "mid": (f"{BASE}, {SERVER}, percent_used: 61, used_tokens: 122000, is_estimate: false, last_request_cached: 118400, {CURRENT}",
            61, False, 118400),
    "after_model_change": (f"{BASE}, {SERVER}, percent_used: 44, used_tokens: 88000, is_estimate: true, last_request_cached: 0, "
                           "next_estimate: { tokens: 88000, scale: 1.3, percent_of_input_budget: 48.5 }", 44, True, 0),
    "just_under_the_mark": (f"{BASE}, {SERVER}, percent_used: 81, used_tokens: 162000, is_estimate: false, last_request_cached: 90000, {CURRENT}",
                            81, False, 90000),
    "over_the_mark": (f"{BASE}, {SERVER}, percent_used: 88, used_tokens: 176000, is_estimate: true, last_request_cached: 0",
                      88, True, 0),
}


@pytest.mark.parametrize("state", list(STATES))
def test_the_meter_the_card_and_progress_show_the_same_server_percent(ui, page, state):
    info, percent, estimate, cached = STATES[state]
    ui.mount("components/ContextMeter.js", f"{{ info: {{ {info} }} }}")
    summary = page.locator("details.context-meter > summary")
    expect(summary).to_contain_text(f"{percent}%")
    # A leftover local calculation (33% or 30% here) would show up in one of the three places.
    for old in ("33%", "30%", "~33", "~30"):
        assert old not in summary.inner_text()
    expect(summary).to_contain_text("estimate") if estimate else expect(summary).not_to_contain_text("estimate")
    bar = page.get_by_role("progressbar")
    expect(bar).to_have_attribute("aria-valuetext", re.compile(rf"{percent}% of context budget" if not estimate
                                                              else rf"Approximately {percent}% of context budget \(estimate\)"))
    expect(bar).to_have_attribute("value", str(percent))
    expect(page.locator(".context-bar-mark")).to_have_attribute("style", re.compile(r"left: ?82%"))
    expect(page.locator(".context-bar")).to_have_class(re.compile(r"\bover\b")) if percent >= 82 else \
        expect(page.locator(".context-bar")).not_to_have_class(re.compile(r"\bover\b"))
    summary.click()
    card = page.get_by_role("region", name="Context usage")
    head = card.locator(".context-usage-head")
    expect(head.locator("strong")).to_have_text(f"{percent}% used")
    expect(head).to_contain_text("estimate") if estimate else expect(head).not_to_contain_text("estimate")
    expect(card.locator(".context-stack-mark")).to_have_attribute("style", re.compile(r"left: ?82%"))
    expect(card.locator(".context-usage-note")).to_contain_text("percent of the context budget (200,000 tokens), compacts at 82%")
    expect(card.get_by_text(re.compile(r"^Last request: .* cached$"))).to_have_count(1 if cached else 0)
    if cached:
        expect(card.get_by_text("Last request: 118k cached" if cached == 118400 else "Last request: 90.0k cached")).to_be_visible()
    # Progress reads the same fields: its line, its bar's mark and its legend.
    ui.mount("components/ProgressPanel.js", f"{{ session: {{ id: 'a', model: 'test-model', events: [], context_info: {{ {info} }} }} }}")
    expect(page.locator(".progress-context-line")).to_contain_text(f"{percent}%")
    expect(page.locator(".progress-context-line")).to_contain_text("estimate") if estimate else \
        expect(page.locator(".progress-context-line")).not_to_contain_text("estimate")
    expect(page.locator(".progress-context-bar .context-bar-mark")).to_have_attribute("style", re.compile(r"left: ?82%"))
    expect(page.locator(".progress-note").first).to_have_text("Compacts automatically at 82% of the context budget.")
    expect(page.locator(".context-usage-head strong")).to_have_text(f"{percent}% used")
    expect(page.locator(".context-usage-head")).to_contain_text("estimate") if estimate else \
        expect(page.locator(".context-usage-head")).not_to_contain_text("estimate")
    expect(page.get_by_text(re.compile(r"^Last request: .* cached$"))).to_have_count(1 if cached else 0)


def test_a_session_without_the_server_fields_shows_one_number_in_both_places(ui, page):
    # An older server: no percent_used. The fallback uses the one formula of the card's own figures (tokens in use over
    # the context budget, 30%) for the meter and the card head; no "estimate" word, no cached line.
    info = f"{{ {BASE} }}"
    ui.mount("components/ContextMeter.js", f"{{ info: {info} }}")
    summary = page.locator("details.context-meter > summary")
    expect(summary).to_contain_text("Last model input · ~30%")
    expect(summary).not_to_contain_text("estimate")
    summary.click()
    expect(page.locator(".context-usage-head strong")).to_have_text("30% used")
    expect(page.locator(".context-usage-estimate")).to_have_count(0)
    expect(page.get_by_text(re.compile(r"^Last request: "))).to_have_count(0)
    ui.mount("components/ProgressPanel.js", f"{{ session: {{ id: 'a', model: 'test-model', events: [], context_info: {info} }} }}")
    expect(page.locator(".progress-context-line")).to_have_text("Last model input · ~30%")
    expect(page.locator(".context-usage-head strong")).to_have_text("30% used")


def test_the_estimate_word_is_in_the_accessible_text_and_the_zero_cache_line_is_hidden(ui, page):
    info = STATES["fresh"][0]
    ui.mount("components/ContextMeter.js", f"{{ info: {{ {info} }} }}")
    expect(page.get_by_role("progressbar", name="Estimated model input usage")).to_have_attribute(
        "aria-valuetext", "Approximately 41% of context budget (estimate)")
    page.locator("details.context-meter > summary").click()
    expect(page.get_by_text(re.compile("^Last request: "))).to_have_count(0)
    expect(page.get_by_role("region", name="Context usage")).to_contain_text("41% used")


# --- Largest items (Shorten and Drop) -------------------------------------------------------------------------------

import json  # noqa: E402


def items_info(**changes):
    """context_info with the server's reading and ten largest items: the four newest are not open to trimming."""
    items = [{"event_id": f"e{number}", "kind": "file_read" if number % 2 else "tool_result",
              "label": f"{'read_file' if number % 2 else 'run_command'}: src/part_{number}.py", "tokens": 20_000 - number * 1_500,
              "can_trim": number < 6} for number in range(10)]
    info = {"estimated_tokens": 90_000, "input_budget": 181_568, "context_window": 200_000, "reply_reserve": 16_384,
            "auto_compact_threshold": 163_411, "auto_compact_percent": 90, "compactions": 0, "summarized_messages": 0,
            "estimate_method": "weighted_utf8", "instruction_files": [], "warnings": [],
            "breakdown": {"system_instructions": 5_000, "tool_definitions": 8_000, "messages_and_results": 76_000,
                          "summary": 0, "request_overhead": 1_000},
            "percent_used": 45, "used_tokens": 90_000, "limit_tokens": 200_000, "limit_name": "context budget",
            "is_estimate": True, "compact_at_percent": 82, "last_request_cached": 0,
            "largest_items": items, "trimmed": {"events": 0, "tokens": 0}}
    return {**info, **changes}


def mount_items(ui, page, info=None, routes=""):
    """The meter with the ten items and the fake server's answers (routes: JavaScript for fake.override)."""
    ui.mount("components/ContextMeter.js", f"{{ info: {json.dumps(info or items_info())}, sessionId: 's1' }}",
             setup=f"fake.override = (path, options) => {{ {routes} }}" if routes else "")
    page.locator("details.context-meter > summary").click()
    return page.get_by_role("region", name="Largest items")


def preview_route(freed=12_300, cost=45_600):
    return ("if (path.startsWith('/api/sessions/s1/context/trim-preview')) "
            f"return fake.json({{ freed_tokens: {freed}, cache_cost_tokens: {cost} }})")


def test_the_card_lists_the_ten_largest_items_with_their_size_and_share(ui, page):
    section = mount_items(ui, page)
    rows = section.locator("li")
    expect(rows).to_have_count(10)
    first = rows.first
    expect(first.locator(".context-legend-name")).to_contain_text("run_command: src/part_0.py")
    expect(first.locator(".context-legend-tokens")).to_have_text("20.0k")
    expect(first.locator(".context-legend-share")).to_have_text("10.0%")  # Of the 200k context budget, like the legend.
    expect(rows.nth(9).locator(".context-legend-tokens")).to_have_text("6.5k")


def test_items_that_cannot_be_trimmed_have_no_buttons(ui, page):
    section = mount_items(ui, page)
    expect(section.get_by_role("button", name="Shorten")).to_have_count(6)
    expect(section.get_by_role("button", name="Drop")).to_have_count(6)
    for number in range(6, 10):
        expect(section.locator("li").nth(number).get_by_role("button")).to_have_count(0)
    expect(section.locator("li").first.get_by_role("button", name="Shorten")).to_be_visible()


def test_shorten_shows_what_it_frees_and_what_it_costs_before_anything_is_sent(ui, page):
    section = mount_items(ui, page, routes=preview_route())
    section.locator("li").first.get_by_role("button", name="Shorten").click()
    expect(section.get_by_text("Frees about 12.3k tokens. At least about 45.6k cached tokens are re-read.")).to_be_visible()
    expect(section.get_by_role("button", name="Confirm")).to_be_visible()
    assert ui.requests("/api/sessions/s1/context/trim", "POST") == []
    assert ui.requests("/api/sessions/s1/context/trim-preview?event_id=e0&mode=shortened", "GET")
    section.get_by_role("button", name="Cancel").click()
    expect(section.get_by_role("button", name="Confirm")).to_have_count(0)
    assert ui.requests("/api/sessions/s1/context/trim", "POST") == []
    section.locator("li").nth(1).get_by_role("button", name="Drop").click()
    expect(section.get_by_role("button", name="Confirm")).to_be_visible()
    assert ui.requests("/api/sessions/s1/context/trim-preview?event_id=e1&mode=dropped", "GET")


def test_confirm_posts_the_trim_and_the_card_shows_the_new_numbers(ui, page):
    after = items_info(percent_used=38, used_tokens=76_000, estimated_tokens=76_000,
                       trimmed={"events": 1, "tokens": 12_300}, largest_items=items_info()["largest_items"][1:])
    routes = preview_route() + f"""
      if (path === '/api/sessions/s1/context/trim' && options.method === 'POST')
        return fake.json({{ context_info: {json.dumps(after)}, cache_cost_tokens: 45600 }})"""
    section = mount_items(ui, page, routes=routes)
    expect(page.locator(".context-usage-head")).to_contain_text("45% used")
    expect(page.get_by_text("results shortened")).to_have_count(0)
    section.locator("li").first.get_by_role("button", name="Shorten").click()
    section.get_by_role("button", name="Confirm").click()
    expect(page.locator(".context-usage-head")).to_contain_text("38% used")
    assert ui.bodies("/api/sessions/s1/context/trim", "POST") == [{"event_id": "e0", "mode": "shortened"}]
    expect(page.get_by_text("1 result shortened, about 12.3k tokens freed")).to_be_visible()
    expect(page.get_by_role("region", name="Largest items").locator("li")).to_have_count(9)
    expect(page.get_by_role("button", name="Confirm")).to_have_count(0)


def test_a_busy_chat_and_a_refused_trim_show_the_servers_message_inline(ui, page):
    routes = preview_route(5_000, 0) + """
      if (path === '/api/sessions/s1/context/trim') return fake.json({ detail: { message:
        '“Trim” is still running a turn. Stop it before trimming context.', session_id: 's1', title: 'Trim' } }, 409)"""
    section = mount_items(ui, page, routes=routes)
    section.locator("li").first.get_by_role("button", name="Drop").click()
    expect(section.get_by_text("Frees about 5.0k tokens. At least about 0 cached tokens are re-read.")).to_be_visible()
    section.get_by_role("button", name="Confirm").click()
    expect(section.get_by_role("alert")).to_have_text("“Trim” is still running a turn. Stop it before trimming context.")
    expect(section.get_by_role("button", name="Confirm")).to_be_visible()  # Still there to try again or cancel.
    ui.run("""fake.override = (path, options) => path === '/api/sessions/s1/context/trim'
      ? fake.json({ detail: 'This result is already dropped from the context.' }, 422) : undefined""")
    section.get_by_role("button", name="Confirm").click()
    expect(section.get_by_role("alert")).to_have_text("This result is already dropped from the context.")


def test_a_label_with_a_hidden_character_is_shown_escaped(ui, page):
    items = items_info()["largest_items"]
    items[0]["label"] = "run_command: echo \u202eevil"
    section = mount_items(ui, page, info=items_info(largest_items=items))
    name = section.locator("li").first.locator(".context-legend-name")
    expect(name).to_have_class(re.compile("logical-order"))
    escape = name.locator(".hidden-char")
    expect(escape).to_have_count(1)
    expect(escape).to_have_attribute("title", "hidden character U+202E")
    assert "\u202e" not in name.inner_text()


def test_no_largest_items_section_without_items_and_no_totals_line_at_zero(ui, page):
    mount_items(ui, page, info=items_info(largest_items=[]))
    expect(page.get_by_role("region", name="Largest items")).to_have_count(0)
    expect(page.get_by_text("results shortened")).to_have_count(0)


# L3, L4 and L6 of the review: the card says when the next request does not fit, a trim has its own label, and the cached
# line follows the report that is current.

def test_a_card_over_100_percent_says_the_next_request_does_not_fit_and_a_normal_one_does_not(ui, page):
    over = f"{BASE}, {SERVER}, percent_used: 120, used_tokens: 240000, is_estimate: true, last_request_cached: 0"
    ui.mount("components/ContextMeter.js", f"{{ info: {{ {over} }} }}")
    page.locator("details.context-meter > summary").click()
    note = page.get_by_role("region", name="Context usage").get_by_text(
        "This does not fit the context budget. The next message compacts the chat first.")
    expect(note).to_be_visible()
    ui.mount("components/ContextMeter.js", f"{{ info: {{ {STATES['mid'][0]} }} }}")
    page.locator("details.context-meter > summary").click()
    expect(page.get_by_text("This does not fit the context budget.")).to_have_count(0)


def test_after_a_trim_the_meter_says_after_trimming_not_a_compaction_preview(ui, page):
    info = f"{STATES['fresh'][0]}, after_trim: true"
    ui.mount("components/ContextMeter.js", f"{{ info: {{ {info} }} }}")
    summary = page.locator("details.context-meter > summary")
    expect(summary).to_contain_text("After trimming · ~41% estimate")
    expect(page.get_by_text("Context preview")).to_have_count(0)
    summary.click()
    page.locator(".context-more > summary").click()
    expect(page.get_by_text("Estimate after trimming. The next model request reports the real count.")).to_be_visible()
    expect(page.get_by_text(re.compile("Preview after compaction"))).to_have_count(0)


def test_the_cached_line_shows_for_the_current_report_even_at_zero_and_never_for_an_estimate(ui, page):
    current = f"{BASE}, {SERVER}, percent_used: 61, used_tokens: 122000, is_estimate: false, last_request_cached: 0, {CURRENT}"
    ui.mount("components/ContextMeter.js", f"{{ info: {{ {current} }} }}")
    page.locator("details.context-meter > summary").click()
    expect(page.get_by_text("Last request: 0 cached")).to_be_visible()  # A 0 right after a trim is the visible cost.
    stale = f"{BASE}, {SERVER}, percent_used: 41, used_tokens: 82000, is_estimate: true, last_request_cached: 5000"
    ui.mount("components/ContextMeter.js", f"{{ info: {{ {stale} }} }}")
    page.locator("details.context-meter > summary").click()
    expect(page.get_by_text(re.compile(r"^Last request: .* cached$"))).to_have_count(0)


# The fallback (no percent_used from the server) puts the compaction mark on the context budget's scale. The
# auto-compact percent is a share of the INPUT budget, so it cannot be used as the mark as it stands.

FALLBACK = """estimated_tokens: 60000, input_budget: 181568, context_window: 200000, reply_reserve: 16384, auto_compact_percent: 90,
  estimate_scale: 1.0, compactions: 0, summarized_messages: 0, estimate_method: 'weighted_utf8', instruction_files: [], warnings: []"""


def fallback_mark(ui, page, info):
    """The mark and the sentence Progress draws for an info without percent_used."""
    ui.mount("components/ProgressPanel.js", f"{{ session: {{ id: 'a', model: 'test-model', events: [], context_info: {{ {info} }} }} }}")
    marks = page.locator(".progress-context-bar .context-bar-mark")
    note = page.locator(".progress-note").filter(has_text="Compacts automatically")
    return marks, note


def test_the_fallback_mark_is_the_percent_of_the_input_budget_on_the_context_scale(ui, page):
    # 90% of 181,568 input tokens is 163,411 tokens: 82% of the 200,000-token context budget, not 90%.
    marks, note = fallback_mark(ui, page, FALLBACK)
    expect(marks).to_have_attribute("style", re.compile(r"left: ?82%"))
    expect(note).to_have_text("Compacts automatically at 82% of the context budget.")
    ui.mount("components/ContextMeter.js", f"{{ info: {{ {FALLBACK} }} }}")
    expect(page.locator(".context-bar-mark")).to_have_attribute("style", re.compile(r"left: ?82%"))


def test_the_fallback_mark_prefers_the_threshold_in_tokens(ui, page):
    marks, note = fallback_mark(ui, page, FALLBACK + ", auto_compact_threshold: 100000")
    expect(marks).to_have_attribute("style", re.compile(r"left: ?50%"))
    expect(note).to_have_text("Compacts automatically at 50% of the context budget.")


@pytest.mark.parametrize("missing", ["input_budget: 181568, ", "context_window: 200000, "])
def test_the_fallback_has_no_mark_and_no_sentence_without_both_budgets(ui, page, missing):
    marks, note = fallback_mark(ui, page, FALLBACK.replace(missing, ""))
    expect(page.locator(".progress-context-line")).to_be_visible()
    expect(marks).to_have_count(0)
    expect(note).to_have_count(0)
