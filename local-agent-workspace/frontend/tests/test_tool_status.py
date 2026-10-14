"""Tool cards say when a command failed and show its output as text (Conversation.js resultOf/toolStatus)."""
import json

import pytest
from playwright.sync_api import expect

SETUP = """
window.makeSession = (events, overrides = {}) => ({
  id: 'parent', title: 'Parent task', workspace: '/project', model: 'test-model', status: 'idle',
  events, updated: 1, permission_mode: 'manual', allowed_directories: [], ...overrides,
})
fake.handle = () => fake.json({ ok: true })
"""
FAILED_OUTPUT = "E   ModuleNotFoundError: No module named pytest\n"
EDIT_NOTE = "[The user edited this command before approving it. The command that ran: python - <<'EOF'\n{\"a\": 1}\nEOF]"


def job_page(state="completed", exit_code=0, output="", **extra):
    page = {"job_id": "0123456789abcdef", "state": state, "exit_code": exit_code, "output": output, "offset": 0,
            "next_offset": None, "total_chars": len(output), "truncated": False, "background": False, **extra}
    return json.dumps(page, indent=2)


def tool(event_id="cmd", name="run_command", state="completed", output="", command="pytest -q", **fields):
    return json.dumps({"id": event_id, "type": "tool", "name": name, "state": state, "input": {"command": command},
                       "output": output, **fields})


def show(ui, *events):
    ui.mount("components/Conversation.js", f"{{ session: makeSession([{', '.join(events)}]), onError: fake.spy('onError') }}",
             setup=SETUP)


def heading(page, name="run command pytest -q"):
    return page.get_by_role("button", name=name)


def test_a_failed_command_shows_its_exit_code_and_last_line(ui, page):
    # B-T1. Mutation: keep the Check for state 'completed' (tone ignored).
    show(ui, tool(output=job_page("failed", 1, FAILED_OUTPUT)))
    card = page.locator(".tool-card")
    expect(card.locator(".success-icon")).to_have_count(0)
    expect(card.locator(".warning-icon")).to_be_visible()
    expect(card.locator(".tool-status")).to_have_text("exit 1")
    expect(heading(page)).to_have_accessible_name("run command pytest -q exit 1")
    expect(card.locator(".tool-outcome")).to_have_text("Exit 1 · E ModuleNotFoundError: No module named pytest")
    heading(page).click()
    output = page.locator("pre[aria-label=Output]")
    assert output.evaluate("e => e.textContent") == FAILED_OUTPUT
    expect(output).not_to_contain_text("job_id")
    expect(card.locator(".tool-job-state")).to_have_text(f"State: failed · exit 1 · {len(FAILED_OUTPUT)} characters")
    raw = page.locator("pre[aria-label='Raw result']")
    expect(raw).not_to_be_visible()
    page.get_by_text("Raw result", exact=True).click()
    expect(raw).to_contain_text('"exit_code": 1')
    # The Action line does not contradict the failed state. Mutation: print event.state.
    expect(card.get_by_text("Action: ran · exit 1")).to_be_visible()
    expect(card.get_by_text("Action: completed", exact=False)).to_have_count(0)
    expect(card.locator(".tool-outcome")).to_have_count(0)


def test_a_successful_command_keeps_the_tick(ui, page):
    # B-T2. Mutation: treat every job page as warn.
    show(ui, tool(output=job_page("completed", 0, "2 passed\n")))
    card = page.locator(".tool-card")
    expect(card.locator(".success-icon")).to_be_visible()
    expect(card.locator(".tool-status")).to_have_count(0)
    expect(card.locator(".warning-icon")).to_have_count(0)
    expect(card.locator(".tool-outcome")).to_have_count(0)


def test_timeouts_background_runs_and_edited_commands(ui, page):
    # B-T3. Mutations: parse without splitting the note, or split at the first '\n{'.
    show(ui, tool("slow", output=job_page("timed_out", None, "waiting\n"), command="sleep 99"),
         tool("bg", output=job_page("running", None, "", background=True), command="serve"),
         tool("edited", output="[The user edited this command before approving it.]\n" + job_page("completed", 0, "ok\n"),
              command="echo ok"),
         tool("heredoc", output=EDIT_NOTE + "\n" + job_page("failed", 1, "Traceback\nValueError: a\n"), command="python -",
              edited=True, approval="approved"))
    slow = page.locator(".tool-card").nth(0)
    expect(slow.locator(".warning-icon")).to_be_visible()
    expect(slow.locator(".tool-status")).to_have_text("timed out")
    expect(slow.locator(".tool-outcome")).to_have_text("Timed out · waiting")
    background = page.locator(".tool-card").nth(1)
    # 'started' stays true after the job has ended (a later wait_job card reports how it ended).
    expect(background.locator(".tool-status")).to_have_text("started in background")
    expect(background.locator(".tool-status")).to_have_class("tool-status neutral")
    expect(background.locator(".warning-icon")).to_have_count(0)
    expect(background.locator(".success-icon")).to_have_count(0)
    expect(background.locator(".tool-outcome")).to_have_count(0)
    edited = page.locator(".tool-card").nth(2)
    expect(edited.locator(".success-icon")).to_be_visible()
    heading(page, "run command echo ok").click()
    expect(edited.locator(".tool-rule")).to_have_text("[The user edited this command before approving it.]")
    expect(edited.locator("pre[aria-label=Output]")).to_have_text("ok")
    heredoc = page.locator(".tool-card").nth(3)
    expect(heredoc.locator(".warning-icon")).to_be_visible()
    expect(heredoc.locator(".tool-status")).to_have_text("exit 1")
    expect(heredoc.locator(".tool-outcome")).to_have_text("Exit 1 · ValueError: a")
    heading(page, "run command python -").click()
    # The rule line says it was edited; the note addressed to the agent is part of Raw result, not a second line.
    expect(heredoc.locator(".tool-rule")).to_have_count(1)
    expect(heredoc.locator(".tool-rule")).to_have_text("Edited by you before approval")
    heredoc.get_by_text("Raw result", exact=True).click()
    raw = heredoc.locator("pre[aria-label='Raw result']").evaluate("e => e.textContent")
    assert raw.startswith(EDIT_NOTE + "\n{") and '"exit_code": 1' in raw


@pytest.mark.parametrize("name, page_json, note", [
    ("run_command", job_page("completed", 0, "x" * 4000, next_offset=4000, total_chars=9000),
     "first part shown; the agent can read the rest with get_job_output"),
    ("run_command", job_page("failed", 2, "head\n…\ntail\n", next_offset=2000, total_chars=9000, excerpt="head_and_tail",
                             omitted={"start": 2000, "end": 8000}),
     "start and end shown; the agent can read the omitted middle with get_job_output"),
    ("wait_job", job_page("completed", 0, "y" * 8000, output_start=4480, next_offset=12480, total_chars=12480,
                          reason="exited", match=None),
     "latest output shown, from character 4,481"),
    ("get_job_output", job_page("completed", 0, "z" * 3000, offset=4000, next_offset=7000, total_chars=9000),
     "characters 4,001–7,000 shown"),
    ("get_job_output", job_page("completed", 0, "all of it", next_offset=None), None),
    # A poll from the last next_offset before new output arrived: no reversed range.
    ("get_job_output", job_page("running", None, "", offset=4000, next_offset=4000, total_chars=4000),
     "no output after character 4,000"),
    # The backend counts code points; 1,000 emoji are 2,000 UTF-16 units in JS.
    ("get_job_output", job_page("completed", 0, "\U0001F600" * 1000, offset=1000, next_offset=None, total_chars=2000),
     "characters 1,001–2,000 shown"),
])
def test_the_state_line_says_which_part_the_result_holds(ui, page, name, page_json, note):
    # B-T3b. Mutation: partial = next_offset != null && !excerpt (fails the wait_job case).
    show(ui, tool(name=name, output=page_json))
    page.locator(".tool-heading").click()
    state = page.locator(".tool-job-state")
    expect(state).to_be_visible()
    if note:
        expect(state).to_contain_text(f" · {note}")
        expect(state).not_to_contain_text("4,001–4,000")
        expect(state).not_to_contain_text("1,001–3,000")
    else:
        expect(state).to_have_text("State: completed · exit 0 · 9 characters")
    if name == "wait_job":
        expect(state).not_to_contain_text("first part shown")


def test_a_wait_job_card_reports_a_failed_job_in_a_muted_label(ui, page):
    # B-T3c. Mutation: key the neutral class off tone instead of labelTone.
    show(ui, tool(name="wait_job", output=job_page("failed", 1, "boom\n", output_start=0, next_offset=5, total_chars=5)))
    card = page.locator(".tool-card")
    expect(card.locator(".success-icon")).to_be_visible()
    status = card.locator(".tool-status")
    expect(status).to_have_text("job exit 1")
    expect(status).to_have_class("tool-status neutral")
    muted = ui.js("getComputedStyle(document.documentElement).getPropertyValue('--muted').trim()")
    probe = ui.js(f"(() => {{ const e = document.createElement('span'); e.style.color = {json.dumps(muted)}; "
                  "document.body.append(e); const c = getComputedStyle(e).color; e.remove(); return c })()")
    assert status.evaluate("e => getComputedStyle(e).color") == probe
    expect(card.locator(".tool-outcome")).to_have_count(0)


@pytest.mark.parametrize("reason, match, state, tick, label, line", [
    ("matched", {"line": "server ready on :8000", "offset": 0}, "running", True, "matched",
     "State: running · wait matched: server ready on :8000"),
    ("timeout", None, "running", False, "wait timed out", "State: running · wait timed out"),
    ("idle", None, "running", False, "output quiet", "State: running · output went quiet"),
    ("exited", None, "completed", True, "job ended", "State: completed · job ended during the wait · exit 0"),
])
def test_a_wait_job_card_shows_how_the_wait_ended(ui, page, reason, match, state, tick, label, line):
    # The heading and the expanded card name the wait's reason, not only the job's state ('job running').
    # Mutation: build the label from job.state alone.
    output = "server ready on :8000\n"
    show(ui, tool(name="wait_job", output=job_page(state, 0 if state == "completed" else None, output, reason=reason,
                                                    match=match, output_start=0, next_offset=len(output))))
    card = page.locator(".tool-card")
    expect(card.locator(".tool-status")).to_have_text(label)
    expect(card.locator(".success-icon")).to_have_count(1 if tick else 0)
    card.locator(".tool-heading").click()
    expect(card.locator(".tool-job-state")).to_have_text(f"{line} · {len(output)} characters")
    expect(card.get_by_text(f"Action: completed · {label}")).to_be_visible()


def test_a_failed_shell_command_shows_its_exit_not_json(ui, page):
    # B-T4. Mutation: drop user_command from JOB_TOOLS.
    show(ui, tool(name="user_command", state="error", output=job_page("failed", 2, "ls: cannot access 'x'\n"), command="ls x"))
    outcome = page.locator(".tool-outcome")
    expect(outcome).to_have_text("Exit 2 · ls: cannot access 'x'")
    expect(page.locator(".tool-status")).to_have_text("exit 2")
    expect(page.locator(".warning-icon")).to_be_visible()


def test_a_failed_tool_shows_its_error_category_and_hint(ui, page):
    # B-T5. Mutation: read error_kind from output instead of model_output.
    model_output = json.dumps({"error_kind": "edit_mismatch", "message": "old_text was not found",
                               "hint": "Read the file again"})
    show(ui, json.dumps({"id": "edit", "type": "tool", "name": "edit_file", "state": "error", "input": {"path": "a.py"},
                         "output": "old_text was not found in a.py.", "model_output": model_output}))
    card = page.locator(".tool-card")
    expect(card.locator(".tool-status")).to_have_text("no match")
    expect(card.locator(".warning-icon")).to_be_visible()
    expect(card.locator(".tool-outcome")).to_have_text("old_text was not found in a.py.")
    page.get_by_role("button", name="edit file a.py no match").click()
    expect(card.get_by_text("Error category", exact=True)).to_be_visible()
    expect(card.get_by_text("Hint given to the agent: Read the file again")).to_be_visible()
    expect(card.get_by_text("Action: error · no match")).to_be_visible()


@pytest.mark.parametrize("state, output", [("completed", "plain text from an older version"), ("running", "streamed line\n")])
def test_plain_text_output_renders_as_before(ui, page, state, output):
    # B-T6. Mutation: always render Raw result.
    show(ui, tool(state=state, output=output))
    if state == "running":
        expect(page.locator(".tool-card .spin")).to_be_visible()
    page.locator(".tool-heading").click()
    assert page.locator("pre[aria-label=Output]").evaluate("e => e.textContent") == output
    expect(page.get_by_text("Raw result")).to_have_count(0)
    expect(page.locator(".tool-job-state")).to_have_count(0)
    expect(page.locator(".tool-status")).to_have_count(0)


def test_an_mcp_error_shows_its_text(ui, page):
    # B-T7. Mutation: skip mcp__ names in resultOf.
    show(ui, json.dumps({"id": "mcp", "type": "tool", "name": "mcp__x__y", "state": "error", "input": {"query": "q"},
                         "output": json.dumps({"output": "boom", "is_error": True, "truncated": False})}))
    expect(page.locator(".tool-outcome")).to_have_text("boom")
    expect(page.locator(".tool-status")).to_have_text("tool error")
    page.locator(".tool-heading").click()
    expect(page.locator("pre[aria-label=Output]")).to_have_text("boom")


def test_the_activity_summary_names_the_exit_code(ui, page):
    # B-T8. Mutation: leave the summary on event.state. A command that failed 'ran' (not 'completed').
    show(ui, tool(output=job_page("failed", 1, FAILED_OUTPUT)), tool("ok", output=job_page("completed", 0, "fine"), command="true"))
    page.get_by_text("Activity · 2 actions").click()
    items = page.locator(".activity-summary li")
    expect(items.nth(0)).to_contain_text("Action: ran · exit 1")
    expect(items.nth(1).locator("small")).to_have_text("Action: completed")


def test_the_status_fits_a_phone_heading(ui, page):
    # B-T9. Mutation: remove .tool-subject shrinking (or let .tool-status shrink with overflow).
    page.set_viewport_size({"width": 360, "height": 740})
    show(ui, tool(output=job_page("failed", 1, FAILED_OUTPUT), command="python -m pytest tests/test_a_very_long_name.py -k slow"))
    button = page.locator(".tool-heading")
    sizes = button.evaluate("e => [e.scrollWidth, e.clientWidth]")
    assert sizes[0] <= sizes[1], sizes
    status = page.locator(".tool-status")
    expect(status).to_be_visible()
    box, outer = status.bounding_box(), button.bounding_box()
    assert box["x"] + box["width"] <= outer["x"] + outer["width"]
    assert status.evaluate("e => e.scrollWidth <= e.clientWidth")


def test_a_withheld_result_keeps_the_tick(ui, page):
    # B-T10. Mutation: read model_output for completed events.
    show(ui, json.dumps({"id": "read", "type": "tool", "name": "read_file", "state": "completed", "input": {"path": "big.log"},
                         "output": "x" * 50, "result_withheld": {"estimated_tokens": 90000, "allowance_tokens": 30000},
                         "model_output": json.dumps({"error_kind": "limit", "message": "withheld"})}))
    card = page.locator(".tool-card")
    expect(card.locator(".success-icon")).to_be_visible()
    expect(card.locator(".warning-icon")).to_have_count(0)
    expect(card.locator(".tool-status")).to_have_count(0)
    expect(card.locator(".tool-outcome")).to_contain_text("Result not sent to the agent")
    card.locator(".tool-heading").click()
    expect(card.get_by_text("Error category")).to_have_count(0)
    expect(card.get_by_text("Action: completed", exact=True)).to_be_visible()


@pytest.mark.parametrize("name, state, tone, label, outcome", [
    ("run_command", "interrupted", "warn", "interrupted", "Interrupted · partial"),
    ("run_command", "cancelled", "neutral", "stopped", None),
    ("wait_job", "running", "ok", "job running", None),
    ("wait_job", "timed_out", "ok", "job timed out", None),
    ("get_job_output", "cancelled", "ok", "job stopped", None),
    ("stop_job", "interrupted", "ok", "job interrupted", None),
])
def test_every_job_state_has_its_icon_and_label(ui, page, name, state, tone, label, outcome):
    # Mutations: drop the 'interrupted' branch of commandJob, or an OTHER_JOB entry.
    show(ui, tool(name=name, output=job_page(state, None, "partial\n")))
    card = page.locator(".tool-card")
    expect(card.locator(".warning-icon")).to_have_count(1 if tone == "warn" else 0)
    expect(card.locator(".success-icon")).to_have_count(1 if tone == "ok" else 0)
    status = card.locator(".tool-status")
    expect(status).to_have_text(label)
    assert ("neutral" in status.get_attribute("class").split()) == (tone != "warn")
    if outcome:
        expect(card.locator(".tool-outcome")).to_have_text(outcome)
    else:
        expect(card.locator(".tool-outcome")).to_have_count(0)


def test_the_failure_line_comes_before_the_shortened_line(ui, page):
    # Spec B1.5. Mutation: swap the failure and allowance paragraphs.
    show(ui, tool(output=job_page("failed", 1, "boom\n"), result_shortened=True))
    lines = page.locator(".tool-card .tool-outcome")
    expect(lines).to_have_count(2)
    expect(lines.nth(0)).to_have_text("Exit 1 · boom")
    expect(lines.nth(1)).to_have_text("Result shortened for the agent to fit its context allowance.")


NOT_KEPT = ("\n[… output after this point was not kept (over max_output_bytes); rerun with a larger max_output_bytes, "
            "or write it to a file and use read_file with tail_lines …]")


def test_output_that_was_not_kept_is_named_not_quoted(ui, page):
    # A failed command whose output went past max_output_bytes: the last kept line is not its last line, and the
    # bracketed note is for the agent. Mutation: take the last line of the page's output.
    output = "line 1\n[… 70,000 characters omitted; read them with get_job_output offset=2000 …]\nline 9000" + NOT_KEPT
    show(ui, tool(output=job_page("failed", 2, output, truncated=True, total_chars=80000, next_offset=2000,
                                  excerpt="head_and_tail", omitted={"start": 2000, "end": 74000})))
    card = page.locator(".tool-card")
    expect(card.locator(".tool-outcome")).to_have_text("Exit 2 · output after 80,000 characters was not kept")
    card.locator(".tool-heading").click()
    expect(card.locator(".tool-job-state")).to_have_text(
        "State: failed · exit 2 · 80,000 characters · output after 80,000 characters not kept · start and end shown; "
        "the agent can read the omitted middle with get_job_output")


def test_a_trailing_note_is_never_the_outcome(ui, page):
    # The same note without the truncated flag (defensive): the line before it is the outcome.
    show(ui, tool(output=job_page("failed", 2, "first\nlast real line" + NOT_KEPT)))
    expect(page.locator(".tool-outcome")).to_have_text("Exit 2 · last real line")


@pytest.mark.parametrize("state, code, label, state_line", [
    ("timed_out", -15, "timed out", "State: timed out · 9 characters"),
    ("failed", -9, "signal 9", "State: failed · signal 9 · 9 characters"),
])
def test_signal_codes_are_not_exit_codes(ui, page, state, code, label, state_line):
    # Mutation: print `exit ${exitCode}` for every integer code.
    show(ui, tool(output=job_page(state, code, "starting\n")))
    card = page.locator(".tool-card")
    expect(card.locator(".tool-status")).to_have_text(label)
    card.locator(".tool-heading").click()
    expect(card.locator(".tool-job-state")).to_have_text(state_line)
    expect(card).not_to_contain_text(f"exit {code}")


def test_the_expanded_card_keeps_the_label_spelling(ui, page):
    # .tool-body small capitalises the state; the label keeps the heading's spelling, and the State line is 11 px.
    # Mutations: drop the .tool-status-text rule, or the .tool-body .tool-job-state specificity.
    show(ui, tool(output=job_page("running", None, "", background=True)))
    card = page.locator(".tool-card")
    card.locator(".tool-heading").click()
    assert card.locator("small.muted").evaluate("e => e.innerText") == "Action: Completed · started in background"
    sizes = card.locator(".tool-job-state").evaluate(
        "e => ['fontSize', 'marginTop', 'marginBottom'].map(key => getComputedStyle(e)[key])")
    assert sizes == ["11px", "6px", "0px"], sizes


@pytest.mark.parametrize("event", [
    {"id": "mcp", "type": "tool", "name": "mcp__github__search_pull_requests", "state": "error", "input": {"query": "is:open"},
     "output": json.dumps({"output": "boom", "is_error": True, "truncated": False})},
    {"id": "bg", "type": "tool", "name": "run_command", "state": "completed", "input": {"command": "python -m http.server 8000"},
     "output": job_page("running", None, "", background=True)},
])
def test_long_names_and_labels_fit_a_phone_heading(ui, page, event):
    # B-T9 for a long MCP name with 'tool error' and a background run. Mutations: drop the .tool-name shrinking (the
    # chevron is clipped), or the short 'background' label (the command shrinks to a few characters).
    page.set_viewport_size({"width": 360, "height": 740})
    # The whole app: the short label depends on the chat column (the `main` container).
    ui.app(session="a", scenario="workspace",
           setup=f"fake.sessions.set('a', {{ ...makeSession('a', 'Tools'), events: [{json.dumps(event)}] }})")
    ui.show_session("a")
    button = page.locator(".tool-heading")
    sizes = button.evaluate("e => [e.scrollWidth, e.clientWidth]")
    assert sizes[0] <= sizes[1], sizes
    chevron, card = button.locator("svg").last.bounding_box(), page.locator(".tool-card").bounding_box()
    assert chevron["x"] + chevron["width"] <= card["x"] + card["width"]
    status = page.locator(".tool-status")
    box = status.bounding_box()
    assert box["width"] > 0 and box["x"] + box["width"] <= card["x"] + card["width"]
    if event["id"] == "bg":
        assert status.evaluate("e => getComputedStyle(e, '::after').content") == '"background"'
        assert page.locator(".tool-subject").evaluate("e => e.getBoundingClientRect().width") >= 60
        expect(button).to_have_accessible_name("run command python -m http.server 8000 started in background")


@pytest.mark.parametrize("fields, note", [
    ({"result_shortened": True}, "Result shortened for the agent to fit its context allowance."),
    ({"result_withheld": {"estimated_tokens": 900, "allowance_tokens": 500}},
     "Result not sent to the agent: it is larger than the step's context allowance (about 900 of 500 tokens). "
     "The agent was asked to read a smaller part."),
])
def test_the_expanded_card_says_when_the_agent_did_not_get_the_whole_result(ui, page, fields, note):
    # Raw result holds the full result; the expanded card keeps saying what the agent received.
    # Mutation: show the allowance note only while the card is collapsed.
    output = json.dumps({"output": "row\n" * 3000, "is_error": False, "truncated": False})
    show(ui, json.dumps({"id": "mcp", "type": "tool", "name": "mcp__db__query", "state": "completed",
                         "input": {"sql": "select 1"}, "output": output, **fields}))
    card = page.locator(".tool-card")
    expect(card.locator(".tool-outcome")).to_have_text(note)
    card.locator(".tool-heading").click()
    expect(card.locator(".tool-outcome")).to_have_count(0)
    expect(card.locator(".tool-allowance")).to_have_text(note)
