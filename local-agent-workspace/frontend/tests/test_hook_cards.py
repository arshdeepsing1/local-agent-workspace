"""Structured hooks in the chat (Conversation.js, ApprovalOptions.js) and their help in Agent tools → Extensions."""
import json
import re

from playwright.sync_api import expect

SETUP = """
window.makeSession = (events, overrides = {}) => ({
  id: 'parent', title: 'Parent task', workspace: '/project', model: 'test-model', status: 'idle',
  events, updated: 1, permission_mode: 'manual', allowed_directories: [], ...overrides,
})
fake.handle = () => fake.json({ ok: true })
"""


def show(ui, *events):
    ui.mount("components/Conversation.js",
             f"{{ session: makeSession([{', '.join(json.dumps(event) for event in events)}]), onError: fake.spy('onError') }}",
             setup=SETUP)


def hook_card(event_id, name, result, **fields):
    return {"id": event_id, "type": "tool", "name": name, "state": "completed", "output": json.dumps(result),
            "input": {"hook_id": "guard", "tool": "write_file", "call_id": "w"}, "hook_result": {"json": True, **result},
            **fields}


def test_a_hook_card_says_what_its_json_output_decided(ui, page):
    show(ui, hook_card("deny", "hook_before_tool", {"decision": "deny", "reason": "Main is frozen."}),
         hook_card("context", "hook_after_tool", {"context": "Run make lint next.", "ignored": ["colour"]}),
         hook_card("stop", "hook_agent_stop", {"decision": "block", "reason": "Tests were not run."},
                   input={"hook_id": "stop", "event": "agent_stop"}))
    cards = page.locator(".tool-card")
    expect(cards.nth(0).locator(".hook-outcome")).to_have_text("Hook blocked: Main is frozen.")
    expect(cards.nth(1).locator(".hook-outcome")).to_have_text("Hook added context")
    expect(cards.nth(2).locator(".hook-outcome")).to_have_text("Hook asked the agent to continue: Tests were not run.")
    # The heading names the hook and the tool it ran for.
    expect(page.get_by_role("button", name="hook before tool guard · write_file")).to_be_visible()
    expect(page.get_by_role("button", name="hook agent stop stop")).to_be_visible()
    page.get_by_role("button", name="hook after tool guard · write_file").click()
    expect(cards.nth(1).locator("pre[aria-label='Hook context']")).to_have_text("Run make lint next.")
    expect(cards.nth(1).get_by_text("Ignored fields: colour")).to_be_visible()


def test_changed_arguments_show_before_and_after(ui, page):
    show(ui, hook_card("swap", "hook_before_tool", {"arguments": {"before": {"command": "pwd"}, "after": {"command": "ls -la"}}}),
         {"id": "cmd", "type": "tool", "name": "run_command", "state": "completed", "input": {"command": "ls -la"},
          "output": "", "call_id": "w", "hook_change": {"hooks": ["swap"], "original": {"command": "pwd"}}})
    hook = page.locator(".tool-card").nth(0)
    expect(hook.locator(".hook-outcome")).to_have_text("Hook changed the arguments")
    page.get_by_role("button", name="hook before tool guard · write_file").click()
    expect(hook.locator("pre[aria-label='Arguments before']")).to_contain_text('"command": "pwd"')
    expect(hook.locator("pre[aria-label='Arguments after']")).to_contain_text('"command": "ls -la"')
    tool = page.locator(".tool-card").nth(1)
    expect(tool.locator(".hook-outcome")).to_have_text("Hook swap changed the arguments")
    page.get_by_role("button", name="run command ls -la").click()
    expect(tool.locator("pre[aria-label='Arguments before the hook']")).to_contain_text('"command": "pwd"')
    expect(tool.get_by_text("Command after the hook")).to_be_visible()
    expect(tool.locator("pre[aria-label='Command']")).to_have_text("ls -la")


def test_a_card_a_hook_asked_for_says_so_and_cannot_be_remembered(ui, page):
    show(ui, {"id": "card", "type": "tool", "name": "write_file", "state": "pending", "preview": "preview",
              "input": {"path": "docs/a.md", "content": "x"}, "rememberable": [],
              "hook_ask": {"hook_id": "review", "reason": "Docs need a second look."}})
    expect(page.get_by_text("The hook review asked for your approval: Docs need a second look.", exact=False)).to_be_visible()
    expect(page.get_by_role("button", name="Approve")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("in this chat$"))).to_have_count(0)
    expect(page.get_by_role("button", name="Always allow…")).to_have_count(0)
    page.get_by_role("button", name="Approve").click()
    ui.wait("fake.calls.length === 1")
    assert [json.loads(call["body"]) for call in ui.requests("/api/sessions/parent/approvals/card", "POST")] == [{"allowed": True}]


def test_user_prompt_hooks_are_visible_on_the_message(ui, page):
    show(ui, {"id": "edited", "type": "user", "text": "Fix the parser bug", "in_wire": True,
              "hook_edit": {"hooks": ["tidy"], "original": "fix teh parser bug pls"},
              "hook_notes": [{"event": "user_prompt", "hook_id": "tidy", "context": "Ticket PRJ-7."},
                             {"event": "session_start", "hook_id": "start", "context": "Branch feature/x."}]},
         {"id": "blocked", "type": "user", "text": "Customer 42 owes money", "in_wire": False,
          "hook_blocked": {"hook_id": "guard", "reason": "No customer data."}},
         {"id": "notice", "type": "notice", "text": "A user-prompt hook (guard) blocked this message: No customer data. "
                                                     "It was not sent to the agent."},
         {"id": "again", "type": "notice", "text": "A hook asked the agent to continue: Run the tests first. (1 of 3)"})
    messages = page.locator(".user-message")
    expect(messages.nth(0).locator(".hook-label")).to_have_text("Edited by hook tidy")
    messages.nth(0).get_by_text("Your original message").click()
    expect(messages.nth(0).get_by_text("fix teh parser bug pls")).to_be_visible()
    messages.nth(0).get_by_text("Hook context sent with this message").click()
    expect(messages.nth(0).locator(".hook-notes li")).to_have_text(["user prompt hook tidy: Ticket PRJ-7.",
                                                                     "session start hook start: Branch feature/x."])
    expect(messages.nth(1).locator(".hook-label")).to_have_text("Not sent: blocked by hook guard (No customer data.)")
    expect(messages.nth(1)).to_have_class(re.compile("hook-blocked"))
    expect(page.get_by_text("A hook asked the agent to continue: Run the tests first. (1 of 3)")).to_be_visible()


def test_the_extensions_help_lists_every_hook_event(ui, page):
    ui.mount("components/AgentToolsDialog.js", "{ ...dialogProps(session()) }", scenario="agent_tools")
    page.get_by_role("button", name="Extensions", exact=True).click()
    page.get_by_text("Hook events and JSON output").click()
    table = page.get_by_role("table", name="Hook events")
    expect(table.locator("tbody th")).to_have_text(["before_tool", "after_tool", "tool_failure", "user_prompt",
                                                    "session_start", "agent_stop", "subagent_start", "subagent_stop"])
    expect(table.get_by_role("row", name=re.compile("^agent_stop"))).to_contain_text("at most 3 times")
    expect(page.get_by_text('"decision":"deny"', exact=False)).to_be_visible()
    page.get_by_text("Configuration example").click()
    example = json.loads(page.locator(".agent-tools-example pre").first.text_content())
    assert [hook["event"] for hook in example["hooks"]] == ["before_tool", "user_prompt", "agent_stop"]
