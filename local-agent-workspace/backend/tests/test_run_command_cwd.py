"""run_command's cwd: approved folders run there, others ask for folder access, and rules follow the folder (M6i)."""
import asyncio
import json
import shutil
import subprocess

import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.jobs import JobManager
from local_agent.store import Store
from local_agent.tools import TOOL_DEFINITIONS

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git is required")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "README.md").write_text("hello\n")
    granted = tmp_path / "granted"
    granted.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "cwd.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["allowed_directories"] = [str(granted.resolve())]
    yield manager, session, project.resolve(), granted.resolve(), outside.resolve()
    store.db.close()


def chat_tools(manager, session):
    tools = manager.session_tools(session)
    guidance = load_project_instructions(tools)
    tools.instruction_signature, tools.skill_signature = (guidance["text"], guidance["warnings"]), ""
    return tools


async def card(manager, seen=()):
    for _ in range(300):
        waiting = [key for key in manager.pending if key not in seen]
        if waiting:
            return waiting[0]
        await asyncio.sleep(0.01)
    raise AssertionError("Expected an approval card")


def event_of(session, key):
    return next(item for item in session["events"] if item["id"] == key[1])


async def run(manager, session, command, cwd, call_id="c", **extra):
    arguments = {"command": command, "cwd": cwd, **extra}
    return await manager.execute_tool(session, chat_tools(manager, session), "run_command", arguments, call_id)


def test_the_tool_definition_has_a_short_cwd():
    definition = next(item["function"] for item in TOOL_DEFINITIONS if item["function"]["name"] == "run_command")
    cwd = definition["parameters"]["properties"]["cwd"]
    assert cwd["type"] == "string" and len(cwd["description"]) <= 100 and "cd" in cwd["description"]
    assert definition["parameters"]["required"] == ["command"]


@pytest.mark.parametrize("folder", ["src", "granted", "code"])
async def test_approved_folders_run_there_after_the_command_card(runtime, folder):
    manager, session, project, granted, outside = runtime
    target = {"src": project / "src", "granted": granted, "code": outside}[folder]
    if folder == "code":
        manager.settings.values["coding_dir"] = str(outside)
    task = asyncio.create_task(run(manager, session, "pwd -P; echo ran", "src" if folder == "src" else str(target)))
    key = await card(manager)
    event = event_of(session, key)
    assert event["name"] == "run_command" and f"Runs in: {target}" in event["preview"]
    assert event["command_cwd"] == str(target)
    manager.decide(*key, True)
    result = json.loads(await asyncio.wait_for(task, 10))
    assert result["output"] == f"{target}\nran\n"
    job = manager.jobs.get(result["job_id"])
    assert job["cwd"] == str(target) and job["workspace"] == str(project)


async def test_another_folder_asks_for_folder_access_first(runtime):
    manager, session, project, granted, outside = runtime
    task = asyncio.create_task(run(manager, session, "pwd -P", str(outside)))
    access = await card(manager)
    assert event_of(session, access)["name"] == "access_directory"
    assert event_of(session, access)["input"] == {"path": str(outside)}
    manager.decide(*access, True)
    command = await card(manager, {access})
    assert event_of(session, command)["name"] == "run_command"
    manager.decide(*command, True)
    result = json.loads(await asyncio.wait_for(task, 10))
    assert result["output"].strip() == str(outside) and str(outside) in session["allowed_directories"]


async def test_a_declined_folder_access_runs_nothing(runtime):
    manager, session, project, granted, outside = runtime
    task = asyncio.create_task(run(manager, session, "touch made.txt", str(outside), "d"))
    access = await card(manager)
    manager.decide(*access, False)
    output = await asyncio.wait_for(task, 10)
    assert "declined folder access" in output and not (outside / "made.txt").exists()
    event = next(item for item in session["events"] if item.get("call_id") == "d")
    assert event["state"] == "rejected"


@pytest.mark.parametrize("mode, expected", [("dontAsk", "Don't ask"), ("plan", "Plan mode runs commands only")])
async def test_dont_ask_and_plan_refuse_other_folders_without_a_card(runtime, mode, expected):
    manager, session, project, granted, outside = runtime
    session["permission_mode"] = mode
    command = "ls" if mode == "plan" else "touch made.txt"
    output = await asyncio.wait_for(run(manager, session, command, str(outside)), 10)
    assert expected in output and not manager.pending and not (outside / "made.txt").exists()


async def test_bypass_runs_in_another_folder_without_a_grant(runtime):
    manager, session, project, granted, outside = runtime
    session["permission_mode"] = "bypassPermissions"
    tools = chat_tools(manager, session)
    tools.unrestricted = True
    result = json.loads(await manager.execute_tool(session, tools, "run_command", {"command": "pwd -P", "cwd": str(outside)},
                                                   "b"))
    assert result["output"].strip() == str(outside) and str(outside) not in session["allowed_directories"]


@pytest.mark.parametrize("cwd, message", [(".git", "excluded"), ("README.md", "existing folder"),
                                          ("missing", "existing folder")])
async def test_excluded_and_missing_folders_are_refused(runtime, cwd, message):
    manager, session, project, granted, outside = runtime
    (project / ".git").mkdir()
    output = await run(manager, session, "ls", cwd)
    assert message in output and not manager.pending


async def test_the_state_folder_is_refused(runtime):
    manager, session, *_ = runtime
    output = await run(manager, session, "ls", str(manager.settings.state_dir))
    assert "excluded" in output and not manager.pending


async def test_a_symbolic_link_out_of_the_project_asks_for_its_target(runtime):
    manager, session, project, granted, outside = runtime
    (project / "link").symlink_to(outside, target_is_directory=True)
    session["permission_mode"] = "auto"
    task = asyncio.create_task(run(manager, session, "ls", "link"))
    access = await card(manager)
    assert event_of(session, access)["input"] == {"path": str(outside)}
    manager.decide(*access, False)
    await asyncio.wait_for(task, 10)


async def test_allow_rules_apply_only_in_the_project_and_deny_rules_everywhere(runtime):
    manager, session, project, granted, outside = runtime
    manager.rules.add("project", session["workspace"], {"behavior": "allow", "tool": "run_command", "argv": ["make", "x"]})
    tools = chat_tools(manager, session)
    assert manager.permission(session, tools, "run_command", {"command": "make x"}).decision == "allow"
    assert manager.permission(session, tools, "run_command", {"command": "make x", "cwd": "."}).decision == "allow"
    assert manager.permission(session, tools, "run_command", {"command": "make x", "cwd": "src"}).decision == "ask"
    manager.rules.add("project", session["workspace"], {"behavior": "deny", "tool": "run_command", "prefix": ["rm"]})
    verdict = manager.permission(session, tools, "run_command", {"command": "rm -rf build", "cwd": str(granted)})
    assert verdict.decision == "deny"


async def test_a_card_in_another_folder_remembers_only_its_exact_text_there(runtime):
    manager, session, project, granted, outside = runtime
    task = asyncio.create_task(run(manager, session, "echo hi", "src", "first"))
    key = await card(manager)
    event = event_of(session, key)
    assert event["rule_options"]["argv"] is None and event["rememberable"] == ["chat"]
    with pytest.raises(ValueError, match="only be remembered for this chat"):
        manager.decide(*key, True, scope="project")
    manager.decide(*key, True, remember=True)
    await asyncio.wait_for(task, 10)
    assert session["allow_rules"] == [{"tool": "run_command", "command": "echo hi", "cwd": str(project / "src")}]
    tools = chat_tools(manager, session)
    assert manager.permission(session, tools, "run_command", {"command": "echo hi", "cwd": "src"}).decision == "allow"
    assert manager.permission(session, tools, "run_command", {"command": "echo hi"}).decision == "ask"
    assert manager.permission(session, tools, "run_command", {"command": "echo hi", "cwd": str(granted)}).decision == "ask"


@needs_git
async def test_read_only_git_runs_in_the_cwd_repository(runtime):
    manager, session, project, granted, outside = runtime
    for command in (["git", "init", "-q"], ["git", "commit", "-q", "--allow-empty", "-m", "granted repo"]):
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *command[1:]],
                       cwd=granted, check=True, capture_output=True)
    session["permission_mode"] = "auto"
    result = json.loads(await run(manager, session, "git log --oneline", str(granted), "git"))
    assert "granted repo" in result["output"]
    event = next(item for item in session["events"] if item.get("call_id") == "git")
    assert event["read_only_command"] is True


async def test_a_background_job_keeps_its_cwd(runtime):
    manager, session, project, granted, outside = runtime
    session["permission_mode"] = "bypassPermissions"
    tools = chat_tools(manager, session)
    tools.unrestricted = True
    page = json.loads(await manager.execute_tool(session, tools, "run_command",
                                                 {"command": "pwd -P", "cwd": "src", "background": True}, "bg"))
    job = await manager.jobs.wait(page["job_id"])
    assert job["output"].strip() == str(project / "src") and job["cwd"] == str(project / "src")


async def test_expected_durations_are_kept_per_folder(tmp_path):
    store = Store(tmp_path / "jobs.sqlite3")
    try:
        manager = JobManager(store, lambda text: text, None)
        (tmp_path / "a").mkdir()
        first = await manager.start("true", str(tmp_path), cwd=str(tmp_path / "a"))
        await manager.wait(first["id"])
        same = await manager.start("true", str(tmp_path), cwd=str(tmp_path / "a"))
        other = await manager.start("true", str(tmp_path))
        assert manager.expected_seconds(manager.get(same["id"])) is not None
        assert manager.expected_seconds(manager.get(other["id"])) is None
        await manager.wait(same["id"])
        await manager.wait(other["id"])
    finally:
        store.db.close()


def test_the_heading_and_hooks_see_the_cwd(runtime):
    manager, session, project, *_ = runtime
    assert manager.command_preview({"command": "ls"}, str(project / "src")).startswith(f"ls\n\nRuns in: {project / 'src'}\n")
    assert "Runs in" not in manager.command_preview({"command": "ls"})
