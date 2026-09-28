import asyncio
import json
import os
from pathlib import Path

import pytest
import httpx
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager, visible_text
from local_agent.api import create_app
from local_agent.config import Settings, read_env
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


@pytest.fixture
def setup(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hello\n")
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(workspace), env_file="")
    store = Store(settings.state_dir / "tests.sqlite3")
    yield settings, store, WorkspaceTools(str(workspace))
    store.db.close()


def test_env_file_is_data_not_code(tmp_path):
    env = tmp_path / "secrets.txt"
    env.write_text("DBRICKS_URL = 'https://workspace.example'\nDBRICKS_TOKEN=\"$(touch never)\"\nexport NAME = value # comment\n")
    assert read_env(env) == {"DBRICKS_URL": "https://workspace.example", "DBRICKS_TOKEN": "$(touch never)", "NAME": "value"}


def test_state_directory_can_be_configured_in_dotenv(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    app_root.mkdir()
    state_dir = tmp_path / "private-state"
    (app_root / ".env").write_text(f"LOCAL_AGENT_STATE_DIR={state_dir}\n")
    monkeypatch.setattr("local_agent.config.APP_ROOT", app_root)

    settings = Settings()

    assert settings.state_dir == state_dir
    assert state_dir.is_dir()
    assert settings.path == state_dir / "settings.json"
    with TestClient(create_app(settings)):
        assert (state_dir / "conversations.sqlite3").is_file()


def test_paths_prevent_escape_and_symlink_access(setup, tmp_path):
    _, _, tools = setup
    secret = tmp_path / "outside.txt"
    secret.write_text("secret")
    (tools.root / "link.txt").symlink_to(secret)
    for path in ("../outside.txt", str(secret), "link.txt", ".env", ".env.production", ".git/config"):
        with pytest.raises(ValueError):
            tools.path(path)
    (tools.root / ".env.example").write_text("DBRICKS_TOKEN=placeholder")
    assert "placeholder" in tools.read_file(".env.example")


def test_search_never_reads_credentials(setup):
    _, _, tools = setup
    (tools.root / ".env").write_text("findthis secret")
    (tools.root / "public.txt").write_text("findthis public")
    result = tools.search_files("findthis")
    assert [(match["path"], match["line"]) for match in result["matches"]] == [("public.txt", 1)]
    assert "secret" not in json.dumps(result)


def test_edit_requires_unique_match_and_preview_does_not_write(setup):
    _, _, tools = setup
    args = {"path": "hello.txt", "old_text": "hello", "new_text": "goodbye"}
    assert "+goodbye" in tools.change("edit_file", args)
    assert tools.read_file("hello.txt") == "hello\n"
    tools.change("edit_file", args, apply=True)
    assert tools.read_file("hello.txt") == "goodbye\n"
    with pytest.raises(ValueError):
        tools.change("edit_file", args, apply=True)


@pytest.mark.parametrize("allow", [False, True])
async def test_write_waits_for_approval(setup, allow):
    settings, store, tools = setup
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    args = {"path": "new.txt", "content": "approved content"}
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", args, "call1"))
    await asyncio.sleep(0)
    assert not (tools.root / "new.txt").exists()
    event_id = next(iter(manager.pending))[1]
    manager.decide(session["id"], event_id, allow)
    await task
    assert (tools.root / "new.txt").exists() == allow
    assert session["events"][0]["state"] == ("completed" if allow else "rejected")


async def test_approval_cannot_overwrite_a_newer_disk_edit(setup):
    settings, store, tools = setup
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "hello.txt", "content": "model edit"}, "call1"))
    await asyncio.sleep(0)
    (tools.root / "hello.txt").write_text("user edited while waiting")
    manager.decide(session["id"], next(iter(manager.pending))[1], True)
    await task
    assert tools.read_file("hello.txt") == "user edited while waiting"
    assert session["events"][0]["state"] == "error"


async def test_shell_does_not_inherit_gateway_token(setup, monkeypatch):
    _, _, tools = setup
    monkeypatch.setenv("DBRICKS_TOKEN", "test-secret-value")
    monkeypatch.setenv("DATABRICKS_TOKEN", "second-secret")
    result = await tools.run_command('/usr/bin/env')
    assert result["exit_code"] == 0
    assert "test-secret-value" not in result["output"]
    assert "second-secret" not in result["output"]


async def test_cancelling_command_terminates_process_group(setup):
    _, _, tools = setup
    task = asyncio.create_task(tools.run_command('echo $$ > command.pid; sleep 30'))
    pid_file = tools.root / "command.pid"
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_cancel_unblocks_approval_and_allows_next_turn(setup):
    settings, store, tools = setup
    manager = AgentManager(store, settings)
    session = store.create(settings.values)

    async def run(session, prompt):
        await manager.execute_tool(session, tools, "write_file", {"path": "cancel.txt", "content": "no"}, "cancel")
    manager.run_databricks = run
    manager.start(session["id"], "make a file")
    for _ in range(10):
        await asyncio.sleep(0)
        if manager.pending:
            break
    assert manager.pending
    await manager.stop(session["id"])
    assert not manager.pending
    assert not (tools.root / "cancel.txt").exists()
    assert manager.statuses[session["id"]] == "idle"
    assert next(e for e in store.get(session["id"])["events"] if e["type"] == "tool")["state"] == "cancelled"


def test_pending_actions_cancelled_after_restart(setup):
    settings, store, _ = setup
    session = store.create(settings.values)
    session["events"].append({"type": "tool", "state": "pending"})
    store.save(session)
    AgentManager(store, settings)
    assert store.get(session["id"])["events"][0]["state"] == "cancelled"


def test_api_requires_local_token_and_rejects_foreign_origin(setup):
    settings, _, _ = setup
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/sessions").status_code == 403
        assert client.get("/api/bootstrap", headers={"Origin": "https://evil.example"}).status_code == 403
        bootstrap = client.get("/api/bootstrap").json()
        assert "DBRICKS_TOKEN" not in json.dumps(bootstrap)
        headers = {"X-Local-Token": bootstrap["token"]}
        session = client.post("/api/sessions", headers=headers).json()
        assert client.get("/api/sessions", headers=headers).json()[0]["id"] == session["id"]
        response = client.get("/api/file", params={"path": "../outside.txt"}, headers=headers)
        assert response.status_code == 400
        with client.websocket_connect(f'/api/sessions/{session["id"]}/stream', subprotocols=["local-workspace", bootstrap["token"]]) as socket:
            assert socket.receive_json()["type"] == "snapshot"


def test_editor_detects_concurrent_write(setup):
    settings, _, tools = setup
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/bootstrap").json()["token"]
        headers = {"X-Local-Token": token}
        response = client.put("/api/file", headers=headers, json={"path": "hello.txt", "content": "bad", "original": "stale"})
        assert response.status_code == 409
        assert tools.read_file("hello.txt") == "hello\n"


def test_provider_reasoning_is_not_rendered_as_answer():
    assert visible_text([{"type": "reasoning", "summary": "private"}, {"type": "text", "text": "answer"}]) == "answer"
    assert visible_text("plain answer") == "plain answer"


async def test_same_greeting_sessions_have_separate_model_context_after_resume(setup, monkeypatch):
    settings, store, _ = setup
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        await asyncio.sleep(0)  # Let both conversations run concurrently.
        chunk = {"choices": [{"delta": {"content": "Acknowledged."}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(gateway)))
    first, second = store.create(settings.values), store.create(settings.values)
    manager = AgentManager(store, settings)
    for session in (first, second):
        manager.start(session["id"], "HI")
    await asyncio.gather(*manager.tasks.values())
    assert first["id"] != second["id"]
    for session in (first, second):
        saved = store.get(session["id"])
        assert saved["title"] == "HI"
        assert [m["content"] for m in saved["wire"] if m["role"] == "user"] == ["HI"]
        assert saved["allowed_directories"] == []

    manager.start(first["id"], "Remember alpha-only-4829.")
    await manager.tasks[first["id"]]
    manager.start(second["id"], "What have I said in this chat?")
    await manager.tasks[second["id"]]
    assert "alpha-only-4829" not in json.dumps(requests[-1])
    resumed = next(request for request in reversed(requests) if request["stream"])
    assert "alpha-only-4829" not in json.dumps(resumed)
    assert [m["content"] for m in resumed["messages"] if m["role"] == "user"] == [
        "HI", "What have I said in this chat?"]

    # Reopening the persisted session restores only that session's context.
    manager = AgentManager(store, settings)
    manager.start(first["id"], "Recall my marker.")
    await manager.tasks[first["id"]]
    resumed = next(request for request in reversed(requests) if request["stream"])
    assert [m["content"] for m in resumed["messages"] if m["role"] == "user"] == [
        "HI", "Remember alpha-only-4829.", "Recall my marker."]
    assert len([e for e in store.get(second["id"])["events"] if e["type"] == "user"]) == 2
