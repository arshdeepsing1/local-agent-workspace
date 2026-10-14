"""Permission rules in the app: precedence, the saved rule file, protected paths, turned-off tools and the routes."""
import asyncio
import json
import stat
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent import permissions
from local_agent import tools as tools_module
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.feature_tools import FEATURE_TOOLS
from local_agent.instructions import load_project_instructions
from local_agent.permissions import (EDIT_CONTENT_KEYS, EDIT_TOOL_PATHS, TOOL_KINDS, evaluate, mode_prompt,
                                     tool_decision)
from local_agent.planning import DelegateManager
from local_agent.portability import export_bundle
from local_agent.rules import RULE_FILE, RuleStore, protected_reason
from local_agent.store import Store
from local_agent.tools import TOOL_DEFINITIONS, WorkspaceTools

DENY_PUSH = {"behavior": "deny", "tool": "run_command", "prefix": ["git", "push"]}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "rules.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    tools = WorkspaceTools(str(project))
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    tools.skill_signature = ""
    yield manager, session, project, tools
    store.db.close()


async def card(manager):
    for _ in range(300):
        if manager.pending:
            return next(iter(manager.pending))
        await asyncio.sleep(0.01)
    raise AssertionError("Expected an approval card")


async def run(manager, session, tools, name, arguments, call_id="call"):
    output = await asyncio.wait_for(manager.execute_tool(session, tools, name, arguments, call_id), 10)
    assert not manager.pending
    return output, next(event for event in reversed(session["events"]) if event.get("call_id") == call_id)


def connection(manager, session, names=("mcp__docs__search", "mcp__docs__fetch", "mcp__other__echo")):
    called = []

    class Connection:
        tool_names = set(names)
        definitions = [{"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object"}}}
                       for name in names]

        async def call(self, name, arguments):
            called.append(name)
            return {"ok": True}
    manager.extension_connections[session["id"]] = Connection()
    return called, Connection.definitions


def names(definitions):
    return {item["function"]["name"] for item in definitions}


# Precedence

def test_a_deny_rule_beats_an_allow_rule_in_another_scope():
    allow = {"behavior": "allow", "tool": "run_command", "argv": ["git", "push"]}
    for chat, project, user in (([allow], [], [DENY_PUSH]), ([DENY_PUSH], [allow], []), ([], [DENY_PUSH], [allow])):
        verdict = evaluate("manual", "run_command", {"command": "git push"}, chat_rules=chat, project_rules=project,
                           user_rules=user, resolve_path=Path, protected=lambda value: None)
        assert verdict.decision == "deny"


def test_an_ask_rule_beats_accept_edits_and_the_read_only_list():
    ask = {"behavior": "ask", "tool": "edits"}
    verdict = evaluate("acceptEdits", "write_file", {"path": "a.txt", "content": "x"}, user_rules=[ask],
                       resolve_path=Path, protected=lambda value: None)
    assert (verdict.decision, verdict.source, verdict.scope) == ("ask", "rule", "user")
    assert evaluate("acceptEdits", "write_file", {"path": "a.txt", "content": "x"}, resolve_path=Path).decision == "allow"
    ask = {"behavior": "ask", "tool": "run_command", "prefix": ["git", "status"]}
    assert evaluate("auto", "run_command", {"command": "git status"}, project_rules=[ask]).decision == "ask"


async def test_an_ask_rule_shows_a_card_in_bypass(runtime):
    manager, session, project, tools = runtime
    session.update(permission_mode="bypassPermissions", allow_rules=[{"behavior": "ask", "tool": "edits"}])
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "a.txt", "content": "x"}, "w"))
    session_id, event_id = await card(manager)
    event = next(item for item in session["events"] if item["id"] == event_id)
    # An Always ask rule always asks, so the card offers nothing to remember.
    assert event["name"] == "write_file" and event["rememberable"] == []
    for answer in ({"scope": "project"}, {"remember": True}):
        with pytest.raises(ValueError, match="An Always ask rule or a command that cannot be checked exactly"):
            manager.decide(session_id, event_id, True, **answer)
    manager.decide(session_id, event_id, True)
    await task
    assert (project / "a.txt").read_text() == "x" and session["allow_rules"] == [{"behavior": "ask", "tool": "edits"}]


async def test_deny_rules_block_paths_and_mcp_tools_in_bypass(runtime, tmp_path):
    manager, session, project, tools = runtime
    tools.unrestricted = True
    (project / "src").mkdir()
    manager.rules.add("project", session["workspace"], {"behavior": "deny", "tool": "edits", "path": str(project / "src")})
    manager.rules.add("user", session["workspace"], {"behavior": "deny", "tool": "mcp__docs__search"})
    session["permission_mode"] = "bypassPermissions"
    output, event = await run(manager, session, tools, "write_file", {"path": "src/a.py", "content": "x"}, "src")
    assert output == ("Blocked by a deny rule for this project: file edits under " + str(project / "src")
                      + ". Do not retry it or work around it.")
    assert event["state"] == "rejected" and not (project / "src" / "a.py").exists()
    await run(manager, session, tools, "write_file", {"path": "b.py", "content": "x"}, "other")
    assert (project / "b.py").exists()
    # A move into the denied folder is blocked through its destination.
    output, _ = await run(manager, session, tools, "move_path", {"path": "b.py", "destination": "src/b.py"}, "move")
    assert output.startswith("Blocked by a deny rule") and (project / "b.py").exists()
    called, _ = connection(manager, session)
    output, _ = await run(manager, session, tools, "mcp__docs__search", {}, "mcp")
    assert output.startswith("Blocked by a deny rule for all projects: the tool mcp__docs__search.") and called == []
    await run(manager, session, tools, "mcp__docs__fetch", {}, "fetch")
    assert called == ["mcp__docs__fetch"]


def test_unconditional_deny_rules_remove_tools_from_the_definitions(runtime):
    manager, session, _, _ = runtime
    _, external = connection(manager, session)
    offered = names(manager.step_definitions(session, external))
    assert {"run_command", "mcp__docs__search", "mcp__docs__fetch", "mcp__other__echo"} <= offered
    manager.rules.add("user", session["workspace"], {"behavior": "deny", "tool": "mcp__docs__*"})
    session["allow_rules"] = [{"behavior": "deny", "tool": "run_command"}]
    offered = names(manager.step_definitions(session, external))
    assert "run_command" not in offered and not offered & {"mcp__docs__search", "mcp__docs__fetch"}
    assert "mcp__other__echo" in offered and "write_file" in offered
    # A deny rule with a matcher leaves the tool offered.
    session["allow_rules"] = [DENY_PUSH, {"behavior": "deny", "tool": "edits", "path": "/srv/x"}]
    assert {"run_command", "write_file"} <= names(manager.step_definitions(session, external))
    session["allow_rules"] = [{"behavior": "deny", "tool": "edits"}]
    offered = names(manager.step_definitions(session, external))
    assert not offered & {"write_file", "edit_file", "move_path", "delete_path", "notebook_edit", "insert_activity_log"}


def test_submit_plan_is_offered_only_in_plan_and_never_to_subagents(runtime, monkeypatch):
    manager, session, _, _ = runtime
    plan_tool = {"type": "function", "function": {"name": "submit_plan", "description": "x", "parameters": {"type": "object"}}}
    monkeypatch.setattr("local_agent.agents.FEATURE_TOOLS", [*FEATURE_TOOLS, plan_tool])
    assert "submit_plan" not in names(manager.step_definitions(session, []))
    session["permission_mode"] = "plan"
    assert "submit_plan" in names(manager.step_definitions(session, []))
    session["is_subagent"] = True
    assert "submit_plan" not in names(manager.step_definitions(session, []))


# The saved rule file

def test_rule_store_persists_reloads_and_keeps_the_file_private(tmp_path):
    state = tmp_path / "state"
    store = RuleStore(state)
    store.add("user", "/srv/x", DENY_PUSH)
    store.add("user", "/srv/x", dict(DENY_PUSH))  # Duplicates are ignored.
    store.add("project", str(tmp_path), {"behavior": "allow", "tool": "edits"})
    path = state / RULE_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data == {"version": 1, "user": [DENY_PUSH],
                    "projects": {str(tmp_path.resolve()): [{"behavior": "allow", "tool": "edits"}]}}
    again = RuleStore(state)
    assert again.rules(str(tmp_path)) == {"project": [{"behavior": "allow", "tool": "edits"}], "user": [DENY_PUSH]}
    # Another instance's write is picked up (the cache follows the file's mtime and size).
    store.remove("user", "/srv/x", DENY_PUSH)
    assert again.rules(str(tmp_path))["user"] == []
    assert not [item for item in state.iterdir() if item.name.endswith(".tmp")]


def test_rule_store_writes_atomically(tmp_path, monkeypatch):
    store = RuleStore(tmp_path)
    store.add("user", "/srv/x", DENY_PUSH)
    before = (tmp_path / RULE_FILE).read_text()

    def broken(*args):
        raise OSError("disk full")
    monkeypatch.setattr("local_agent.rules.os.replace", broken)
    with pytest.raises(OSError):
        store.add("user", "/srv/x", {"behavior": "deny", "tool": "stop_job"})
    assert (tmp_path / RULE_FILE).read_text() == before
    assert [item.name for item in tmp_path.iterdir()] == [RULE_FILE]


def test_a_corrupt_rule_file_is_ignored_reported_and_reset(tmp_path, monkeypatch):
    path = tmp_path / RULE_FILE
    path.write_text('{"version": 1, "user": [{"tool": "run_command", "secret": "dapi-x"}], "projects": [] }')
    store = RuleStore(tmp_path)
    assert store.problem == "Saved permission rules could not be read: the file does not have the expected shape."
    assert "dapi" not in store.problem and store.rules(str(tmp_path)) == {"project": [], "user": []}
    with pytest.raises(ValueError, match="could not be read; reset them first"):
        store.add("user", "/srv/x", DENY_PUSH)
    path.write_text("not json")
    assert RuleStore(tmp_path).problem == "Saved permission rules could not be read: the file is not valid JSON."
    monkeypatch.setattr("local_agent.rules.time.time", lambda: 1700000000)
    store.reset()
    assert store.problem is None and not path.exists()
    assert (tmp_path / f"{RULE_FILE}.bad-1700000000").read_text() == "not json"
    store.add("user", "/srv/x", DENY_PUSH)
    assert store.rules("/srv/x")["user"] == [DENY_PUSH]


def test_project_rules_follow_the_resolved_folder(tmp_path):
    store = RuleStore(tmp_path / "state")
    project, other = tmp_path / "project", tmp_path / "other"
    project.mkdir()
    other.mkdir()
    (tmp_path / "link").symlink_to(project)
    store.add("project", str(project), DENY_PUSH)
    assert store.rules(str(tmp_path / "link"))["project"] == [DENY_PUSH]
    assert store.rules(str(project) + "/")["project"] == [DENY_PUSH]
    assert store.rules(str(other))["project"] == []
    assert store.rules(str(project / "sub"))["project"] == []  # A worktree or subfolder is its own project.


def test_rule_store_limits(tmp_path, monkeypatch):
    store = RuleStore(tmp_path)
    monkeypatch.setattr("local_agent.rules.MAX_USER_RULES", 3)
    monkeypatch.setattr("local_agent.rules.MAX_PROJECT_RULES", 2)
    for index in range(3):
        store.add("user", "/srv/x", {"behavior": "deny", "tool": "run_command", "prefix": [f"p{index}"]})
    with pytest.raises(ValueError, match="already have 3 rules for all projects"):
        store.add("user", "/srv/x", DENY_PUSH)
    for index in range(2):
        store.add("project", "/srv/x", {"behavior": "deny", "tool": "run_command", "prefix": [f"p{index}"]})
    with pytest.raises(ValueError, match="This project already has 2 rules"):
        store.add("project", "/srv/x", DENY_PUSH)
    store.add("project", "/srv/y", DENY_PUSH)  # Each project has its own limit.
    monkeypatch.setattr("local_agent.rules.MAX_FILE_BYTES", 600)
    with pytest.raises(ValueError, match="limited to 1 MB"):
        store.add("project", "/srv/z", {"behavior": "deny", "tool": "run_command", "prefix": ["x" * 300]})


# Protected paths

@pytest.mark.parametrize("relative", [".vscode/settings.json", ".github/workflows/ci.yml", ".github/actions/a/action.yml",
                                      "AGENTS.md", "docs/CLAUDE.md", "CLAUDE.local.md", ".gitattributes", ".envrc",
                                      ".claude/settings.json", ".agents/permissions.json", ".husky/pre-commit",
                                      ".pre-commit-config.yaml", ".devcontainer/devcontainer.json", ".IDEA/x.xml"])
def test_protected_reason_covers_configuration_that_runs_code(relative):
    root = Path("/srv/project")
    assert protected_reason(root / relative, root / relative, root=root, home=Path("/srv/home"), app_root=Path("/srv/app"))


def test_protected_reason_for_home_app_and_granted_folders():
    root, home, app = Path("/srv/project"), Path("/srv/home"), Path("/srv/app")
    check = lambda path: protected_reason(path, path, root=root, home=home, app_root=app)  # noqa: E731
    assert check(home / ".bashrc") and check(home / ".ssh" / "config") and check(home / ".config" / "git" / "config")
    assert check(app / "backend" / "x.py") == "this app's own files"
    # A granted folder named like a protected one stays protected: absolute parts are checked.
    assert check(Path("/srv/granted/.vscode/settings.json")) == "editor and repository configuration (.vscode)"
    assert check(root / "src" / "main.py") is None and check(home / "notes.txt") is None
    assert check(root / "src" / "github" / "workflows.txt") is None


def test_protected_reason_compares_home_and_app_by_identity(tmp_path, monkeypatch):
    # A bind mount, a macOS firmlink or a case variant on a case-insensitive disk is another name for the same
    # folder; resolve() does not map it, so the home and app tests also compare file identity.
    root, home, app = tmp_path / "project", tmp_path / "home", tmp_path / "app"
    home_alias, app_alias = tmp_path / "Home", tmp_path / "App"
    for folder in (root, home / ".ssh", app, home_alias / ".ssh", app_alias / "backend"):
        folder.mkdir(parents=True, exist_ok=True)
    real = tools_module._identity
    names = {home_alias: home, app_alias: app}
    monkeypatch.setattr(tools_module, "_identity", lambda path: real(names.get(Path(path), path)))
    check = lambda path: protected_reason(path, path, root=root, home=home, app_root=app)  # noqa: E731
    assert check(home_alias / ".ssh" / "authorized_keys") == "shell and Git startup files (~/.ssh)"
    assert check(home_alias / ".bashrc") == "shell and Git startup files (~/.bashrc)"
    assert check(home_alias / ".config" / "git" / "config") == "Git configuration (~/.config/git)"
    assert check(app_alias / "backend" / "x.py") == "this app's own files"
    assert check(home_alias / "notes.txt") is None and check(root / "src" / "a.py") is None


@pytest.mark.parametrize("mode, decision", [("acceptEdits", "ask"), ("auto", "ask"), ("manual", "ask"),
                                            ("dontAsk", "deny"), ("bypassPermissions", "allow")])
async def test_protected_files_ask_even_with_an_edits_rule(runtime, mode, decision):
    manager, session, project, tools = runtime
    session.update(permission_mode=mode, allow_rules=[{"tool": "edits"}])
    tools.unrestricted = mode == "bypassPermissions"
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": ".vscode/tasks.json", "content": "{}"}, "p"))
    if decision == "ask":
        session_id, event_id = await card(manager)
        event = next(item for item in session["events"] if item["id"] == event_id)
        assert event["protected"] == "editor and repository configuration (.vscode)" and event["rememberable"] == []
        with pytest.raises(ValueError, match="Protected files are always confirmed"):
            manager.decide(session_id, event_id, True, scope="chat")
        with pytest.raises(ValueError, match="Protected files are always confirmed"):
            manager.decide(session_id, event_id, True, remember=True)
        manager.decide(session_id, event_id, True)
    output = await asyncio.wait_for(task, 10)
    event = next(item for item in session["events"] if item.get("call_id") == "p")
    if decision == "deny":
        assert output.startswith("Not run: this edit changes a protected file") and not (project / ".vscode").exists()
        assert "allowed_by_rule" not in event
    else:
        assert (project / ".vscode" / "tasks.json").read_text() == "{}"
        assert event.get("protected") == ("editor and repository configuration (.vscode)" if decision == "ask" else None)


async def test_a_symlink_into_workflows_is_protected(runtime):
    manager, session, project, tools = runtime
    (project / ".github" / "workflows").mkdir(parents=True)
    (project / "ci").symlink_to(project / ".github" / "workflows")
    session["permission_mode"] = "acceptEdits"
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "ci/build.yml", "content": "x"}, "s"))
    session_id, event_id = await card(manager)
    event = next(item for item in session["events"] if item["id"] == event_id)
    assert event["protected"] == "CI configuration (.github/workflows)"
    manager.decide(session_id, event_id, False)
    await task
    assert not (project / ".github" / "workflows" / "build.yml").exists()


# Subagents and stopping jobs

@pytest.mark.parametrize("mode, decision", [("manual", "ask"), ("plan", "ask"), ("acceptEdits", "allow"),
                                            ("auto", "allow"), ("dontAsk", "deny"), ("bypassPermissions", "allow")])
def test_delegate_task_decisions(mode, decision):
    assert evaluate(mode, "delegate_task", {"task": "x"}).decision == decision
    chat = [{"tool": "delegate_task"}]
    if mode in ("manual", "dontAsk"):
        verdict = evaluate(mode, "delegate_task", {"task": "x"}, chat_rules=chat)
        assert (verdict.decision, verdict.source) == ("allow", "rule")


@pytest.mark.parametrize("mode, decision", [("manual", "ask"), ("acceptEdits", "ask"), ("auto", "allow"),
                                            ("plan", "deny"), ("dontAsk", "deny"), ("bypassPermissions", "allow")])
def test_stop_job_decisions(mode, decision):
    assert evaluate(mode, "stop_job", {"job_id": "j"}).decision == decision


async def test_delegate_task_shows_a_card_in_manual_and_a_chat_rule_allows_it(runtime):
    manager, session, _, tools = runtime
    started = []

    async def worker(child, prompt):
        started.append(prompt)
        await manager.event(child, "assistant", text="Done.")
    manager.run_databricks = worker
    task = asyncio.create_task(manager.execute_tool(session, tools, "delegate_task",
                                                    {"task": "Count files", "context": "Use ls", "max_steps": 3}, "d"))
    session_id, event_id = await card(manager)
    event = next(item for item in session["events"] if item["id"] == event_id)
    assert event["preview"] == ("Start a subagent (general; separate paid model requests, at most 3 steps)"
                                "\n\nTask: Count files\nContext: Use ls")
    assert started == []
    manager.decide(session_id, event_id, True, scope="chat")
    result = json.loads(await asyncio.wait_for(task, 10))
    assert result["status"] == "completed" and len(started) == 1
    assert session["allow_rules"] == [{"behavior": "allow", "tool": "delegate_task"}]
    output, event = await run(manager, session, tools, "delegate_task", {"task": "Again"}, "d2")
    assert event["allowed_by_rule"] is True and event["rule_scope"] == "chat" and len(started) == 2


async def test_stop_job_card_names_the_job(runtime):
    manager, session, project, tools = runtime
    job = await manager.jobs.start(command="sleep 30", workspace=str(project), session_id=session["id"], background=True)
    try:
        task = asyncio.create_task(manager.execute_tool(session, tools, "stop_job", {"job_id": job["id"]}, "s"))
        session_id, event_id = await card(manager)
        event = next(item for item in session["events"] if item["id"] == event_id)
        assert event["preview"] == f"Stop job {job['id']}: sleep 30"
        manager.decide(session_id, event_id, False)
        assert (await task).startswith("User declined this action.")
        assert manager.jobs.get(job["id"])["state"] == "running"
    finally:
        await manager.jobs.stop(job["id"])
        await manager.jobs.shutdown()


# Turned-off tools

async def test_turned_off_tools_are_not_offered_and_are_refused(runtime):
    manager, session, project, tools = runtime
    session.update(permission_mode="bypassPermissions", disabled_tools=["write_file", "mcp__docs__search"])
    _, external = connection(manager, session)
    offered = names(manager.step_definitions(session, external))
    assert "write_file" not in offered and "mcp__docs__search" not in offered and "edit_file" in offered
    output, event = await run(manager, session, tools, "write_file", {"path": "a.txt", "content": "x"})
    assert output == "The user turned off write_file in this chat. Use other tools." and event["state"] == "rejected"
    assert not (project / "a.txt").exists()
    verdict = evaluate("bypassPermissions", "read_file", {"path": "a"}, disabled={"read_file"})
    assert (verdict.decision, verdict.source) == ("deny", "disabled")


async def test_a_subagent_inherits_turned_off_tools_and_saved_rules(runtime):
    manager, parent, _, tools = runtime
    parent["disabled_tools"] = ["run_command"]
    manager.store.save(parent)
    manager.rules.add("project", parent["workspace"], {"behavior": "deny", "tool": "stop_job"})
    manager.rules.add("user", parent["workspace"], {"behavior": "deny", "tool": "edits"})
    observed = {}

    async def worker(child, prompt):
        observed["disabled"] = list(child["disabled_tools"])
        observed["push"] = manager.permission(child, tools, "run_command", {"command": "git push"}).message
        observed["stop"] = manager.permission(child, tools, "stop_job", {"job_id": "x"})
        observed["write"] = manager.permission(child, tools, "write_file", {"path": "a.txt", "content": "x"})
        await manager.event(child, "assistant", text="Done.")
    manager.run_databricks = worker
    await DelegateManager(manager).delegate(parent, "Look around")
    assert observed["disabled"] == ["run_command"]
    assert observed["push"] == "The user turned off run_command in this chat. Use other tools."
    assert (observed["stop"].decision, observed["stop"].scope) == ("deny", "project")
    assert (observed["write"].decision, observed["write"].scope) == ("deny", "user")


async def test_fork_import_and_export_keep_turned_off_tools_and_new_outcomes(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project, destination = tmp_path / "project", tmp_path / "destination"
    project.mkdir()
    destination.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    app = create_app(settings)
    manager = app.state.manager
    source = manager.store.create(settings.values)
    source.update(title="Done", terminal_reason="completed", disabled_tools=["run_command", "mcp__docs__search"],
                  wire=[{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello."}],
                  events=[{"id": "u", "type": "user", "text": "Hi"},
                          {"id": "t", "type": "tool", "name": "run_command", "state": "rejected", "approval": "blocked",
                           "blocked_reason": "Not run: the user switched this chat to Plan mode while this card was waiting.",
                           "input": {"command": "make"}, "output": "Not run", "rememberable": ["chat"], "uncertain": True},
                          {"id": "t2", "type": "tool", "name": "run_command", "state": "completed", "approved_by_mode": "auto",
                           "read_only_command": True, "rule_scope": "project", "allowed_by_rule": True,
                           "without_shell": True, "edited": True, "protected": "x", "input": {"command": "ls"}, "output": "ok"},
                          {"id": "a", "type": "assistant", "text": "Hello."}])
    manager.store.save(source)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
        bundle = (await client.get(f"/api/sessions/{source['id']}/export")).json()
        exported = bundle["sessions"][0]
        assert exported["disabled_tools"] == ["run_command", "mcp__docs__search"]
        assert exported["events"][1]["approval"] == "blocked" and "rememberable" not in exported["events"][1]
        assert "uncertain" not in exported["events"][1]
        assert {key: exported["events"][2][key] for key in ("approved_by_mode", "read_only_command", "rule_scope",
                                                            "without_shell", "edited", "protected")} == {
            "approved_by_mode": "auto", "read_only_command": True, "rule_scope": "project", "without_shell": True,
            "edited": True, "protected": "x"}
        imported = (await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)).json()
        fork = (await client.post(f"/api/sessions/{source['id']}/fork")).json()
        assert imported["session"]["disabled_tools"] == ["run_command", "mcp__docs__search"]
        assert fork["disabled_tools"] == ["run_command", "mcp__docs__search"]
        assert manager.store.get(fork["id"])["disabled_tools"] == ["run_command", "mcp__docs__search"]
        bundle["sessions"][0]["disabled_tools"] = ["x" * 301]
        assert (await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)).status_code == 400
    assert export_bundle([source], source["id"])["sessions"][0]["events"][1]["blocked_reason"].startswith("Not run")
    manager.store.db.close()


# Classification and prompts

def schema_strings(schema, path=()):
    """Every string property name in a JSON schema, through nested objects and array items."""
    found = set()
    if not isinstance(schema, dict):
        return found
    for key, value in (schema.get("properties") or {}).items():
        if isinstance(value, dict) and value.get("type") == "string":
            found.add(key)
        found |= schema_strings(value, (*path, key))
    if isinstance(schema.get("items"), dict):
        found |= schema_strings(schema["items"], path)
    return found


def all_properties(schema):
    found = set()
    if isinstance(schema, dict):
        found |= set(schema.get("properties") or {})
        for value in (schema.get("properties") or {}).values():
            found |= all_properties(value)
        found |= all_properties(schema.get("items"))
    return found


def test_every_tool_has_a_permission_kind():
    tools = [*TOOL_DEFINITIONS, *FEATURE_TOOLS]
    for name in [item["function"]["name"] for item in tools] + ["ask_user", "submit_plan"]:
        assert name in TOOL_KINDS, name
    for item in tools:
        name, schema = item["function"]["name"], item["function"]["parameters"]
        if TOOL_KINDS[name] != "edit":
            continue
        paths = EDIT_TOOL_PATHS[name]
        for key in schema_strings(schema):
            assert key in paths or key in EDIT_CONTENT_KEYS, (name, key)
        for key in paths:
            assert key in all_properties(schema), (name, key)
    assert set(EDIT_TOOL_PATHS) == {name for name, kind in TOOL_KINDS.items() if kind == "edit"}
    assert EDIT_TOOL_PATHS["move_path"] == ("path", "destination")


def test_read_and_edit_tool_sets_stay_derived():
    assert {"read_file", "find_files", "wait_job", "notebook_read", "ask_user", "submit_plan"} <= permissions.READ_TOOLS
    assert "stop_job" not in permissions.READ_TOOLS and "delegate_task" not in permissions.READ_TOOLS
    assert permissions.EDIT_TOOLS == {"write_file", "edit_file", "insert_activity_log", "move_path", "delete_path", "notebook_edit"}
    assert not hasattr(permissions, "BASIC_COMMANDS")
    assert tool_decision("auto", "run_command", {"command": "pwd"}) == "allow"
    assert tool_decision("plan", "submit_plan", {}) == "allow" and tool_decision("plan", "ask_user", {}) == "allow"


def test_mode_prompts_are_exact():
    assert mode_prompt("auto") == (
        "Auto mode: file edits run directly, and a fixed list of read-only commands (pwd, ls, and read-only git "
        "status/diff/log/show/branch commands) runs without an approval card; other commands and actions need an "
        "approval card unless a rule the user saved allows them.")
    first = ("Plan mode: inspect files and describe a concrete plan with the proposed steps and content, rather than "
             "merely refusing an implementation request. File changes and commands are blocked, except read-only "
             "commands from a fixed list (pwd, ls, git status/diff/log/show/branch).")
    assert mode_prompt("plan") == first + (
        " Use ask_user for clarifying questions. When the plan is complete, call submit_plan with the full plan: the "
        "user can approve it and switch modes, and you then implement it in this same turn. Do not invent mode names.")
    assert mode_prompt("plan", True) == first + (
        " Use ask_user only when a decision blocks you. Return the finished plan as your final reply.")
    assert mode_prompt("dontAsk") == (
        "Don't ask mode: actions run only when the mode or a rule the user saved allows them; anything else is blocked "
        "without an approval card. If an action is blocked, continue with what you can do and tell the user what "
        "needs their approval.")
    assert mode_prompt("bypassPermissions") == (
        "Bypass permissions mode: actions run without approval cards. Deny and ask rules the user saved and file-tool "
        "credential exclusions still apply.")
    assert mode_prompt("manual") == "Manual mode: reads run directly; file changes and commands require an approval card."
    assert mode_prompt("acceptEdits") == "Accept edits mode: file edits run directly; commands require an approval card."


def test_plan_denial_text_for_agents_and_subagents():
    verdict = evaluate("plan", "write_file", {"path": "a", "content": ""})
    assert verdict.message.endswith("Describe the plan, or call submit_plan when it is ready.")
    verdict = evaluate("plan", "write_file", {"path": "a", "content": ""}, subagent=True)
    assert verdict.message.endswith("Return the plan as your final reply.")


# Routes

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    app = create_app(settings)
    client = TestClient(app)
    client.__enter__()
    client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
    yield app, client, project, settings
    client.__exit__(None, None, None)


def test_permission_rule_routes_add_list_remove_and_reset(api):
    app, client, project, settings = api
    sid = client.post("/api/sessions").json()["id"]
    empty = client.get("/api/permission-rules", params={"session_id": sid}).json()
    assert empty == {"chat": [], "project": [], "user": [], "project_path": str(project.resolve()), "problem": None}
    draft = {"tool": "run_command", "behavior": "deny", "text": "git push", "match": "prefix"}
    listed = client.post("/api/permission-rules", json={"scope": "user", "rule": draft}).json()
    assert listed["user"] == [DENY_PUSH] and listed["chat"] == []
    listed = client.post("/api/permission-rules", json={"scope": "project", "session_id": sid, "rule": {
        "tool": "run_command", "behavior": "allow", "text": "npm test", "match": "exact"}}).json()
    assert listed["project"] == [{"argv": ["npm", "test"], "behavior": "allow", "tool": "run_command"}]
    listed = client.post("/api/permission-rules", json={"scope": "chat", "session_id": sid, "rule": {"tool": "edits"}}).json()
    assert listed["chat"] == [{"behavior": "allow", "tool": "edits"}]
    assert client.get(f"/api/sessions/{sid}").json()["allow_rules"] == [{"behavior": "allow", "tool": "edits"}]
    bad = client.post("/api/permission-rules", json={"scope": "project", "session_id": sid, "rule": {
        "tool": "run_command", "behavior": "allow", "text": "bash -c x", "match": "prefix"}})
    assert bad.status_code == 400 and "runs other programs" in bad.json()["detail"]
    assert client.post("/api/permission-rules", json={"scope": "project", "rule": {"tool": "edits"}}).status_code == 400
    assert client.post("/api/permission-rules", json={"scope": "user", "rule": {"tool": "x" * 9000}}).status_code == 400
    assert client.post("/api/permission-rules", json={"scope": "team", "rule": {"tool": "edits"}}).status_code == 422
    # Busy chats can change rules.
    app.state.manager.statuses[sid] = "running"
    removed = client.request("DELETE", "/api/permission-rules", json={"scope": "user", "rule": DENY_PUSH}).json()
    assert removed["user"] == []
    removed = client.request("DELETE", "/api/permission-rules", json={"scope": "chat", "session_id": sid,
                                                                      "rule": {"tool": "edits"}}).json()
    assert removed["chat"] == []
    path = settings.state_dir / RULE_FILE
    path.write_text("broken")
    problem = client.get("/api/permission-rules").json()["problem"]
    assert problem == "Saved permission rules could not be read: the file is not valid JSON."
    assert client.post("/api/permission-rules", json={"scope": "user", "rule": draft}).status_code == 400
    reset = client.post("/api/permission-rules/reset").json()
    assert reset["problem"] is None and reset["project"] == []
    assert client.post("/api/permission-rules", json={"scope": "user", "rule": draft}).json()["user"] == [DENY_PUSH]


def test_disabled_tools_and_tool_list_routes(api):
    app, client, _, _ = api
    sid = client.post("/api/sessions").json()["id"]
    listed = client.get("/api/tools", params={"session_id": sid}).json()
    assert listed["disabled"] == [] and {"name", "description"} == set(listed["tools"][0])
    assert "run_command" in {item["name"] for item in listed["tools"]}
    assert all(len(item["description"]) <= 160 for item in listed["tools"])
    # A long description is cut at a word boundary and marked with an ellipsis, never mid-word.
    full = {item["function"]["name"]: item["function"]["description"] for item in [*TOOL_DEFINITIONS, *FEATURE_TOOLS]}
    cut = [item for item in listed["tools"] if item["description"] != full[item["name"]]]
    assert cut and all(item["description"].endswith("…") for item in cut)
    assert all(full[item["name"]].startswith(item["description"][:-1]) and
               not full[item["name"]][len(item["description"]) - 1].isalnum() for item in cut), cut
    app.state.manager.statuses[sid] = "running"
    route = f"/api/sessions/{sid}/disabled-tools"
    saved = client.put(route, json={"tools": ["run_command", "run_command", "mcp__docs__search", "ask_user"]}).json()
    assert saved["disabled_tools"] == ["run_command", "mcp__docs__search", "ask_user"] and saved["status"] == "running"
    assert client.get("/api/tools", params={"session_id": sid}).json()["disabled"] == saved["disabled_tools"]
    assert client.put(route, json={"tools": ["rm_everything"]}).status_code == 400
    assert client.put(route, json={"tools": ["mcp__bad name__x"]}).status_code == 400
    assert client.put(route, json={"tools": ["write_file"] * 65}).status_code == 422
    assert client.post("/api/sessions").json()["disabled_tools"] == []


async def test_always_allow_from_a_card_saves_project_and_user_rules(runtime):
    manager, session, project, tools = runtime
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "git log --oneline"}, "c1"))
    session_id, event_id = await card(manager)
    event = next(item for item in session["events"] if item["id"] == event_id)
    assert event["rule_options"] == {"argv": ["git", "log", "--oneline"], "reason": None, "prefixes": [
        {"prefix": ["git"], "flags": ["log", "--oneline"]}, {"prefix": ["git", "log"], "flags": ["--oneline"]}]}
    assert event["rememberable"] == ["chat", "project", "user"] and event["editable"] == ["command"]
    with pytest.raises(ValueError, match="does not match the action on the card"):
        manager.decide(session_id, event_id, True, scope="project",
                       rule={"tool": "run_command", "argv": ["git", "log"]})
    with pytest.raises(ValueError, match="Only an approval"):
        manager.decide(session_id, event_id, False, scope="project")
    manager.decide(session_id, event_id, True, scope="project",
                   rule={"tool": "run_command", "behavior": "deny", "prefix": ["git", "log"], "flags": ["--oneline", "-n"]})
    await task
    assert "rememberable" not in event and "rule_options" not in event
    assert manager.rules.rules(session["workspace"])["project"] == [
        {"behavior": "allow", "flags": ["--oneline", "-n"], "prefix": ["git", "log"], "tool": "run_command"}]
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "echo $HOME"}, "c2"))
    session_id, event_id = await card(manager)
    event = next(item for item in session["events"] if item["id"] == event_id)
    assert event["rule_options"] == {"argv": None, "prefixes": [], "reason": "This command uses shell syntax ($)."}
    assert event["rememberable"] == ["chat"]
    with pytest.raises(ValueError, match="only be remembered for this chat"):
        manager.decide(session_id, event_id, True, scope="user")
    manager.decide(session_id, event_id, True, remember=True)
    await task
    assert session["allow_rules"] == [{"tool": "run_command", "command": "echo $HOME"}]
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "a.txt", "content": "x"}, "c3"))
    session_id, event_id = await card(manager)
    event = next(item for item in session["events"] if item["id"] == event_id)
    assert event["rule_options"] == {"folder": str(project)}
    manager.decide(session_id, event_id, True, scope="user")
    await task
    assert manager.rules.rules("/srv/elsewhere")["user"] == [{"behavior": "allow", "tool": "edits"}]


async def test_dont_ask_blocks_hooks_unless_a_hook_rule_allows_them(runtime):
    manager, session, project, tools = runtime
    manager.extensions.update_config({"hooks": [{"id": "check", "enabled": True, "event": "before_tool", "command": "true"}]})
    session["permission_mode"] = "dontAsk"
    session["allow_rules"] = [{"behavior": "allow", "tool": "edits"}]
    output, event = await run(manager, session, tools, "write_file", {"path": "a.txt", "content": "x"}, "blocked")
    assert "Before-tool hook blocked write_file: Not run: Don't ask mode blocks actions" in output
    hook = next(item for item in session["events"] if item.get("name") == "hook_before_tool")
    assert (hook["state"], hook["approval"]) == ("rejected", "blocked")
    assert hook["blocked_reason"].startswith("Not run: Don't ask mode") and not (project / "a.txt").exists()
    session["allow_rules"].append({"behavior": "allow", "tool": "hook", "hook": "check"})
    output, event = await run(manager, session, tools, "write_file", {"path": "a.txt", "content": "x"}, "allowed")
    assert (project / "a.txt").read_text() == "x" and event["allowed_by_rule"] is True
    hook = [item for item in session["events"] if item.get("name") == "hook_before_tool"][-1]
    assert (hook["state"], hook["allowed_by_rule"], hook["rule_scope"]) == ("completed", True, "chat")
    # A deny rule for the hook blocks it in Bypass too.
    session.update(permission_mode="bypassPermissions",
                   allow_rules=[{"behavior": "deny", "tool": "hook", "hook": "check"}])
    output, _ = await run(manager, session, tools, "write_file", {"path": "b.txt", "content": "x"}, "denied")
    assert "Blocked by a deny rule for this chat: the hook check." in output and not (project / "b.txt").exists()


async def test_remembered_rules_are_checked_before_they_are_saved(runtime):
    manager, session, _, tools = runtime
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "make lint"}, "c"))
    session_id, event_id = await card(manager)
    with pytest.raises(ValueError, match="too large"):
        manager.decide(session_id, event_id, True, scope="user", rule={"tool": "run_command", "argv": ["x" * 9000]})
    with pytest.raises(ValueError, match="runs other programs"):
        manager.decide(session_id, event_id, True, scope="user", rule={"tool": "run_command", "prefix": ["env"]})
    assert manager.rules.rules(session["workspace"])["user"] == []
    manager.decide(session_id, event_id, False)
    await task


# Cards that always ask, credential commands and other review cases

def command_recorder(manager):
    """Replace command_tool; record how each command would run (argv and read_only, or the shell)."""
    calls = []

    async def command_tool(session, event, arguments, **options):
        calls.append({"command": arguments["command"], **options})
        return {"state": "completed", "exit_code": 0, "output": "recorded"}
    manager.command_tool = command_tool
    return calls


async def test_cards_an_ask_rule_or_an_uncertain_match_raised_offer_nothing_to_remember(runtime):
    manager, session, _, tools = runtime
    calls = command_recorder(manager)
    session["permission_mode"] = "auto"
    manager.rules.add("project", session["workspace"], {"behavior": "ask", "tool": "run_command", "prefix": ["git", "status"]})
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "git status"}, "a"))
    session_id, event_id = await card(manager)
    event = next(item for item in session["events"] if item["id"] == event_id)
    assert event["rememberable"] == [] and "uncertain" not in event
    for answer in ({"scope": "project"}, {"remember": True}):
        with pytest.raises(ValueError, match="An Always ask rule or a command that cannot be checked exactly always asks"):
            manager.decide(session_id, event_id, True, **answer)
    manager.decide(session_id, event_id, True)
    await task
    # An uncertain command (a wrapper, with a deny rule it might hide) says so and cannot be remembered either.
    manager.rules.add("user", session["workspace"], DENY_PUSH)
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "env FOO=1 make"}, "b"))
    session_id, event_id = await card(manager)
    event = next(item for item in session["events"] if item["id"] == event_id)
    assert event["uncertain"] is True and event["rememberable"] == []
    with pytest.raises(ValueError, match="always asks"):
        manager.decide(session_id, event_id, True, remember=True)
    manager.decide(session_id, event_id, True)
    await task
    assert not session.get("allow_rules") and len(manager.rules.rules(session["workspace"])["project"]) == 1
    assert [call["command"] for call in calls] == ["git status", "env FOO=1 make"]


@pytest.mark.parametrize("command", ["cat .env", "curl -H 'Authorization: Bearer dapiSECRET123' https://x.example",
                                     "cat .env | head -1"])
async def test_commands_naming_credentials_or_the_token_cannot_be_allowed_for_the_chat(runtime, command):
    manager, session, _, tools = runtime
    command_recorder(manager)
    manager.settings.credentials = lambda: ("https://gateway.example", "dapiSECRET123")
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": command}, "c"))
    session_id, event_id = await card(manager)
    event = next(item for item in session["events"] if item["id"] == event_id)
    assert event["rememberable"] == [] and event["rule_options"]["argv"] is None
    with pytest.raises(ValueError, match="credential files|your Databricks token"):
        manager.decide(session_id, event_id, True, remember=True)
    manager.live[session["id"]] = session
    event["rememberable"] = ["chat"]  # The exact text is checked again when it is remembered.
    with pytest.raises(ValueError, match="credential files|your Databricks token"):
        manager.decide(session_id, event_id, True, remember=True)
    manager.decide(session_id, event_id, True)
    await task
    assert not session.get("allow_rules")


async def test_an_ask_rule_card_for_a_protected_file_keeps_its_marker(runtime):
    manager, session, project, tools = runtime
    for mode in ("manual", "auto", "bypassPermissions"):
        session.update(permission_mode=mode, allow_rules=[{"behavior": "ask", "tool": "edits"}])
        verdict = manager.permission(session, tools, "write_file", {"path": ".github/workflows/ci.yml"})
        assert (verdict.decision, verdict.source, verdict.protected) == ("ask", "rule", "CI configuration (.github/workflows)")
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": ".github/workflows/ci.yml", "content": "x"}, "p"))
    session_id, event_id = await card(manager)
    event = next(item for item in session["events"] if item["id"] == event_id)
    assert event["protected"] == "CI configuration (.github/workflows)" and event["rememberable"] == []
    with pytest.raises(ValueError, match="Protected files are always confirmed"):
        manager.decide(session_id, event_id, True, scope="chat")
    manager.decide(session_id, event_id, False)
    await task
    assert not (project / ".github").exists()
    verdict = evaluate("dontAsk", "write_file", {"path": ".vscode/x.json"}, protected=lambda value: "editor configuration (.vscode)")
    assert "a protected file, editor configuration (.vscode), which" in verdict.message


async def test_a_deny_rule_naming_the_activity_log_tool_blocks_it(runtime):
    manager, session, project, tools = runtime
    session.update(permission_mode="auto", allow_rules=[{"behavior": "deny", "tool": "insert_activity_log"}])
    assert "insert_activity_log" not in names(manager.step_definitions(session, []))
    output, event = await run(manager, session, tools, "insert_activity_log", {"path": "log.md"})
    assert output == "Blocked by a deny rule for this chat: the tool insert_activity_log. Do not retry it or work around it."
    assert event["state"] == "rejected" and not (project / "log.md").exists()


async def test_auto_read_only_commands_are_marked_on_their_event(runtime):
    manager, session, _, tools = runtime
    calls = command_recorder(manager)
    session["permission_mode"] = "auto"
    _, event = await run(manager, session, tools, "run_command", {"command": "ls"})
    assert (event["read_only_command"], event["without_shell"]) == (True, True) and "approval" not in event
    assert calls == [{"command": "ls", "argv": ["ls"], "read_only": True}]


def test_the_folder_offered_for_a_move_covers_both_ends():
    from local_agent.rules import card_rule_options, rule_matches
    resolve = lambda value: Path("/srv/project") / value if not value.startswith("/") else Path(value)  # noqa: E731
    move = {"path": "src/a.py", "destination": "lib/a.py"}
    options = card_rule_options("move_path", move, resolve_path=resolve)
    assert options == {"folder": "/srv/project"}
    rule = {"behavior": "allow", "tool": "edits", "path": options["folder"]}
    assert rule_matches(rule, "move_path", move, resolve_path=resolve)
    assert card_rule_options("move_path", {"path": "src/a.py", "destination": "src/b.py"}, resolve_path=resolve) == {
        "folder": "/srv/project/src"}
    assert card_rule_options("move_path", {"path": "/a/x", "destination": "/b/y"}, resolve_path=resolve) == {"folder": None}
    assert card_rule_options("write_file", {"path": "src/a.py"}, resolve_path=resolve) == {"folder": "/srv/project/src"}


@pytest.mark.parametrize("mode, rules, reason", [
    ("dontAsk", [], "Don't ask mode blocks commands that no rule allows."),
    ("bypassPermissions", [{"behavior": "deny", "tool": "run_command", "prefix": ["make"]}],
     "a deny rule for this chat blocks it (commands starting with make (any options))."),
    ("bypassPermissions", "disabled", "run_command is turned off in this chat.")])
async def test_verify_blocked_by_a_mode_or_rule_says_why_once(runtime, mode, rules, reason):
    manager, session, project, tools = runtime
    manager.projects.update(str(project.resolve()), {"verify_command": "make test"})
    session.update(permission_mode=mode, **({"disabled_tools": ["run_command"]} if rules == "disabled" else
                                             {"allow_rules": rules}))
    session["events"] = [{"id": "u", "type": "user", "text": "Edit"},
                         {"id": "t", "type": "tool", "name": "edit_file", "state": "completed", "call_id": "c",
                          "input": {"path": "src/app.py"}}]
    state = {"rounds": 0, "since": 0}
    assert await manager.verify_step(session, tools, [], state, True) == "done"
    texts = [event["text"] for event in session["events"] if event["type"] == "notice"]
    assert texts[-1] == "Verify did not run: " + reason and state["off"] is True
    session["events"].append({"id": "t2", "type": "tool", "name": "edit_file", "state": "completed", "call_id": "d",
                              "input": {"path": "src/app.py"}})
    count = len(session["events"])
    assert await manager.verify_step(session, tools, [], state, True) == "done" and len(session["events"]) == count


async def test_after_tool_hooks_blocked_in_dont_ask_add_no_notice(runtime):
    manager, session, project, tools = runtime
    manager.extensions.update_config({"hooks": [{"id": "after", "enabled": True, "event": "after_tool", "command": "true"}]})
    session.update(permission_mode="dontAsk", allow_rules=[{"behavior": "allow", "tool": "edits"}])
    output, event = await run(manager, session, tools, "write_file", {"path": "a.txt", "content": "x"})
    assert (project / "a.txt").read_text() == "x" and event["state"] == "completed"
    hook = next(item for item in session["events"] if item.get("name") == "hook_after_tool")
    assert (hook["state"], hook["approval"]) == ("rejected", "blocked")
    assert not [item for item in session["events"] if item["type"] == "notice"]


async def test_after_tool_hook_warnings_have_one_period(runtime):
    manager, session, project, tools = runtime
    manager.extensions.update_config({"hooks": [{"id": "after", "enabled": True, "event": "after_tool",
                                                 "command": "echo Lint failed.; exit 1"}]})
    session.update(permission_mode="bypassPermissions")
    tools.unrestricted = True
    await run(manager, session, tools, "write_file", {"path": "a.txt", "content": "x"})
    notice = next(item["text"] for item in session["events"] if item["type"] == "notice")
    assert notice == "After-tool hook warning: Hook failed (exit 1): Lint failed. The tool action already completed."


# --- outline_file and project_overview (u-large-inputs item 7) -------------------------------------

def test_the_large_input_tools_are_read_tools_everywhere():
    from local_agent import tool_profiles
    from local_agent.tool_schema import validate_arguments
    for name, arguments in (("outline_file", {"path": "docs/h.md", "max_level": 2, "offset": 0}),
                            ("project_overview", {"path": ".", "include_ignored": True})):
        assert TOOL_KINDS[name] == "read" and name in permissions.READ_TOOLS and name in tool_profiles.READ_TOOLS
        assert name in tool_profiles.FILE_TOOLS
        for mode in ("plan", "dontAsk", "manual", "auto"):
            assert evaluate(mode, name, arguments).decision == "allow"
        definition = next(item["function"] for item in TOOL_DEFINITIONS if item["function"]["name"] == name)
        validate_arguments(definition["parameters"], arguments)


def test_rules_and_turned_off_tools_for_read_file_and_listing_cover_the_new_tools():
    deny_read = {"behavior": "deny", "tool": "read_file"}
    verdict = evaluate("bypassPermissions", "outline_file", {"path": "a.md"}, project_rules=[deny_read])
    assert verdict.decision == "deny" and verdict.message == (
        "Blocked by a deny rule for this project: the tool read_file, which also covers outline_file. Do not retry it or "
        "work around it.")
    assert permissions.denied_tools([], [deny_read], [], ["outline_file", "project_overview"]) == {"outline_file"}
    deny_find = {"behavior": "deny", "tool": "find_files"}
    assert evaluate("manual", "project_overview", {}, user_rules=[deny_find]).decision == "deny"
    assert permissions.denied_tools([], [], [deny_find], ["project_overview"]) == {"project_overview"}
    verdict = evaluate("manual", "outline_file", {"path": "a.md"}, disabled={"read_file"})
    assert verdict.decision == "deny" and verdict.message == ("The user turned off read_file in this chat, which "
                                                              "also covers outline_file. Use other tools.")
    assert evaluate("manual", "project_overview", {}, disabled={"list_files"}).decision == "deny"
    assert evaluate("manual", "outline_file", {"path": "a.md"}, disabled={"list_files"}).decision == "allow"


async def test_a_turned_off_read_file_hides_and_refuses_outline_file(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.md").write_text("# A\n")
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "rules.sqlite3")
    try:
        manager = AgentManager(store, settings)
        session = store.create(settings.values)
        session["disabled_tools"] = ["read_file"]
        offered = [item["function"]["name"] for item in manager.step_definitions(session, [])]
        assert "outline_file" not in offered and "project_overview" in offered
        output = await manager.execute_tool(session, manager.session_tools(session), "outline_file", {"path": "a.md"},
                                            "c1")
        assert output == "The user turned off read_file in this chat, which also covers outline_file. Use other tools."
    finally:
        store.db.close()


def test_large_input_guidance_follows_the_offered_tools(tmp_path, monkeypatch):
    from local_agent.agents import large_input_guidance
    from local_agent.context import HANDOFF_POINTER
    both = large_input_guidance({"outline_file", "project_overview", "read_file", "write_file"})
    assert "call project_overview first" in both and "call outline_file" in both and "File write limit" in both
    # A read_only chat cannot write a file: no write-limit guidance.
    assert "File write limit" not in large_input_guidance({"outline_file", "project_overview"})
    only_outline = large_input_guidance({"outline_file"})
    assert "project_overview" not in only_outline and "outline_file" in only_outline
    only_overview = large_input_guidance({"project_overview"})
    assert "outline_file" not in only_overview and "project_overview" in only_overview
    assert large_input_guidance({"read_file"}) == ""
    assert "outline_file" in HANDOFF_POINTER
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    store = Store(settings.state_dir / "guidance.sqlite3")
    try:
        manager = AgentManager(store, settings)
        session = store.create(settings.values)
        tools = manager.session_tools(session)
        system, _ = manager.context_inputs(session, tools, "manual", available={"read_file"})
        assert "project_overview" not in system["content"] and "outline_file" not in system["content"]
        system, _ = manager.context_inputs(session, tools, "manual")
        assert "call project_overview first" in system["content"]
    finally:
        store.db.close()
