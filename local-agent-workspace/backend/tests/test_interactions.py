"""ask_user questions and submit_plan reviews: tool calls that wait for the user inside a turn."""
import asyncio
import json

import httpx
import pytest

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.feature_tools import FEATURE_TOOLS
from local_agent.instructions import load_project_instructions
from local_agent.interactions import (DONT_ASK, PLAN_EXPIRED, QUESTIONS_EXPIRED, SKIPPED, validate_questions)
from local_agent.portability import export_bundle
from local_agent.tool_schema import validate_arguments
from local_agent.tools import WorkspaceTools
from test_approval_wait import WAIT_FOR, bounded, expire_waits_of

QUESTIONS = [
    {"question": "Which database?", "header": "Storage",
     "options": [{"label": "SQLite", "description": "One local file"}, {"label": "Postgres"}]},
    {"question": "Which checks should run?", "options": [{"label": "Lint"}, {"label": "Tests"}, {"label": "Types"}],
     "multi_select": True},
]
REAL_CLIENT = httpx.AsyncClient  # The API client; scripted() replaces httpx.AsyncClient for model requests.
PLAN = {"plan": "# Plan\n\n1. Add `notes.txt`.\n2. Check it.", "title": "Add notes"}


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", model="test-model")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    settings.redact = lambda text: text.replace("private-token", "[REDACTED]")
    app = create_app(settings)
    manager = app.state.manager
    session = manager.store.create(settings.values)
    session.update(title="Questions", title_generated=True)
    manager.store.save(session)
    manager.live[session["id"]] = session
    tools = WorkspaceTools(str(project))
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    yield app, manager, session, project, tools
    for session_id in list(manager.tasks):
        await manager.stop(session_id)
    await manager.jobs.shutdown()
    manager.store.db.close()


def client_for(app):
    return REAL_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def connect(client):
    client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
    return client


async def waiting(manager, kind=None, other_than=None):
    """The (session_id, event_id) of the first card waiting for the user."""
    for _ in range(500):
        for key, entry in list(manager.interactions.waiting.items()):
            if (kind is None or entry["kind"] == kind) and key[0] != other_than:
                return key
        await asyncio.sleep(0.01)
    pytest.fail("No question or plan is waiting for the user")


def call(manager, session, tools, name, arguments, call_id="call"):
    return asyncio.create_task(manager.execute_tool(session, tools, name, arguments, call_id))


def tool_event(session, call_id="call"):
    return next(event for event in session["events"] if event.get("call_id") == call_id)


def record_broadcasts(manager, monkeypatch):
    sent = []
    original = manager.broadcast

    async def broadcast(session_id, data):
        sent.append((session_id, data))
        await original(session_id, data)
    monkeypatch.setattr(manager, "broadcast", broadcast)
    return sent


# The gateway: streamed chat completions, scripted per conversation.

def frame(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


def reply(text):
    return httpx.Response(200, text=frame({"content": text}, "stop") + "data: [DONE]\n\n")


def tool_reply(name, arguments, call_id):
    part = {"index": 0, "id": call_id, "function": {"name": name, "arguments": json.dumps(arguments)}}
    return httpx.Response(200, text=frame({"content": "", "tool_calls": [part]}, "tool_calls") + "data: [DONE]\n\n")


def scripted(monkeypatch, parent, child=()):
    """Answer the parent's and a delegated child's streamed requests in order; titles get a fixed reply."""
    requests = {"parent": [], "child": []}

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "A title"}, "finish_reason": "stop"}]})
        delegated = any(str(message.get("content", "")).startswith("Delegated task:") for message in body["messages"])
        script, seen = (child, requests["child"]) if delegated else (parent, requests["parent"])
        seen.append(body)
        return script[min(len(seen), len(script)) - 1]()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    return requests


# The tool definitions and argument checks

def ask_user_schema():
    return next(item["function"]["parameters"] for item in FEATURE_TOOLS if item["function"]["name"] == "ask_user")


@pytest.mark.parametrize("questions", [
    [],
    [QUESTIONS[0]] * 5,
    [{"question": "Which?", "options": [{"label": "Only one"}]}],
    [{"question": "Which?", "options": [{"label": str(n)} for n in range(5)]}],
    [{"question": "Which?", "options": [{"label": "A"}, {"label": "B", "extra": True}]}],
])
def test_the_schema_bounds_questions_and_options(questions):
    with pytest.raises(ValueError):
        validate_arguments(ask_user_schema(), {"questions": questions})


@pytest.mark.parametrize("questions", [
    [{"question": "Which?", "options": [{"label": "Yes"}, {"label": " yes "}]}],
    [{"question": "  ", "options": [{"label": "A"}, {"label": "B"}]}],
    [{"question": "Which?", "options": [{"label": "A"}, {"label": " "}]}],
    [{"question": "Which?", "options": [{"label": "A"}, {"label": "B"}]},
     {"question": " which? ", "options": [{"label": "C"}, {"label": "D"}]}],
])
def test_questions_need_text_and_distinct_options(questions):
    with pytest.raises(ValueError, match="Each question needs a text and 2-4 distinct options."):
        validate_questions(questions)


async def test_duplicate_labels_are_an_error_for_the_model(runtime):
    _, manager, session, _, tools = runtime
    questions = [{"question": "Which?", "options": [{"label": "Yes"}, {"label": "YES"}]}]
    output = await bounded(manager.execute_tool(session, tools, "ask_user", {"questions": questions}, "call"))
    event = tool_event(session)
    assert event["state"] == "error" and event["output"] == "Each question needs a text and 2-4 distinct options."
    assert json.loads(output)["error_kind"] == "invalid_arguments"
    assert not manager.interactions.waiting


# ask_user

async def test_ask_user_waits_and_returns_the_answers_with_other_text_redacted(runtime):
    _, manager, session, _, tools = runtime
    run = call(manager, session, tools, "ask_user", {"questions": QUESTIONS})
    session_id, event_id = await waiting(manager, "question")
    event = tool_event(session)
    assert (session_id, event_id) == (session["id"], event["id"])
    assert event["state"] == "pending" and event["approval_expires"] > event["created"] + 3590
    assert manager.statuses[session["id"]] == "awaiting_approval"
    assert not manager.pending  # Approval routes and re-checks never see it.
    manager.interactions.answer_questions(session["id"], event_id, {"answers": [
        {"selected": ["SQLite"]}, {"selected": ["Lint", "Tests"], "other": "  also run private-token checks "}]})
    output = await bounded(run)
    expected = [{"question": "Which database?", "selected": ["SQLite"], "other": None},
                {"question": "Which checks should run?", "selected": ["Lint", "Tests"], "other": "also run [REDACTED] checks"}]
    assert json.loads(output) == {"answers": expected}
    assert (event["state"], event["question_outcome"], event["answers"]) == ("completed", "answered", expected)
    assert manager.statuses[session["id"]] == "running"
    assert not manager.interactions.waiting


async def test_other_alone_answers_a_single_select_question(runtime):
    _, manager, session, _, tools = runtime
    run = call(manager, session, tools, "ask_user", {"questions": QUESTIONS[:1]})
    _, event_id = await waiting(manager)
    manager.interactions.answer_questions(session["id"], event_id, {"answers": [{"selected": [], "other": "DuckDB"}]})
    assert json.loads(await bounded(run))["answers"] == [{"question": "Which database?", "selected": [], "other": "DuckDB"}]


@pytest.mark.parametrize("body", [
    {"answers": [{"selected": ["SQLite", "Postgres"]}, {"selected": ["Lint"]}]},  # Two labels for single-select.
    {"answers": [{"selected": ["SQLite"], "other": "Both"}, {"selected": ["Lint"]}]},  # A label and Other.
    {"answers": [{"selected": ["MySQL"]}, {"selected": ["Lint"]}]},  # Not a listed option.
    {"answers": [{"selected": ["SQLite"]}, {"selected": ["Lint", "Lint"]}]},  # The same option twice.
    {"answers": [{"selected": ["SQLite"]}]},  # A question left out.
    {"answers": [{"selected": ["SQLite"]}, {"selected": [], "other": "   "}]},  # Nothing chosen.
    {"answers": [{"selected": []}, {"selected": ["Lint"]}]},
    {"answers": [{"selected": ["SQLite"]}, {"selected": [], "other": "x" * 1001}]},  # Other over 1,000 characters.
    {"answers": [{"selected": ["SQLite"]}, {"selected": ["Lint"]}], "skip": True},
    {},
])
async def test_invalid_answers_are_refused_over_the_api(runtime, body):
    app, manager, session, _, tools = runtime
    run = call(manager, session, tools, "ask_user", {"questions": QUESTIONS})
    _, event_id = await waiting(manager)
    async with client_for(app) as client:
        await connect(client)
        route = f"/api/sessions/{session['id']}/questions/{event_id}"
        response = await client.post(route, json=body)
        assert response.status_code == 400, response.text
        for strict in ({"answers": [{"selected": "SQLite"}]}, {"skip": "yes"}, {"skip": True, "note": "x"},
                       {"answers": [{"selected": ["A", "B", "C", "D", "E"]}]}):
            assert (await client.post(route, json=strict)).status_code == 422
        assert not run.done()
        answer = {"answers": [{"selected": ["Postgres"]}, {"selected": [], "other": "x" * 1000}]}
        assert (await client.post(route, json=answer)).json() == {"ok": True}
        assert (await client.post(route, json=answer)).status_code == 400  # Already answered.
    assert json.loads(await bounded(run))["answers"][1]["other"] == "x" * 1000


async def test_a_second_answer_is_refused_before_the_tool_resumes(runtime):
    _, manager, session, _, tools = runtime
    run = call(manager, session, tools, "ask_user", {"questions": QUESTIONS[:1]})
    _, event_id = await waiting(manager)
    manager.interactions.answer_questions(session["id"], event_id, {"answers": [{"selected": ["SQLite"]}]})
    # The answer is set but the tool has not resumed yet: the card is no longer waiting.
    with pytest.raises(ValueError, match="This question is no longer waiting for an answer."):
        manager.interactions.answer_questions(session["id"], event_id, {"skip": True})
    assert json.loads(await bounded(run))["answers"][0]["selected"] == ["SQLite"]
    session["permission_mode"] = "plan"
    run = call(manager, session, tools, "submit_plan", PLAN, "plan")
    _, event_id = await waiting(manager, "plan")
    manager.interactions.answer_plan(session["id"], event_id, {"choice": "keep_planning"})
    with pytest.raises(ValueError, match="This plan is no longer waiting for review."):
        manager.interactions.answer_plan(session["id"], event_id, {"choice": "acceptEdits"})
    await bounded(run)
    assert session["permission_mode"] == "plan"


async def test_skip(runtime):
    app, manager, session, _, tools = runtime
    run = call(manager, session, tools, "ask_user", {"questions": QUESTIONS})
    _, event_id = await waiting(manager)
    async with client_for(app) as client:
        await connect(client)
        response = await client.post(f"/api/sessions/{session['id']}/questions/{event_id}", json={"skip": True})
        assert response.json() == {"ok": True}
    assert await bounded(run) == SKIPPED
    event = tool_event(session)
    assert (event["state"], event["question_outcome"]) == ("completed", "skipped") and "answers" not in event


async def test_an_unanswered_question_expires_after_the_approval_wait(runtime, monkeypatch):
    _, manager, session, _, tools = runtime
    manager.settings.values["approval_timeout_minutes"] = 30
    timeouts = expire_waits_of(monkeypatch, 1800)
    output = await bounded(manager.execute_tool(session, tools, "ask_user", {"questions": QUESTIONS}, "call"))
    event = tool_event(session)
    assert timeouts == [1800] and output == QUESTIONS_EXPIRED
    assert (event["state"], event["question_outcome"]) == ("completed", "expired")
    assert event["approval_expires"] > event["created"] + 1790
    assert not manager.interactions.waiting


async def test_an_unreviewed_plan_expires_after_the_approval_wait(runtime, monkeypatch):
    _, manager, session, _, tools = runtime
    session["permission_mode"] = "plan"
    timeouts = expire_waits_of(monkeypatch, 3600)  # The default wait: 60 minutes.
    output = await bounded(manager.execute_tool(session, tools, "submit_plan", PLAN, "call"))
    event = tool_event(session)
    assert timeouts == [3600] and output == PLAN_EXPIRED
    assert (event["state"], event["plan_outcome"]) == ("completed", "expired")
    assert session["permission_mode"] == "plan"


async def test_with_no_limit_a_question_waits_until_answered(runtime, monkeypatch):
    _, manager, session, _, tools = runtime
    manager.settings.values["approval_timeout_minutes"] = 0
    timeouts = expire_waits_of(monkeypatch, 3600)
    run = call(manager, session, tools, "ask_user", {"questions": QUESTIONS[:1]})
    _, event_id = await waiting(manager)
    assert "approval_expires" not in tool_event(session)
    manager.interactions.answer_questions(session["id"], event_id, {"answers": [{"selected": ["SQLite"]}]})
    await bounded(run)
    assert None in timeouts and 3600 not in timeouts


async def test_in_dont_ask_mode_questions_return_at_once(runtime, monkeypatch):
    _, manager, session, _, tools = runtime
    session["permission_mode"] = "dontAsk"

    async def no_wait(*args, **kwargs):
        pytest.fail("A question waited in Don't ask mode")
    monkeypatch.setattr(manager.interactions, "wait", no_wait)
    output = await bounded(manager.execute_tool(session, tools, "ask_user", {"questions": QUESTIONS}, "call"))
    event = tool_event(session)
    assert output == DONT_ASK
    assert (event["state"], event["question_outcome"]) == ("completed", "skipped") and "approval_expires" not in event
    assert not manager.interactions.waiting


async def test_ask_user_works_in_plan_mode(runtime):
    _, manager, session, _, tools = runtime
    session["permission_mode"] = "plan"
    run = call(manager, session, tools, "ask_user", {"questions": QUESTIONS[:1]})
    _, event_id = await waiting(manager)
    manager.interactions.answer_questions(session["id"], event_id, {"answers": [{"selected": ["Postgres"]}]})
    assert json.loads(await bounded(run))["answers"][0]["selected"] == ["Postgres"]


async def test_stop_while_a_question_waits_cancels_it(runtime, monkeypatch):
    _, manager, session, _, _ = runtime
    scripted(monkeypatch, [lambda: tool_reply("ask_user", {"questions": QUESTIONS}, "ask")])
    manager.start(session["id"], "Set up storage")
    await waiting(manager)
    await WAIT_FOR(manager.stop(session["id"]), 5)
    event = tool_event(session, "ask")
    assert event["state"] == "cancelled" and "question_outcome" not in event
    assert not manager.interactions.waiting
    assert session["terminal_reason"] == "stopped" and manager.statuses[session["id"]] == "idle"


async def test_an_approval_answer_cannot_resolve_a_question(runtime):
    app, manager, session, _, tools = runtime
    run = call(manager, session, tools, "ask_user", {"questions": QUESTIONS[:1]})
    _, event_id = await waiting(manager)
    async with client_for(app) as client:
        await connect(client)
        response = await client.post(f"/api/sessions/{session['id']}/approvals/{event_id}", json={"allowed": True})
        assert response.status_code == 400
        # Nor can a plan review answer a question.
        response = await client.post(f"/api/sessions/{session['id']}/plans/{event_id}", json={"choice": "manual"})
        assert response.status_code == 400
    assert not run.done() and tool_event(session)["state"] == "pending"
    manager.interactions.answer_questions(session["id"], event_id, {"skip": True})
    assert await bounded(run) == SKIPPED


# submit_plan

@pytest.mark.parametrize("mode", ["manual", "acceptEdits", "auto", "bypassPermissions"])
async def test_submit_plan_outside_plan_mode_is_an_error(runtime, mode):
    _, manager, session, _, tools = runtime
    session["permission_mode"] = mode
    output = await bounded(manager.execute_tool(session, tools, "submit_plan", PLAN, "call"))
    event = tool_event(session)
    assert event["state"] == "error"
    assert event["output"] == "submit_plan is only available in Plan mode. Describe the plan in your reply instead."
    assert json.loads(output)["error_kind"] == "unavailable_tool"
    assert session["permission_mode"] == mode and not manager.interactions.waiting


async def test_subagents_cannot_submit_plans(runtime):
    _, manager, session, _, tools = runtime
    session.update(permission_mode="plan", is_subagent=True)
    await bounded(manager.execute_tool(session, tools, "submit_plan", PLAN, "call"))
    event = tool_event(session)
    assert (event["state"], event["output"]) == ("error", "Subagents cannot submit plans; return the plan as your final reply.")
    assert not manager.interactions.waiting


@pytest.mark.parametrize("choice, label", [("acceptEdits", "Accept edits"), ("manual", "Manual")])
async def test_approving_a_plan_switches_the_mode(runtime, monkeypatch, choice, label):
    app, manager, session, _, tools = runtime
    session["permission_mode"] = "plan"
    sent = record_broadcasts(manager, monkeypatch)
    run = call(manager, session, tools, "submit_plan", PLAN)
    _, event_id = await waiting(manager, "plan")
    event = tool_event(session)
    assert event["state"] == "pending" and manager.statuses[session["id"]] == "awaiting_approval"
    async with client_for(app) as client:
        await connect(client)
        response = await client.post(f"/api/sessions/{session['id']}/plans/{event_id}",
                                     json={"choice": choice, "feedback": "  Keep private-token out of logs. "})
        assert response.json() == {"ok": True}
    output = await bounded(run)
    assert output == (f"The user approved the plan and switched this chat to {label} mode. Implement the plan now, "
                      "step by step. Their note: Keep [REDACTED] out of logs.")
    assert session["permission_mode"] == choice
    assert manager.store.get(session["id"])["permission_mode"] == choice
    assert ((event["state"], event["plan_outcome"], event["plan_mode"], event["feedback"])
            == ("completed", "approved", choice, "Keep [REDACTED] out of logs."))
    permissions = [data for _, data in sent if data["type"] == "permissions"]
    assert permissions and permissions[-1]["permission_mode"] == choice
    notice = session["events"][-1]
    assert (notice["type"], notice["text"]) == ("notice", f"Plan approved. Permission mode: {label}.")
    assert manager.statuses[session["id"]] == "running"


async def test_approving_without_feedback_has_no_note(runtime):
    _, manager, session, _, tools = runtime
    session["permission_mode"] = "plan"
    run = call(manager, session, tools, "submit_plan", PLAN)
    _, event_id = await waiting(manager, "plan")
    manager.interactions.answer_plan(session["id"], event_id, {"choice": "acceptEdits", "feedback": "   "})
    assert await bounded(run) == ("The user approved the plan and switched this chat to Accept edits mode. "
                                  "Implement the plan now, step by step.")
    assert "feedback" not in tool_event(session)


async def test_keep_planning_returns_the_feedback_and_stays_in_plan(runtime):
    _, manager, session, _, tools = runtime
    session["permission_mode"] = "plan"
    run = call(manager, session, tools, "submit_plan", PLAN)
    _, event_id = await waiting(manager, "plan")
    with pytest.raises(ValueError, match="at most 2,000"):
        manager.interactions.answer_plan(session["id"], event_id, {"choice": "keep_planning", "feedback": "x" * 2001})
    manager.interactions.answer_plan(session["id"], event_id, {"choice": "keep_planning", "feedback": "Split step 2."})
    assert await bounded(run) == ("The user wants to keep planning and did not approve this plan. Their feedback: "
                                  "Split step 2.. Revise the plan; do not make changes.")
    event = tool_event(session)
    assert (event["plan_outcome"], event["feedback"]) == ("keep_planning", "Split step 2.")
    assert session["permission_mode"] == "plan"
    assert not any(item["type"] == "notice" for item in session["events"])


async def test_leaving_plan_from_the_menu_supersedes_a_waiting_plan(runtime):
    app, manager, session, _, tools = runtime
    session["permission_mode"] = "plan"
    run = call(manager, session, tools, "submit_plan", PLAN)
    _, event_id = await waiting(manager, "plan")
    session["permission_mode"] = "acceptEdits"
    await manager.interactions.mode_changed(session, "plan", "acceptEdits")
    output = await bounded(run)
    assert output == ("The user switched this chat to Accept edits mode from the menu instead of reviewing the plan, "
                      "so it was not approved. Ask the user before implementing it.")
    assert tool_event(session)["plan_outcome"] == "superseded"
    assert session["permission_mode"] == "acceptEdits"
    async with client_for(app) as client:
        await connect(client)
        response = await client.post(f"/api/sessions/{session['id']}/plans/{event_id}", json={"choice": "manual"})
        assert response.status_code == 400 and response.json()["detail"] == "This plan is no longer waiting for review."
    assert session["permission_mode"] == "acceptEdits"


async def test_a_plan_is_not_approved_once_the_chat_left_plan_mode(runtime):
    _, manager, session, _, tools = runtime
    session["permission_mode"] = "plan"
    run = call(manager, session, tools, "submit_plan", PLAN)
    _, event_id = await waiting(manager, "plan")
    session["permission_mode"] = "auto"  # Changed, but not (yet) reported through mode_changed.
    with pytest.raises(ValueError, match="This plan is no longer waiting for review."):
        manager.interactions.answer_plan(session["id"], event_id, {"choice": "manual"})
    assert session["permission_mode"] == "auto" and not run.done()
    await manager.interactions.mode_changed(session, "plan", "auto")
    assert "Auto mode" in await bounded(run)


async def test_mode_changes_skip_questions_only_for_dont_ask(runtime):
    _, manager, session, _, tools = runtime
    run = call(manager, session, tools, "ask_user", {"questions": QUESTIONS})
    _, event_id = await waiting(manager, "question")
    await manager.interactions.mode_changed(session, "manual", "acceptEdits")
    await asyncio.sleep(0.05)
    assert not run.done()
    await manager.interactions.mode_changed(session, "acceptEdits", "dontAsk")
    await manager.interactions.mode_changed(session, "acceptEdits", "dontAsk")  # A second change sets nothing twice.
    assert await bounded(run) == DONT_ASK
    assert tool_event(session)["question_outcome"] == "skipped"
    with pytest.raises(ValueError, match="This question is no longer waiting for an answer."):
        manager.interactions.answer_questions(session["id"], event_id, {"skip": True})


async def test_mode_changes_in_another_chat_leave_cards_alone(runtime):
    _, manager, session, _, tools = runtime
    session["permission_mode"] = "plan"
    run = call(manager, session, tools, "submit_plan", PLAN)
    await waiting(manager, "plan")
    await manager.interactions.mode_changed({"id": "other"}, "plan", "manual")
    await asyncio.sleep(0.05)
    assert not run.done()
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    assert not manager.interactions.waiting


# Whole turns with a scripted model

@pytest.mark.parametrize("choice", ["acceptEdits", "keep_planning"])
async def test_an_approved_plan_is_implemented_in_the_same_turn(runtime, monkeypatch, choice):
    _, manager, session, project, _ = runtime
    session["permission_mode"] = "plan"
    requests = scripted(monkeypatch, [
        lambda: tool_reply("submit_plan", PLAN, "plan"),
        lambda: tool_reply("write_file", {"path": "notes.txt", "content": "planned"}, "write"),
        lambda: reply("Finished."),
    ])
    manager.start(session["id"], "Add a notes file")
    session_id, event_id = await waiting(manager, "plan")
    manager.interactions.answer_plan(session_id, event_id, {"choice": choice})
    await WAIT_FOR(manager.tasks[session["id"]], 5)
    write = tool_event(session, "write")
    assert len(requests["parent"]) == 3 and session["terminal_reason"] == "completed"
    assert "approval" not in write  # No approval card either way.
    if choice == "acceptEdits":
        assert (project / "notes.txt").read_text() == "planned"
        assert write["state"] == "completed" and session["permission_mode"] == "acceptEdits"
    else:
        assert not (project / "notes.txt").exists()
        assert write["state"] == "rejected" and write["output"].startswith("Plan mode is read-only.")
        assert session["permission_mode"] == "plan"
    plan_result = next(message for message in requests["parent"][1]["messages"] if message.get("tool_call_id") == "plan")
    assert ("approved the plan" if choice == "acceptEdits" else "keep planning") in plan_result["content"]


async def test_a_subagent_question_waits_in_the_child_and_is_answered_there(runtime, monkeypatch):
    app, manager, session, _, _ = runtime
    session["permission_mode"] = "acceptEdits"
    requests = scripted(monkeypatch, parent=[
        lambda: tool_reply("delegate_task", {"task": "Pick the storage"}, "delegate"),
        lambda: reply("Parent done."),
    ], child=[
        lambda: tool_reply("ask_user", {"questions": QUESTIONS[:1]}, "ask"),
        lambda: reply("Child used SQLite."),
    ])
    manager.start(session["id"], "Plan storage")
    child_id, event_id = await waiting(manager, "question", other_than=session["id"])
    delegate = tool_event(session, "delegate")
    assert delegate["child_session_id"] == child_id
    for _ in range(100):
        if delegate["delegation"]["status"] == "awaiting_approval":
            break
        await asyncio.sleep(0.01)
    assert delegate["delegation"]["status"] == "awaiting_approval"
    assert manager.statuses[child_id] == "awaiting_approval" and manager.statuses[session["id"]] == "delegating"
    async with client_for(app) as client:
        await connect(client)
        route = f"/api/sessions/{session['id']}/questions/{event_id}"
        assert (await client.post(route, json={"skip": True})).status_code == 400  # Not the parent's question.
        response = await client.post(f"/api/sessions/{child_id}/questions/{event_id}",
                                     json={"answers": [{"selected": ["SQLite"]}]})
        assert response.json() == {"ok": True}
    await WAIT_FOR(manager.tasks[session["id"]], 5)
    result = json.loads(delegate["output"])
    assert result["status"] == "completed" and result["output"] == "Child used SQLite."
    assert session["events"][-1]["text"] == "Parent done."
    child = manager.store.get(child_id)
    assert tool_event(child, "ask")["question_outcome"] == "answered"
    # The question never reaches the parent's context.
    assert not any("Which database?" in json.dumps(body) for body in requests["parent"])


# Export

async def test_export_keeps_answers_and_outcomes(runtime):
    _, manager, session, _, tools = runtime
    run = call(manager, session, tools, "ask_user", {"questions": QUESTIONS[:1]}, "ask")
    _, event_id = await waiting(manager)
    manager.interactions.answer_questions(session["id"], event_id, {"answers": [{"selected": [], "other": "DuckDB"}]})
    await bounded(run)
    session["permission_mode"] = "plan"
    run = call(manager, session, tools, "submit_plan", PLAN, "plan")
    _, event_id = await waiting(manager, "plan")
    manager.interactions.answer_plan(session["id"], event_id, {"choice": "manual", "feedback": "Go slowly."})
    await bounded(run)
    bundle = export_bundle([session], session["id"])
    events = {event.get("call_id"): event for event in bundle["sessions"][0]["events"]}
    assert events["ask"]["answers"] == [{"question": "Which database?", "selected": [], "other": "DuckDB"}]
    assert events["ask"]["question_outcome"] == "answered"
    assert (events["plan"]["plan_outcome"], events["plan"]["plan_mode"], events["plan"]["feedback"]) == (
        "approved", "manual", "Go slowly.")
