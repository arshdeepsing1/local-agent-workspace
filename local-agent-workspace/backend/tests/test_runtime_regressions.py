import asyncio
import json

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(tmp_path / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    yield manager, session
    store.db.close()


@pytest.mark.parametrize("legacy_runtime", ["obsolete-runtime", None, ""])
def test_unsupported_runtime_history_is_preserved_and_cannot_start(runtime, legacy_runtime):
    manager, session = runtime
    session["runtime"] = legacy_runtime
    session["events"].append({"type": "user", "text": "A preserved historical message."})
    manager.store.save(session)
    saved = manager.store.get(session["id"])
    with pytest.raises(ValueError, match="unsupported runtime.*read-only"):
        manager.start(session["id"], "Do not append this message")
    assert manager.store.get(session["id"]) == saved
    assert session["id"] not in manager.tasks
    assert session["id"] not in manager.live


async def test_legacy_databricks_history_can_continue(runtime):
    manager, session = runtime
    session["runtime"] = "databricks"
    manager.store.save(session)

    async def no_model(session, prompt):
        pass

    manager.run_databricks = no_model
    manager.start(session["id"], "Continue")
    await manager.tasks[session["id"]]
    assert manager.store.get(session["id"])["events"][0]["text"] == "Continue"


async def test_stop_before_run_starts_clears_session_state(runtime):
    manager, session = runtime
    manager.start(session["id"], "Hello")
    await manager.stop(session["id"])
    assert manager.tasks[session["id"]].done()
    assert manager.statuses[session["id"]] == "idle"
    assert session["id"] not in manager.live

    async def no_model(session, prompt):
        pass

    manager.run_databricks = no_model
    manager.start(session["id"], "Try again")
    await manager.tasks[session["id"]]
    assert manager.statuses[session["id"]] == "idle"
    assert manager.store.get(session["id"])["events"][0]["text"] == "Try again"


@pytest.mark.parametrize("blocked_message", ["status", "user", "title"])
async def test_stop_during_startup_broadcast_cleans_up(runtime, blocked_message):
    manager, session = runtime
    entered = asyncio.Event()

    async def broadcast(session_id, data):
        kind = data.get("event", {}).get("type", data["type"])
        if kind == blocked_message and data.get("status") != "idle":
            entered.set()
            await asyncio.Event().wait()

    manager.broadcast = broadcast
    manager.start(session["id"], "Hello")
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(manager.stop(session["id"]), 2)
    assert manager.statuses[session["id"]] == "idle"
    assert session["id"] not in manager.live


async def test_stop_during_approval_broadcast_removes_pending_future(runtime):
    manager, session = runtime
    entered = asyncio.Event()

    async def broadcast(session_id, data):
        if data.get("event", {}).get("state") == "pending":
            entered.set()
            await asyncio.Event().wait()

    async def run_tool(session, prompt):
        await manager.execute_tool(session, WorkspaceTools(session["workspace"]), "write_file",
                                   {"path": "declined.txt", "content": "Never written."}, "pending-call")

    manager.broadcast = broadcast
    manager.run_databricks = run_tool
    manager.start(session["id"], "Write the file.")
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(manager.stop(session["id"]), 2)
    assert not manager.pending
    assert manager.statuses[session["id"]] == "idle"


async def test_resume_recovers_completed_tool_result_after_cancelled_broadcast(runtime, tmp_path, monkeypatch):
    manager, session = runtime
    session["permission_mode"] = "acceptEdits"
    manager.store.save(session)
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            delta = {"tool_calls": [{"index": 0, "id": "write-call", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "written.txt", "content": "Already written."})}}]}
        else:
            delta = {"content": "Resumed."}
        chunk = {"choices": [{"delta": delta, "finish_reason": "tool_calls" if len(requests) == 1 else "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(gateway)))
    completed = asyncio.Event()

    async def broadcast(session_id, data):
        event = data.get("event", {})
        if event.get("name") == "write_file" and event.get("state") == "completed":
            completed.set()
            await asyncio.Event().wait()

    manager.broadcast = broadcast
    manager.start(session["id"], "Write the file.")
    await asyncio.wait_for(completed.wait(), 2)
    await asyncio.wait_for(manager.stop(session["id"]), 2)
    assert (tmp_path / "written.txt").read_text() == "Already written."
    saved = manager.store.get(session["id"])
    output = next(event["output"] for event in saved["events"] if event.get("call_id") == "write-call")
    assert not any(message["role"] == "tool" for message in saved["wire"])

    restarted = AgentManager(manager.store, manager.settings)
    restarted.start(session["id"], "Continue.")
    await restarted.tasks[session["id"]]
    resumed = next(request for request in reversed(requests) if request["stream"])
    result = next(message for message in resumed["messages"] if message["role"] == "tool")
    assert result["tool_call_id"] == "write-call"
    assert result["content"] == output


@pytest.mark.parametrize("completed_empty_output", [False, True])
async def test_resume_preserves_empty_success_or_reports_unknown_outcome(runtime, monkeypatch, completed_empty_output):
    manager, session = runtime
    session["wire"] = [{"role": "assistant", "content": None, "tool_calls": [{
        "id": "interrupted-call", "type": "function", "function": {"name": "run_command", "arguments": "{}"}}]}]
    if completed_empty_output:
        session["events"].append({"type": "tool", "call_id": "interrupted-call", "state": "completed", "output": ""})
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Resumed."},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(gateway)))
    await manager.run_databricks(session, "Continue.")
    result = next(message["content"] for message in requests[0]["messages"] if message["role"] == "tool")
    if completed_empty_output:
        assert result == ""
    else:
        assert "unknown" in result.lower()
        assert "not completed" not in result.lower()
