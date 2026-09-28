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
    ui.rerender(f"{{ info: {{ ...{CONTEXT}, summary_adjustment: 'trimmed' }} }}")
    expect(page.get_by_text(re.compile("Heuristic text-size estimate"))).to_be_visible()
    expect(page.get_by_text(re.compile(r"part of it was omitted\. Full history is preserved\."))).to_be_visible()
    ui.rerender(f"{{ info: {CONTEXT} }}")
    expect(page.get_by_text(re.compile("over its size limit"))).to_have_count(0)
