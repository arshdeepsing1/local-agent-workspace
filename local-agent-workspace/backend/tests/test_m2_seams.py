"""Seams between the M2 packages: new tools under the folder-access rules, and settings that mirror tool limits."""
import asyncio
import json

import pytest

from local_agent import config, jobs, tools as tools_module
from local_agent.agents import PATH_TOOLS, AgentManager
from local_agent.config import Settings
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
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    yield manager, store.create(settings.values), WorkspaceTools(str(project))
    store.db.close()


async def pending(manager):
    for _ in range(200):
        if manager.pending:
            return next(iter(manager.pending))
        await asyncio.sleep(.01)
    raise AssertionError("Expected an approval card")


@pytest.mark.parametrize("allow", [True, False])
async def test_find_files_on_an_external_path_asks_for_folder_access(setup, tmp_path, allow):
    manager, session, tools = setup
    downloads = tmp_path / "Downloads"
    (downloads / "nested").mkdir(parents=True)
    (downloads / "nested" / "report.csv").write_text("a\n")
    (downloads / "AGENTS.md").write_text("External guidance is never loaded.")
    assert "find_files" in PATH_TOOLS
    task = asyncio.create_task(manager.execute_tool(session, tools, "find_files", {"pattern": "*.csv", "path": str(downloads)}, "find"))
    session_id, event_id = await pending(manager)
    card = session["events"][-1]
    assert (card["name"], card["input"]) == ("access_directory", {"path": str(downloads)})
    manager.decide(session_id, event_id, allow)
    result = await task
    find = next(event for event in session["events"] if event.get("name") == "find_files")
    if allow:
        assert json.loads(result)["files"] == [str(downloads / "nested" / "report.csv")]
        assert session["allowed_directories"] == [str(downloads)]
    else:
        assert find["state"] == "rejected" and find["approval"] == "declined"
    assert session.get("instruction_directories", []) == []


async def test_find_files_in_the_workspace_records_its_instruction_directory(setup):
    manager, session, tools = setup
    (tools.root / "src" / "pkg").mkdir(parents=True)
    (tools.root / "src" / "pkg" / "a.py").write_text("x = 1\n")
    (tools.root / "src" / "AGENTS.md").write_text("Run the scoped checks in src.")
    await manager.execute_tool(session, tools, "find_files", {"pattern": "*.py", "path": "src"}, "find")
    assert session["events"][-1]["state"] == "completed" and session["instruction_directories"] == ["src"]


def test_settings_and_jobs_deny_the_same_command_environment():
    assert config.CREDENTIAL_ENV == jobs.CREDENTIAL_ENV
    assert config.DENIED_ENV == jobs.DENIED_ENV
    assert config.DENIED_ENV_PREFIXES == jobs.DENIED_ENV_PREFIXES


def test_settings_and_tools_agree_on_the_write_limit_bounds():
    assert config.MIN_WRITE_LIMIT_KB * 1000 == tools_module.MIN_WRITE_LIMIT
    assert config.MAX_WRITE_LIMIT_KB * 1000 == tools_module.MAX_WRITE_LIMIT
