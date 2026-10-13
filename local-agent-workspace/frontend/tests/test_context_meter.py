import re

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
    ui.rerender(f"{{ info: {CONTEXT} }}")
    expect(page.get_by_text(re.compile("over its size limit"))).to_have_count(0)


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
    # The conversation switched models since the report.
    ui.rerender(f"{{ info: {REPORTED}, model: 'databricks-gpt-oss-120b' }}")
    expect(page.get_by_text("Last model input · ~7%")).to_be_visible()
    expect(page.get_by_text("Earlier request · reported by Databricks")).to_be_visible()
    ui.rerender(f"{{ info: {REPORTED}, model: 'databricks-claude-opus-5-5' }}")
    expect(page.get_by_text("Last model input · 10,000 tokens · 8.3%")).to_be_visible()
    expect(page.get_by_text("Last request · reported by Databricks")).to_be_visible()


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
