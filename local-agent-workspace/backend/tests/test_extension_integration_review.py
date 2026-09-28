import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.mcp_client import MCPConnection
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-token")
    store = Store(settings.state_dir / "extension-review.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    tools = WorkspaceTools(str(project))
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    tools.skill_signature = ""
    yield manager, session, tools
    await manager.jobs.shutdown()
    store.db.close()


@pytest.mark.parametrize("change_during", ["approval", "before_hook"])
async def test_mcp_rechecks_guidance_after_approval_and_before_hook(runtime, change_during):
    manager, session, tools = runtime
    calls = []

    class Connection:
        tool_names = {"mcp__demo__change"}
        definitions = [{"function": {"name": "mcp__demo__change", "parameters": {"type": "object"}}}]

        async def call(self, name, arguments):
            calls.append(name)
            return {"output": "changed", "is_error": False, "truncated": False}

    manager.extension_connections[session["id"]] = Connection()
    session["permission_mode"] = "manual" if change_during == "approval" else "bypassPermissions"

    async def approve(session, event):
        (tools.root / "AGENTS.md").write_text("Ask for a fresh review before external writes.")
        return True

    async def hook(*args):
        (tools.root / "AGENTS.md").write_text("Ask for a fresh review before external writes.")
        return {"exit_code": 0, "output": "", "truncated": False}

    manager.approve = approve
    if change_during == "before_hook":
        manager.extensions.update_config({"hooks": [{"id": "change", "event": "before_tool", "enabled": True, "command": "true"}]})
        manager.extensions.run_hook = hook
    result = await manager.execute_tool(session, tools, "mcp__demo__change", {}, "external-call")
    assert not calls, "Changed project guidance must be read before an external action executes."
    assert "changed" in result.lower()


async def test_hook_configuration_changed_during_approval_cannot_run_old_or_new_command(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "acceptEdits"
    original = {"id": "check", "enabled": True, "event": "before_tool", "command": "touch old-hook-ran"}
    manager.extensions.update_config({"hooks": [original]})

    async def approve(session, event):
        manager.extensions.update_config({"hooks": [{**original, "command": "touch new-hook-ran"}]})
        return True

    manager.approve = approve
    result = await manager.execute_tool(session, tools, "write_file", {"path": "note.txt", "content": "new"}, "edit")
    assert "disabled or changed" in result
    assert not (tools.root / "old-hook-ran").exists()
    assert not (tools.root / "new-hook-ran").exists()
    assert not (tools.root / "note.txt").exists()


async def test_mcp_rpc_error_is_bounded_before_reaching_model_history(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"

    class FailedRPC:
        async def call_tool(self, *args, **kwargs):
            raise ValueError("Provider error: " + "🌍" * 5000)

    connection = MCPConnection([], lambda text: text)
    connection.registry = {"mcp__demo__fail": {"session": FailedRPC(), "tool_name": "fail", "server_id": "demo", "schema": {"type": "object"}}}
    connection.definitions = [{"function": {"name": "mcp__demo__fail", "parameters": {"type": "object"}}}]
    connection.tool_names = {"mcp__demo__fail"}
    connection._discovered = True
    manager.extension_connections[session["id"]] = connection
    result = await manager.execute_tool(session, tools, "mcp__demo__fail", {}, "rpc-error")
    assert "Provider error" in result
    assert len(json.dumps(result).encode()) <= 8000


def model_stream(content=None, call=None):
    delta = {"content": content} if call is None else {"tool_calls": [{"index": 0, "id": "write-call", "function": call}]}
    return httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": delta,
        "finish_reason": "stop" if call is None else "tool_calls"}]}) + "\n\ndata: [DONE]\n\n")


async def test_cancelled_after_hook_approval_preserves_completed_result_for_resume(runtime, monkeypatch):
    manager, session, tools = runtime
    session["permission_mode"] = "acceptEdits"
    manager.store.save(session)
    manager.extensions.update_config({"hooks": [{"id": "after", "enabled": True, "event": "after_tool", "command": "true"}]})
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return model_stream(call={"name": "write_file", "arguments": json.dumps({"path": "note.txt", "content": "saved"})})
        return model_stream("Resumed without repeating the edit.")

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(gateway)))
    manager.start(session["id"], "Write the file")
    async with asyncio.timeout(3):
        while not manager.pending:
            await asyncio.sleep(.01)
    await manager.stop(session["id"])
    saved = manager.store.get(session["id"])
    edit = next(event for event in saved["events"] if event.get("call_id") == "write-call")
    assert edit["state"] == "completed"
    assert (tools.root / "note.txt").read_text() == "saved"
    assert not manager.pending
    manager.start(session["id"], "Continue")
    await manager.tasks[session["id"]]
    resumed = next(request for request in reversed(requests) if request["stream"])
    recovered = next(message for message in resumed["messages"] if message.get("tool_call_id") == "write-call")
    assert recovered["content"] == edit["output"]


async def test_api_rejects_extension_changes_and_tests_while_mcp_call_is_active(runtime, monkeypatch):
    original, _, _ = runtime
    app = create_app(original.settings)
    manager = app.state.manager
    called = asyncio.Event()
    released = asyncio.Event()
    owner = []
    closed = []

    class Connection:
        definitions = [{"type": "function", "function": {"name": "mcp__demo__slow", "description": "Slow operation",
            "parameters": {"type": "object", "properties": {}}}}]

        async def discover(self):
            return self.definitions

        async def call(self, name, arguments):
            called.set()
            await released.wait()
            return {"output": "done", "is_error": False, "truncated": False}

    @asynccontextmanager
    async def turn():
        owner.append(asyncio.current_task())
        try:
            yield Connection()
        finally:
            closed.append(asyncio.current_task())

    manager.extensions.turn = turn
    client_class = httpx.AsyncClient
    request_count = 0

    async def gateway(request):
        nonlocal request_count
        request_count += 1
        return model_stream(call={"name": "mcp__demo__slow", "arguments": "{}"}) if request_count == 1 else model_stream("Done")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(gateway)))
    async with app.router.lifespan_context(app):
        async with client_class(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            sid = (await client.post("/api/sessions", json={"permission_mode": "bypassPermissions"})).json()["id"]
            await client.post(f"/api/sessions/{sid}/messages", json={"text": "Call the external tool"})
            await asyncio.wait_for(called.wait(), 3)
            assert (await client.put("/api/extensions", json={"servers": [], "hooks": []})).status_code == 409
            assert (await client.post("/api/extensions/test")).status_code == 409
            assert (await client.post(f"/api/sessions/{sid}/stop")).status_code == 200
            assert closed == owner
            assert sid not in manager.extension_connections
            assert (await client.put("/api/extensions", json={"servers": [], "hooks": []})).status_code == 200


async def test_stopping_parent_cancels_child_without_another_parent_model_request(runtime, monkeypatch):
    manager, session, _ = runtime
    session["permission_mode"] = "bypassPermissions"
    manager.store.save(session)
    child_started = asyncio.Event()
    parent_requests = []
    child_requests = []

    async def gateway(request):
        payload = json.loads(request.content)
        user_text = next(message["content"] for message in reversed(payload["messages"]) if message["role"] == "user")
        if user_text.startswith("Delegated task:"):
            child_requests.append(payload)
            child_started.set()
            await asyncio.Event().wait()
        parent_requests.append(payload)
        if len(parent_requests) == 1:
            return model_stream(call={"name": "delegate_task", "arguments": json.dumps({"task": "Inspect this project"})})
        return model_stream("This request must not run after Stop.")

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(gateway)))
    original_cancel = manager.delegates.cancel_parent

    async def cancel_with_yield(parent_id):
        await original_cancel(parent_id)
        # Leave the normal scheduler window open after child cleanup. The parent
        # must already be cancelled when that cleanup releases its awaited child.
        await asyncio.sleep(0)

    monkeypatch.setattr(manager.delegates, "cancel_parent", cancel_with_yield)
    manager.start(session["id"], "Delegate an inspection")
    await asyncio.wait_for(child_started.wait(), 3)
    child_id = manager.delegates.active[session["id"]]["child_session_id"]
    await asyncio.wait_for(manager.stop(session["id"]), 3)
    assert len(parent_requests) == 1
    assert len(child_requests) == 1
    assert manager.tasks[session["id"]].done()
    assert manager.tasks[child_id].done()
    assert manager.statuses[session["id"]] == manager.statuses[child_id] == "idle"
    assert not manager.delegates.active
