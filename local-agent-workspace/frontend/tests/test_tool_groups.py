"""Folded tool calls (transcriptGroups.js, Conversation.js ToolGroupHead): two or more finished read-only cards in a
row show one head ("Ran 3 commands"), while every card that needs you, runs or went wrong stays in the transcript;
Settings → Appearance → Group tool calls turns folding off (viewPrefs.js). Also the outcome a background command's
card shows once its end notice came."""
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
OFF = "localStorage.setItem('group-tool-calls', 'off')"


def job_page(state="completed", exit_code=0, output="ok\n", **extra):
    return json.dumps({"job_id": "0123456789abcdef", "state": state, "exit_code": exit_code, "output": output, "offset": 0,
                       "next_offset": None, "total_chars": len(output), "truncated": False, "background": False, **extra}, indent=2)


def command(event_id, cmd="pytest -q", output=None, **fields):
    return {"id": event_id, "type": "tool", "name": "run_command", "state": "completed", "input": {"command": cmd},
            "output": job_page() if output is None else output, **fields}


def tool(event_id, name, state="completed", output="", **input_fields):
    return {"id": event_id, "type": "tool", "name": name, "state": state, "input": input_fields, "output": output}


def read(event_id, path="src/app.py", start=1, end=40):
    return tool(event_id, "read_file", output=json.dumps({"path": path, "start_line": start, "end_line": end, "content": "x"}),
                path=path, start_line=start)


def quiet(event_id):
    """A model response that only called tools: no text, no thinking (its request details are all it shows)."""
    return {"id": event_id, "type": "assistant", "text": "", "request_info": {"model": "m", "status": "completed",
                                                                            "finish_reason": "tool_calls"}}


def reply(event_id, text="Done."):
    return {"id": event_id, "type": "assistant", "text": text}


def user(event_id, text="Go on"):
    return {"id": event_id, "type": "user", "text": text}


def hook(event_id, **result):
    return {"id": event_id, "type": "tool", "name": "hook_after_tool", "state": "completed", "output": json.dumps(result),
            "input": {"hook_id": "log", "tool": "run_command", "call_id": "c"}, "hook_result": {"json": True, **result}}


def props(events, **overrides):
    return f"{{ session: makeSession({json.dumps(events)}, {json.dumps(overrides)}), onError: fake.spy('onError') }}"


def show(ui, events, setup="", **overrides):
    ui.mount("components/Conversation.js", props(events, **overrides), setup=SETUP + setup)


def rerender(ui, events, **overrides):
    ui.rerender(props(events, **overrides))


def order(page):
    """The transcript's items in page order: 'head:<phrase>' for a group head, the event id for a card or reply."""
    return page.locator(".transcript-inner").evaluate("""el => [...el.children].filter(child => child.className && !child.matches('.activity-summary'))
      .map(child => child.matches('.tool-group') ? `head:${child.querySelector('.tool-group-phrase').textContent}`
        : child.dataset.eventId || child.className)""")


def head(page, phrase):
    return page.locator(".tool-group-head").filter(has_text=phrase)


def test_three_commands_with_quiet_replies_between_them_fold_under_one_head(ui, page):
    events = [user("u"), command("a", "pytest -q"), quiet("q1"), command("b", "ruff check ."), quiet("q2"), command("c", "npm test")]
    show(ui, events)
    group = page.get_by_role("button", name="Ran 3 commands npm test", exact=True)
    expect(group).to_have_attribute("aria-expanded", "false")
    expect(group.locator(".tool-group-subject")).to_have_text("npm test")
    expect(group.locator(".tool-group-steps")).to_have_text("· 3 steps")
    # Closed: the cards and the replies between them are not on the page at all.
    expect(page.locator(".tool-card")).to_have_count(0)
    expect(page.locator(".assistant-reply")).to_have_count(0)
    group.click()
    expect(group).to_have_attribute("aria-expanded", "true")
    expect(page.locator(".tool-card.grouped")).to_have_count(3)
    expect(page.locator(".assistant-reply.grouped")).to_have_count(2)
    # In place, in order, after the head.
    assert order(page)[1:] == ["head:Ran 3 commands", "a", "assistant-reply grouped", "b", "assistant-reply grouped", "c"]
    assert group.get_attribute("aria-controls") == "tool-a tool-b tool-c"
    group.click()
    expect(page.locator(".tool-card")).to_have_count(0)
    assert ui.spy("onError") == []


def test_reads_count_distinct_files_and_a_search_reads_once(ui, page):
    show(ui, [read("p1", start=1, end=40), read("p2", start=41, end=80), tool("s", "search_files", pattern="TODO", path=".")])
    expect(page.locator(".tool-group-phrase")).to_have_text("Read 1 file, searched once")


def test_a_failed_command_splits_the_group_and_stays_visible_with_its_outcome(ui, page):
    events = [command("a"), command("b"), command("x", "make", job_page("failed", 1, "Error 1\n")), command("c"), command("d")]
    show(ui, events)
    assert order(page) == ["head:Ran 2 commands", "x", "head:Ran 2 commands"]
    failed = page.locator(".tool-card")
    expect(failed).to_have_count(1)
    expect(failed.locator(".tool-outcome")).to_have_text("Exit 1 · Error 1")


def test_a_card_waiting_for_approval_stays_out_of_the_group_and_is_the_pending_card(ui, page):
    pending = {**command("p", "rm -rf build"), "state": "pending", "preview": "rm -rf build", "output": ""}
    show(ui, [command("a"), command("b"), pending])
    assert order(page) == ["head:Ran 2 commands", "p"]
    expect(page.locator(".tool-card.needs-approval")).to_be_visible()
    found = ui.js("import('/static/js/components/Conversation.js').then(m => [...document.querySelectorAll(m.PENDING_CARDS)].map(el => el.dataset.eventId))")
    assert found == ["p"]


def test_the_message_box_show_still_focuses_a_waiting_card_after_a_group(ui, page):
    events = [user("u", "Clean up"), command("a"), quiet("q"), command("b"),
              {**command("p", "rm -rf build"), "state": "pending", "preview": "rm -rf build", "output": ""}]
    ui.app(session="a", scenario="workspace", setup=f"""fake.sessions.set('a', {{ ...makeSession('a', 'Cleanup'),
      status: 'awaiting_approval', events: {json.dumps(events)} }})""")
    ui.show_session("a")
    expect(page.get_by_role("button", name="Ran 2 commands pytest -q", exact=True)).to_be_visible()
    hint = page.locator(".composer-hint")
    expect(hint.get_by_role("status")).to_have_text("An action is waiting for your approval above.")
    hint.get_by_role("button", name="Show", exact=True).click()
    expect(page.get_by_role("button", name="run command rm -rf build", exact=True)).to_be_focused()


def test_a_running_card_stays_below_the_head_then_joins_and_a_card_you_used_keeps_the_group_open(ui, page):
    running = {**command("c", "npm test"), "state": "running", "output": "3 passed"}
    events = [command("a"), command("b"), running]
    show(ui, events)
    assert order(page) == ["head:Ran 2 commands", "c"]
    expect(page.locator(".tool-card")).to_have_count(1)
    rerender(ui, [command("a"), command("b"), command("c", "npm test")])
    expect(page.locator(".tool-group-phrase")).to_have_text("Ran 3 commands")
    expect(page.locator(".tool-card")).to_have_count(0)
    # A card you opened while it ran does not fold away when it ends: its group opens.
    rerender(ui, [command("a"), command("b"), command("c", "npm test"), user("u"), command("d", "ls"), {**command("e", "make"), "state": "running"}])
    page.get_by_role("button", name="run command make", exact=True).click()
    expect(page.locator("pre[aria-label=Command]")).to_have_text("make")
    rerender(ui, [command("a"), command("b"), command("c", "npm test"), user("u"), command("d", "ls"), command("e", "make")])
    second = head(page, "Ran 2 commands")
    expect(second).to_have_attribute("aria-expanded", "true")
    expect(page.locator("pre[aria-label=Command]")).to_have_text("make")  # The same card, still expanded.
    expect(head(page, "Ran 3 commands")).to_have_attribute("aria-expanded", "false")
    # Closing the group forgets that card: it folds like the others.
    second.click()
    expect(page.locator(".tool-card")).to_have_count(0)


def test_what_never_folds_and_what_ends_a_group(ui, page):
    show(ui, [read("r1"), tool("e", "edit_file", path="src/app.py"), read("r2"),
              user("u"), command("lone")])
    expect(page.locator(".tool-group")).to_have_count(0)
    expect(page.locator(".tool-card")).to_have_count(4)
    # Each of these ends a group: a reply with text, a streaming reply, a notice, a user message.
    breakers = [reply("t"), {"id": "s", "type": "assistant", "text": "", "request_info": {"model": "m", "status": "running"}},
                {"id": "n", "type": "notice", "text": "Background job completed (exit 0): serve"}, user("m")]
    for breaker in breakers:
        rerender(ui, [command("a"), breaker, command("b")])
        expect(page.locator(".tool-group")).to_have_count(0)
        expect(page.locator(".tool-card")).to_have_count(2)
    # Quiet replies before the first card and after the last stay: they belong to their own step or the next.
    rerender(ui, [user("u"), quiet("before"), command("a"), command("b"), quiet("after"), reply("t")])
    expect(page.locator(".assistant-reply")).to_have_count(3)
    assert order(page) == ["user-message", "assistant-reply", "head:Ran 2 commands", "assistant-reply", "assistant-reply"]


LAYOUT = """async events => {
  const { layoutTranscript } = await import('/static/js/transcriptGroups.js')
  const { toolStatesFor } = await import('/static/js/components/Conversation.js')
  // A group's cards follow its head: the head names them.
  return layoutTranscript(events, toolStatesFor(events)).filter(item => !item.group).map(item => item.kind === 'group'
    ? `[${item.phrase}: ${item.members.join(' ')}]` : item.kind === 'run' ? `run(${item.members.join(' ')})` : item.event.id)
}"""

NEVER = [
    {**command("bg", "serve", job_page("running", None, "", background=True)), "input": {"command": "serve", "background": True}},
    command("slow", "sleep 99", job_page("timed_out", None, "waiting\n")),
    command("stopped", "tail -f log", job_page("cancelled", None, "")),
    {**command("edited", "echo ok"), "edited": True, "approval": "approved"},
    {**command("secret", "cat .env"), "secret_files": [".env"]},
    {**read("withheld"), "result_withheld": {"estimated_tokens": 9000, "allowance_tokens": 4000}},
    {**read("shortened"), "result_shortened": True},
    tool("mcp", "mcp__docs__search", output=json.dumps({"output": "hit", "is_error": False}), query="x"),
    {**command("mine", "ls"), "name": "user_command"},
    tool("write", "write_file", path="a.txt", content="x"),
    tool("task", "update_task", task_id="1", status="done"),
    {**read("sibling"), "sibling_change": {"subagent_id": "abcdef12", "agent_type": "editor", "path": "src/app.py"}},
    hook("decided", decision="deny", reason="No."),
    {**command("hooked"), "hook_change": {"hooks": ["swap"], "original": {"command": "pwd"}}},
    tool("failed", "read_file", state="error", output="No such file", path="nope.py"),
    tool("refused", "run_command", state="rejected", output="Not run: blocked", command="rm -rf /"),
]


def test_the_layout_folds_only_finished_read_only_cards(ui, page):
    ui.start(setup=SETUP)
    layout = lambda events: page.evaluate(LAYOUT, events)  # noqa: E731
    # Every groupable kind folds, with its phrase; hooks that did nothing join without counting.
    folds = [command("c1"), command("c2"), read("r"), tool("o", "outline_file", path="src/app.py"), tool("l", "list_files", path="src"),
             tool("v", "project_overview"), tool("f", "find_files", pattern="*.py"), tool("j", "list_jobs"),
             tool("w", "wait_job", output=job_page("completed", 0, "", reason="exited"), job_id="0123456789abcdef"),
             tool("g", "get_job_output", output=job_page("completed", 1, "", reason=None), job_id="0123456789abcdef"),
             tool("fe", "web_fetch", url="https://example.com"), tool("ws", "web_search", query="x"), tool("t", "list_tasks"),
             tool("sk", "list_skills"), tool("sr", "get_subagent_result"), hook("h")]
    assert layout(folds) == ["[Ran 2 commands, read 1 file, listed 2 folders and 10 more: c1 c2 r o l v f j w g fe ws t sk sr h]"]
    assert layout([read("r1"), read("r2", path="b.py"), hook("h1"), hook("h2")]) == ["[Read 2 files, ran 2 hooks: r1 r2 h1 h2]"]
    # One counted card and quiet hooks: no group.
    assert layout([hook("h1"), command("c"), hook("h2")]) == ["h1", "c", "h2"]
    # A card that must stay visible ends the group around it, whatever it is.
    for card in NEVER:
        ids = layout([command("a"), command("b"), card, command("c"), command("d")])
        assert ids == ["[Ran 2 commands: a b]", card["id"], "[Ran 2 commands: c d]"], (card["id"], ids)
    # Off: every event as it is.
    assert page.evaluate("""async () => {
      const { layoutTranscript } = await import('/static/js/transcriptGroups.js')
      return layoutTranscript([{ id: 'a', type: 'tool', name: 'list_jobs', state: 'completed' }], new Map(), { group: false })
    }""") == [{"kind": "event", "event": {"id": "a", "type": "tool", "name": "list_jobs", "state": "completed"}}]


def test_never_folding_cards_show_on_the_page_between_groups(ui, page):
    events = []
    for card in NEVER:
        events += [command(f"{card['id']}-1"), command(f"{card['id']}-2"), card]
    show(ui, events)
    expect(page.locator(".tool-group")).to_have_count(len(NEVER))
    expect(page.locator(".tool-card")).to_have_count(len(NEVER))
    assert page.locator(".tool-card").evaluate_all("els => els.map(el => el.dataset.eventId)") == [card["id"] for card in NEVER]


def test_the_open_state_outlasts_fresh_events_and_load_earlier_and_resets_in_another_chat(ui, page):
    events = [command("a"), command("b"), command("c")]
    show(ui, [user("u"), *events], events_start=2)
    head(page, "Ran 3 commands").click()
    expect(page.locator(".tool-card")).to_have_count(3)
    # A reconnect's snapshot: the same events as new objects.
    rerender(ui, json.loads(json.dumps(events)), events_start=2)
    expect(page.locator(".tool-card")).to_have_count(3)
    # Load earlier adds two commands before them: one group, its head now keyed by the earlier card, still open.
    rerender(ui, [command("y"), command("z"), *events], events_start=0)
    expect(head(page, "Ran 5 commands")).to_have_attribute("aria-expanded", "true")
    expect(page.locator(".tool-card")).to_have_count(5)
    # Another chat starts with every group closed.
    ui.rerender(f"{{ session: makeSession({json.dumps([command('y'), command('z'), *events])}, {{ id: 'other' }}), onError: fake.spy('onError') }}")
    expect(head(page, "Ran 5 commands")).to_have_attribute("aria-expanded", "false")
    expect(page.locator(".tool-card")).to_have_count(0)


def test_with_the_setting_off_the_cards_render_as_before(ui, page):
    events = [user("u"), command("a"), quiet("q1"), command("b"), read("r"), reply("t")]
    show(ui, events, setup=OFF)
    expect(page.locator(".tool-group")).to_have_count(0)
    expect(page.locator(".tool-card")).to_have_count(3)
    expect(page.locator(".grouped")).to_have_count(0)
    assert order(page) == ["user-message", "a", "assistant-reply", "b", "r", "assistant-reply"]


def test_the_appearance_setting_folds_and_unfolds_the_open_chat_at_once(ui, page):
    events = [user("u", "Check it"), command("a"), command("b")]
    ui.app(session="a", scenario="workspace", setup=f"fake.sessions.set('a', {{ ...makeSession('a', 'Checks'), events: {json.dumps(events)} }})")
    ui.show_session("a")
    expect(page.locator(".transcript .tool-group")).to_have_count(1)
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.get_by_role("dialog")
    box = dialog.get_by_role("group", name="Appearance").get_by_role("checkbox", name="Group tool calls in the transcript")
    expect(box).to_be_checked()
    box.uncheck()
    expect(page.locator(".transcript .tool-group")).to_have_count(0)
    expect(page.locator(".transcript .tool-card")).to_have_count(2)
    assert ui.js("localStorage.getItem('group-tool-calls')") == "off"
    box.check()
    expect(page.locator(".transcript .tool-group")).to_have_count(1)
    assert ui.js("localStorage.getItem('group-tool-calls')") is None
    dialog.get_by_role("button", name="Cancel").click()
    assert ui.bodies("/api/settings", "PUT") == []  # Kept in this browser, never saved with Settings.


def test_twenty_thousand_events_lay_out_in_under_a_second_and_closed_groups_leave_no_cards(ui, page):
    ui.start(setup=SETUP)
    seconds = page.evaluate("""async () => {
      const { layoutTranscript } = await import('/static/js/transcriptGroups.js')
      const { toolStatesFor } = await import('/static/js/components/Conversation.js')
      const page = JSON.stringify({ job_id: 'j', state: 'completed', exit_code: 0, output: 'ok', background: false })
      const events = Array.from({ length: 20000 }, (_, n) => n % 50 === 0 ? { id: `u${n}`, type: 'user', text: 'next' }
        : n % 3 === 0 ? { id: `q${n}`, type: 'assistant', text: '' }
        : { id: `c${n}`, type: 'tool', name: 'run_command', state: 'completed', input: { command: 'ls' }, output: page })
      const start = performance.now()
      const items = layoutTranscript(events, toolStatesFor(events))
      if (items.filter(item => item.kind === 'group').length !== 400) throw new Error('unexpected groups')
      return (performance.now() - start) / 1000
    }""")
    assert seconds < 1, seconds
    events = []
    for n in range(100):
        events += [user(f"u{n}"), *(command(f"c{n}-{k}") for k in range(29))]
    events.append({**command("p", "rm -rf build"), "state": "pending", "output": ""})
    ui.mount("components/Conversation.js", props(events), setup=SETUP)
    expect(page.locator(".tool-group")).to_have_count(100)
    expect(page.locator(".tool-card")).to_have_count(1)


def test_the_activity_list_still_lists_every_action(ui, page):
    show(ui, [command("a"), command("b"), read("r"), user("u"), command("c")])
    page.get_by_text("Activity · 4 actions").click()
    expect(page.locator(".activity-summary li")).to_have_count(4)


def test_a_background_commands_card_shows_how_it_ended_once_its_notice_came(ui, page):
    started = {**command("bg", "serve", job_page("running", None, "", background=True)), "job_id": "job-1",
               "input": {"command": "serve", "background": True}}
    show(ui, [started])
    status = page.locator(".tool-card .tool-status")
    expect(status).to_have_text("started in background")
    done = {"id": "n", "type": "notice", "text": "Background job completed (exit 0): serve", "job_id": "job-1",
            "job_state": "completed", "exit_code": 0}
    rerender(ui, [started, done])
    expect(status).to_have_text("background · exit 0")
    expect(status).to_have_class("tool-status neutral")
    expect(page.locator(".tool-card .success-icon")).to_be_visible()
    failed = {**done, "text": "Background job failed (exit 1): serve", "job_state": "failed", "exit_code": 1}
    rerender(ui, [started, failed])
    expect(status).to_have_text("background · exit 1")
    expect(status).not_to_have_class(re.compile("neutral"))
    expect(page.locator(".tool-card .warning-icon")).to_be_visible()
    expect(page.locator(".tool-card .tool-outcome")).to_have_text("Background job failed (exit 1): serve")
    # Older notices say only that it ended; a timed-out one warns.
    rerender(ui, [started, {"id": "n", "type": "notice", "text": "Background job timed out: serve", "job_id": "job-1"}])
    expect(status).to_have_text("background · ended")
    rerender(ui, [started, {**done, "text": "Background job timed out: serve", "job_state": "timed_out", "exit_code": None}])
    expect(status).to_have_text("background · timed out")
    # It still never folds, also next to other commands.
    rerender(ui, [command("a"), started, command("b"), done])
    expect(page.locator(".tool-group")).to_have_count(0)
    page.get_by_role("button", name="run command serve background · exit 0", exact=True).click()
    expect(page.locator("#tool-bg").get_by_text("Action: completed · background · exit 0")).to_be_visible()
    # The Activity list reads the same outcome.
    page.get_by_text("Activity · 3 actions").click()
    expect(page.locator(".activity-summary li").nth(1)).to_contain_text("Action: completed · background · exit 0")
