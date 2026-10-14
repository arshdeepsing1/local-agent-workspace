"""How long an approval card waits, and how an unanswered card is reported."""
import asyncio
import json

import pytest

WAIT_FOR = asyncio.wait_for  # The real one; the tests below replace it in the app module.
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.portability import export_bundle
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "approvals.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    tools = WorkspaceTools(str(project))
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    yield manager, session, project, tools
    store.db.close()


def expire_waits_of(monkeypatch, seconds):
    """Make an approval wait of exactly `seconds` time out at once; other waits are unchanged."""
    original = asyncio.wait_for
    timeouts = []

    async def wait_for(awaitable, timeout=None):
        timeouts.append(timeout)
        if timeout == seconds:
            raise TimeoutError
        return await original(awaitable, timeout)
    monkeypatch.setattr("local_agent.agents.asyncio.wait_for", wait_for)
    return timeouts


async def bounded(call):
    """Fail within seconds instead of hanging if an approval wait regresses."""
    return await WAIT_FOR(call, 5)


async def answer_when_pending(manager, session, allowed):
    while not manager.pending:
        await asyncio.sleep(0.01)
    (session_id, event_id), = manager.pending
    manager.decide(session_id, event_id, allowed)


async def test_by_default_an_approval_waits_an_hour(runtime, monkeypatch):
    manager, session, _, tools = runtime
    assert manager.settings.values["approval_timeout_minutes"] == 60
    timeouts = expire_waits_of(monkeypatch, 3600)
    output = await bounded(manager.execute_tool(session, tools, "write_file", {"path": "a.txt", "content": "x"}, "call"))
    assert timeouts == [3600] and "did not answer" in output


async def test_with_no_limit_an_approval_waits_until_it_is_answered(runtime, monkeypatch):
    manager, session, project, tools = runtime
    manager.settings.values["approval_timeout_minutes"] = 0
    timeouts = expire_waits_of(monkeypatch, 300)
    (project / "note.txt").write_text("before")
    manager.remember_file(session, project / "note.txt")  # As if read: overwriting needs a read first.
    run = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "note.txt", "content": "after"}, "call"))
    await asyncio.sleep(0.05)
    assert not run.done()
    event = next(item for item in session["events"] if item.get("call_id") == "call")
    assert event["state"] == "pending" and "approval_expires" not in event
    await answer_when_pending(manager, session, True)
    await run
    assert None in timeouts and 300 not in timeouts
    assert (event["state"], event["approval"]) == ("completed", "approved")
    assert (project / "note.txt").read_text() == "after"


async def test_a_declined_action_is_reported_as_declined(runtime):
    manager, session, project, tools = runtime
    run = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "new.txt", "content": "x"}, "call"))
    await answer_when_pending(manager, session, False)
    output = await run
    event = session["events"][-1]
    assert (event["state"], event["approval"]) == ("rejected", "declined")
    assert output.startswith("User declined this action.") and not (project / "new.txt").exists()


async def test_an_unanswered_action_expires_after_the_configured_wait(runtime, monkeypatch):
    manager, session, project, tools = runtime
    manager.settings.values["approval_timeout_minutes"] = 30
    expire_waits_of(monkeypatch, 1800)
    output = await bounded(manager.execute_tool(session, tools, "run_command", {"command": "touch ran.txt"}, "call"))
    event = session["events"][-1]
    assert (event["state"], event["approval"]) == ("rejected", "expired")
    assert event["approval_expires"] > event["created"] + 1790
    assert "did not answer this approval request in time" in output and "declined" not in output
    assert not (project / "ran.txt").exists()
    # The outcome survives an export of the conversation.
    bundle = export_bundle([session], session["id"])
    exported = next(item for item in bundle["sessions"][0]["events"] if item.get("call_id") == "call")
    assert exported["approval"] == "expired" and exported["approval_expires"] == event["approval_expires"]


async def test_an_unanswered_folder_access_request_is_not_called_a_decline(runtime, monkeypatch, tmp_path):
    manager, session, _, tools = runtime
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.csv").write_text("a,b\n")
    manager.settings.values["approval_timeout_minutes"] = 1
    expire_waits_of(monkeypatch, 60)
    output = await bounded(manager.execute_tool(session, tools, "read_file", {"path": str(outside / "data.csv")}, "call"))
    access = next(item for item in session["events"] if item.get("name") == "access_directory")
    assert (access["approval"], access["output"]) == ("expired", "Folder access was not answered in time.")
    read = next(item for item in session["events"] if item.get("call_id") == "call")
    assert (read["state"], read["approval"]) == ("rejected", "expired")
    assert "did not answer the folder access request in time" in output
    assert not session.get("allowed_directories")


async def test_a_declined_folder_access_request_marks_the_file_tool_declined(runtime, tmp_path):
    manager, session, _, tools = runtime
    outside = tmp_path / "outside"
    outside.mkdir()
    run = asyncio.create_task(manager.execute_tool(session, tools, "list_files", {"path": str(outside)}, "call"))
    await answer_when_pending(manager, session, False)
    assert (await run).startswith("User declined folder access.")
    listing = next(item for item in session["events"] if item.get("call_id") == "call")
    assert (listing["state"], listing["approval"]) == ("rejected", "declined")


async def test_unanswered_mcp_calls_and_hooks_are_not_called_declined(runtime, monkeypatch):
    manager, session, project, tools = runtime
    manager.settings.values["approval_timeout_minutes"] = 5
    expire_waits_of(monkeypatch, 300)
    called = []

    class Connection:
        tool_names = {"mcp__demo__echo"}
        definitions = [{"function": {"name": "mcp__demo__echo", "parameters": {"type": "object"}}}]

        async def call(self, name, arguments):
            called.append(arguments)
            return {"ok": True}
    manager.extension_connections[session["id"]] = Connection()
    output = await bounded(manager.execute_tool(session, tools, "mcp__demo__echo", {"value": "hi"}, "mcp"))
    assert "did not answer this approval request in time" in output and "declined" not in output and not called
    assert session["events"][-1]["approval"] == "expired"

    manager.extensions.update_config({"hooks": [{"id": "check", "enabled": True, "event": "before_tool", "command": "true"}]})
    session["permission_mode"] = "acceptEdits"
    output = await bounded(manager.execute_tool(session, tools, "write_file", {"path": "note.txt", "content": "x"}, "edit"))
    assert json.loads(output)["message"].endswith("Hook approval was not answered in time.") and not (project / "note.txt").exists()


def test_the_wait_is_a_validated_setting(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    with TestClient(create_app(settings)) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["settings"]["approval_timeout_minutes"] == 60
        headers = {"X-Local-Token": bootstrap["token"]}
        body = {"workspace": str(tmp_path), "model": "test-model", "env_file": ""}
        for bad in (-1, 1441, 2.5, True, "30"):
            assert client.put("/api/settings", headers=headers, json={**body, "approval_timeout_minutes": bad}).status_code == 422
        saved = client.put("/api/settings", headers=headers, json={**body, "approval_timeout_minutes": 45}).json()
        assert saved["approval_timeout_minutes"] == 45
        # Saving without the field keeps the earlier choice.
        assert client.put("/api/settings", headers=headers, json=body).json()["approval_timeout_minutes"] == 45
    assert Settings(tmp_path / "state").values["approval_timeout_minutes"] == 45
    with pytest.raises(ValueError, match="Approval wait"):
        settings.update({"approval_timeout_minutes": False})


# Cards added with permission rules (subagents, stopping jobs, protected files) wait the same way.

async def test_subagent_job_and_protected_file_cards_expire_after_the_configured_wait(runtime, monkeypatch):
    manager, session, project, tools = runtime
    tools.skill_signature = ""
    manager.settings.values["approval_timeout_minutes"] = 10
    expire_waits_of(monkeypatch, 600)
    started = []

    async def worker(child, prompt):
        started.append(prompt)
    manager.run_databricks = worker
    output = await bounded(manager.execute_tool(session, tools, "delegate_task", {"task": "Count files"}, "delegate"))
    assert "did not answer this approval request in time" in output and started == []
    job = await manager.jobs.start(command="sleep 30", workspace=str(project), session_id=session["id"], background=True)
    try:
        output = await bounded(manager.execute_tool(session, tools, "stop_job", {"job_id": job["id"]}, "stop"))
        assert "did not answer this approval request in time" in output
        assert manager.jobs.get(job["id"])["state"] == "running"
    finally:
        await manager.jobs.stop(job["id"])
        await manager.jobs.shutdown()
    session["permission_mode"] = "acceptEdits"
    output = await bounded(manager.execute_tool(session, tools, "write_file", {"path": "AGENTS.md", "content": "x"}, "protected"))
    assert "did not answer this approval request in time" in output and not (project / "AGENTS.md").exists()
    for call_id in ("delegate", "stop", "protected"):
        event = next(item for item in session["events"] if item.get("call_id") == call_id)
        assert (event["state"], event["approval"]) == ("rejected", "expired"), call_id
        assert event["approval_expires"] > event["created"] + 590


async def test_a_card_resolved_by_a_mode_change_returns_before_its_expiry(runtime):
    manager, session, project, tools = runtime
    manager.settings.values["approval_timeout_minutes"] = 60
    run = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "a.txt", "content": "x"}, "call"))
    while not manager.pending:
        await asyncio.sleep(0.01)
    await manager.change_mode(session, "acceptEdits")
    await bounded(run)
    event = next(item for item in session["events"] if item.get("call_id") == "call")
    assert (event["approval"], event["approved_by_mode"]) == ("approved", "acceptEdits")
    assert (project / "a.txt").read_text() == "x"


async def test_an_edited_card_keeps_its_expiry(runtime):
    manager, session, project, tools = runtime
    manager.settings.values["approval_timeout_minutes"] = 15
    run = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "a.txt", "content": "x"}, "call"))
    while not manager.pending:
        await asyncio.sleep(0.01)
    (session_id, event_id), = manager.pending
    event = next(item for item in session["events"] if item["id"] == event_id)
    expires = event["approval_expires"]
    await manager.revise_card(session_id, event_id, {"content": "edited"})
    assert event["approval_expires"] == expires and event["edited"] is True
    manager.decide(session_id, event_id, True)
    output = await bounded(run)
    assert output.startswith("[The user edited the file content before approving it.")
    assert (project / "a.txt").read_text() == "edited"
