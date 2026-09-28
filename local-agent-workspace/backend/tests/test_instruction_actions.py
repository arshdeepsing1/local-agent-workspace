import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.instructions import MAX_INSTRUCTION_BYTES, load_project_instructions
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


@pytest.fixture
def setup(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("local_agent.config.APP_ROOT", project)
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://fake.example", "fake-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    tools = WorkspaceTools(str(project))
    yield manager, session, tools
    store.db.close()


def mark_guidance_sent(tools, session):
    guidance = load_project_instructions(tools, session.get("instruction_directories", []))
    tools.instruction_signature = (guidance["text"], guidance["warnings"])


async def test_nested_read_defers_same_batch_command_before_approval(setup, monkeypatch):
    manager, session, tools = setup
    (tools.root / "src").mkdir()
    (tools.root / "src" / "AGENTS.md").write_text("Run the scoped checks before changing this directory.")
    (tools.root / "src" / "note.txt").write_text("file contents")
    mark_guidance_sent(tools, session)
    approve = AsyncMock(return_value=True)
    monkeypatch.setattr(manager, "approve", approve)

    read = json.loads(await manager.execute_tool(session, tools, "read_file", {"path": "src/note.txt"}, "read"))
    assert read["content"] == "1: file contents"
    result = await manager.execute_tool(session, tools, "run_command", {"command": "touch src/changed"}, "command")
    assert "Review the refreshed project guidance" in result
    assert "no command ran" in result
    assert session["events"][-1]["state"] == "rejected"
    approve.assert_not_awaited()
    assert not (tools.root / "src" / "changed").exists()
    assert manager.jobs.list(session_id=session["id"]) == []

    mark_guidance_sent(tools, session)
    await manager.execute_tool(session, tools, "run_command", {"command": "touch src/changed"}, "retry")
    approve.assert_awaited_once()
    assert (tools.root / "src" / "changed").exists()
    assert session["events"][-1]["state"] == "completed"


@pytest.mark.parametrize("new_guidance", ["Updated root guidance", "x" * (MAX_INSTRUCTION_BYTES + 1)])
async def test_command_approval_rechecks_changed_or_omitted_guidance(setup, monkeypatch, new_guidance):
    manager, session, tools = setup
    instructions = tools.root / "AGENTS.md"
    instructions.write_text("Original root guidance")
    mark_guidance_sent(tools, session)
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "touch changed"}, "command"))
    try:
        async with asyncio.timeout(2):
            while not manager.pending:
                await asyncio.sleep(.01)
        instructions.write_text(new_guidance)
        sid, event_id = next(iter(manager.pending))
        manager.decide(sid, event_id, True)
        result = await task
        assert "Project instructions changed while preparing this action" in result
        assert "no command ran" in result
        assert session["events"][-1]["state"] == "error"
        assert not manager.pending
        assert not (tools.root / "changed").exists()
        assert manager.jobs.list(session_id=session["id"]) == []
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("mode", ["acceptEdits", "bypassPermissions"])
async def test_external_file_access_does_not_load_external_instructions(setup, tmp_path, monkeypatch, mode):
    manager, session, _ = setup
    external = tmp_path / "external"
    external.mkdir()
    instructions = external / "AGENTS.md"
    instructions.write_text("External guidance must not be loaded.")
    session["permission_mode"] = mode
    tools = WorkspaceTools(session["workspace"], allowed_directories=[str(external)] if mode == "acceptEdits" else (),
                           unrestricted=mode == "bypassPermissions")
    mark_guidance_sent(tools, session)
    read_text = Path.read_text

    def read_without_external_guidance(self, *args, **kwargs):
        assert self != instructions, "An external folder grant must not load its project instructions"
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_without_external_guidance)
    target = external / "note.txt"
    await manager.execute_tool(session, tools, "write_file", {"path": str(target), "content": "allowed edit"}, "write")
    assert target.read_text() == "allowed edit"
    assert session.get("instruction_directories", []) == []
    assert session["events"][-1]["state"] == "completed"
    assert not manager.pending
