import asyncio
import json

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.store import Store


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="", model="test-model")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-title-token")
    return settings


@pytest.fixture
async def runtime(settings):
    store = Store(settings.state_dir / "titles.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    yield manager, session
    await asyncio.gather(*(manager.stop(sid) for sid in list(manager.tasks)))
    await manager.jobs.shutdown()
    store.db.close()


def mock_gateway(monkeypatch, handler):
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(handler)))
    return client_class


def assistant_response(text="The requested work is complete.", finish="stop", call=None):
    delta = {"content": text} if call is None else {"tool_calls": [call]}
    chunk = {"choices": [{"delta": delta, "finish_reason": finish}]}
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


def title_response(title="Improve Workspace Search"):
    return httpx.Response(200, json={"choices": [{
        "message": {"content": title}, "finish_reason": "stop",
    }]})


async def run_turn(manager, session_id, prompt):
    manager.start(session_id, prompt)
    await asyncio.wait_for(manager.tasks[session_id], 3)


async def test_title_follows_reply_and_persists_once_without_polluting_history(runtime, monkeypatch):
    manager, session = runtime
    requests, broadcasts = [], []

    async def gateway(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["stream"]:
            return assistant_response()
        saved = manager.store.get(session["id"])
        assert saved["wire"][-1] == {"role": "assistant", "content": "The requested work is complete."}
        assert manager.statuses[session["id"]] == "naming"
        assert "tools" not in payload
        return title_response()

    async def broadcast(session_id, message):
        broadcasts.append(message)
        if message.get("type") == "title" and message["title"] == "Improve Workspace Search":
            saved = manager.store.get(session_id)
            assert saved["title"] == message["title"]
            assert saved["title_generated"] is True

    mock_gateway(monkeypatch, gateway)
    manager.broadcast = broadcast
    await run_turn(manager, session["id"], "Please improve workspace search.")
    saved = manager.store.get(session["id"])
    assert [request["stream"] for request in requests] == [True, False]
    assert saved["title"] == "Improve Workspace Search"
    assert saved["title_generated"] is True
    assert saved["wire"] == [
        {"role": "user", "content": "Please improve workspace search."},
        {"role": "assistant", "content": "The requested work is complete."},
    ]
    assert [event["type"] for event in saved["events"]] == ["user", "assistant"]
    assert [message["status"] for message in broadcasts if message["type"] == "status"][-2:] == ["naming", "idle"]

    restarted = AgentManager(manager.store, manager.settings)
    await run_turn(restarted, session["id"], "Now verify the changes.")
    assert [request["stream"] for request in requests] == [True, False, True]
    assert manager.store.get(session["id"])["title"] == "Improve Workspace Search"
    assert [message["content"] for message in requests[-1]["messages"] if message["role"] == "user"] == [
        "Please improve workspace search.", "Now verify the changes.",
    ]


async def test_legacy_prompt_title_changes_only_after_successful_resume(runtime, monkeypatch):
    manager, session = runtime
    session.update(title="HI", events=[{"type": "user", "text": "HI"},
                                       {"type": "assistant", "text": "Hello."}],
                   wire=[{"role": "user", "content": "HI"}, {"role": "assistant", "content": "Hello."}])
    manager.store.save(session)
    restarted = AgentManager(manager.store, manager.settings)
    assert manager.store.get(session["id"])["title"] == "HI"
    requests = []

    async def gateway(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return assistant_response("Search now supports filenames.") if payload["stream"] else title_response()

    mock_gateway(monkeypatch, gateway)
    await run_turn(restarted, session["id"], "Add filename search.")
    saved = manager.store.get(session["id"])
    assert saved["title"] == "Improve Workspace Search"
    assert saved["title_generated"] is True
    assert [request["stream"] for request in requests] == [True, False]


@pytest.mark.parametrize("failure", ["http_error", "response_limit", "step_limit"])
async def test_unsuccessful_turn_does_not_generate_title(runtime, monkeypatch, failure):
    manager, session = runtime
    requests = []

    async def gateway(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if not payload["stream"]:
            return title_response()
        if failure == "http_error":
            return httpx.Response(503, text="Unavailable")
        if failure == "response_limit":
            return assistant_response("Incomplete response", finish="length")
        return assistant_response(finish="tool_calls", call={
            "index": 0, "id": f"list-{len(requests)}", "type": "function",
            "function": {"name": "list_files", "arguments": '{"path":"."}'},
        })

    mock_gateway(monkeypatch, gateway)
    await run_turn(manager, session["id"], "Inspect the project.")
    saved = manager.store.get(session["id"])
    expected_requests = {"http_error": 1, "response_limit": 3, "step_limit": 32}[failure]
    assert len(requests) == expected_requests
    assert all(request["stream"] for request in requests)
    assert saved["title"] == "Inspect the project."
    assert not saved.get("title_generated")
    assert saved["events"][-1]["type"] == ("notice" if failure == "step_limit" else "error")
    if failure == "step_limit":
        assert "model requests, not tool calls" in saved["events"][-1]["text"]
    if failure == "response_limit":
        assert "after two automatic retries" in saved["events"][-1]["text"]
    assert manager.statuses[session["id"]] == "idle"


@pytest.mark.parametrize("title", ["My custom release notes", "Worktree: codex/search-improvements"])
async def test_custom_and_worktree_titles_are_preserved(runtime, monkeypatch, title):
    manager, session = runtime
    session["title"] = title
    manager.store.save(session)
    requests = []

    async def gateway(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return assistant_response() if payload["stream"] else title_response()

    mock_gateway(monkeypatch, gateway)
    await run_turn(manager, session["id"], "Improve the search page.")
    assert [request["stream"] for request in requests] == [True]
    assert manager.store.get(session["id"])["title"] == title


@pytest.mark.parametrize("stop_before_delete", [False, True])
async def test_stop_during_naming_preserves_reply_and_delete_does_not_resurrect(settings, monkeypatch, stop_before_delete):
    app = create_app(settings)
    manager = app.state.manager
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def gateway(request):
        payload = json.loads(request.content)
        if payload["stream"]:
            return assistant_response()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client_class = mock_gateway(monkeypatch, gateway)
    async with app.router.lifespan_context(app):
        async with client_class(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            token = (await client.get("/api/bootstrap")).json()["token"]
            client.headers["X-Local-Token"] = token
            session_id = (await client.post("/api/sessions")).json()["id"]
            response = await client.post(f"/api/sessions/{session_id}/messages", json={"text": "Improve workspace search."})
            assert response.status_code == 200
            await asyncio.wait_for(entered.wait(), 2)
            assert manager.statuses[session_id] == "naming"
            if stop_before_delete:
                stopped = await asyncio.wait_for(client.post(f"/api/sessions/{session_id}/stop"), 2)
                assert stopped.status_code == 200
                assert cancelled.is_set()
                assert manager.statuses[session_id] == "idle"
            saved = manager.store.get(session_id)
            assert saved["wire"][-1] == {"role": "assistant", "content": "The requested work is complete."}
            assert not any(event["type"] in ("notice", "error") for event in saved["events"])
            assert not saved.get("title_generated")
            deleted = await asyncio.wait_for(client.delete(f"/api/sessions/{session_id}"), 2)
            assert deleted.status_code == 200
            assert cancelled.is_set()
            await asyncio.sleep(0)
            assert manager.store.get(session_id) is None
            assert manager.get(session_id) is None
            assert manager.tasks[session_id].done()
            assert (await client.get(f"/api/sessions/{session_id}")).status_code == 404


async def test_parallel_sessions_generate_isolated_titles(runtime, monkeypatch):
    manager, first = runtime
    second = manager.store.create(manager.settings.values)
    both_naming = asyncio.Event()
    excerpts = []

    async def gateway(request):
        payload = json.loads(request.content)
        if payload["stream"]:
            prompt = payload["messages"][-1]["content"]
            return assistant_response("Completed " + prompt)
        excerpt = json.loads(payload["messages"][-1]["content"])
        excerpts.append(excerpt)
        if len(excerpts) == 2:
            both_naming.set()
        await both_naming.wait()
        return title_response("Improve Alpha Search" if "alpha-only" in excerpt["request"] else "Improve Beta Search")

    mock_gateway(monkeypatch, gateway)
    prompts = {first["id"]: "Improve alpha-only search.", second["id"]: "Improve beta-only search."}
    for session_id, prompt in prompts.items():
        manager.start(session_id, prompt)
    await asyncio.wait_for(asyncio.gather(*(manager.tasks[sid] for sid in prompts)), 3)
    assert manager.store.get(first["id"])["title"] == "Improve Alpha Search"
    assert manager.store.get(second["id"])["title"] == "Improve Beta Search"
    assert len(excerpts) == 2
    for excerpt in excerpts:
        assert excerpt["response"] == "Completed " + excerpt["request"]
        assert ("alpha-only" in json.dumps(excerpt)) != ("beta-only" in json.dumps(excerpt))


@pytest.mark.parametrize("cancel_parent", [False, True])
async def test_child_title_cancellation_preserves_completed_handback_unless_parent_cancelled(runtime, monkeypatch, cancel_parent):
    manager, parent = runtime
    naming_started, naming_cancelled = asyncio.Event(), asyncio.Event()

    async def gateway(request):
        payload = json.loads(request.content)
        if payload["stream"]:
            return assistant_response("The parser handles all inspected inputs.")
        naming_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            naming_cancelled.set()

    mock_gateway(monkeypatch, gateway)
    delegated = asyncio.create_task(manager.delegates.delegate(parent, "Inspect the parser."))
    try:
        await asyncio.wait_for(naming_started.wait(), 2)
        child_id = manager.delegates.active[parent["id"]]["child_session_id"]
        assert manager.statuses[child_id] == "naming"
        if cancel_parent:
            await asyncio.wait_for(manager.delegates.cancel_parent(parent["id"]), 2)
        else:
            await asyncio.wait_for(manager.stop(child_id), 2)
        result = await asyncio.wait_for(delegated, 2)
        assert result == {
            "child_session_id": child_id,
            "status": "cancelled" if cancel_parent else "completed",
            "terminal_reason": "stopped" if cancel_parent else "completed",
            "output": "The parser handles all inspected inputs.",
        }
        saved = manager.store.get(child_id)
        assert saved["wire"][-1] == {"role": "assistant", "content": result["output"]}
        assert [event["type"] for event in saved["events"]] == ["user", "assistant"]
        assert naming_cancelled.is_set()
        assert manager.tasks[child_id].done()
        assert manager.tasks[child_id].cancelling() == 0
        assert manager.statuses[child_id] == "idle"
        assert not manager.delegates.active
    finally:
        if not delegated.done():
            delegated.cancel()
        await asyncio.gather(delegated, return_exceptions=True)
