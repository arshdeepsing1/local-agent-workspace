"""The handoff commands and the two files a handoff writes: a brief and a session log under handoffs/HANDOFF/."""
import json
from pathlib import Path

import pytest

from local_agent.activity import ACTIVITY_MARKER
from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.store import Store
from local_agent.tools import WorkspaceTools

SHIPPED_SKILLS = Path(__file__).resolve().parents[2] / "skills"
LOG = "handoffs/HANDOFF/demo-handoff-v{}-2026-11-14-first-session.md"
BRIEF = "handoffs/demo-00-context.md"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "handoff.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["permission_mode"] = "acceptEdits"
    yield manager, session, project
    store.db.close()


async def tool(manager, session, name, arguments, call_id="call"):
    tools = manager.session_tools(session)
    return await manager.execute_tool(session, tools, name, arguments, call_id)


def log_body(version):
    return f"# Session log v{version}\n\n## Commands\n{ACTIVITY_MARKER}\n\nEnd of log.\n"


@pytest.mark.parametrize("command, selected", [
    ("/handoff", ["handoff"]), ("/resume", ["resume"]), ("/migrate-handoffs", ["migrate-handoffs"]),
    ("/handoff write the notes", ["handoff"]), ("/read-handoff", []),
])
def test_the_three_handoff_commands_select_their_shipped_skills(runtime, command, selected):
    manager, session, project = runtime
    manager.extensions.app_skills = SHIPPED_SKILLS
    tools = WorkspaceTools(str(project))
    assert manager.requested_skills(tools, command, session) == selected


async def test_a_session_log_is_written_under_HANDOFF_and_gets_the_activity_log(runtime):
    manager, session, project = runtime
    body = log_body("01")
    output = await tool(manager, session, "write_file", {"path": LOG.format("01"), "content": body}, "w1")
    assert "error" not in output.lower()
    written = project / LOG.format("01")
    assert written.parent == project / "handoffs" / "HANDOFF" and written.is_file()
    result = await tool(manager, session, "insert_activity_log", {"path": LOG.format("01")}, "l1")
    assert json.loads(result)["placement"] == "marker"
    text = written.read_text()
    assert ACTIVITY_MARKER not in text
    assert text.startswith("# Session log v01\n\n## Commands\n")
    assert text.rstrip().endswith("End of log.")


async def test_writing_over_an_existing_log_is_refused(runtime):
    manager, session, project = runtime
    await tool(manager, session, "write_file", {"path": LOG.format("01"), "content": log_body("01")}, "w1")
    first = (project / LOG.format("01")).read_bytes()
    session["file_views"] = {}

    output = await tool(manager, session, "write_file", {"path": LOG.format("01"), "content": "# Other chat\n"}, "w2")
    assert "read_file" in output
    assert (project / LOG.format("01")).read_bytes() == first


async def test_the_activity_log_in_a_session_log_redacts_secrets(runtime):
    manager, session, project = runtime
    secret = "ghp" + "_abcdefghijklmnopqrstuvwx123456"
    session["events"] = [{
        "id": "c1", "type": "tool", "name": "run_command", "created": 1790238001.0, "state": "completed",
        "call_id": "call-c1", "input": {"command": f"export GITHUB_TOKEN={secret} && git push"},
        "output": json.dumps({"job_id": "job-c1", "state": "completed", "exit_code": 0}), "job_id": "job-c1"}]
    await tool(manager, session, "write_file", {"path": LOG.format("01"), "content": log_body("01")}, "w1")
    await tool(manager, session, "insert_activity_log", {"path": LOG.format("01")}, "l1")
    text = (project / LOG.format("01")).read_text()
    assert "git push" in text and secret not in text and "[REDACTED]" in text
