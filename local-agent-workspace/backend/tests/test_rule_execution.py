"""End to end: commands that a rule or the read-only list allows run without a shell (needs the command sandbox)."""
import asyncio
import inspect
import json
import shutil
import subprocess

import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.store import Store
from local_agent.tools import WorkspaceTools

pytestmark = pytest.mark.skipif('argv' not in inspect.signature(AgentManager.command_tool).parameters,
                                reason='needs m3-command-sandbox')


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("hello\n")
    if shutil.which("git"):
        for command in (["git", "init", "-q"], ["git", "add", "README.md"],
                        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "init"]):
            subprocess.run(command, cwd=project, check=True, capture_output=True)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "exec.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    tools = WorkspaceTools(str(project))
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    tools.skill_signature = ""

    def no_shell(*args, **kwargs):
        raise AssertionError("This command must not run through a shell.")
    monkeypatch.setattr("local_agent.jobs.shell_argv", no_shell)
    calls = []
    original = manager.command_tool

    async def command_tool(session, event, arguments, **options):
        calls.append(options)
        return await original(session, event, arguments, **options)
    manager.command_tool = command_tool
    yield manager, session, project, tools, calls
    store.db.close()


async def card(manager):
    for _ in range(300):
        if manager.pending:
            return next(iter(manager.pending))
        await asyncio.sleep(0.01)
    raise AssertionError("Expected an approval card")


async def test_rule_allowed_and_read_only_commands_run_without_a_shell(runtime):
    manager, session, project, tools, calls = runtime
    manager.rules.add("project", session["workspace"], {"behavior": "allow", "tool": "run_command", "argv": ["ls", "-1", "."]})
    result = json.loads(await manager.execute_tool(session, tools, "run_command", {"command": "ls -1 ."}, "rule"))
    assert "README.md" in result["output"] and calls[-1] == {"argv": ["ls", "-1", "."], "read_only": False}
    event = next(item for item in session["events"] if item.get("call_id") == "rule")
    assert event["without_shell"] is True and event["rule_scope"] == "project"
    session["permission_mode"] = "auto"
    result = json.loads(await manager.execute_tool(session, tools, "run_command", {"command": "pwd"}, "pwd"))
    assert result["output"].strip() == str(project.resolve()) and calls[-1] == {"argv": ["pwd"], "read_only": True}


async def test_a_card_approved_command_still_uses_the_shell(runtime):
    manager, session, _, tools, calls = runtime
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "ls -1 ."}, "card"))
    manager.decide(*(await card(manager)), True)
    output = await task
    assert "must not run through a shell" in output and calls == [{}]


@pytest.mark.skipif(not shutil.which("git"), reason="git is required")
async def test_cards_resolved_by_a_mode_or_rule_change_run_like_the_mode_or_rule(runtime):
    manager, session, _, tools, calls = runtime
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "git status"}, "status"))
    await card(manager)
    await manager.change_mode(session, "auto")
    result = json.loads(await asyncio.wait_for(task, 10))
    assert result["exit_code"] == 0 and calls[-1] == {"argv": ["git", "status"], "read_only": True}
    session["permission_mode"] = "manual"
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "ls -a"}, "ls"))
    await card(manager)
    manager.rules.add("project", session["workspace"], {"behavior": "allow", "tool": "run_command", "argv": ["ls", "-a"]})
    await manager.rules_changed("project", session)
    result = json.loads(await asyncio.wait_for(task, 10))
    assert ".git" in result["output"] and calls[-1] == {"argv": ["ls", "-a"], "read_only": True}
    manager.rules.add("user", session["workspace"], {"behavior": "allow", "tool": "run_command", "argv": ["git", "status", "-s"]})
    result = json.loads(await manager.execute_tool(session, tools, "run_command", {"command": "git status -s"}, "short"))
    assert result["exit_code"] == 0 and calls[-1] == {"argv": ["git", "status", "-s"], "read_only": True}


async def test_a_rule_allowed_command_that_is_not_installed(runtime):
    manager, session, _, tools, _ = runtime
    session["allow_rules"] = [{"behavior": "allow", "tool": "run_command", "argv": ["no-such-tool-x1", "run"]}]
    output = await manager.execute_tool(session, tools, "run_command", {"command": "no-such-tool-x1 run"}, "missing")
    assert "not found on PATH" in output
