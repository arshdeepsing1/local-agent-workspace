import re

import pytest
from playwright.sync_api import expect

INFO = """{ estimated_tokens: 6000, input_budget: 22768, context_window: 33008, reply_reserve: 8192,
  compactions: 1, summarized_messages: 4, estimate_method: 'weighted_utf8', instruction_files: ['AGENTS.md'], warnings: [] }"""
CONTEXT = """{ estimated_tokens: 6000, input_budget: 120832, context_window: 131072, reply_reserve: 8192,
  compactions: 1, summarized_messages: 6, estimate_method: 'weighted_utf8', instruction_files: [], warnings: [] }"""


def definition(scope, label):
    return scope.locator(f'xpath=.//dt[normalize-space()="{label}"]/following-sibling::dd[1]')


def test_shows_breakdown_and_compaction_threshold(ui, page):
    ui.mount("components/ContextMeter.js", f"""{{ info: {{ ...{CONTEXT}, breakdown: {{
        system_instructions: 1000, tool_definitions: 2000, messages_and_results: 2500, summary: 400, request_overhead: 100 }} }} }}""")
    details = page.locator("details.context-meter")
    expect(details).not_to_have_attribute("open", "")
    page.get_by_text("Last model input · ~5%").click()
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
    expect(definition(page, "Conversation summary")).to_have_text("~0 tokens")
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
    summary = page.get_by_text("Last model input · 10,000 tokens · 8.3%")
    expect(summary).to_be_visible()
    bar = page.get_by_role("progressbar", name="Reported model input usage")
    expect(bar).to_have_attribute("aria-valuetext", "8.3% of input budget: 10,000 of 120,832 tokens reported by "
                                  "Databricks; 1% of the model's 1,000,000-token window")
    expect(page.get_by_role("progressbar", name="Estimated model input usage")).to_have_count(0)
    expect(page.locator(".context-bar-mark")).to_have_attribute("style", re.compile("left: ?90%"))
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


def test_a_report_from_before_a_compaction_is_labelled_and_the_estimate_leads(ui, page):
    ui.mount("components/ContextMeter.js", f"{{ info: {{ ...{REPORTED}, compactions: 2, prepared_for_next_turn: true }} }}")
    summary = page.get_by_text("Context preview · ~7%")
    expect(summary).to_be_visible()
    expect(page.get_by_role("progressbar", name="Estimated model input usage")).to_have_attribute(
        "aria-valuetext", "Approximately 7% of input budget")
    summary.click()
    expect(page.get_by_text("Before compaction · reported by Databricks")).to_be_visible()
    expect(page.get_by_text(re.compile("Databricks counted"))).to_have_count(0)
    # Not a preview, but a compaction ran since the report: still history.
    ui.rerender(f"{{ info: {{ ...{REPORTED}, compactions: 2 }} }}")
    expect(page.get_by_text("Last model input · ~7%")).to_be_visible()
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
    expect(page.get_by_text("Last model input · 10,000 tokens · 8.3%")).to_be_visible()
    expect(page.get_by_text("Last request · reported by Databricks")).to_be_visible()
    # A request to the new model was prepared (streaming, or it failed): the report is history.
    ui.rerender(f"{{ info: {{ ...{REPORTED}, estimated_tokens: 8800 }}, model: 'databricks-gpt-oss-120b' }}")
    expect(page.get_by_text("Last model input · ~7%")).to_be_visible()
    expect(page.get_by_text("Earlier request · reported by Databricks")).to_be_visible()
    ui.rerender(f"{{ info: {REPORTED}, model: 'databricks-claude-opus-5-5' }}")
    expect(page.get_by_text("Last model input · 10,000 tokens · 8.3%")).to_be_visible()
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
    summary = page.get_by_text("Next request · ~89%")
    expect(summary).to_be_visible()
    expect(page.get_by_text(re.compile("115%"))).to_have_count(0)
    expect(page.locator(".context-bar")).not_to_have_class(re.compile("over"))
    expect(page.get_by_role("progressbar", name="Estimated next request input usage")).to_have_attribute(
        "aria-valuetext", "Approximately 89% of input budget")
    summary.click()
    expect(page.get_by_text("Earlier request · reported by Databricks")).to_be_visible()
    expect(page.get_by_text(re.compile(r"^Next request: ~107,003 tokens \(88\.6% of the input budget\)"))).to_be_visible()
    # Once a request to the new model is prepared, the meter shows that request's own estimate again.
    ui.rerender(f"{{ info: {{ ...{SWITCHED}, estimated_tokens: 107500, estimate_scale: undefined }}, "
                "model: 'databricks-gpt-oss-120b' }")
    expect(page.get_by_text("Last model input · ~89%")).to_be_visible()


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
    summary = page.get_by_text("Last model input · 10,000 tokens · 8.3% (partial)")
    expect(summary).to_be_visible()
    expect(page.get_by_role("progressbar", name="Reported model input usage")).to_have_attribute(
        "aria-valuetext", "8.3% of input budget: 10,000 of 120,832 tokens reported by Databricks")
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
    expect(page.get_by_text("Last model input · ~91%")).to_be_visible()
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
    expect(page.get_by_text(re.compile("8.3% \\(partial\\)"))).to_be_visible()
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
