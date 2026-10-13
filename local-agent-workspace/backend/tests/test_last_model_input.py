"""Provider-reported input in "Last model input", the auto-compact setting, and what a
compaction makes the agent forget (agent level, fake Databricks gateway)."""
import asyncio
import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent import mentions
from local_agent.agents import AgentManager, forget_summarized_reads
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.context import SAFETY_MARGIN, VERIFY_CONTINUATION_PREFIX
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", compaction_handoffs=False, context_window=65_536)
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["title"] = "Billing work"
    yield manager, session, project
    store.db.close()


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(gateway)))


def sse(*chunks):
    return httpx.Response(200, text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                          + "data: [DONE]\n\n")


def reply_chunk(text="Done.", finish="stop", usage=None):
    chunk = {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}
    return {**chunk, "usage": usage} if usage else chunk


def tool_chunk(call_id, name, arguments):
    return {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}]}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 7000, "completion_tokens": 30}}


def summary_reply(text="Summary of the earlier turns.", usage=None):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                                     "usage": usage or {"prompt_tokens": 50_000, "completion_tokens": 900}})


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


def long_history(count=4, size=45_000):
    return [message for i in range(count) for message in (
        user(f"Earlier request {i}: " + "x" * size), assistant(f"Completed step {i}."))]


USAGE = {"prompt_tokens": 9000, "completion_tokens": 50, "cache_read_input_tokens": 1000}


def capture(manager):
    updates = []
    original = manager.broadcast

    async def broadcast(session_id, data):
        updates.append(copy.deepcopy(data))
        await original(session_id, data)
    manager.broadcast = broadcast
    return updates


# --- 1-3: last_request and next_estimate --------------------------------------------------

async def test_a_streamed_reply_sets_the_reported_input_and_broadcasts_it(runtime, monkeypatch):
    manager, session, _ = runtime
    updates = capture(manager)
    mock_gateway(monkeypatch, lambda request: sse(reply_chunk(usage=USAGE)) if json.loads(request.content)["stream"]
                 else summary_reply("Billing title"))
    await manager.run(session, "Hello")
    info = session["context_info"]
    report = info["last_request"]
    assert report["input_tokens"] == 10_000 and report["uncached_input_tokens"] == 9000
    assert report["percent_of_input_budget"] == round(10_000 * 100 / info["input_budget"], 1)
    assert report["estimated_tokens"] == info["estimated_tokens"] and report["partial"] is False
    agent_call = next(call for call in session["inference_calls"] if call["purpose"] == "agent")
    assert report["call_id"] == agent_call["id"] and report["model"] == session["model"]
    # Broadcast as soon as the request ends, before the next-request estimate is added.
    assert any(update.get("type") == "context" and update["context_info"].get("last_request") == report
               and "next_estimate" not in update["context_info"] for update in updates)
    assert manager.store.get(session["id"])["context_info"]["last_request"] == report


async def test_next_estimate_and_last_request_carry_over_and_mark_a_compaction(runtime, monkeypatch):
    manager, session, project = runtime
    (project / "a.py").write_text("print(1)\n")
    updates = capture(manager)
    streamed = []

    async def gateway(request):
        payload = json.loads(request.content)
        if not payload["stream"]:
            return summary_reply()
        streamed.append(payload)
        if len(streamed) == 1:
            return sse(tool_chunk("r1", "read_file", {"path": "a.py"}))
        return sse(reply_chunk(usage=USAGE))

    mock_gateway(monkeypatch, gateway)
    manager.settings.values["permission_mode"] = "bypassPermissions"
    session["permission_mode"] = "bypassPermissions"
    await manager.run(session, "Read a.py")
    contexts = [update["context_info"] for update in updates if update.get("type") == "context"]
    first = next(index for index, info in enumerate(contexts) if info.get("last_request"))
    # The second step's prepared context still shows the first step's report, without a stale next estimate.
    prepared = next(info for info in contexts[first + 1:] if "next_estimate" not in info)
    assert prepared["last_request"]["uncached_input_tokens"] == 7000
    info = session["context_info"]
    assert info["last_request"]["input_tokens"] == 10_000
    assert info["next_estimate"]["tokens"] > 0
    assert info["next_estimate"]["percent_of_input_budget"] == round(
        info["next_estimate"]["tokens"] * 100 / info["input_budget"], 1)
    # Manual compaction keeps the report; its compactions count no longer matches.
    session["wire"] = long_history(2, 2000) + session["wire"]
    manager.store.save(session)
    manager.start_compaction(session["id"])
    await manager.tasks[session["id"]]
    saved = manager.store.get(session["id"])["context_info"]
    assert saved["prepared_for_next_turn"] and saved["compactions"] == 1
    assert saved["last_request"]["compactions"] == 0 and saved["last_request"]["input_tokens"] == 10_000


async def test_a_cancelled_stream_is_partial_and_an_output_limit_stop_is_not(runtime, monkeypatch):
    manager, session, _ = runtime
    entered = asyncio.Event()
    mode = {"cut_off": False}

    async def body():
        yield ("data: " + json.dumps({"choices": [{"delta": {"content": "Part"}}], "usage": USAGE}) + "\n\n").encode()
        entered.set()
        await asyncio.Event().wait()

    def gateway(request):
        if not json.loads(request.content)["stream"]:
            return summary_reply("A title")
        if mode["cut_off"]:
            return sse(reply_chunk("Long", "length", USAGE))
        return httpx.Response(200, content=body())

    mock_gateway(monkeypatch, gateway)
    manager.store.save(session)
    manager.start(session["id"], "Write a long answer")
    await asyncio.wait_for(entered.wait(), 2)
    await manager.stop(session["id"])
    report = manager.store.get(session["id"])["context_info"]["last_request"]
    assert report["partial"] is True and report["status"] == "cancelled" and "finish_reason" not in report

    manager.settings.values["max_agent_steps"] = 1
    mode["cut_off"] = True
    session = manager.get(session["id"])
    await manager.run(session, "Again")
    report = session["context_info"]["last_request"]
    assert report["partial"] is False and report["finish_reason"] == "length" and report["status"] == "error"


async def test_compaction_and_title_requests_never_set_the_reported_input(runtime, monkeypatch):
    manager, session, _ = runtime
    session["wire"] = long_history()
    manager.store.save(session)
    session["title"] = "New conversation"
    mock_gateway(monkeypatch, lambda request: sse(reply_chunk(usage=USAGE)) if json.loads(request.content)["stream"]
                 else summary_reply(usage={"prompt_tokens": 50_000, "completion_tokens": 900}))
    await manager.run(session, "Continue")
    purposes = [call["purpose"] for call in session["inference_calls"]]
    assert "compaction" in purposes and "title" in purposes
    agent = next(call for call in session["inference_calls"] if call["purpose"] == "agent")
    report = session["context_info"]["last_request"]
    assert report["call_id"] == agent["id"] and report["input_tokens"] == 10_000 and report["compactions"] == 1


# --- 4-5: metrics and settings -------------------------------------------------------------

def test_metrics_return_the_last_reported_input(runtime):
    manager, _, project = runtime
    with TestClient(create_app(manager.settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        app_manager = client.app.state.manager
        session = app_manager.store.create(manager.settings.values)
        url = f"/api/sessions/{session['id']}/metrics"
        assert client.get(url, headers=headers).json()["last_input"] is None
        session["context_info"] = {"last_request": {"input_tokens": 10_000, "percent_of_input_budget": 8.3}}
        app_manager.store.save(session)
        app_manager.live.pop(session["id"], None)
        assert client.get(url, headers=headers).json()["last_input"] == {
            "input_tokens": 10_000, "percent_of_input_budget": 8.3}


def test_the_auto_compact_setting_is_validated_and_persisted(runtime):
    manager, _, project = runtime
    settings = manager.settings
    assert settings.values["auto_compact_percent"] == 90
    editable = {"workspace": str(project), "model": "test-model", "env_file": ""}
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        for value in (50, 100):
            response = client.put("/api/settings", headers=headers, json={**editable, "auto_compact_percent": value})
            assert response.status_code == 200 and response.json()["auto_compact_percent"] == value
            assert Settings(settings.state_dir).values["auto_compact_percent"] == value
        for invalid in (49, 101, "90", True, 75.5):
            assert client.put("/api/settings", headers=headers,
                              json={**editable, "auto_compact_percent": invalid}).status_code == 422
    for invalid in (49, 101, "90", True):
        with pytest.raises(ValueError, match="Auto-compact threshold must be an integer from 50 to 100 percent"):
            settings.update({"auto_compact_percent": invalid})


async def test_a_lower_auto_compact_setting_compacts_the_same_history_earlier(runtime, monkeypatch):
    manager, session, _ = runtime
    window = 65_536
    budget = window - 8192 - SAFETY_MARGIN
    manager.settings.values["context_window"] = window
    session["wire"] = long_history(3, 40_000)
    manager.store.save(session)
    original = copy.deepcopy(session)
    mock_gateway(monkeypatch, lambda request: sse(reply_chunk()) if json.loads(request.content)["stream"]
                 else summary_reply())
    manager.settings.values["auto_compact_percent"] = 100
    await manager.run(session, "Continue")
    assert "compaction" not in session["context_info"] and session["context_state"]["compactions"] == 0
    assert budget * 60 // 100 < session["context_info"]["estimated_tokens"] <= budget
    session = original
    manager.live.pop(session["id"], None)
    manager.settings.values["auto_compact_percent"] = 60
    await manager.run(session, "Continue")
    assert session["context_state"]["compactions"] == 1
    assert session["context_info"]["auto_compact_threshold"] == budget * 60 // 100


async def test_a_cut_off_plain_summary_with_text_is_kept(runtime, monkeypatch):
    manager, session, _ = runtime
    session["wire"] = long_history()
    manager.store.save(session)
    mock_gateway(monkeypatch, lambda request: sse(reply_chunk()) if json.loads(request.content)["stream"]
                 else httpx.Response(200, json={"choices": [{"message": {"content": "Partial summary"},
                                                             "finish_reason": "length"}]}))
    await manager.run(session, "Continue")
    assert session["context_state"]["summary"].startswith(
        "Partial summary\n\n[This summary was cut off at the output limit.]")


# --- 6-9: what a compaction forgets -----------------------------------------------------------

def read_call(call_id, path, name="read_file"):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps({"path": path})}}]}


def tool_event(call_id, name, path, state="completed", as_text=False):
    arguments = {"path": path}
    return {"id": f"event-{call_id}", "type": "tool", "name": name, "call_id": call_id, "state": state,
            "input": json.dumps(arguments) if as_text else arguments, "output": "{}"}


def remember(session, project, *names):
    for name in names:
        path = project / name
        path.write_text(f"{name} contents\n")
        AgentManager.remember_file(session, path)


async def test_an_automatic_compaction_saves_the_forgotten_reads_with_the_session(runtime, monkeypatch):
    manager, session, project = runtime
    remember(session, project, "a.py")
    session["wire"] = [user("Read a.py"), read_call("r1", "a.py"),
                       {"role": "tool", "tool_call_id": "r1", "content": "a.py contents"}, assistant("Read it."),
                       *long_history()]
    session["events"] = [tool_event("r1", "read_file", "a.py")]
    manager.store.save(session)
    mock_gateway(monkeypatch, lambda request: sse(reply_chunk()) if json.loads(request.content)["stream"]
                 else summary_reply())
    await manager.run(session, "Continue")
    saved = manager.store.get(session["id"])
    target = str((project / "a.py").resolve())
    assert saved["context_state"]["summarized_reads"] == [target]
    assert target not in saved["file_views"]
    tools = WorkspaceTools(str(project))
    assert "summarized out of your context" in manager.overwrite_refusal(saved, tools, "a.py")


async def test_manual_compaction_saves_the_forgotten_reads_with_the_session(runtime, monkeypatch):
    manager, session, project = runtime
    remember(session, project, "a.py")
    session["wire"] = [user("Read a.py"), read_call("r1", "a.py"),
                       {"role": "tool", "tool_call_id": "r1", "content": "a.py contents"}, assistant("Read it."),
                       user("Next"), assistant("Done.")]
    session["events"] = [tool_event("r1", "read_file", "a.py")]
    manager.store.save(session)
    mock_gateway(monkeypatch, lambda request: summary_reply())
    manager.start_compaction(session["id"])
    await manager.tasks[session["id"]]
    saved = manager.store.get(session["id"])
    target = str((project / "a.py").resolve())
    assert saved["context_state"]["summarized_reads"] == [target] and target not in saved["file_views"]
    notices = [event for event in saved["events"] if event["type"] == "notice"]
    assert len(notices) == 1 and notices[0]["compaction"]["trigger"] == "manual"
    assert notices[0]["text"].startswith("Context compacted (compaction 1): 4 earlier messages were summarized in 1 request")


def test_forget_summarized_reads_does_not_change_its_inputs():
    views, state = {"/srv/x/a.py": [1, 2], "/srv/x/b.py": [3, 4]}, {"summarized_reads": ["/srv/x/c.py", "/srv/x/a.py"]}
    original = copy.deepcopy((views, state))
    new_views, new_state = forget_summarized_reads(views, state, ["/srv/x/a.py"])
    assert new_views == {"/srv/x/b.py": [3, 4]}
    assert new_state["summarized_reads"] == ["/srv/x/c.py", "/srv/x/a.py"]
    assert (views, state) == original
    many = forget_summarized_reads({}, {"summarized_reads": [f"/srv/x/{n}" for n in range(200)]}, ["/srv/x/new"])[1]
    assert len(many["summarized_reads"]) == 200 and many["summarized_reads"][-1] == "/srv/x/new"


def test_the_record_keeps_files_still_in_context_and_lists_the_forgotten_ones(runtime):
    manager, session, project = runtime
    remember(session, project, "a.py", "b.py", "c.py", "d.py")
    tools = manager.session_tools(session)
    request, _, views = mentions.expand_context(manager, session, tools, "Migrate billing using @d.py")
    assert mentions.attached_paths(request) == ["d.py"]
    wire = [user("Earlier"), assistant("Done."), user(request),
            read_call("r1", "a.py"), {"role": "tool", "tool_call_id": "r1", "content": "a"},
            read_call("r2", "b.py"), {"role": "tool", "tool_call_id": "r2", "content": "b"},
            read_call("r3", "c.py"), {"role": "tool", "tool_call_id": "r3", "content": "error"}]
    session["events"] = [tool_event("r1", "read_file", "a.py"), tool_event("r2", "read_file", "b.py"),
                         tool_event("r3", "read_file", "c.py", state="error")]
    record, holder = manager.compaction_record(session, tools)
    # An in-turn cut before the second read: the turn's request (with @d.py) stays in context.
    notes = record(wire, 5)
    resolved = lambda name: str((project / name).resolve())
    assert sorted(holder["forgotten"]) == sorted([resolved("a.py"), resolved("c.py")])
    assert "Files read before this compaction whose contents are no longer in your context" in notes
    listed = notes.split("no longer in your context")[1]
    assert "- a.py" in listed and "- c.py" in listed and "- b.py" not in listed and "- d.py" not in listed


def test_files_changed_come_only_from_calls_in_the_summarized_history(runtime):
    manager, session, project = runtime
    tools = manager.session_tools(session)
    (project / "src").mkdir()
    wire = [user("Edit"), read_call("w1", "src/api.py", "write_file"), {"role": "tool", "tool_call_id": "w1", "content": "ok"},
            assistant("Done."), user("Next")]
    session["events"] = [tool_event("w1", "write_file", "src/api.py", as_text=True),
                         tool_event("gone", "write_file", "src/rewound.py"),
                         {**tool_event("m1", "move_path", "src/old.py"), "input": {"path": "src/old.py", "destination": "src/new.py"}}]
    wire[1]["tool_calls"].append({"id": "m1", "type": "function", "function": {"name": "move_path", "arguments": "{}"}})
    manager.store.save(session)
    task = manager.task_board.create(session["id"], title="Ship the billing API")
    record, _ = manager.compaction_record(session, tools)
    notes = record(wire, 4)
    changed = notes.split("Files this conversation changed before this point (newest first):\n")[1].split("\n\n")[0]
    assert changed.split("\n") == ["- src/new.py", "- src/old.py", "- src/api.py"]
    assert "rewound" not in notes
    assert f"- [pending] Ship the billing API (task_id {task['id']})" in notes and "and 0 completed tasks." in notes


async def test_a_declined_verify_note_is_recorded_as_the_users_request(runtime, monkeypatch):
    manager, session, _ = runtime
    note = (f"{VERIFY_CONTINUATION_PREFIX} the user declined the project's verify command `pytest`, so it did not "
            "run. Their note: keep using tabs\nAnswer the note.")
    session["wire"] = [*long_history(2), user("Fix it"), assistant("Edited."), user(note), assistant("Okay."),
                       *long_history(2)]
    manager.store.save(session)
    mock_gateway(monkeypatch, lambda request: sse(reply_chunk()) if json.loads(request.content)["stream"]
                 else summary_reply())
    await manager.run(session, "Continue")
    assert "(Note when declining Verify) keep using tabs" in session["context_state"]["user_requests"]
