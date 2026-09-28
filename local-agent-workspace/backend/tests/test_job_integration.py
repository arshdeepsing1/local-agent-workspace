import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager, public_session
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://fake.example", "synthetic-secret-token")
    return settings


@pytest.fixture
async def runtime(settings):
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    manager.live[session["id"]] = session
    yield manager, session, WorkspaceTools(session["workspace"])
    await manager.jobs.shutdown()
    store.db.close()


@pytest.mark.parametrize("mode", ["plan", "manual", "acceptEdits", "auto"])
async def test_background_commands_obey_permissions(runtime, mode):
    manager, session, tools = runtime
    session["permission_mode"] = mode
    manager.approve = AsyncMock(return_value=False)
    output = await manager.execute_tool(session, tools, "run_command", {
        "command": "touch must-not-exist", "background": True}, "denied")
    assert not (tools.root / "must-not-exist").exists()
    assert not manager.jobs.list(session_id=session["id"])
    assert "read-only" in output if mode == "plan" else "declined" in output
    if mode == "plan":
        manager.approve.assert_not_awaited()
    else:
        assert "Background job" in session["events"][-1]["preview"]


async def test_foreground_streams_before_completion_and_returns_paged_output(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    first_output = asyncio.Event()
    async def broadcast(sid, data):
        if data.get("event", {}).get("output") == "first\n":
            first_output.set()
    manager.broadcast = broadcast
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {
        "command": "printf 'first\\n'; sleep .3; printf 'last\\n'"}, "stream"))
    await asyncio.wait_for(first_output.wait(), 2)
    assert not task.done()
    result = json.loads(await task)
    assert result["output"] == "first\nlast\n"
    assert result["state"] == "completed"
    page = json.loads(await manager.execute_tool(session, tools, "get_job_output", {
        "job_id": result["job_id"], "offset": 6, "max_chars": 3}, "page"))
    assert page["output"] == "las"
    assert page["next_offset"] == 9


async def test_cancel_foreground_stops_job_but_completed_background_launch_survives(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    background = json.loads(await manager.execute_tool(session, tools, "run_command", {
        "command": "sleep 30", "background": True}, "background"))
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {
        "command": "sleep 30"}, "foreground"))
    async with asyncio.timeout(2):
        while session["events"][-1].get("call_id") != "foreground" or not session["events"][-1].get("job_id"):
            await asyncio.sleep(.01)
    foreground_id = session["events"][-1]["job_id"]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert manager.jobs.get(foreground_id)["state"] == "cancelled"
    assert manager.jobs.get(background["job_id"])["state"] == "running"
    session["permission_mode"] = "plan"
    stopped = json.loads(await manager.execute_tool(session, tools, "stop_job", {
        "job_id": background["job_id"]}, "stop"))
    assert stopped["state"] == "cancelled"


async def test_model_cannot_inspect_or_stop_another_conversations_job(runtime):
    manager, session, tools = runtime
    other = manager.store.create(manager.settings.values)
    job = await manager.jobs.start("sleep 30", session["workspace"], session_id=other["id"], background=True)
    for name in ("get_job_output", "stop_job"):
        result = await manager.execute_tool(session, tools, name, {"job_id": job["id"]}, name)
        assert "not found in this conversation" in result
    assert manager.jobs.get(job["id"])["state"] == "running"
    assert json.loads(await manager.execute_tool(session, tools, "list_jobs", {}, "list")) == {"jobs": [], "next_offset": None}


def test_public_session_does_not_expose_canonical_command_history():
    session = {"id": "chat", "events": [], "wire": [], "command_jobs": [{"command": "private"}]}
    assert "command_jobs" not in public_session(session)


async def test_unicode_job_output_pages_bound_serialized_size_without_losing_characters(runtime):
    manager, _, _ = runtime
    job = {"id": "unicode", "state": "completed", "exit_code": 0, "output": "😀\n" * 3000,
           "truncated": False, "background": False}
    parts, offset = [], 0
    while True:
        page = manager.job_output(job, offset, 8000)
        assert len(json.dumps(page, indent=2).encode()) <= 8000
        parts.append(page["output"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert "".join(parts) == job["output"]


def test_job_api_is_scoped_validated_and_persists_on_refresh(settings):
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        first = client.post("/api/sessions", headers=headers).json()["id"]
        second = client.post("/api/sessions", headers=headers).json()["id"]
        query = f"?session_id={first}"
        assert client.post("/api/jobs" + query, json={"command": "pwd"}).status_code == 403
        for values in ({"timeout_seconds": 0}, {"timeout_seconds": 3601}, {"max_output_bytes": 100}, {"background": "yes"}):
            assert client.post("/api/jobs" + query, headers=headers, json={"command": "pwd", **values}).status_code == 422
        job = client.post("/api/jobs" + query, headers=headers, json={
            "command": "printf 'early\\n'; sleep 30", "background": True}).json()
        job_url = f"/api/jobs/{job['id']}"
        for suffix in ("", f"?session_id={second}"):
            assert client.get(job_url + suffix, headers=headers).status_code == 404
            assert client.post(job_url + "/stop" + suffix, headers=headers).status_code == 404
        deadline = time.monotonic() + 2
        while True:
            snapshot = client.get(job_url + query, headers=headers).json()
            if "early" in snapshot["output"]:
                break
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert snapshot["state"] == "running"
        summaries = client.get("/api/jobs" + query, headers=headers).json()
        assert summaries[0]["id"] == job["id"] and "output" not in summaries[0]
        stopped = client.post(job_url + "/stop" + query, headers=headers).json()
        assert stopped["state"] == "cancelled"
        assert client.get(job_url + query, headers=headers).json()["output"] == stopped["output"]
        assert client.delete(f"/api/sessions/{first}", headers=headers).status_code == 200
        assert client.app.state.manager.jobs.get(job["id"]) is None


def test_app_shutdown_stops_background_jobs(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        job = client.post("/api/jobs", headers=headers, json={"command": "sleep 30", "background": True}).json()
    with TestClient(create_app(settings)) as restarted:
        headers = {"X-Local-Token": restarted.get("/api/bootstrap").json()["token"]}
        restored = restarted.get(f"/api/jobs/{job['id']}", headers=headers).json()
        assert restored["state"] == "cancelled"
