"""Subagent cards in the parent chat: Stop subagent, Allow or Decline the card a subagent waits on (or open it
to answer a question), a background subagent's badge and late report with its structured findings, the
notice when it ended, a sibling's change note, and Agent tools → Usage with the chat's subagents."""
import json

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


def delegation(**progress):
    base = {"status": "running", "completed_tools": 1, "agent_type": "editor"}
    return {"id": "d", "type": "tool", "name": "delegate_task", "call_id": "c", "input": {"task": "Fix the docs"},
            "state": "running", "child_session_id": "child-1", "delegation": {**base, **progress}}


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
        "Sent by the app when a background subagent finished")
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
