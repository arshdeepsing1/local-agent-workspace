"""Workspace → Progress: the chat's context (as the meter reads it), its task list, and its background tasks
(subagents from the chats' summaries and the loaded cards, commands from GET /api/jobs) with their actions; the
tab bar that remembers its tab and fits its labels; and Show in panel."""
import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

FRONTEND = Path(__file__).resolve().parents[1]
CONTEXT_INFO = {"estimated_tokens": 60380, "input_budget": 120832, "context_window": 131072, "reply_reserve": 8192,
                "compactions": 1, "summarized_messages": 8, "estimate_method": "weighted_utf8", "auto_compact_percent": 90,
                "instruction_files": ["AGENTS.md", "src/AGENTS.md"], "warnings": ["Project instructions were truncated."]}
TURN_RESULT = {"steps": 3, "tool_calls": 5, "duration_s": 70, "usage": {"input_tokens": 1000, "output_tokens": 200},
               "terminal_reason": "completed"}
CHAT = [{"id": "u1", "type": "user", "text": "Run the checks", "created": 1},
        {"id": "r1", "type": "assistant", "text": "Started them.", "created": 2}]
# Answers for a subagent's Stop and Allow (they post to the child chat).
POSTS = """fake.override = (path, options) => options.method === 'POST' && /^\\/api\\/sessions\\/[^/]+\\/(stop|approvals\\/[^/]+)$/.test(path)
  ? fake.json({ ok: true }) : undefined
"""


def chat(events=CHAT, **fields):
    """Chat 'a' with these events and header fields, and the subagent chats' answers."""
    extra = "".join(f", {key}: {json.dumps(value)}" for key, value in fields.items())
    return f"fake.sessions.set('a', {{ ...makeSession('a', 'Checks'), events: {json.dumps(events)}{extra} }})\n" + POSTS


def subagent(child_id, title, parent_event="d1", **progress):
    """A subagent chat of 'a' with its delegation progress (its summary in GET /api/sessions)."""
    delegation = {"status": "running", "completed_tools": 3, "agent_type": "explore", "background": True,
                  "started": 1000, **progress}
    return (f"fake.sessions.set({json.dumps(child_id)}, {{ ...makeSession({json.dumps(child_id)}, {json.dumps(title)}), "
            f"parent_session_id: 'a', parent_event_id: {json.dumps(parent_event)}, is_subagent: true, "
            f"delegation: {json.dumps(delegation)} }})\n")


def card(event_id, child_id, task, **progress):
    delegation = {"status": "running", "completed_tools": 1, "agent_type": "general", "background": True, "started": 1000, **progress}
    return {"id": event_id, "type": "tool", "name": "delegate_task", "state": "completed", "created": 3,
            "input": {"task": task, "background": True}, "child_session_id": child_id, "delegation": delegation}


def job(job_id, command, **fields):
    return f"workspaceJob({json.dumps(job_id)}, {{ command: {json.dumps(command)}, ...{json.dumps(fields)} }})"


def start(ui, page, setup="", session="a", width=1280, height=900, clock=False):
    page.set_viewport_size({"width": width, "height": height})
    ui.app(session=session, scenario="workspace", setup=setup, clock=clock)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    if session:
        ui.show_session(session)


def panel(page):
    return page.get_by_role("complementary", name="Workspace panel")


def open_progress(page):
    page.get_by_role("button", name=re.compile(r"^Workspace( \(|$)")).click()
    panel(page).get_by_role("button", name="Progress", exact=True).click()
    expect(panel(page).get_by_role("button", name="Progress", exact=True)).to_have_attribute("aria-pressed", "true")
    return panel(page)


def section(page, name):
    return panel(page).get_by_role("region", name=name, exact=True)


def background(page):
    return panel(page).get_by_role("list", name="Background tasks").get_by_role("listitem")


def jobs_requests(ui):
    return ui.js("fake.requests(/^\\/api\\/jobs\\?/).length")


# Context

def test_the_context_section_reads_as_the_meter_does_and_follows_each_context_push(ui, page):
    start(ui, page, chat(context_info=CONTEXT_INFO, last_turn_result=TURN_RESULT))
    open_progress(page)
    context = section(page, "Context")
    meter_line = page.locator(".chat-composer .context-meter > summary > span").first
    expect(meter_line).to_have_text("Last model input · ~50%")
    expect(context.locator(".progress-context-line")).to_have_text("Last model input · ~50%")
    # Its own name: the composer's bar is "Estimated model input usage".
    bar = context.get_by_role("progressbar", name="Context used")
    expect(bar).to_have_attribute("aria-valuetext", "Approximately 50% of input budget")
    expect(context).to_contain_text("Compacts automatically at 90% of the input budget.")
    expect(context).to_contain_text("1 compaction · 1 warning")
    expect(context).to_contain_text("Last turn: 3 steps · 5 tools · 1 min 10 s · 1.0k in / 200 out")
    expect(context.get_by_role("listitem")).to_have_text(["AGENTS.md", "src/AGENTS.md"])
    # A context push moves both views at once (meterReading is shared).
    ui.emit("a", {"type": "context", "context_info": {**CONTEXT_INFO, "estimated_tokens": 108748, "compactions": 2}})
    expect(context.locator(".progress-context-line")).to_have_text("Last model input · ~90%")
    expect(meter_line).to_have_text("Last model input · ~90%")
    expect(bar).to_have_attribute("aria-valuetext", "Approximately 90% of input budget")
    expect(context).to_contain_text("2 compactions · 1 warning")
    expect(context.locator(".context-bar.over")).to_have_count(1)


def test_before_the_first_turn_and_without_a_chat_the_panel_says_so(ui, page):
    start(ui, page, chat(events=[]))
    open_progress(page)
    expect(section(page, "Context")).to_contain_text("Context usage is estimated after the first turn.")
    expect(section(page, "Context").get_by_role("button", name="Context details")).to_have_count(0)
    page.get_by_role("button", name="New conversation").click()
    expect(panel(page).locator(".progress-panel")).to_have_text("Open a chat to see its progress.")


@pytest.mark.parametrize("width,height", [(1280, 900), (820, 1180), (390, 844)], ids=["docked", "drawer", "phone"])
def test_context_details_opens_the_meters_details_and_a_drawer_closes_first(ui, page, width, height):
    start(ui, page, chat(context_info=CONTEXT_INFO), width=width, height=height)
    open_progress(page)
    section(page, "Context").get_by_role("button", name="Context details").click()
    expect(page.locator(".chat-composer .context-details")).to_be_visible()
    expect(page.locator(".chat-composer .context-meter > summary")).to_be_focused()
    if width < 1024:
        expect(panel(page)).to_have_count(0)
        assert ui.js("document.querySelector('.main-area').inert") is False
    else:
        expect(panel(page)).to_be_visible()


# Tasks

TASKS = [{"id": "t1", "title": "Read the failing test", "status": "completed"},
         {"id": "t2", "title": "Fix the parser", "status": "in_progress", "description": "Handle empty lines."},
         {"id": "t3", "title": "Run the suite", "status": "pending"},
         {"id": "t4", "title": "Rewrite the lexer", "status": "cancelled"}]


def test_the_tasks_section_lists_the_chats_checklist_read_only_with_its_own_empty_text(ui, page):
    start(ui, page, chat())
    open_progress(page)
    tasks = section(page, "Task list")
    expect(tasks.get_by_role("heading", name="Tasks")).to_be_visible()
    expect(tasks).to_contain_text("No tasks in this chat. When the agent plans its work as tasks, its checklist shows here")
    ui.emit("a", {"type": "tasks", "tasks": TASKS})
    # The checklist above the message box is the page's one region named "Tasks"; this one has a name of its own.
    expect(page.get_by_role("region", name="Tasks", exact=True)).to_have_count(1)
    expect(page.locator(".chat-composer").get_by_role("region", name="Tasks", exact=True)).to_have_count(1)
    # The same counts and rows as the checklist above the message box (cancelled tasks are listed, not counted).
    expect(tasks.locator(".progress-note")).to_have_text("1/3 done · now: Fix the parser")
    rows = tasks.get_by_role("listitem")
    expect(rows).to_have_text(["Done: Read the failing test", "In progress: Fix the parser", "Pending: Run the suite",
                               "Cancelled: Rewrite the lexer"])
    expect(rows.nth(1).locator(".task-checklist-title")).to_have_attribute("title", "Handle empty lines.")
    expect(page.get_by_role("button", name=re.compile(r"^Tasks 1\/3 done"))).to_be_visible()
    page.get_by_role("button", name=re.compile(r"^Tasks 1\/3 done")).click()
    composer = page.locator(".chat-composer .task-checklist-list li")
    assert composer.all_inner_texts() == rows.all_inner_texts()
    # Read-only: nothing in it to press or tick.
    expect(tasks.locator("button, input, select, a")).to_have_count(0)
    ui.emit("a", {"type": "tasks", "tasks": []})
    expect(tasks).to_contain_text("No tasks in this chat.")


# Background tasks: commands

def test_a_background_command_is_stopped_or_shown_in_terminal_and_names_who_started_it(ui, page):
    events = CHAT + [{"id": "c1", "type": "tool", "name": "run_command", "state": "completed", "created": 3,
                      "input": {"command": "pytest -x", "background": True}, "job_id": "old"}]
    jobs = ", ".join([
        job("dev", "npm run dev", created=200, updated=200),
        job("tail", "tail -f server.log", origin="user", background=False, created=300, updated=300),
        job("build", "make build", state="completed", exit_code=0, created=100, updated=150),
        job("old", "pytest -x", state="failed", exit_code=1, origin=None, created=50, updated=160),
        job("mine", "ls", state="completed", exit_code=0, origin="user", created=10, updated=20),
        job("fore", "git status", state="completed", exit_code=0, background=False, created=10, updated=20),
    ])
    start(ui, page, chat(events) + f"fake.jobs = [{jobs}]")
    open_progress(page)
    rows = background(page)
    # Running first (oldest first), then what ended, newest first. Ended commands of yours and foreground ones are
    # Terminal's; a job without an origin is the agent's when one of its command cards has it.
    expect(rows).to_have_count(4)
    expect(rows.locator(".progress-item-title")).to_have_text(["npm run dev", "tail -f server.log", "pytest -x", "make build"])
    expect(rows.locator(".progress-chip")).to_have_text(["Agent", "You", "Agent", "Agent"])
    expect(rows.nth(0).locator(".progress-status")).to_have_text("running · background")
    expect(rows.nth(2).locator(".progress-status")).to_have_text("failed · exit 1 · background")
    expect(rows.nth(3)).to_contain_text("50 s")
    expect(rows.nth(2).get_by_role("button", name=re.compile("^Stop job"))).to_have_count(0)
    rows.nth(0).get_by_role("button", name="Stop job npm run dev").click()
    page.wait_for_function("fake.requests('/api/jobs/dev/stop?session_id=a', 'POST').length === 1")
    expect(rows.filter(has_text="npm run dev").locator(".progress-status")).to_have_text("cancelled · exit -15 · background")
    rows.filter(has_text="tail -f server.log").get_by_role("button", name="View output of tail -f server.log").click()
    terminal = panel(page).get_by_role("button", name="Terminal", exact=True)
    expect(terminal).to_have_attribute("aria-pressed", "true")
    selected = panel(page).get_by_role("button", name=re.compile("^Show job tail -f server.log"))
    expect(selected).to_have_attribute("aria-pressed", "true")
    expect(selected).to_be_focused()


def test_no_jobs_are_fetched_while_another_tab_is_shown_and_running_jobs_are_polled_each_second(ui, page):
    start(ui, page, chat() + f"fake.jobs = [{job('dev', 'npm run dev')}]", clock=True)
    page.get_by_role("button", name=re.compile(r"^Workspace( \(|$)")).click()
    expect(panel(page).get_by_role("button", name="Files", exact=True)).to_have_attribute("aria-pressed", "true")
    ui.tick(10000)
    assert jobs_requests(ui) == 0
    panel(page).get_by_role("button", name="Progress", exact=True).click()
    expect(background(page)).to_have_count(1)
    first = jobs_requests(ui)
    ui.tick(1000)
    ui.wait(f"fake.requests(/^\\/api\\/jobs\\?/).length > {first}")
    # The job ended: polling slows to every 3 s.
    ui.run("fake.jobs[0].state = 'completed'; fake.jobs[0].exit_code = 0; fake.jobs[0].updated = 160")
    ui.tick(1000)
    expect(background(page).locator(".progress-status")).to_have_text("completed · exit 0 · background")
    settled = jobs_requests(ui)
    ui.tick(2500)
    assert jobs_requests(ui) == settled
    ui.tick(600)
    ui.wait(f"fake.requests(/^\\/api\\/jobs\\?/).length === {settled + 1}")
    panel(page).get_by_role("button", name="Files", exact=True).click()
    hidden = jobs_requests(ui)
    ui.tick(10000)
    assert jobs_requests(ui) == hidden


def test_a_jobs_error_backs_off_and_recovers(ui, page):
    start(ui, page, chat() + """window.jobsDown = true
      const posts = fake.override
      fake.override = (path, options) => window.jobsDown && path.startsWith('/api/jobs?')
        ? fake.json({ detail: 'The job list is unavailable.' }, 500) : posts(path, options)
      fake.jobs = [workspaceJob('dev', { command: 'npm run dev' })]""", clock=True)
    open_progress(page)
    alert = section(page, "Background tasks").get_by_role("alert")
    expect(alert).to_contain_text("Could not load this chat's commands: The job list is unavailable.")
    expect(section(page, "Background tasks")).not_to_contain_text("Nothing runs in the background")
    assert jobs_requests(ui) == 1
    # It waits 3 s, then 6 s, then 12 s between tries.
    for wait, count in [(3000, 2), (6000, 3), (12000, 4)]:
        ui.tick(wait - 200)
        assert jobs_requests(ui) == count - 1
        ui.tick(200)
        ui.wait(f"fake.requests(/^\\/api\\/jobs\\?/).length === {count}")
    # Retry tries at once; once the list loads, the error goes and the rows show.
    ui.run("window.jobsDown = false")
    alert.get_by_role("button", name="Retry").click()
    expect(background(page)).to_have_count(1)
    expect(alert).to_have_count(0)


# Background tasks: subagents

def test_subagents_whose_cards_are_not_loaded_are_listed_from_their_chats_and_answered_from_the_panel(ui, page):
    pending = {"event_id": "p1", "name": "write_file", "kind": "approval", "title": "Write src/app.py"}
    setup = (chat(events_start=12, events_total=14)
             + subagent("k1", "Check the tests", steps=4, max_steps=10, last_tool="run_command", last_tool_state="running",
                        last_tool_subject="npm test")
             + subagent("k2", "Update the docs", parent_event="d2", agent_type="editor", status="awaiting_approval", pending=pending)
             + subagent("k3", "Old survey", parent_event="d3", status="completed", terminal_reason="completed", ended=1300)
             + subagent("k4", "Foreground helper", parent_event="d4", background=False, status="completed",
                        terminal_reason="completed", ended=1400)
             + subagent("k5", "Look up the API", parent_event="d5", background=False, started=1060)
             + subagent("q1", "Not a child", parent_event="x").replace("parent_session_id: 'a'", "parent_session_id: 'z'")
             + "Date.now = () => 1065000")
    start(ui, page, setup)
    open_progress(page)
    rows = background(page)
    # Waiting first, then running (oldest first), then ended background ones; an ended foreground subagent is its
    # turn's card, a running one is marked as part of the turn.
    expect(rows).to_have_count(4)
    expect(rows.locator(".progress-item-title")).to_have_text(["Update the docs", "Check the tests", "Look up the API", "Old survey"])
    expect(rows.nth(2).locator(".progress-chip")).to_have_text(["Explore", "In this turn"])
    expect(rows.nth(0).locator(".progress-status")).to_have_text("Waiting for approval")
    expect(rows.nth(0).locator(".subagent-pending")).to_have_text("Waits for approval: write file · Write src/app.py")
    running = rows.nth(1)
    expect(running.locator(".progress-chip")).to_have_text(["Explore"])
    expect(running).to_contain_text("3 actions")
    expect(running).to_contain_text("Step 4 of 10")
    expect(running.locator(".progress-tool")).to_have_text("Now: run command · npm test")
    expect(running.locator(".progress-elapsed")).to_have_text("1 min 05 s")
    expect(rows.nth(3)).to_contain_text("Reason: completed")
    # Their cards are on a page that is not loaded: no Show card, but the child chat answers.
    expect(panel(page).get_by_role("button", name="Show card")).to_have_count(0)
    rows.nth(0).get_by_role("button", name="Allow").click()
    page.wait_for_function("fake.requests('/api/sessions/k2/approvals/p1', 'POST').length === 1")
    assert ui.bodies("/api/sessions/k2/approvals/p1", "POST") == [{"allowed": True}]
    running.get_by_role("button", name="Stop subagent").click()
    page.wait_for_function("fake.requests('/api/sessions/k1/stop', 'POST').length === 1")
    expect(rows.nth(3).get_by_role("button", name="Stop subagent")).to_have_count(0)
    running.get_by_role("button", name="Open subagent").click()
    ui.wait("location.search === '?session=k1'")
    ui.show_session("k1")
    # The panel opens again on Progress, now for the subagent's own chat.
    expect(panel(page).get_by_role("button", name="Progress", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(section(page, "Background tasks")).to_contain_text("Nothing runs in the background in this chat.")


def test_a_waiting_subagents_answer_buttons_read_like_its_other_actions_and_only_stop_is_red(ui, page):
    pending = {"event_id": "p1", "name": "write_file", "kind": "approval", "title": "Write src/app.py"}
    start(ui, page, chat() + subagent("k2", "Update the docs", status="awaiting_approval", pending=pending))
    open_progress(page)
    row = background(page).first
    def color(name):
        return row.get_by_role("button", name=name, exact=True).evaluate("e => getComputedStyle(e).color")
    open_color = color("Open subagent")
    assert [color("Allow"), color("Decline")] == [open_color, open_color]
    assert color("Stop subagent") != open_color


def test_a_loaded_card_updates_its_row_live_and_a_queued_one_is_listed_without_actions(ui, page):
    events = CHAT + [card("d1", "k1", "Survey the parser"),
                     {**card("d2", None, "Wait for a slot", status="queued"), "child_session_id": None}]
    start(ui, page, chat(events) + subagent("k1", "Survey the parser (chat title)", agent_type="general"))
    open_progress(page)
    rows = background(page)
    expect(rows.locator(".progress-item-title")).to_have_text(["Survey the parser", "Wait for a slot"])
    expect(rows.nth(1).locator(".progress-status")).to_have_text("waiting for a free slot")
    expect(rows.nth(1).get_by_role("button")).to_have_text(["Show card"])
    # The card's progress is pushed live, ahead of the chat summary.
    ui.emit("a", {"type": "event", "event": card("d1", "k1", "Survey the parser", completed_tools=7, steps=8, max_steps=12,
                                                 last_tool="read_file", last_tool_state="completed", last_tool_subject="src/parser.py")})
    expect(rows.nth(0)).to_contain_text("7 actions")
    expect(rows.nth(0)).to_contain_text("Step 8 of 12")
    expect(rows.nth(0).locator(".progress-tool")).to_have_text("Last tool: read file · src/parser.py")
    expect(rows.nth(0).get_by_role("button", name="Show card")).to_be_visible()


@pytest.mark.parametrize("width,height", [(1280, 700), (820, 1180)], ids=["docked", "drawer"])
def test_show_card_scrolls_the_transcript_to_the_card_and_focuses_its_heading(ui, page, width, height):
    filler = [{"id": f"f{n}", "type": "assistant", "text": f"Paragraph {n}. " + "Words to fill the transcript. " * 30,
               "created": 10 + n} for n in range(12)]
    events = CHAT[:1] + [card("d1", "k1", "Survey the parser")] + filler
    start(ui, page, chat(events) + subagent("k1", "Survey the parser"), width=width, height=height)
    heading = page.locator("#tool-d1 .tool-heading")
    expect(heading).not_to_be_in_viewport()
    open_progress(page)
    background(page).get_by_role("button", name="Show card").click()
    expect(heading).to_be_focused()
    expect(heading).to_be_in_viewport()
    if width < 1024:
        expect(panel(page)).to_have_count(0)


def test_the_empty_state_points_to_schedules(ui, page):
    start(ui, page, chat())
    open_progress(page)
    content = section(page, "Background tasks")
    expect(content).to_contain_text("Nothing runs in the background in this chat. Background subagents and background "
                                    "commands appear here.")
    expect(content).to_contain_text("Scheduled runs are not listed here: each one is a chat of its own (Agent tools → Schedules).")


# The tab bar

def test_the_panel_remembers_its_tab_across_chats_and_reloads(ui, page):
    start(ui, page, chat() + "fake.sessions.set('b', makeSession('b', 'Second chat'))")
    open_progress(page)
    tabs = panel(page).get_by_role("navigation", name="Workspace views").get_by_role("button")
    expect(tabs).to_have_text(["Files", "Changes", "Terminal", "Progress"])
    assert [tab.get_attribute("aria-pressed") for tab in tabs.all()] == ["false", "false", "false", "true"]
    page.locator(".conversation-row > button", has_text="Second chat").click()
    ui.show_session("b")
    expect(panel(page).get_by_role("button", name="Progress", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(section(page, "Context")).to_contain_text("Context usage is estimated after the first turn.")
    panel(page).get_by_role("button", name="Terminal", exact=True).click()
    assert ui.js("localStorage.getItem('workspace-panel-tab')") == "terminal"
    page.reload()
    page.wait_for_function("window.harnessReady === true")
    page.evaluate("() => { mount('App.js', () => ({}), 'default') }")
    ui.show_session("b")
    page.get_by_role("button", name=re.compile(r"^Workspace( \(|$)")).click()
    expect(panel(page).get_by_role("button", name="Terminal", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(panel(page).get_by_role("textbox", name="Shell command")).to_be_visible()


@pytest.mark.parametrize("last", ["Progress", "Terminal"])
def test_add_project_file_opens_the_panel_on_files_whatever_tab_was_used_last(ui, page, last):
    # The message box's + is for picking a project file, so it opens Files, not the remembered tab.
    start(ui, page, chat())
    page.get_by_role("button", name=re.compile(r"^Workspace( \(|$)")).click()
    panel(page).get_by_role("button", name=last, exact=True).click()
    expect(panel(page).get_by_role("button", name=last, exact=True)).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="Close workspace").click()
    expect(panel(page)).to_have_count(0)
    page.get_by_role("button", name="Add project file").click()
    files = panel(page).get_by_role("button", name="Files", exact=True)
    expect(files).to_have_attribute("aria-pressed", "true")
    expect(panel(page).get_by_role("button", name="note.txt")).to_be_visible()
    # Also while the docked panel is open on another tab.
    panel(page).get_by_role("button", name=last, exact=True).click()
    expect(files).to_have_attribute("aria-pressed", "false")
    page.get_by_role("button", name="Add project file").click()
    expect(files).to_have_attribute("aria-pressed", "true")
    expect(panel(page).get_by_role("button", name="note.txt")).to_be_visible()


def test_a_one_shot_tab_request_shows_that_tab_once(ui, page):
    ui.mount("components/WorkspacePanel.js", """{ sessionId: 'a', workspace: '/project', file: null, setFile: () => {},
      session: null, showTab: 'progress', onTabShown: fake.spy('shown') }""", scenario="workspace")
    expect(page.get_by_role("button", name="Progress", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".progress-panel")).to_have_text("Open a chat to see its progress.")
    assert ui.spy("shown") == [[]]
    page.get_by_role("button", name="Files", exact=True).click()
    ui.rerender("{ sessionId: 'a', workspace: '/project', file: null, setFile: () => {}, session: null, showTab: null }")
    expect(page.get_by_role("button", name="Files", exact=True)).to_have_attribute("aria-pressed", "true")


@pytest.mark.skipif(not (FRONTEND / "static" / "js" / "transcriptGroups.js").exists(),
                    reason="the run head and its Show in panel come with the transcript package")
def test_show_in_panel_on_a_runs_head_opens_the_progress_tab(ui, page):
    batch = [{**card(f"d{n}", f"k{n}", f"Task {n}"), "state": "running",
              "delegation_batch": {"id": "b1", "index": n, "size": 2}} for n in range(2)]
    start(ui, page, chat(CHAT + batch, status="delegating") + subagent("k0", "Task 0", parent_event="d0")
          + subagent("k1", "Task 1", parent_event="d1"))
    page.get_by_role("button", name="Show in panel").click()
    expect(panel(page).get_by_role("button", name="Progress", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(background(page)).to_have_count(2)


@pytest.mark.skipif(not (FRONTEND / "static" / "js" / "transcriptGroups.js").exists(),
                    reason="folded runs come with the transcript package")
@pytest.mark.parametrize("width,height", [(1280, 900), (820, 1180)], ids=["docked", "drawer"])
def test_show_card_on_a_subagent_whose_run_is_folded_opens_the_run_and_focuses_its_card(ui, page, width, height):
    # A run that had ended when the chat opened starts folded, so its cards are not rendered: Show card opens the
    # run (the closed head whose aria-controls names the card), then shows the card with focus on its heading.
    def ended(n):
        return {"status": "completed", "terminal_reason": "completed", "started": 1000, "ended": 1100 + n}
    batch = [{**card(f"d{n}", f"k{n}", f"Task {n}", **ended(n)), "delegation_batch": {"id": "b1", "index": n, "size": 2}}
             for n in range(2)]
    start(ui, page, chat(CHAT + batch) + subagent("k0", "Task 0", parent_event="d0", **ended(0))
          + subagent("k1", "Task 1", parent_event="d1", **ended(1)), width=width, height=height)
    head = page.locator(".transcript [aria-expanded][aria-controls~='tool-d1']")
    expect(head).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#tool-d1")).to_have_count(0)
    open_progress(page)
    rows = background(page)
    expect(rows.locator(".progress-item-title")).to_have_text(["Task 1", "Task 0"])
    rows.nth(0).get_by_role("button", name="Show card").click()
    expect(page.locator("#tool-d1 .tool-heading")).to_be_focused()
    expect(page.locator("#tool-d1 .tool-heading")).to_be_in_viewport()
    expect(head).to_have_attribute("aria-expanded", "true")
    if width < 1024:
        expect(panel(page)).to_have_count(0)
