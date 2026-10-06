"""How long an approval card waits, and how an unanswered card is reported."""
import asyncio

import pytest
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


async def answer_when_pending(manager, session, allowed):
    while not manager.pending:
        await asyncio.sleep(0.01)
    (session_id, event_id), = manager.pending
    manager.decide(session_id, event_id, allowed)


async def test_by_default_an_approval_waits_until_it_is_answered(runtime, monkeypatch):
    manager, session, project, tools = runtime
    assert manager.settings.values["approval_timeout_minutes"] == 0
    timeouts = expire_waits_of(monkeypatch, 300)
    (project / "note.txt").write_text("before")
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
    output = await manager.execute_tool(session, tools, "run_command", {"command": "touch ran.txt"}, "call")
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
    output = await manager.execute_tool(session, tools, "read_file", {"path": str(outside / "data.csv")}, "call")
    access = next(item for item in session["events"] if item.get("name") == "access_directory")
    assert (access["approval"], access["output"]) == ("expired", "Folder access was not answered in time.")
    assert "did not answer the folder access request in time" in output
    assert not session.get("allowed_directories")


def test_the_wait_is_a_validated_setting(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    with TestClient(create_app(settings)) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["settings"]["approval_timeout_minutes"] == 0
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
