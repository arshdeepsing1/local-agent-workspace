import json
import re

import pytest
from playwright.sync_api import expect

SETUP = """
window.makeSession = (events, overrides = {}) => ({
  id: 'parent', title: 'Parent task', workspace: '/project', model: 'test-model', status: 'idle',
  events, updated: 1, permission_mode: 'manual', allowed_directories: [], ...overrides,
})
"""


def show(ui, events: str, extra: str = "", setup: str = ""):
    ui.mount("components/Conversation.js", f"{{ session: makeSession({events}), onError: fake.spy('onError'){extra} }}",
             setup=SETUP + setup)


def details_for(page, summary_text):
    return page.locator("details").filter(has=page.get_by_text(summary_text, exact=True)).last


def definition(scope, label):
    return scope.locator(f'xpath=.//dt[normalize-space()="{label}"]/following-sibling::dd[1]')


def test_delegation_progress_updates_without_expanding(ui, page):
    event = """{ id: 'delegate', type: 'tool', name: 'delegate_task', input: { task: 'Review the parser' },
      state: 'running', child_session_id: 'child-1', delegation: { status: 'running', completed_tools: 0 } }"""
    show(ui, f"[{event}]", ", onSelectSession: fake.spy('select')")
    heading = page.get_by_role("button", name="delegate task Review the parser")
    expect(heading).to_have_attribute("aria-expanded", "false")
    expect(page.get_by_label("Subagent progress")).to_contain_text("Subagent: running")
    ui.rerender(f"""{{ session: makeSession([{{ ...{event}, delegation: {{ status: 'awaiting_approval', completed_tools: 1,
      last_tool: 'run_command' }} }}]), onError: fake.spy('onError'), onSelectSession: fake.spy('select') }}""")
    expect(page.get_by_text("Subagent: Waiting for approval")).to_be_visible()
    expect(page.get_by_text("1 completed action", exact=True)).to_be_visible()
    expect(page.get_by_text("Last tool: run command")).to_be_visible()
    expect(page.get_by_label("Input")).to_have_count(0)
    page.get_by_role("button", name="Open subagent").click()
    assert ui.spy("select") == [["child-1"]]
    ui.rerender(f"""{{ session: makeSession([{{ ...{event}, state: 'completed', delegation: {{ status: 'failed', completed_tools: 2,
      last_tool: 'read_file', terminal_reason: 'step_limit' }} }}]), onError: fake.spy('onError'), onSelectSession: fake.spy('select') }}""")
    expect(page.get_by_text("Subagent: failed")).to_be_visible()
    expect(page.get_by_text("Reason: step limit")).to_be_visible()
    expect(page.get_by_text("2 completed actions")).to_be_visible()
    expect(heading).to_have_attribute("aria-expanded", "false")


def test_only_the_delegated_prompt_shows_provenance(ui, page):
    ui.mount("components/Conversation.js", """{ onError: fake.spy('onError'), session: makeSession([
      { id: 'delegated', type: 'user', text: 'Review source', origin: {
        kind: 'delegated', parent_session_id: 'parent', parent_event_id: 'delegate-1', parent_call_id: 'call-1' } },
      { id: 'human', type: 'user', text: 'Check one more thing' },
    ], { id: 'child', parent_session_id: 'parent', is_subagent: true }) }""", setup=SETUP)
    expect(page.get_by_text("Delegated by parent conversation")).to_have_count(1)
    expect(page.locator(".user-message").nth(1)).to_have_text("Check one more thing")


def test_copies_raw_user_and_assistant_text(ui, page):
    show(ui, """[
      { id: 'user', type: 'user', text: 'Review this path:\\n/project/file.ts', child_session_id: 'child' },
      { id: 'empty-user', type: 'user', text: '' },
      { id: 'reply', type: 'assistant', text: 'I reviewed **the file**.' },
    ]""", ", onSelectSession: fake.spy('select')")
    expect(page.get_by_role("button", name="Copy message")).to_have_count(1)
    page.get_by_role("button", name="Copy message").click()
    ui.wait("fake.clipboard.length === 1")
    page.get_by_role("button", name="Copy response").click()
    ui.wait("fake.clipboard.length === 2")
    assert ui.js("fake.clipboard") == ["Review this path:\n/project/file.ts", "I reviewed **the file**."]
    page.get_by_role("button", name="Open subagent").click()
    assert ui.spy("select") == [["child"]]


def test_reasoning_is_collapsed_and_separate_from_the_answer(ui, page):
    show(ui, """[{ id: 'reply', type: 'assistant', text: 'The answer is ready.', reasoning_summary: 'Provider considered **two approaches**.' }]""")
    summary = page.get_by_text("Thought", exact=True)
    details = page.locator("details.thinking")
    expect(details).not_to_have_attribute("open", "")
    expect(details.get_by_text("The answer is ready.")).to_have_count(0)
    summary.click()
    expect(details).to_have_attribute("open", "")
    expect(details.locator("strong", has_text="two approaches")).to_be_visible()
    expect(page.get_by_text("The answer is ready.")).to_have_count(1)
    summary.click()
    expect(details).not_to_have_attribute("open", "")
    expect(page.get_by_text("The answer is ready.")).to_be_visible()


def test_ordinary_messages_have_no_invented_reasoning_or_activity(ui, page):
    show(ui, """[{ id: 'user', type: 'user', text: 'Please explain the parser.' },
      { id: 'reply', type: 'assistant', text: 'I considered two approaches. Here is the answer.' }]""")
    expect(page.get_by_text("I considered two approaches. Here is the answer.")).to_be_visible()
    expect(page.locator(".thinking, .thinking-line")).to_have_count(0)
    expect(page.get_by_text(re.compile("Activity ·"))).to_have_count(0)
    expect(page.get_by_text("Thinking display limit reached.")).to_have_count(0)
    expect(page.get_by_text(re.compile("Request details"))).to_have_count(0)


def test_markdown_renders_gfm_and_shows_raw_html_as_text(ui, page):
    code = ('\\n\\n```html\\n<img src=x onerror="window.pwned=2"><script>window.pwned=3</script>\\n```'
            '\\n\\n```\\n<img src=x onerror="window.pwned=4">\\n```'
            '\\n\\n```"><img src=x onerror=window.pwned=5>\\nplain\\n```')
    show(ui, """[{ id: 'reply', type: 'assistant', text: '| a | b |\\n|---|---|\\n| 1 | ~~2~~ |\\n\\n<img src=x onerror="window.pwned=1"> [ok](https://example.com) [bad](javascript:alert(1))""" + code + "' }]")
    expect(page.locator(".markdown table td del")).to_have_text("2")
    expect(page.locator(".markdown img")).to_have_count(0)
    expect(page.locator(".markdown script")).to_have_count(0)
    expect(page.locator(".markdown")).to_contain_text('<img src=x onerror="window.pwned=1">')
    expect(page.get_by_role("link", name="ok")).to_have_attribute("href", "https://example.com")
    expect(page.locator(".markdown a", has_text="bad")).not_to_have_attribute("href", re.compile("javascript"))
    # Code blocks are highlighted (or left plain) from their text, never parsed as HTML.
    blocks = page.locator(".markdown .code-block pre code")
    expect(blocks).to_have_count(3)
    expect(blocks.nth(0)).to_have_class(re.compile(r"\bhljs\b"))
    expect(blocks.nth(0)).to_have_text('<img src=x onerror="window.pwned=2"><script>window.pwned=3</script>')
    expect(blocks.nth(1)).to_have_text('<img src=x onerror="window.pwned=4">')
    expect(blocks.nth(2)).to_have_text("plain")
    page.wait_for_timeout(100)
    assert ui.js("window.pwned === undefined")


def test_finished_code_blocks_are_highlighted_and_copy_raw_code(ui, page):
    code = 'def greet(name):\n    return f"Hi {name} & <bye>"'
    show(ui, "[{ id: 'reply', type: 'assistant', text: " + json.dumps(f"Run this:\n\n```python\n{code}\n```") + " }]")
    block = page.locator(".markdown pre code")
    expect(block).to_have_class(re.compile(r"\bhljs\b"))
    expect(block.locator(".hljs-keyword").first).to_have_text("def")
    expect(block.locator(".hljs-string")).to_have_count(1)
    copy = page.get_by_role("button", name="Copy code")
    copy.focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Copied")).to_be_visible()
    assert ui.js("fake.clipboard") == [code]
    expect(page.get_by_role("button", name="Copy code")).to_be_visible()


@pytest.mark.parametrize("tag,grammar_word", [("c#", "namespace"), ("c++", "template"), ("cpp", "template")])
def test_code_blocks_use_the_tagged_language_including_aliases_with_symbols(ui, page, tag, grammar_word):
    # "namespace" and "template" are keywords in C# and C++, not in C.
    code = "namespace App { template<typename T> class Box {}; }"
    show(ui, "[{ id: 'reply', type: 'assistant', text: " + json.dumps(f"```{tag}\n{code}\n```") + " }]")
    block = page.locator(".markdown pre code")
    expect(block).to_have_class(re.compile(r"\bhljs\b"))
    expect(block).not_to_have_class(re.compile(r"(^|\s)language-c(\s|$)"))
    expect(block.locator(".hljs-keyword", has_text=grammar_word)).to_have_count(1)
    expect(block).to_have_text(code)


def test_code_is_highlighted_only_after_the_reply_finishes_and_only_once(ui, page):
    ui.start(setup=SETUP)
    ui.js("""import('/static/vendor/highlight.min.js').then(({ default: hljs }) => {
      const highlight = hljs.highlight.bind(hljs), auto = hljs.highlightAuto.bind(hljs)
      window.highlighted = []
      hljs.highlight = (text, options) => { window.highlighted.push(text); return highlight(text, options) }
      hljs.highlightAuto = text => { window.highlighted.push(text); return auto(text) }
    })""")
    done = "{ id: 'done', type: 'assistant', text: '```js\\nconst done = 1\\n```' }"
    def props(text, status):
        return ("{ onError: fake.spy('onError'), session: makeSession([" + done + ", { id: 'live', type: 'assistant', text: "
                + json.dumps(text) + ", request_info: { model: 'm', status: '" + status + "' } }]) }")
    page.evaluate("() => mount('components/Conversation.js', () => (" + props("```sh\nls", "running") + "))")
    expect(page.get_by_role("button", name="Copy code")).to_have_count(1)
    ui.run("window.finishedBlock = document.querySelector('.code-block')")
    for text in ("```sh\nls -la", "```sh\nls -la\n```\n\nMore"):
        ui.rerender(props(text, "running"))
        expect(page.locator(".markdown").nth(1)).to_contain_text(text.split("\n")[1])
    expect(page.locator(".markdown").nth(1).locator("code.hljs")).to_have_count(0)
    assert ui.js("window.highlighted") == ["const done = 1\n"]
    ui.rerender(props("```sh\nls -la\n```\n\nMore", "completed"))
    expect(page.get_by_role("button", name="Copy code")).to_have_count(2)
    expect(page.locator(".markdown").nth(1).locator("code.hljs")).to_have_count(1)
    assert ui.js("window.highlighted") == ["const done = 1\n", "ls -la\n"]
    assert ui.js("document.querySelector('.code-block') === window.finishedBlock")


def test_large_untagged_and_unknown_language_blocks_stay_plain_but_copyable(ui, page):
    large = "x = 1\n" * 4000  # 24 KB
    text = f"```\n{large}```\n\n```python\n{large}```\n\n```no-such-language\nlet x = 1\n```"
    show(ui, "[{ id: 'reply', type: 'assistant', text: " + json.dumps(text) + " }]")
    blocks = page.locator(".markdown pre code")
    expect(page.get_by_role("button", name="Copy code")).to_have_count(3)
    expect(blocks.nth(0)).not_to_have_class(re.compile(r"\bhljs\b"))
    expect(blocks.nth(1)).to_have_class(re.compile(r"\bhljs\b"))
    expect(blocks.nth(2)).not_to_have_class(re.compile(r"\bhljs\b"))
    page.get_by_role("button", name="Copy code").nth(0).click()
    ui.wait("fake.clipboard.length === 1")
    assert ui.js("fake.clipboard[0]") == large.rstrip("\n")


def test_usage_snapshot_for_event_without_text(ui, page):
    show(ui, """[{ id: 'request', type: 'assistant', request_info: {
      model: 'databricks-test-model', status: 'completed', finish_reason: 'tool_calls', http_status: 200,
      usage: { input_tokens: 1000, output_tokens: 200, total_tokens: 1200, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, reasoning_tokens: 50 } } }]""")
    summary = page.get_by_text("Request details · completed")
    details = page.locator("details.request-details")
    expect(details).not_to_have_attribute("open", "")
    expect(page.get_by_role("button", name="Copy response")).to_have_count(0)
    summary.click()
    expect(details).to_have_attribute("open", "")
    expect(definition(details, "Model endpoint")).to_have_text("databricks-test-model")
    expect(definition(details, "Finish reason")).to_have_text("tool_calls")
    expect(definition(details, "HTTP status")).to_have_text("200")
    for label, value in [("Input tokens", "1,000"), ("Output tokens", "200"), ("Total tokens", "1,200"),
                         ("Cache read input tokens", "0"), ("Cache creation input tokens", "0"), ("Reasoning tokens", "50")]:
        expect(definition(details, label)).to_have_text(value)
    expect(details.get_by_text(re.compile("excludes title and summary calls"))).to_be_visible()
    expect(details.get_by_text("Usage received before completion may be partial.")).to_have_count(0)
    expect(details).not_to_contain_text("%")
    summary.click()
    expect(details).not_to_have_attribute("open", "")


def test_a_reply_from_the_fallback_endpoint_says_so(ui, page):
    show(ui, """[{ id: 'fallback', type: 'assistant', text: 'Answered.', request_info: { model: 'databricks-gpt-oss-120b',
      status: 'completed', http_status: 200, fallback_from: 'databricks-claude-opus-5-5', fallback_reason: 'HTTP 503' } },
      { id: 'own', type: 'assistant', text: 'Own.', request_info: { model: 'databricks-claude-opus-5-5', status: 'completed' } },
      { id: 'failed', type: 'assistant', request_info: { model: 'databricks-gpt-oss-120b', status: 'error', http_status: 503,
        fallback_from: 'databricks-claude-opus-5-5', fallback_reason: 'connection failure' } }]""")
    fallback, own, failed = page.locator("details.request-details").all()
    fallback.locator("summary").click()
    expect(definition(fallback, "Model endpoint")).to_have_text("databricks-gpt-oss-120b")
    expect(definition(fallback, "Answered by")).to_have_text("databricks-gpt-oss-120b (fallback)")
    expect(definition(fallback, "Fallback from")).to_have_text("databricks-claude-opus-5-5 (HTTP 503)")
    own.locator("summary").click()
    expect(definition(own, "Model endpoint")).to_have_text("databricks-claude-opus-5-5")
    expect(own.get_by_text("Answered by")).to_have_count(0)
    # A fallback request that failed too did not answer.
    failed.locator("summary").click()
    expect(definition(failed, "Sent to")).to_have_text("databricks-gpt-oss-120b (fallback)")
    expect(definition(failed, "Fallback from")).to_have_text("databricks-claude-opus-5-5 (connection failure)")
    expect(failed.get_by_text("Answered by")).to_have_count(0)


def test_claude_reasoning_tokens_read_as_part_of_the_output(ui, page):
    usage = "{ input_tokens: 1000, output_tokens: 200, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }"
    anthropic = "{ effort: 'high', format: 'anthropic', fields: { thinking: { type: 'adaptive' } }, effort_field: 'output_config.effort' }"
    show(ui, f"""[
      {{ id: 'claude', type: 'assistant', text: 'A', request_info: {{ model: 'databricks-claude-opus-5-5', status: 'completed', usage: {usage}, reasoning: {anthropic} }} }},
      {{ id: 'other', type: 'assistant', text: 'B', request_info: {{ model: 'm', status: 'completed', usage: {usage} }} }},
      {{ id: 'none', type: 'assistant', text: 'C', request_info: {{ model: 'databricks-claude-opus-5-5', status: 'error', reasoning: {anthropic} }} }}]""")
    for summary in page.get_by_text(re.compile("^Request details · ")).all():
        summary.click()
    rows = page.locator('xpath=//dt[normalize-space()="Reasoning tokens"]/following-sibling::dd[1]')
    # Claude counts its thinking in output tokens; without usage, or for other endpoints, nothing was reported.
    expect(rows).to_have_text(["In output tokens (not reported separately)", "Unavailable", "Unavailable"])
    output = page.locator('xpath=//dt[normalize-space()="Output tokens"]/following-sibling::dd[1]')
    expect(output).to_have_text(["200", "200", "Unavailable"])


def test_request_metadata_updates_in_place(ui, page):
    running = "{ id: 'request', type: 'assistant', request_info: { model: 'model-a', status: 'running' } }"
    show(ui, f"[{running}]")
    page.get_by_text("Request details · running").click()
    details = page.locator("details.request-details")
    expect(definition(details, "Input tokens")).to_have_text("Unavailable")
    ui.rerender(f"""{{ onError: fake.spy('onError'), session: makeSession([{{ ...{running}, request_info: {{ model: 'model-a',
      status: 'completed', usage: {{ input_tokens: 12, output_tokens: 0 }} }} }}]) }}""")
    expect(details).to_have_attribute("open", "")
    expect(page.get_by_text("Request details · completed")).to_be_visible()
    expect(definition(details, "Input tokens")).to_have_text("12")
    expect(definition(details, "Output tokens")).to_have_text("0")
    expect(definition(details, "Total tokens")).to_have_text("Unavailable")
    expect(definition(details, "Finish reason")).to_have_text("Unavailable")
    expect(definition(details, "HTTP status")).to_have_text("Unavailable")
    expect(details.get_by_text("Error category")).to_have_count(0)


@pytest.mark.parametrize("error_kind", ["authentication", "permission", "rate_limit", "invalid_request", "server", "network",
                                        "output_limit", "invalid_tool_arguments", "incomplete_response", "invalid_response", "unknown"])
def test_request_error_category(ui, page, error_kind):
    show(ui, f"""[{{ id: 'failed', type: 'assistant', request_info: {{ model: 'model-b', status: 'error', error_kind: '{error_kind}', http_status: 429 }} }}]""")
    page.get_by_text("Request details · error").click()
    expect(definition(page, "Error category")).to_have_text(error_kind.replace("_", " "))
    expect(definition(page, "HTTP status")).to_have_text("429")
    expect(definition(page, "Input tokens")).to_have_text("Unavailable")
    expect(page.get_by_role("button", name="Copy response")).to_have_count(0)


def test_cancelled_and_interrupted_snapshots(ui, page):
    show(ui, """[
      { id: 'cancelled', type: 'assistant', request_info: { model: 'model-a', status: 'cancelled', usage: { total_tokens: 15 } } },
      { id: 'interrupted', type: 'assistant', request_info: { model: 'model-b', status: 'interrupted' } },
      { id: 'legacy-empty', type: 'assistant' },
    ]""")
    expect(page.get_by_text(re.compile("Request details ·"))).to_have_count(2)
    cancelled = details_for(page, "Request details · cancelled")
    interrupted = details_for(page, "Request details · interrupted")
    cancelled.locator("summary").click()
    interrupted.locator("summary").click()
    expect(definition(cancelled, "Model endpoint")).to_have_text("model-a")
    expect(definition(cancelled, "Total tokens")).to_have_text("15")
    expect(cancelled.get_by_text("Usage received before completion may be partial.")).to_be_visible()
    expect(definition(interrupted, "Model endpoint")).to_have_text("model-b")
    expect(definition(interrupted, "Total tokens")).to_have_text("Unavailable")
    expect(interrupted.get_by_text("Usage received before completion may be partial.")).to_have_count(0)


def test_usage_before_failure_is_marked_partial(ui, page):
    show(ui, """[{ id: 'failed-stream', type: 'assistant', request_info: { model: 'model-a', status: 'error',
      error_kind: 'incomplete_response', usage: { output_tokens: 0 } } }]""")
    page.get_by_text("Request details · error").click()
    expect(page.get_by_text("Usage received before completion may be partial.")).to_be_visible()
    expect(definition(page, "Output tokens")).to_have_text("0")


@pytest.mark.parametrize("truncated", [True, False])
def test_reasoning_display_limit_notice(ui, page, truncated):
    show(ui, f"""[{{ id: 'reply', type: 'assistant', reasoning_summary: 'A provider-supplied partial summary.',
      reasoning_truncated: {json.dumps(truncated)} }}]""")
    summary = page.get_by_text("Thought", exact=True)
    expect(page.locator("details.thinking")).not_to_have_attribute("open", "")
    summary.click()
    expect(page.get_by_text("A provider-supplied partial summary.")).to_be_visible()
    expect(page.get_by_text("Thinking display limit reached.")).to_have_count(1 if truncated else 0)


def test_activity_summary_counts_one_action_in_the_singular(ui, page):
    show(ui, "[{ id: 'only', type: 'tool', name: 'list_files', state: 'completed', input: { path: '.' } }]")
    expect(page.get_by_text("Activity · 1 action", exact=True)).to_be_visible()


def test_find_files_and_wait_job_cards_name_their_pattern_and_job(ui, page):
    show(ui, """[
      { id: 'find', type: 'tool', name: 'find_files', state: 'completed', input: { pattern: 'src/**/*.test.ts', path: 'web' } },
      { id: 'find-here', type: 'tool', name: 'find_files', state: 'completed', input: { pattern: '*.py', path: '.' } },
      { id: 'wait', type: 'tool', name: 'wait_job', state: 'completed', input: { job_id: '0123456789abcdef', until_regex: 'ready on [0-9]+' } },
      { id: 'wait-exit', type: 'tool', name: 'wait_job', state: 'running', input: { job_id: 'fedcba9876543210' } },
    ]""")
    expect(page.get_by_role("button", name="find files src/**/*.test.ts in web", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="find files *.py", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="wait job 01234567 until /ready on [0-9]+/", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="wait job fedcba98", exact=True)).to_be_visible()
    page.get_by_text("Activity · 4 actions").click()
    expect(page.locator("details.activity-summary").get_by_text("· src/**/*.test.ts in web")).to_be_visible()
    expect(page.locator("details.activity-summary").get_by_text("· 01234567 until /ready on [0-9]+/")).to_be_visible()


def test_activity_summary_keeps_states_and_approval(ui, page):
    states = ["running", "pending", "completed", "rejected", "cancelled", "error"]
    events = "[" + ",".join(f"{{ id: 'tool-{i}', type: 'tool', name: 'action_{i}', state: '{state}', input: {{ path: 'file-{i}' }} }}"
                            for i, state in enumerate(states)) + "]"
    show(ui, events)
    details = page.locator("details.activity-summary")
    expect(details).not_to_have_attribute("open", "")
    expect(page.get_by_role("button", name="Approve")).to_be_visible()
    page.get_by_text("Activity · 6 actions").click()
    expect(details).to_have_attribute("open", "")
    rows = details.get_by_role("listitem")
    expect(rows).to_have_count(len(states))
    for index, state in enumerate(states):
        expect(rows.nth(index).get_by_text(f"action {index}", exact=True)).to_be_visible()
        expect(rows.nth(index).get_by_text(f"Action: {state}")).to_be_visible()
    ui.rerender(f"{{ onError: fake.spy('onError'), session: makeSession({events}.map(event => ({{ ...event, state: 'completed' }}))) }}")
    expect(details.get_by_text("Action: completed")).to_have_count(6)
    expect(page.get_by_role("button", name="Approve")).to_have_count(0)
    page.get_by_text("Activity · 6 actions").click()
    expect(details).not_to_have_attribute("open", "")


def test_distinct_failed_tool_diagnostics(ui, page):
    show(ui, """[
      { id: 'file', type: 'tool', name: 'search_files', state: 'error', input: { path: 'run.log' }, output: 'Search path must be a regular file or directory.' },
      { id: 'timeout', type: 'tool', name: 'search_files', state: 'error', input: { path: '/large' }, output: 'File query time limit reached; narrow the path, glob, or line range.' },
      { id: 'regex', type: 'tool', name: 'search_files', state: 'error', input: { query: 'connect(' }, output: 'Invalid regular expression: missing ).' },
    ]""")
    expect(page.get_by_text("Search path must be a regular file or directory.")).to_be_visible()
    expect(page.get_by_text("File query time limit reached; narrow the path, glob, or line range.")).to_be_visible()
    expect(page.get_by_text("Invalid regular expression: missing ).")).to_be_visible()
    expect(page.get_by_text("Tool returned an error")).to_have_count(0)


def test_task_actions_are_named_with_task_status(ui, page):
    show(ui, """[
      { id: 'create', type: 'tool', name: 'create_task', state: 'completed', input: { title: 'Write exporter script' },
        output: JSON.stringify({ id: 'task-1', title: 'Write exporter script', status: 'pending' }) },
      { id: 'create-other', type: 'tool', name: 'create_task', state: 'completed', input: { title: 'Render portable HTML' },
        output: JSON.stringify({ id: 'task-2', title: 'Render portable HTML', status: 'pending' }) },
      { id: 'update', type: 'tool', name: 'update_task', state: 'completed', input: { task_id: 'task-1', status: 'in_progress' },
        output: JSON.stringify({ id: 'task-1', title: 'Write exporter script', status: 'in_progress' }) },
    ]""")
    create = page.get_by_role("button", name="create task Write exporter script Task: pending", exact=True)
    update = page.get_by_role("button", name="update task Write exporter script Task: in progress", exact=True)
    expect(create).to_have_attribute("aria-expanded", "false")
    expect(update).to_have_attribute("aria-expanded", "false")
    expect(page.get_by_role("button", name="create task Render portable HTML Task: pending", exact=True)).to_be_visible()
    page.get_by_text("Activity · 3 actions").click()
    row = page.get_by_role("listitem").nth(2)
    expect(row).to_contain_text("Write exporter script")
    expect(row.get_by_text("Task: in progress")).to_be_visible()
    expect(row.get_by_text("Action: completed")).to_be_visible()


def test_task_updates_do_not_claim_requested_status(ui, page):
    create = """{ id: 'create', type: 'tool', name: 'create_task', state: 'completed', input: { title: 'Verify exported HTML' },
      output: JSON.stringify({ id: 'task-1', status: 'pending' }) }"""
    update = "{ id: 'update', type: 'tool', name: 'update_task', input: { task_id: 'task-1', status: 'completed' } }"
    show(ui, f"[{create}, {{ ...{update}, state: 'running', output: '' }}]")
    expect(page.get_by_role("button", name="update task Verify exported HTML", exact=True)).to_be_visible()
    expect(page.get_by_text("Task: completed")).to_have_count(0)
    ui.rerender(f"{{ onError: fake.spy('onError'), session: makeSession([{create}, {{ ...{update}, state: 'error', output: 'Complete dependencies first.' }}]) }}")
    # A failed tool names its status after the subject (no error category here: "failed").
    page.get_by_role("button", name="update task Verify exported HTML failed", exact=True).click()
    expect(page.get_by_label("Output")).to_have_text("Complete dependencies first.")
    expect(page.get_by_text("Task: completed")).to_have_count(0)


def test_command_and_options_show_only_after_expansion(ui, page):
    command = "python - <<'PY'\n# " + "script content " * 50 + "\nprint('Export ready')\nPY"
    show(ui, f"""[{{ id: 'python', type: 'tool', name: 'run_command', state: 'completed',
      input: {{ command: {json.dumps(command)}, timeout_seconds: 90 }}, output: 'Export ready' }}]""")
    expect(page.get_by_label("Command", exact=True)).to_have_count(0)
    expect(page.get_by_label("Output")).to_have_count(0)
    heading = page.get_by_role("button", name=re.compile("run command"))
    assert len(heading.text_content()) < 270
    heading.click()
    assert page.get_by_label("Command", exact=True).text_content() == command
    expect(page.get_by_label("Options")).to_contain_text('"timeout_seconds": 90')
    expect(page.get_by_label("Output")).to_have_text("Export ready")
    heading.click()
    expect(page.get_by_label("Command", exact=True)).to_have_count(0)
    expect(page.get_by_label("Output")).to_have_count(0)


def test_completed_tool_keeps_input_without_output(ui, page):
    show(ui, """[{ id: 'write', type: 'tool', name: 'write_file', state: 'completed',
      input: { path: 'result.txt', content: 'Report contents' }, output: '' }]""")
    page.get_by_role("button", name="write file result.txt").click()
    expect(page.get_by_label("Input")).to_contain_text("Report contents")
    expect(page.get_by_label("Output")).to_have_text("No output.")


def test_approval_keeps_details_and_sends_decision(ui, page):
    command = "python -c \"print('ready')\""
    preview = f"{command}\n\nTimeout: 60 seconds · Output limit: 80000 bytes\nForeground command: Stop response also stops this job."
    show(ui, f"""[{{ id: 'approval', type: 'tool', name: 'run_command', state: 'pending',
      input: {{ command: {json.dumps(command)} }}, preview: {json.dumps(preview)} }}]""",
         setup="fake.handle = () => new Response('{}', { status: 200 })")
    assert page.get_by_label("Command", exact=True).text_content() == preview
    expect(page.get_by_label("Output")).to_have_count(0)
    page.get_by_role("button", name="Approve").click()
    ui.wait("fake.calls.length === 1")
    assert ui.requests("/api/sessions/parent/approvals/approval", "POST")[0]["body"] == '{"allowed":true}'


def test_an_approval_with_a_limit_shows_when_it_expires_and_an_expired_one_says_so(ui, page):
    card = "{ id: 'approval', type: 'tool', name: 'write_file', state: 'pending', input: { path: 'a.txt' }, preview: 'diff' }"
    show(ui, f"[{card}]")
    expect(page.get_by_text("Local needs your approval to write file.", exact=True)).to_be_visible()
    expires = "new Date().setHours(15, 42, 0, 0) / 1000"  # Today, so the card shows only the time.
    ui.rerender(f"{{ onError: fake.spy('onError'), session: makeSession([{{ ...{card}, approval_expires: {expires} }}]) }}")
    time = ui.js(f"new Date({expires} * 1000).toLocaleTimeString([], {{ hour: '2-digit', minute: '2-digit' }})")
    expect(page.get_by_text(f"Local needs your approval to write file. If you do not answer by {time}, it will not run.")).to_be_visible()
    ui.rerender(f"{{ onError: fake.spy('onError'), session: makeSession([{{ ...{card}, state: 'rejected', approval: 'expired' }}, "
                f"{{ ...{card}, id: 'declined', state: 'rejected', approval: 'declined' }}]) }}")
    expect(page.get_by_text("Not answered in time; the action did not run")).to_have_count(1)
    expect(page.get_by_text("Action declined")).to_have_count(1)
    # A limit can reach 24 hours, so an expiry on another day names the weekday.
    module = "import('/static/js/components/Conversation.js')"
    now = "new Date(2026, 9, 6, 15, 42)"
    later_today = "new Date(2026, 9, 6, 18, 5).getTime() / 1000"
    tomorrow = "new Date(2026, 9, 7, 15, 42).getTime() / 1000"
    assert ui.js(f"{module}.then(m => m.expiryTime({later_today}, {now}))") == ui.js(
        f"new Date({later_today} * 1000).toLocaleTimeString([], {{ hour: '2-digit', minute: '2-digit' }})")
    assert ui.js(f"{module}.then(m => m.expiryTime({tomorrow}, {now}))") == ui.js(
        f"new Date({tomorrow} * 1000).toLocaleString([], {{ weekday: 'short', hour: '2-digit', minute: '2-digit' }})")


def test_an_action_blocked_without_an_approval_card_says_why_instead_of_declined(ui, page):
    blocked = ("{ id: 'blocked', type: 'tool', name: 'write_file', state: 'rejected', input: { path: 'sub/x.py' }, "
               "output: 'Project instructions were discovered or changed. Review the refreshed project guidance first.' }")
    show(ui, f"[{blocked}, {{ id: 'plan', type: 'tool', name: 'run_command', state: 'rejected', input: {{ command: 'ls' }} }}]")
    expect(page.get_by_text("Not run: Project instructions were discovered or changed.", exact=False)).to_be_visible()
    expect(page.get_by_text("Action not run", exact=True)).to_be_visible()
    expect(page.get_by_text("Action declined")).to_have_count(0)


@pytest.mark.parametrize("kind", ["notice", "user"])
def test_child_link_navigation_and_back(ui, page, kind):
    setup = SETUP + f"""
    window.parent = makeSession([{{ id: 'link', type: '{kind}', text: 'Delegated parser review.', child_session_id: 'child' }}])
    window.child = makeSession([{{ id: 'child-reply', type: 'assistant', text: 'Child findings.' }}],
      {{ id: 'child', title: 'Child task', parent_session_id: 'parent', is_subagent: true }})
    window.navigation = id => {{
      const session = id === 'child' ? child : parent
      rerender(() => ({{ key: session.id, session, onError: fake.spy('onError'), onSelectSession: navigation }}))
    }}"""
    ui.mount("components/Conversation.js", "{ key: 'parent', session: parent, onError: fake.spy('onError'), onSelectSession: navigation }", setup=setup)
    expect(page.get_by_role("button", name="Back to parent conversation")).to_have_count(0)
    page.get_by_role("button", name="Open subagent").click()
    expect(page.get_by_text("Child findings.")).to_be_visible()
    expect(page.get_by_text("Delegated parser review.")).to_have_count(0)
    page.get_by_role("button", name="Back to parent conversation").click()
    expect(page.get_by_text("Delegated parser review.")).to_be_visible()
    expect(page.get_by_text("Child findings.")).to_have_count(0)
    expect(page.get_by_role("button", name="Open subagent")).to_have_count(1)


def test_pages_of_one_file_show_the_lines_each_read_covered(ui, page):
    # Reported: consecutive pages of a long file looked like the same read repeated.
    def read(event_id, start, end=None, state="completed", output=None):
        page_json = json.dumps({"path": "notes/v7.md", "start_line": start, "end_line": end, "content": "…"})
        return (f"{{ id: '{event_id}', type: 'tool', name: 'read_file', state: '{state}', "
                f"input: {{ path: 'notes/v7.md'{f', start_line: {start}' if start > 1 else ''} }}, "
                f"output: {json.dumps(output if output is not None else page_json)} }}")
    show(ui, "[" + ", ".join([read("p1", 1, 108), read("p2", 109, 243), read("p3", 244, state="running", output=""),
                               read("empty", 1, 0), read("failed", 500, state="error", output="Not a file.")]) + "]")
    for name in ("read file notes/v7.md · lines 1–108", "read file notes/v7.md · lines 109–243",
                 "read file notes/v7.md · from line 244", "read file notes/v7.md · from line 500 failed"):
        expect(page.get_by_role("button", name=name, exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="read file notes/v7.md", exact=True)).to_be_visible()
    page.get_by_text("Activity · 5 actions").click()
    expect(page.locator(".activity-subject").nth(1)).to_have_text(" · notes/v7.md · lines 109–243")


# File paths in replies and tool cards open the Workspace editor.

PATH_REPLY = ("Edit `src/app.py:12` now, and see docs/guide.md for details. "
              "The page https://example.com/docs/page.html, file:///srv/x/notes.md, e.g. this, and `npm test` stay text.")


def test_paths_in_a_reply_become_links_and_urls_do_not(ui, page):
    show(ui, "[{ id: 'reply', type: 'assistant', text: " + json.dumps(PATH_REPLY) + " }]",
         ", onOpenPath: fake.spy('open')")
    links = page.locator(".markdown a.path-link")
    expect(links).to_have_count(2)
    expect(links.nth(0)).to_have_text("src/app.py:12")
    expect(links.nth(0)).to_have_attribute("data-path", "src/app.py")
    expect(links.nth(0)).to_have_attribute("data-line", "12")
    expect(links.nth(0).locator("code")).to_have_text("src/app.py:12")
    expect(links.nth(1)).to_have_text("docs/guide.md")
    expect(links.nth(1)).not_to_have_attribute("data-line", re.compile("."))
    expect(page.get_by_role("link", name="https://example.com/docs/page.html")).to_have_attribute(
        "href", "https://example.com/docs/page.html")
    expect(page.get_by_text(re.compile(r"file:///srv/x/notes\.md, e\.g\. this"))).to_be_visible()
    links.nth(1).click()
    links.nth(0).click()
    assert ui.spy("open") == [[{"path": "docs/guide.md"}], [{"path": "src/app.py", "line": 12}]]
    assert ui.js("location.hash") == ""


IDENTIFIER_REPLY = ("Call `os.path.join` and `json.dumps`, read `settings.values`, use `console.log`, set `self.timeout`. "
                    "Connect to db.example.com:5432 and api.internal.io:8080 or `db.example.com:5432`. "
                    "Edit `package.json`, `app.ts:3`, `Main.PY` and main.go:7.\n\n"
                    "```\nsrc/app.py:12\nsee docs/guide.md and main.go:7\n```")


def test_identifiers_hosts_and_code_blocks_are_not_linked(ui, page):
    # Reported: dotted identifiers in inline code and host:port text became broken file links.
    show(ui, "[{ id: 'reply', type: 'assistant', text: " + json.dumps(IDENTIFIER_REPLY) + " }]",
         ", onOpenPath: fake.spy('open')")
    expect(page.locator(".markdown pre")).to_contain_text("src/app.py:12")
    expect(page.locator(".markdown a.path-link")).to_have_text(["package.json", "app.ts:3", "Main.PY", "main.go:7"])
    expect(page.locator(".markdown pre a")).to_have_count(0)


def test_reasoning_summaries_and_conversations_without_a_handler_have_no_path_links(ui, page):
    show(ui, "[{ id: 'reply', type: 'assistant', text: 'See docs/guide.md.', reasoning_summary: 'Read src/app.py:3 first.' }]",
         ", onOpenPath: fake.spy('open')")
    page.get_by_text("Thought", exact=True).click()
    expect(page.locator(".thinking a.path-link")).to_have_count(0)
    expect(page.locator(".markdown a.path-link")).to_have_count(1)
    ui.rerender("{ session: makeSession([{ id: 'other', type: 'assistant', text: 'See docs/other.md.' }]), onError: fake.spy('onError') }")
    expect(page.get_by_text("See docs/other.md.")).to_be_visible()
    expect(page.locator(".markdown a.path-link")).to_have_count(0)


def test_markup_next_to_a_path_stays_text(ui, page):
    text = 'Open docs/guide.md <img src=x onerror="window.pwned=1"> <b>bold</b> and lib/x.py:3 <i>it</i>'
    show(ui, "[{ id: 'reply', type: 'assistant', text: " + json.dumps(text) + " }]", ", onOpenPath: fake.spy('open')")
    expect(page.locator(".markdown a.path-link")).to_have_count(2)
    expect(page.locator(".markdown img, .markdown b, .markdown i")).to_have_count(0)
    expect(page.locator(".markdown")).to_contain_text('<img src=x onerror="window.pwned=1"> <b>bold</b>')
    page.wait_for_timeout(100)
    assert ui.js("window.pwned === undefined")


@pytest.mark.parametrize("name, field, start, expected", [
    ("read_file", "path", 40, {"path": "src/app.py", "line": 40}),
    ("edit_file", "path", None, {"path": "src/app.py"}),
    ("move_path", "destination", None, {"path": "src/app.py"}),
])
def test_tool_card_open_file(ui, page, name, field, start, expected):
    extra = f", start_line: {start}" if start else ""
    show(ui, f"[{{ id: 'tool', type: 'tool', name: '{name}', state: 'completed', output: 'ok', "
             f"input: {{ {field}: 'src/app.py', path_other: 'x'{extra} }} }}]", ", onOpenPath: fake.spy('open')")
    page.get_by_role("button", name=re.compile(f"^{name.replace('_', ' ')}")).click()
    page.get_by_role("button", name="Open file").click()
    assert ui.spy("open") == [[expected]]


def test_tool_cards_without_a_file_have_no_open_button(ui, page):
    show(ui, "[{ id: 'tool', type: 'tool', name: 'run_command', state: 'completed', output: 'ok', input: { command: 'ls' } }]",
         ", onOpenPath: fake.spy('open')")
    page.get_by_role("button", name=re.compile("^run command")).click()
    expect(page.get_by_role("button", name="Open file")).to_have_count(0)


@pytest.mark.parametrize("newline", ["\\n", "\\r\\n"], ids=["lf", "crlf"])
def test_a_path_link_opens_the_file_at_its_line_in_the_workspace_panel(ui, page, newline):
    reply = json.dumps(PATH_REPLY)
    ui.app(session="a", scenario="workspace", setup=f"""
      fake.sessions.set('a', {{ ...makeSession('a', 'Other conversation'), events: [{{ id: 'r', type: 'assistant', text: {reply} }}] }})
      window.fileText = Array.from({{ length: 40 }}, (_, index) => `line ${{index + 1}} of the file`).join('{newline}')
      fake.override = path => path.startsWith('/api/file?') ? fake.json({{ path: 'src/app.py', content: fileText, absolute: '/abs/path/src/app.py' }}) : undefined""")
    ui.show_session("a")
    page.locator(".markdown a.path-link").first.click()
    editor = page.get_by_role("textbox", name="Edit src/app.py")
    expect(editor).to_be_visible()
    assert ui.requests("/api/file?path=src%2Fapp.py&session_id=a")
    # The textarea holds CRLF line breaks as LF; the selection indexes that text.
    offset = ui.js("fileText.split(/\\r?\\n/).slice(0, 11).join('\\n').length + 1")
    ui.wait(f"document.activeElement?.selectionStart === {offset}")
    assert ui.js("document.activeElement.selectionEnd") == offset + len("line 12 of the file")
    assert ui.js("document.activeElement.value.slice(document.activeElement.selectionStart, "
                 "document.activeElement.selectionEnd)") == "line 12 of the file"
    expect(editor).to_be_focused()
    expect(page.locator(".editor-status")).to_contain_text("40 lines · Line 12")
    editor.press("End")
    expect(page.locator(".editor-status")).not_to_contain_text("Line 12")


def test_outline_and_overview_cards_name_their_subject(ui, page):
    show(ui, """[
      { id: 'outline', type: 'tool', name: 'outline_file', state: 'completed', output: '{}',
        input: { path: 'docs/handoff.md', max_level: 2 } },
      { id: 'outline-next', type: 'tool', name: 'outline_file', state: 'completed', output: '{}',
        input: { path: 'src/app.py', offset: 120, max_level: 6 } },
      { id: 'here', type: 'tool', name: 'project_overview', state: 'completed', output: '{}', input: { path: '.' } },
      { id: 'default', type: 'tool', name: 'project_overview', state: 'completed', output: '{}', input: {} },
      { id: 'backend', type: 'tool', name: 'project_overview', state: 'completed', output: '{}', input: { path: 'backend' } },
    ]""", ", onOpenPath: fake.spy('open')")
    expect(page.get_by_role("button", name="outline file docs/handoff.md · to level 2", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="outline file src/app.py · from entry 120", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="project overview workspace", exact=True)).to_have_count(2)
    expect(page.get_by_role("button", name="project overview backend", exact=True)).to_be_visible()
    page.get_by_role("button", name="outline file docs/handoff.md · to level 2", exact=True).click()
    page.get_by_role("button", name="Open file").click()
    assert ui.spy("open") == [[{"path": "docs/handoff.md"}]]


def test_collapsed_cards_say_when_the_agent_got_less_than_the_card(ui, page):
    show(ui, """[
      { id: 'withheld', type: 'tool', name: 'read_file', state: 'completed', output: '{"content": "1: a"}',
        input: { path: 'a.txt' }, model_output: '{"error_kind": "result_deferred"}',
        result_withheld: { estimated_tokens: 9000, allowance_tokens: 4000 } },
      { id: 'deferred', type: 'tool', name: 'read_file', state: 'completed', output: '{"content": "1: c"}',
        input: { path: 'c.txt' }, model_output: '{"error_kind": "result_deferred"}',
        result_withheld: { estimated_tokens: 3000, allowance_tokens: 4000 } },
      { id: 'shortened', type: 'tool', name: 'run_command', state: 'completed', output: '{"output": "long"}',
        input: { command: 'make test' }, model_output: '{"result_shortened": true}', result_shortened: true },
      { id: 'plain', type: 'tool', name: 'read_file', state: 'completed', output: '{}', input: { path: 'b.txt' } },
    ]""")
    expect(page.locator(".tool-outcome")).to_have_count(3)
    # One read larger than the whole allowance: running it again would not help.
    expect(page.get_by_text("Result not sent to the agent: it is larger than the step's context allowance (about "
                            "9,000 of 4,000 tokens). The agent was asked to read a smaller part.",
                            exact=True)).to_be_visible()
    expect(page.get_by_text("Result not sent to the agent: earlier results in the same step filled its context "
                            "allowance. The agent can run it again.", exact=True)).to_be_visible()
    expect(page.get_by_text("Result shortened for the agent to fit its context allowance.", exact=True)).to_be_visible()


# Markdown tables keep words whole and scroll sideways; the phone tool-card subject uses the free space (styles.css)

TABLE_REPLY = ("| File | Test | Note |\n|---|---|---|\n"
               "| `src/utils/helpers.py` | `tests/test_app.py` | The helper keeps the order of the sections and the test "
               "compares the rendered output line by line, so a change in one section shows as one failing line. |")


def test_markdown_table_cells_keep_code_whole_at_phone_width(ui, page):
    # V-T11. Mutations: delete .markdown :is(th,td){overflow-wrap:break-word} (code cells break mid-token), or set
    # .markdown{overflow-wrap:normal} (the long path overflows). With .markdown{overflow-wrap:break-word} the path still
    # wraps here (the reply, reasoning and plan containers have min-width:0), but its min-content width grows, which
    # is why only table cells use break-word.
    page.set_viewport_size({"width": 390, "height": 844})
    long_path = "/srv/x/" + "_".join(f"generated_module_{n:02d}" for n in range(10)) + ".py"
    assert len(long_path) >= 200
    sentence = f"The file is {long_path} today."
    show(ui, json.dumps([{"id": "r", "type": "assistant", "text": TABLE_REPLY},
                         {"id": "p", "type": "assistant", "text": sentence, "reasoning_summary": sentence},
                         {"id": "plan", "type": "tool", "name": "submit_plan", "state": "pending", "input": {"plan": sentence}}]))
    codes = page.locator(".markdown td code")
    expect(codes).to_have_count(2)
    assert codes.evaluate_all("els => els.map(el => el.getClientRects().length)") == [1, 1]
    assert page.locator(".markdown table").evaluate("e => e.scrollWidth >= e.clientWidth")
    assert ui.js("document.documentElement.scrollWidth") <= 390
    page.get_by_text("Thought", exact=True).click()
    paragraphs = page.locator(".markdown p", has_text="The file is")
    expect(paragraphs).to_have_count(3)  # the reply, its reasoning summary and the plan
    for paragraph in paragraphs.all():
        assert paragraph.evaluate("e => e.getBoundingClientRect().height > 40")  # it wraps
        assert paragraph.evaluate("e => e.scrollWidth <= e.clientWidth && e.getBoundingClientRect().right <= innerWidth")
    assert ui.js("document.documentElement.scrollWidth") <= 390


def test_a_phone_tool_card_shows_its_whole_subject(ui, page):
    # V-T12. Mutation: restore .tool-subject{max-width:110px} in the 600 px block.
    page.set_viewport_size({"width": 390, "height": 844})
    output = json.dumps({"path": "README.md", "start_line": 1, "end_line": 40, "content": "# Readme"})
    show(ui, json.dumps([{"id": "t", "type": "tool", "name": "read_file", "state": "completed", "input": {"path": "README.md"},
                          "output": output}]))
    subject = page.locator(".tool-card .tool-subject")
    expect(subject).to_have_text("README.md · lines 1–40")
    assert subject.evaluate("e => e.scrollWidth <= e.clientWidth")
    heading = page.locator(".tool-card .tool-heading")
    assert heading.evaluate("e => e.lastElementChild.getBoundingClientRect().right <= e.getBoundingClientRect().right + 0.5")


PARALLEL = """now => [
  { id: 'd1', type: 'tool', name: 'delegate_task', call_id: 'c1', input: { task: 'Map the parser', agent_type: 'explore' },
    state: 'running', child_session_id: 'child-1',
    delegation: { status: 'running', completed_tools: 2, last_tool: 'read_file', agent_type: 'explore', started: now - 65 } },
  { id: 'd2', type: 'tool', name: 'delegate_task', call_id: 'c2', input: { task: 'Fix the docs', agent_type: 'editor' },
    state: 'completed', child_session_id: 'child-2',
    delegation: { status: 'completed', completed_tools: 3, agent_type: 'editor', started: now - 100, ended: now - 40,
      terminal_reason: 'completed' },
    output: JSON.stringify({ child_session_id: 'child-2', agent_type: 'editor', status: 'completed',
      output: '## Summary\\n\\nUpdated the install steps.', changed_files: ['README.md', 'docs/setup.md'], changed_files_total: 5,
      steps_used: 3, tool_calls: 4, transcript: 'conversations/child-2.jsonl' }) },
  { id: 'd3', type: 'tool', name: 'delegate_task', call_id: 'c3', input: { task: 'Check the tests' }, state: 'running',
    delegation: { status: 'queued', completed_tools: 0, agent_type: 'general' } },
]"""


def test_parallel_subagents_have_their_own_cards_with_type_time_and_report(ui, page):
    ui.mount("components/Conversation.js", f"""{{ session: makeSession(({PARALLEL})(Date.now() / 1000)),
      onError: fake.spy('onError'), onSelectSession: fake.spy('select') }}""", setup=SETUP)
    cards = page.locator(".tool-card")
    expect(cards).to_have_count(3)
    running, finished, queued = (cards.nth(index).get_by_label("Subagent progress") for index in range(3))
    expect(running.locator(".delegation-status")).to_have_text("Subagent: running")
    expect(running.locator(".delegation-type")).to_have_text("Explore")
    expect(running).to_contain_text("2 completed actions")
    expect(running).to_contain_text("Last tool: read file")
    expect(running.locator(".delegation-elapsed")).to_have_text(re.compile(r"^1 min 0[5-7] s$"))
    expect(running.locator(".delegation-elapsed")).to_have_text(re.compile(r"^1 min (0[6-9]|1\d) s$"))  # It keeps counting.
    expect(running.locator(".delegation-report")).to_have_count(0)
    expect(finished.locator(".delegation-type")).to_have_text("Editor")
    expect(finished.locator(".delegation-elapsed")).to_have_text("1 min 00 s")
    expect(finished).to_contain_text("Reason: completed")
    report = finished.locator("details.delegation-report")
    expect(report).not_to_have_attribute("open", "")
    expect(report.get_by_role("heading", name="Summary")).to_be_hidden()
    report.locator("summary").click()
    expect(report.get_by_role("heading", name="Summary")).to_be_visible()
    expect(report).to_contain_text("Updated the install steps.")
    expect(report.locator(".delegation-files")).to_have_text("Files changed: README.md, docs/setup.md and 3 more")
    expect(report.locator("small")).to_have_text("3 steps · 4 tool calls · Transcript: conversations/child-2.jsonl")
    expect(queued.locator(".delegation-status")).to_have_text("Subagent: waiting for a free slot")
    expect(queued.locator(".delegation-type")).to_have_text("General")
    expect(queued.get_by_role("button", name="Open subagent")).to_have_count(0)
    finished.get_by_role("button", name="Open subagent").click()
    assert ui.spy("select") == [["child-2"]]


def test_a_report_that_is_not_json_shows_no_report(ui, page):
    show(ui, """[{ id: 'd', type: 'tool', name: 'delegate_task', input: { task: 'x' }, state: 'completed', child_session_id: 'c',
      output: 'Subagents cannot delegate further tasks.', delegation: { status: 'failed', completed_tools: 0, terminal_reason: 'error' } }]""")
    expect(page.get_by_label("Subagent progress")).to_contain_text("Subagent: failed")
    expect(page.locator(".delegation-report")).to_have_count(0)
    expect(page.locator(".delegation-elapsed")).to_have_count(0)  # Older progress has no start time.


def test_a_question_or_plan_that_starts_waiting_comes_into_view(ui, page):
    # F1 (b): also for question and plan cards, after the user scrolled up. Mutation: count only tool cards.
    page.set_viewport_size({"width": 1280, "height": 700})
    history = [event for n in range(10) for event in (
        {"id": f"u{n}", "type": "user", "text": f"Step {n}"},
        {"id": f"r{n}", "type": "assistant", "text": "A long reply. " * 120})]
    question = {"id": "ask", "type": "tool", "name": "ask_user", "state": "pending", "output": "",
                "input": {"questions": [{"question": "Which database?", "options": [{"label": "SQLite"}, {"label": "Postgres"}]}]}}
    plan = {"id": "plan", "type": "tool", "name": "submit_plan", "state": "pending", "output": "", "input": {"plan": "Do it."}}
    props = "{{ session: makeSession({}), onError: fake.spy('onError') }}"
    show(ui, json.dumps(history))
    page.add_style_tag(content=".transcript{height:600px}")  # The app's column; mounted alone it would grow.

    def scroll_up():
        page.locator(".transcript").evaluate("e => { e.style.scrollBehavior = 'auto'; e.scrollTop = 0; e.dispatchEvent(new Event('scroll')) }")
        expect(page.get_by_text("Step 0", exact=True)).to_be_in_viewport()

    scroll_up()
    ui.rerender(props.format(json.dumps(history + [question])))
    expect(page.locator(".question-card.needs-answer")).to_be_in_viewport()
    scroll_up()
    answered = {**question, "state": "completed", "question_outcome": "skipped"}
    ui.rerender(props.format(json.dumps(history + [answered, plan])))
    expect(page.locator(".plan-card.needs-answer")).to_be_in_viewport()


def test_a_turns_last_reply_shows_its_result_summary(ui, page):
    result = """{ terminal_reason: 'budget_limit', is_error: false, started: 1, ended: 131.4, duration_s: 130.4, steps: 8, tool_calls: 14,
      usage: { calls: 9, input_tokens: 22000, output_tokens: 3100, cache_read_input_tokens: 160000, cache_creation_input_tokens: 0,
        reasoning_tokens: null },
      tokens: 191000, estimated_dbu: 1.42, unpriced_calls: 1, subagents: { count: 2, tokens: 6000, estimated_dbu: 0.2 },
      changed_files: ['src/app.py', 'README.md'], changed_files_total: 5 }"""
    unreported = """{ terminal_reason: 'completed', steps: 1, tool_calls: 0, duration_s: 4, estimated_dbu: null, unpriced_calls: 0,
      usage: { calls: 1, input_tokens: null, output_tokens: 12, cache_read_input_tokens: null }, subagents: { count: 0 }, changed_files: [] }"""
    show(ui, f"""[{{ id: 'u1', type: 'user', text: 'Work' }},
      {{ id: 'a1', type: 'assistant', text: 'Working.', request_info: {{ model: 'm', status: 'completed' }} }},
      {{ id: 'a2', type: 'assistant', text: 'Summary: done and remaining.', request_info: {{ model: 'm', status: 'completed' }},
        turn_result: {result} }},
      {{ id: 'u2', type: 'user', text: 'Again' }},
      {{ id: 'a3', type: 'assistant', text: 'Hi.', request_info: {{ model: 'm', status: 'completed' }}, turn_result: {unreported} }},
      {{ id: 'a4', type: 'assistant', text: 'Still streaming', request_info: {{ model: 'm', status: 'running' }},
        turn_result: {unreported} }}]""")
    footers = page.locator(".turn-summary")
    expect(footers).to_have_count(2)  # Only replies with a result, and none while one streams.
    first = footers.first
    expect(first.locator("summary")).to_have_text(
        "Turn summary: 8 steps · 14 tools · 2 min 10 s · 182k in (160k cached) / 3.1k out · ~1.4 DBU · 2 subagents")
    expect(first.locator("p")).to_be_hidden()
    first.locator("summary").click()
    expect(first.locator("p")).to_have_text(
        "This turn reached the turn's token or time limit. 191,000 tokens processed, its subagents included. "
        "1 call without price data not in the DBU estimate. Changed: src/app.py, README.md and 3 more.")
    # A count no call reported shows '—', never 0; no DBU part without price data.
    expect(footers.nth(1).locator("summary")).to_have_text("Turn summary: 1 step · 0 tools · 4 s · — in / 12 out")
