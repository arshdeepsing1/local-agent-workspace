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
    expect(card.get_by_text("Action: completed · exit 1")).to_be_visible()
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
         tool("heredoc", output=EDIT_NOTE + "\n" + job_page("failed", 1, "Traceback\nValueError: a\n"), command="python -"))
    slow = page.locator(".tool-card").nth(0)
    expect(slow.locator(".warning-icon")).to_be_visible()
    expect(slow.locator(".tool-status")).to_have_text("timed out")
    expect(slow.locator(".tool-outcome")).to_have_text("Timed out · waiting")
    background = page.locator(".tool-card").nth(1)
    expect(background.locator(".tool-status")).to_have_text("running in background")
    expect(background.locator(".tool-status")).to_have_class("tool-status neutral")
    expect(background.locator(".warning-icon")).to_have_count(0)
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
    assert heredoc.locator(".tool-rule").evaluate("e => e.textContent") == EDIT_NOTE


@pytest.mark.parametrize("name, page_json, note", [
    ("run_command", job_page("completed", 0, "x" * 4000, next_offset=4000, total_chars=9000),
     "first part shown; the agent can read the rest with get_job_output"),
    ("run_command", job_page("failed", 2, "head\n…\ntail\n", next_offset=2000, total_chars=9000, excerpt="head_and_tail",
                             omitted={"start": 2000, "end": 8000}),
     "start and end shown; the agent can read the omitted middle with get_job_output"),
    ("wait_job", job_page("completed", 0, "y" * 8000, output_start=4480, next_offset=12480, total_chars=12480,
                          reason="exited", match=None),
     "latest output shown, from character 4,480"),
    ("get_job_output", job_page("completed", 0, "z" * 3000, offset=4000, next_offset=7000, total_chars=9000),
     "characters 4,001–7,000 shown"),
    ("get_job_output", job_page("completed", 0, "all of it", next_offset=None), None),
])
def test_the_state_line_says_which_part_the_result_holds(ui, page, name, page_json, note):
    # B-T3b. Mutation: partial = next_offset != null && !excerpt (fails the wait_job case).
    show(ui, tool(name=name, output=page_json))
    page.locator(".tool-heading").click()
    state = page.locator(".tool-job-state")
    expect(state).to_be_visible()
    if note:
        expect(state).to_contain_text(f" · {note}")
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
    # B-T8. Mutation: leave the summary on event.state.
    show(ui, tool(output=job_page("failed", 1, FAILED_OUTPUT)), tool("ok", output=job_page("completed", 0, "fine"), command="true"))
    page.get_by_text("Activity · 2 actions").click()
    items = page.locator(".activity-summary li")
    expect(items.nth(0)).to_contain_text("Action: completed · exit 1")
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
