"""Subagent cards in the parent chat: Stop subagent, Allow or Decline the card a subagent waits on (or open it
to answer a question), a background subagent's badge and late report with its structured findings, the
notice when it ended, a sibling's change note, and Agent tools → Usage with the chat's subagents."""
import json
import re
from datetime import datetime

from playwright.sync_api import expect

SETUP = """
window.makeSession = (events, overrides = {}) => ({
  id: 'parent', title: 'Parent task', workspace: '/project', model: 'test-model', status: 'idle',
  events, updated: 1, permission_mode: 'manual', allowed_directories: [], ...overrides,
})
"""


def show(ui, events: str):
    ui.mount("components/Conversation.js", f"""{{ session: makeSession({events}), onError: fake.spy('onError'),
      onSelectSession: fake.spy('select'), onOpenPath: fake.spy('open') }}""",
             setup=SETUP + "fake.override = path => path.endsWith('/stop') ? fake.json({ ok: true }) : undefined")


def delegation(event_id="d", child="child-1", task="Fix the docs", batch=None, state="running", **progress):
    """A delegate_task card with its subagent's progress; batch: (id, index, size) of a multi-agent run."""
    base = {"status": "running", "completed_tools": 1, "agent_type": "editor"}
    event = {"id": event_id, "type": "tool", "name": "delegate_task", "call_id": f"call-{event_id}", "input": {"task": task},
             "state": state, "delegation": {**base, **progress}}
    if child:
        event["child_session_id"] = child
    if batch:
        event["delegation_batch"] = {"id": batch[0], "index": batch[1], "size": batch[2]}
    return event


def test_a_running_subagent_can_be_stopped_from_its_card(ui, page):
    show(ui, json.dumps([delegation()]))
    progress = page.get_by_label("Subagent progress")
    progress.get_by_role("button", name="Stop subagent").click()
    page.wait_for_function("fake.requests('/api/sessions/child-1/stop', 'POST').length === 1")
    expect(progress.get_by_role("button", name="Allow")).to_have_count(0)
    assert ui.spy("onError") == []


def test_the_card_a_subagent_waits_on_is_answered_from_the_parent(ui, page):
    pending = {"event_id": "ev-1", "name": "run_command", "kind": "approval", "title": "npm test"}
    show(ui, json.dumps([delegation(status="awaiting_approval", pending=pending)]))
    progress = page.get_by_label("Subagent progress")
    expect(progress.locator(".delegation-status")).to_have_text("Subagent: Waiting for approval")
    expect(progress.locator(".subagent-pending")).to_have_text("Waits for approval: run command · npm test")
    progress.get_by_role("button", name="Allow").click()
    page.wait_for_function("fake.requests('/api/sessions/child-1/approvals/ev-1', 'POST').length === 1")
    progress.get_by_role("button", name="Decline").click()
    page.wait_for_function("fake.requests('/api/sessions/child-1/approvals/ev-1', 'POST').length === 2")
    assert ui.bodies("/api/sessions/child-1/approvals/ev-1", "POST") == [{"allowed": True}, {"allowed": False}]
    # A card whose title is not the whole action, or that warns, is reviewed in the subagent (Decline stays).
    ui.rerender(f"""{{ session: makeSession({json.dumps([delegation(status="awaiting_approval", pending={
        "event_id": "ev-2", "name": "run_command", "kind": "approval", "title": "cat .netrc", "review": True,
        "warnings": ["names credential-looking files"]})])}),
      onError: fake.spy('onError'), onSelectSession: fake.spy('select') }}""")
    expect(progress.locator(".subagent-pending-warning")).to_have_text("Its card warns: names credential-looking files.")
    expect(progress.get_by_role("button", name="Allow")).to_have_count(0)
    progress.get_by_role("button", name="Review in subagent").click()
    assert ui.spy("select") == [["child-1"]]
    progress.get_by_role("button", name="Decline").click()
    page.wait_for_function("fake.requests('/api/sessions/child-1/approvals/ev-2', 'POST').length === 1")
    # A question (or a plan) is answered in the subagent itself.
    ui.rerender(f"""{{ session: makeSession({json.dumps([delegation(status="awaiting_approval",
        pending={"event_id": "q", "name": "ask_user", "kind": "question", "title": "Which folder?"})])}),
      onError: fake.spy('onError'), onSelectSession: fake.spy('select') }}""")
    expect(progress.locator(".subagent-pending")).to_have_text("Asks: Which folder?")
    expect(progress.get_by_role("button", name="Allow")).to_have_count(0)
    progress.get_by_role("button", name="Answer in subagent").click()
    assert ui.spy("select") == [["child-1"]]
    # A finished subagent has no controls.
    ui.rerender(f"""{{ session: makeSession({json.dumps([{**delegation(status="completed", terminal_reason="completed"),
        "state": "completed"}])}), onError: fake.spy('onError') }}""")
    expect(progress.get_by_role("button", name="Stop subagent")).to_have_count(0)


def test_a_background_subagents_card_and_its_structured_report(ui, page):
    started = {"child_session_id": "child-1", "agent_type": "explore", "status": "running", "background": True,
               "transcript": "conversations/child-1.jsonl", "message": "Started in the background."}
    card = {**delegation(agent_type="explore", background=True), "state": "completed", "output": json.dumps(started)}
    notice = {"id": "n", "type": "notice", "text": "Background subagent explore finished: Map the parser",
              "subagent_id": "child-1", "subagent_status": "completed"}
    show(ui, json.dumps([card]))
    progress = page.get_by_label("Subagent progress")
    expect(progress.locator(".delegation-background")).to_have_text("Background")
    expect(page.locator(".tool-card .tool-status")).to_have_text("started in background")
    expect(progress.locator(".delegation-report")).to_have_count(0)  # Its result has not come yet.
    result = {**started, "status": "completed", "steps_used": 2, "tool_calls": 3, "changed_files": [], "output": "",
              "report": {"summary": "The parser reads **two** settings.",
                         "findings": [{"path": "src/parser.py", "line": 42, "text": "Reads the timeout."},
                                      {"text": "No test covers it."}],
                         "next_steps": ["Add a test."], "findings_total": 4}}
    done = {**card, "delegation": {**card["delegation"], "status": "completed", "terminal_reason": "completed"},
            "delegation_result": result}
    ui.rerender(f"""{{ session: makeSession({json.dumps([done, notice])}), onError: fake.spy('onError'),
      onSelectSession: fake.spy('select'), onOpenPath: fake.spy('open') }}""")
    expect(page.locator(".tool-card .tool-status")).to_have_count(0)  # Ended: the usual tick.
    report = progress.locator("details.delegation-report")
    report.locator("summary").click()
    expect(report.locator(".subagent-report strong")).to_have_text("two")
    findings = report.locator(".subagent-findings li")
    expect(findings).to_have_count(3)
    expect(findings.nth(0)).to_have_text("src/parser.py:42 Reads the timeout.")
    expect(findings.nth(1)).to_have_text("No test covers it.")
    expect(findings.nth(2)).to_have_text("and 2 more")
    expect(report).to_contain_text("Add a test.")
    expect(report.locator("small")).to_have_text("2 steps · 3 tool calls · Transcript: conversations/child-1.jsonl")
    findings.nth(0).get_by_role("button", name="Open src/parser.py:42").click()
    assert ui.spy("open") == [[{"path": "src/parser.py", "line": 42}]]
    expect(progress.get_by_role("button", name="Stop subagent")).to_have_count(0)
    # The notice when it ended opens it too.
    page.locator(".conversation-notice").get_by_role("button", name="Open subagent").click()
    assert ui.spy("select") == [["child-1"]]


def test_a_siblings_change_and_the_apps_wake_message_are_labelled(ui, page):
    events = [{"id": "u", "type": "user", "text": "[Sent by the app: the background subagent 1234abcd (explore) finished.]",
               "background_wake": True},
              {"id": "w", "type": "tool", "name": "write_file", "input": {"path": "shared.txt", "content": "B"},
               "state": "pending", "preview": "shared.txt", "sibling_change": {
                   "subagent_id": "abcdef1234567890", "agent_type": "editor", "path": "shared.txt"}}]
    show(ui, json.dumps(events))
    expect(page.locator(".user-message .delegation-origin")).to_have_text(
        "Sent by the app when a background subagent ended")
    expect(page.locator(".tool-sibling")).to_have_text(
        "Another subagent of this chat (Editor, abcdef12) changed shared.txt during its run, so this change asks first. "
        "Approve it only if both changes should stay.")


def test_usage_lists_the_chats_subagents_with_their_totals(ui, page):
    subagents = {"totals": {"calls": 3, "input_tokens": 600, "output_tokens": 90, "cache_read_input_tokens": 100,
                            "cache_creation_input_tokens": None},
                 "children": [{"id": "child-1", "agent_type": "explore", "task": "Map the parser", "status": "completed",
                               "estimated_dbu": 0.05, "usage": {"calls": 3, "input_tokens": 600, "output_tokens": 90,
                                                                "cache_read_input_tokens": 100,
                                                                "cache_creation_input_tokens": None}},
                              {"id": "child-2", "agent_type": "general", "task": "Old task", "status": "completed",
                               "estimated_dbu": None, "usage": None, "deleted": True}],
                 "without_usage": 1}
    setup = f"""fake.override = path => path.endsWith('/metrics') ? fake.json({{ ...metrics,
      spending: {{ limit_dbu: null, source: 'settings', spent_dbu: 0.25, own_dbu: 0.2, subagents_dbu: 0.05,
        unpriced_calls: 0, percent: null }}, subagents: {json.dumps(subagents)} }}) : undefined"""
    ui.mount("components/AgentToolsDialog.js", "{ ...dialogProps(session()) }", scenario="agent_tools", setup=setup)
    page.get_by_role("button", name="Usage", exact=True).click()
    group = page.get_by_role("group", name="This chat and its subagents")
    cards = group.locator(".usage-cards > div")
    expect(cards.nth(0)).to_have_text("Model calls5")
    expect(cards.nth(1)).to_have_text("Uncached input1,800")
    expect(cards.nth(2)).to_have_text("Cache read400")
    expect(cards.nth(3)).to_have_text("Cache writeUnavailable")
    expect(cards.nth(5)).to_have_text("Estimated cost0.25 DBU")
    expect(group).to_contain_text("1 subagent from before this app kept subagent usage is not in these totals.")
    group.locator("summary").click()
    rows = group.locator("tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text("Explore Map the parser")
    expect(rows.nth(1)).to_contain_text("deleted")
    expect(rows.nth(1).get_by_role("button")).to_have_count(0)
    rows.nth(0).get_by_role("button", name="Open subagent: Map the parser").click()
    assert ui.spy("select") == [["child-1"]] and ui.spy("close") == [[]]
    page.get_by_role("button", name="Export with subagents").click()
    page.wait_for_function("fake.requests('/api/sessions/a/usage.csv?include_subagents=true').length === 1")


# A multi-agent run (2026-10-19): the delegate_task cards of one response under one head (Conversation.js RunHead).

def member(n, size=3, batch="b1", child="", **fields):
    """Card n of run batch: a running editor subagent child-n unless fields say otherwise (child=None: none yet)."""
    return delegation(f"d{n}", f"child-{n}" if child == "" else child, f"Task {n}", batch=(batch, n, size), **fields)


def ended(n, status="completed", reason="completed", size=3, start=1000.0, end=1060.0):
    return member(n, size, status=status, terminal_reason=reason, state="completed", started=start + n, ended=end + n)


def show_run(ui, events, **overrides):
    show(ui, json.dumps(events) + (f", {json.dumps(overrides)}" if overrides else ""))


def rerender_run(ui, events, **overrides):
    ui.rerender(f"""{{ session: makeSession({json.dumps(events)}, {json.dumps(overrides)}), onError: fake.spy('onError'),
      onSelectSession: fake.spy('select'), onOpenPath: fake.spy('open') }}""")


def run_head(page):
    return page.locator(".run-head")


def rows(page):
    """The run's rows on the page, by event id."""
    return page.locator(".tool-card.run-member").evaluate_all("els => els.map(el => el.dataset.eventId)")


def test_a_runs_head_counts_its_subagents_their_statuses_and_how_many_finished(ui, page):
    pending = {"event_id": "ev-1", "name": "run_command", "kind": "approval", "title": "npm test"}
    events = [member(0, 6, started=1000.0), member(1, 6, status="awaiting_approval", pending=pending, started=1001.0),
              member(2, 6, child=None, status="queued", completed_tools=0), ended(3, size=6), ended(4, size=6),
              ended(5, "failed", "error", size=6)]
    show_run(ui, events)
    head = run_head(page)
    toggle = head.get_by_role("button", name="6 subagents", exact=True)
    expect(toggle).to_have_attribute("aria-expanded", "true")  # Seen with subagents still going: open.
    expect(head.get_by_role("status")).to_have_text(
        "1 running · 1 waiting for approval · 1 waiting for a free slot · 2 completed · 1 failed")
    bar = head.get_by_role("progressbar", name="Subagents finished")
    assert bar.evaluate("el => [el.value, el.max]") == [3, 6]
    expect(head.locator(".run-chip")).to_have_count(0)
    expect(head.get_by_role("button", name="Show in panel")).to_have_count(0)  # No onShowProgress here.
    assert rows(page) == ["d0", "d1", "d2", "d3", "d4", "d5"]
    expect(page.locator(".tool-card.run-first")).to_have_attribute("data-event-id", "d0")
    expect(page.locator(".tool-card.run-last")).to_have_attribute("data-event-id", "d5")
    # The rows are today's cards: heading, progress, Open subagent and Stop where there is a subagent.
    expect(page.get_by_role("button", name="delegate task Task 0", exact=True)).to_be_visible()
    queued = page.locator("#tool-d2")
    expect(queued.locator(".delegation-status")).to_have_text("Subagent: waiting for a free slot")
    expect(queued.get_by_role("button", name="Stop subagent")).to_have_count(0)
    expect(queued.get_by_role("button", name="Open subagent")).to_have_count(0)
    expect(page.locator("#tool-d0").get_by_role("button", name="Stop subagent")).to_be_visible()
    # A background run says so.
    rerender_run(ui, [member(0, 2, background=True, started=1000.0), member(1, 2, background=True, started=1000.0)])
    expect(head.locator(".run-chip")).to_have_text("Background")
    expect(head.get_by_role("button", name="2 subagents", exact=True)).to_be_visible()


def test_show_in_panel_is_offered_when_the_page_has_a_progress_panel(ui, page):
    ui.mount("components/Conversation.js", f"""{{ session: makeSession({json.dumps([member(0, 2), member(1, 2)])}),
      onError: fake.spy('onError'), onShowProgress: fake.spy('progress') }}""", setup=SETUP)
    run_head(page).get_by_role("button", name="Show in panel").click()
    assert ui.spy("progress") == [[]]


def test_cards_still_being_prepared_and_cards_on_an_earlier_page(ui, page):
    show_run(ui, [member(0), member(1)], status="running")
    expect(run_head(page).locator(".run-note")).to_have_text("Preparing 2 of 3")
    # A turn stopped while preparing: the head says how many started.
    rerender_run(ui, [member(0), member(1)], status="idle")
    expect(run_head(page).locator(".run-note")).to_have_text("2 of 3 started")
    # The first card is on a page not loaded yet.
    rerender_run(ui, [member(1), member(2)], status="idle", events_start=40)
    expect(run_head(page).locator(".run-note")).to_have_text("1 earlier, not loaded")
    expect(run_head(page).get_by_role("button", name="2 subagents", exact=True)).to_be_visible()


def test_a_card_waiting_for_approval_to_start_a_subagent_keeps_its_approval_in_the_run(ui, page):
    first = {**member(0), "state": "pending", "preview": "Task 0", "rememberable": ["chat"]}
    del first["delegation"]
    show_run(ui, [first, member(1, child=None, status="queued", completed_tools=0)], status="awaiting_approval")
    expect(run_head(page).get_by_role("status")).to_have_text("1 needs approval · 1 waiting for a free slot")
    card = page.locator(".tool-card.needs-approval")
    expect(card).to_have_attribute("data-event-id", "d0")
    expect(card).to_have_class(re.compile(r"\brun-member\b"))
    expect(card.get_by_role("button", name="Allow subagents in this chat")).to_be_visible()
    found = ui.js("import('/static/js/components/Conversation.js').then(m => [...document.querySelectorAll(m.PENDING_CARDS)].map(el => el.dataset.eventId))")
    assert found == ["d0"]
    # Closed by you, the run still shows the card that waits.
    run_head(page).get_by_role("button", name="2 subagents").click()
    assert rows(page) == ["d0"]


def test_a_run_first_seen_finished_starts_closed_with_its_failed_and_stopped_rows_shown(ui, page):
    events = [ended(0), ended(1, "failed", "error"), ended(2, "cancelled", "stopped")]
    show_run(ui, events)
    head = run_head(page)
    toggle = head.get_by_role("button", name="3 subagents", exact=True)
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(head.get_by_role("status")).to_have_text("1 completed · 1 failed · 1 cancelled")
    expect(head.locator(".run-elapsed")).to_have_text("1 min 02 s")
    assert rows(page) == ["d1", "d2"]
    expect(page.locator(".tool-card.run-first")).to_have_attribute("data-event-id", "d1")
    toggle.click()
    assert rows(page) == ["d0", "d1", "d2"]
    toggle.click()
    assert rows(page) == ["d1", "d2"]


def test_a_run_seen_live_stays_open_when_it_ends_until_you_close_it(ui, page):
    show_run(ui, [member(0, 2, started=1000.0), member(1, 2, started=1000.0)], status="delegating")
    toggle = run_head(page).get_by_role("button", name="2 subagents", exact=True)
    expect(toggle).to_have_attribute("aria-expanded", "true")
    rerender_run(ui, [ended(0, size=2), ended(1, size=2)], status="idle")
    expect(run_head(page).get_by_role("status")).to_have_text("2 completed")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    assert rows(page) == ["d0", "d1"]
    toggle.click()
    assert rows(page) == []
    expect(run_head(page)).not_to_have_class(re.compile("has-rows"))


def test_a_subagent_waiting_inside_a_closed_run_stays_answerable(ui, page):
    pending = {"event_id": "ev-1", "name": "run_command", "kind": "approval", "title": "npm test"}
    show_run(ui, [ended(0, size=2), member(1, 2, status="awaiting_approval", pending=pending, started=1000.0)])
    run_head(page).get_by_role("button", name="2 subagents").click()
    assert rows(page) == ["d1"]
    progress = page.locator("#tool-d1").get_by_label("Subagent progress")
    expect(progress).to_have_class(re.compile(r"\bneeds-answer\b"))
    progress.get_by_role("button", name="Allow").click()
    page.wait_for_function("fake.requests('/api/sessions/child-1/approvals/ev-1', 'POST').length === 1")
    progress.get_by_role("button", name="Decline").click()
    page.wait_for_function("fake.requests('/api/sessions/child-1/approvals/ev-1', 'POST').length === 2")
    assert ui.bodies("/api/sessions/child-1/approvals/ev-1", "POST") == [{"allowed": True}, {"allowed": False}]
    # Answered, the subagent runs on and its row folds with the closed run.
    rerender_run(ui, [ended(0, size=2), member(1, 2, started=1000.0)])
    assert rows(page) == []
    expect(page.locator(".needs-answer")).to_have_count(0)


def test_the_message_box_hint_shows_a_subagent_waiting_for_you(ui, page):
    pending = {"event_id": "ev-1", "name": "ask_user", "kind": "question", "title": "Which folder?"}
    events = [{"id": "u", "type": "user", "text": "Split it up"}, ended(0, size=2),
              member(1, 2, status="awaiting_approval", pending=pending, started=1000.0)]
    events += [{"id": f"r{n}", "type": "assistant", "text": "Some detail about the change. " * 40} for n in range(6)]
    page.set_viewport_size({"width": 1280, "height": 700})
    ui.app(session="a", scenario="workspace", setup=f"""fake.sessions.set('a', {{ ...makeSession('a', 'Split'), status: 'delegating',
      events: {json.dumps(events)} }})""")
    ui.show_session("a")
    hint = page.locator(".composer-hint")
    expect(hint.get_by_role("status")).to_have_text("A subagent is waiting for your answer above.")
    answer = page.locator("#tool-d1").get_by_role("button", name="Answer in subagent")
    expect(answer).not_to_be_in_viewport()
    hint.get_by_role("button", name="Show", exact=True).click()
    expect(answer).to_be_focused()
    expect(answer).to_be_in_viewport()
    # Once nothing waits, the hint goes.
    ui.emit("a", {"type": "event", "event": member(1, 2, started=1000.0)})
    expect(hint).to_have_count(0)


def test_a_subagent_row_shows_its_step_its_current_tool_and_its_failed_actions(ui, page):
    live = member(0, 2, steps=4, max_steps=10, last_tool="run_command", last_tool_state="running",
                  last_tool_subject="npm test", failed_tools=1, tool_calls=5, started=1000.0)
    show_run(ui, [live, member(1, 2, started=1000.0)])
    row = page.locator("#tool-d0").get_by_label("Subagent progress")
    expect(row.locator(".delegation-steps")).to_have_text("Step 4 of 10")
    expect(row.locator(".delegation-tool")).to_have_text("Now: run command · npm test")
    expect(row.locator(".delegation-tool")).to_have_attribute("title", "npm test")
    expect(row.locator(".delegation-failed")).to_have_text("1 failed action")
    rerender_run(ui, [{**live, "delegation": {**live["delegation"], "last_tool_state": "completed", "failed_tools": 2}},
                      member(1, 2, started=1000.0)])
    expect(row.locator(".delegation-tool")).to_have_text("Last tool: run command · npm test")
    expect(row.locator(".delegation-failed")).to_have_text("2 failed actions")
    # A card from an older app shows what it showed before.
    rerender_run(ui, [member(0, 2, last_tool="read_file", started=1000.0), member(1, 2, started=1000.0)])
    expect(row.locator(".delegation-tool")).to_have_text("Last tool: read file")
    expect(row.locator(".delegation-steps")).to_have_count(0)
    expect(row.locator(".delegation-failed")).to_have_count(0)
    # The heading's icon is the subagent's (Bot), not a file.
    expect(page.locator("#tool-d0 .tool-heading svg.lucide-bot")).to_have_count(1)


def test_older_chats_join_adjacent_delegations_into_a_run_unless_a_reply_splits_them(ui, page):
    hook = {"id": "h", "type": "tool", "name": "hook_before_tool", "state": "completed", "output": "{}",
            "input": {"hook_id": "log", "tool": "delegate_task", "call_id": "c"}, "hook_result": {"json": True}}
    first, second = delegation("a", "child-a", "Map it"), delegation("b", "child-b", "Test it")
    show_run(ui, [first, hook, second])
    expect(run_head(page).get_by_role("button", name="2 subagents", exact=True)).to_be_visible()
    assert rows(page) == ["a", "h", "b"]
    rerender_run(ui, [first, {"id": "q", "type": "assistant", "text": ""}, second])
    expect(run_head(page)).to_have_count(0)
    expect(page.locator(".tool-card")).to_have_count(2)
    # A lone delegation has no head.
    rerender_run(ui, [first])
    expect(run_head(page)).to_have_count(0)


def test_the_heads_time_counts_while_one_runs_and_its_status_changes_only_with_a_status(ui, page):
    ui.mount("components/Conversation.js", "{ session: makeSession([]), onError: fake.spy('onError') }", setup=SETUP, clock=True)
    page.clock.pause_at(datetime.fromtimestamp(ui.js("Date.now()") / 1000 + 1))
    now = ui.js("Date.now() / 1000")
    rerender_run(ui, [member(0, 2, started=now - 5), member(1, 2, started=now - 3)], status="delegating")
    elapsed = run_head(page).locator(".run-elapsed")
    expect(elapsed).to_have_text("5 s")
    status = run_head(page).get_by_role("status")
    ui.run("window.statusChanges = 0; new MutationObserver(() => window.statusChanges++).observe("
           "document.querySelector('.run-aggregate'), { childList: true, characterData: true, subtree: true })")
    ui.tick(3000)
    expect(elapsed).to_have_text("8 s")
    assert ui.js("window.statusChanges") == 0
    rerender_run(ui, [ended(0, size=2, start=now - 5, end=now + 2), member(1, 2, started=now - 3)], status="delegating")
    expect(status).to_have_text("1 running · 1 completed")
    assert ui.js("window.statusChanges") > 0
    # All ended: the time from the first start to the last end, and it no longer counts.
    rerender_run(ui, [ended(0, size=2, start=now - 5, end=now + 2), ended(1, size=2, start=now - 4, end=now + 3)])
    expect(elapsed).to_have_text("9 s")
    ui.tick(5000)
    expect(elapsed).to_have_text("9 s")
