"""Files that run_command changes join the change review (Git folders, foreground commands)."""
import asyncio
import json
import os
import shutil
import subprocess

import httpx
import pytest

from local_agent import git_tools, shell_changes
from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.store import Store
from local_agent.workspace_folders import WorkspaceFolders

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git is required")


def git(folder, *arguments):
    subprocess.run(["git", *arguments], cwd=folder, check=True, capture_output=True)


def make_repository(folder, files):
    folder.mkdir(parents=True, exist_ok=True)
    git(folder, "init", "-q")
    git(folder, "config", "user.name", "Test")
    git(folder, "config", "user.email", "test@example.invalid")
    git(folder, "config", "commit.gpgsign", "false")
    for name, text in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)
    git(folder, "add", "-A")
    git(folder, "commit", "-qm", "init")


class Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)

    def changes(self):
        return [item["changes"] for item in self.sent if item["type"] == "changes"]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    monkeypatch.setattr(shell_changes, "_locations", {})
    project = tmp_path / "project"
    make_repository(project, {"tracked.txt": "old\n", "a.txt": "alpha\n", "b.txt": "beta\n"})
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "shell.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(permission_mode="bypassPermissions", title_generated=True)
    store.save(session)
    yield manager, session, project
    store.db.close()


def chat_tools(manager, session):
    tools = manager.session_tools(session)
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    return tools


async def run(manager, session, command, call_id="call"):
    return await manager.execute_tool(session, chat_tools(manager, session), "run_command", {"command": command}, call_id)


def listed(manager, session):
    return {item["path"]: item for item in manager.changes.list(session["id"], manager.review_tools(session))["files"]}


def baseline_text(manager, session, path):
    diff = manager.changes.diff(session["id"], manager.review_tools(session), path)
    return "".join(line["text"] + "\n" for line in diff["lines"] if line["kind"] in ("context", "removed"))


def undo(manager, session, *paths):
    files = listed(manager, session)
    return manager.changes.act(session["id"], manager.review_tools(session), "undo",
                               [{"path": path, "current_hash": files[path]["current_hash"]} for path in paths])


def stream(content=None, call=None):
    delta = {"content": content} if call is None else {"tool_calls": [{"index": 0, "id": "call", "type": "function", "function": call}]}
    return httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": "stop" if call is None else "tool_calls"}]}) + "\n\ndata: [DONE]\n\n")


async def test_a_sed_edit_by_the_model_is_listed_with_its_committed_baseline_and_undone(runtime, monkeypatch):
    manager, session, project = runtime
    command = "sed -i.bak 's/old/new/' tracked.txt && rm tracked.txt.bak"

    async def handler(request):
        messages = json.loads(request.content)["messages"]
        if messages[-1]["role"] == "tool":
            return stream("Edited.")
        return stream(call={"name": "run_command", "arguments": json.dumps({"command": command})})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))
    socket = Socket()
    manager.listeners[session["id"]] = {socket}
    await manager.run(session, "Replace old with new")
    assert (project / "tracked.txt").read_text() == "new\n"
    row = listed(manager, session)["tracked.txt"]
    assert (row["status"], row["source"], row["added"], row["removed"]) == ("modified", "command", 1, 1)
    assert "via_session_id" not in row
    assert [item["path"] for item in socket.changes()[0]["files"]] == ["tracked.txt"]
    assert baseline_text(manager, session, "tracked.txt") == "old\n"
    assert undo(manager, session, "tracked.txt")["files"] == []
    assert (project / "tracked.txt").read_text() == "old\n"
    # The undo is a checkpoint of this chat, so Recovery can bring the command's version back.
    assert [item["path"] for item in manager.checkpoints.list(session["id"])] == ["tracked.txt"]


async def test_a_file_changed_before_the_command_keeps_its_pre_command_content(runtime):
    manager, session, project = runtime
    (project / "tracked.txt").write_text("dirty\n")
    await run(manager, session, "sed -i.bak 's/dirty/new/' tracked.txt && rm tracked.txt.bak")
    assert baseline_text(manager, session, "tracked.txt") == "dirty\n"
    undo(manager, session, "tracked.txt")
    assert (project / "tracked.txt").read_text() == "dirty\n"


async def test_a_created_file_is_listed_as_new_and_undo_deletes_it(runtime):
    manager, session, project = runtime
    await run(manager, session, "mkdir -p out && echo hi > out/made.txt")
    assert listed(manager, session)["out/made.txt"]["status"] == "created"
    undo(manager, session, "out/made.txt")
    assert not (project / "out" / "made.txt").exists()


@pytest.mark.parametrize("command", [
    "echo more >> tracked.txt && git add -A && git commit -qm x",
    "git checkout -q -b other && echo more >> tracked.txt && git commit -qam x",
    "git checkout -q feature",
])
async def test_commands_that_stage_commit_or_switch_branches_keep_the_pre_command_baseline(runtime, command):
    manager, session, project = runtime
    git(project, "checkout", "-q", "-b", "feature")
    (project / "tracked.txt").write_text("feature\n")
    git(project, "commit", "-qam", "feature")
    git(project, "checkout", "-q", "-")
    await run(manager, session, command)
    assert (project / "tracked.txt").read_text() != "old\n"
    assert list(listed(manager, session)) == ["tracked.txt"]
    assert baseline_text(manager, session, "tracked.txt") == "old\n"


async def test_a_file_the_agent_edited_earlier_keeps_the_agent_baseline(runtime):
    manager, session, project = runtime
    tools = chat_tools(manager, session)
    await manager.execute_tool(session, tools, "edit_file", {"path": "tracked.txt", "old_text": "old", "new_text": "agent"}, "edit")
    assert (project / "tracked.txt").read_text() == "agent\n"
    await run(manager, session, "sed -i.bak 's/agent/command/' tracked.txt && rm tracked.txt.bak")
    row = listed(manager, session)["tracked.txt"]
    assert "source" not in row
    assert baseline_text(manager, session, "tracked.txt") == "old\n"


async def test_credential_files_are_not_tracked(runtime):
    manager, session, project = runtime
    await run(manager, session, "echo TOKEN=synthetic > .env && echo x > notes.txt")
    assert list(listed(manager, session)) == ["notes.txt"]


async def test_a_file_replaced_by_a_symbolic_link_is_not_tracked_and_undo_never_writes_through_it(runtime):
    manager, session, project = runtime
    await run(manager, session, "rm a.txt && ln -s b.txt a.txt && echo more >> tracked.txt")
    assert list(listed(manager, session)) == ["tracked.txt"]
    undo(manager, session, *listed(manager, session))
    assert (project / "a.txt").is_symlink()
    assert (project / "b.txt").read_text() == "beta\n"
    assert (project / "tracked.txt").read_text() == "old\n"


async def test_a_folder_that_is_not_a_git_repository_tracks_nothing(runtime, tmp_path):
    manager, session, _ = runtime
    plain = tmp_path / "plain"
    plain.mkdir()
    session["workspace"] = str(plain)
    result = json.loads(await run(manager, session, "echo x > f.txt"))
    assert result["exit_code"] == 0 and (plain / "f.txt").exists()
    assert listed(manager, session) == {}


async def test_too_many_changed_files_turn_tracking_off_and_the_command_still_runs(runtime):
    manager, session, project = runtime
    (project / "many").mkdir()
    for number in range(shell_changes.MAX_STATUS_ENTRIES + 1):
        (project / "many" / f"{number}.txt").write_text("x")
    result = json.loads(await run(manager, session, "echo more >> tracked.txt"))
    assert result["exit_code"] == 0 and (project / "tracked.txt").read_text() == "old\nmore\n"
    assert listed(manager, session) == {}


async def test_repository_programs_do_not_run(runtime, tmp_path):
    # fsmonitor and a clean filter named by the repository's own configuration would run during
    # git status; tracking switches both off.
    manager, session, project = runtime
    marker = tmp_path / "markers"
    marker.mkdir()
    script = tmp_path / "program.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}/$1\ncat\n")
    script.chmod(0o755)
    (project / ".gitattributes").write_text("tracked.txt filter=mark\n")
    git(project, "add", ".gitattributes")
    git(project, "commit", "-qm", "attributes")
    git(project, "config", "core.fsmonitor", f"{script} fsmonitor")
    git(project, "config", "filter.mark.clean", f"{script} clean")
    os.utime(project / "tracked.txt", (1, 1))  # Stat-dirty, so status hashes the file.
    await run(manager, session, "echo more >> tracked.txt")
    assert list(marker.iterdir()) == []
    assert list(listed(manager, session)) == ["tracked.txt"]


async def test_a_stopped_command_still_tracks_what_it_changed(runtime):
    manager, session, project = runtime
    task = asyncio.create_task(run(manager, session, "echo changed > tracked.txt; sleep 30"))
    for _ in range(500):
        if (project / "tracked.txt").read_text() == "changed\n":
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert baseline_text(manager, session, "tracked.txt") == "old\n"


async def test_read_only_commands_run_no_git(runtime, monkeypatch):
    manager, session, _ = runtime
    calls = []
    original = git_tools.run_git

    async def counted(*args, **kwargs):
        calls.append(args[1])
        return await original(*args, **kwargs)
    monkeypatch.setattr(shell_changes, "run_git", counted)
    monkeypatch.setattr(git_tools, "run_git", counted)
    result = json.loads(await run(manager, session, "pwd"))
    assert result["exit_code"] == 0 and calls == []
    # Also through the shell path: the command text is on the read-only list.
    assert await shell_changes.ShellChangeTracker.before(manager.session_tools(session), "  git status ") is None
    assert calls == []
    await run(manager, session, "true")
    assert calls  # Any other command is checked.


async def test_a_rule_allowed_command_without_a_shell_is_tracked(runtime):
    manager, session, project = runtime
    session["permission_mode"] = "manual"
    manager.rules.add("project", session["workspace"], {"behavior": "allow", "tool": "run_command", "argv": ["touch", "made.txt"]})
    await run(manager, session, "touch made.txt")
    event = next(item for item in session["events"] if item.get("call_id") == "call")
    assert event["without_shell"] is True and (project / "made.txt").exists()
    assert listed(manager, session)["made.txt"]["status"] == "created"


async def test_granted_and_added_folders_in_other_repositories_are_tracked(runtime, tmp_path):
    manager, session, project = runtime
    granted, added = (tmp_path / "granted").resolve(), (tmp_path / "added").resolve()
    make_repository(granted, {"g.txt": "granted\n"})
    make_repository(added, {"f.txt": "added\n"})
    session["permission_mode"] = "acceptEdits"
    session["allowed_directories"] = [str(granted)]
    WorkspaceFolders(manager.settings).add(str(project), str(added))
    session["permission_mode"] = "bypassPermissions"
    await run(manager, session, f"echo more >> {granted}/g.txt && echo more >> {added}/f.txt && echo more >> tracked.txt")
    session["permission_mode"] = "manual"
    files = listed(manager, session)
    assert sorted(files) == sorted([str(granted / "g.txt"), str(added / "f.txt"), "tracked.txt"])
    assert baseline_text(manager, session, str(added / "f.txt")) == "added\n"
    undo(manager, session, *files)
    assert [(folder / name).read_text() for folder, name in ((granted, "g.txt"), (added, "f.txt"), (project, "tracked.txt"))] == [
        "granted\n", "added\n", "old\n"]
    # Outside Bypass permissions too: the added folder is checked although the agent's tools may not use it.
    manager.rules.add("project", session["workspace"], {"behavior": "allow", "tool": "run_command",
                                                        "argv": ["touch", str(added / "new.txt")]})
    await run(manager, session, f"touch {added / 'new.txt'}", "touch")
    assert listed(manager, session)[str(added / "new.txt")]["status"] == "created"


async def test_a_subagent_command_is_rolled_up_into_the_parent(runtime):
    manager, parent, project = runtime
    child = manager.store.create({"workspace": parent["workspace"], "model": parent["model"]})
    child.update(parent_session_id=parent["id"], is_subagent=True, permission_mode="bypassPermissions")
    manager.store.save(child)
    socket = Socket()
    manager.listeners[parent["id"]] = {socket}
    await run(manager, child, "echo more >> tracked.txt")
    assert listed(manager, child)["tracked.txt"]["source"] == "command"
    row = listed(manager, parent)["tracked.txt"]
    assert (row["source"], row["via_session_id"]) == ("command", child["id"])
    assert [item["path"] for item in socket.changes()[-1]["files"]] == ["tracked.txt"]


async def test_a_subagent_edit_and_a_parent_command_are_undone_together_in_the_parent(runtime):
    manager, parent, project = runtime
    child = manager.store.create({"workspace": parent["workspace"], "model": parent["model"]})
    child.update(parent_session_id=parent["id"], is_subagent=True, permission_mode="bypassPermissions")
    manager.store.save(child)
    await manager.execute_tool(child, chat_tools(manager, child), "write_file", {"path": "note.txt", "content": "child\n"}, "w")
    await run(manager, parent, "sed -i.bak 's/old/new/' tracked.txt && rm tracked.txt.bak")
    files = listed(manager, parent)
    assert {path: (item.get("via_session_id"), item.get("source")) for path, item in files.items()} == {
        "note.txt": (child["id"], None), "tracked.txt": (None, "command")}
    assert undo(manager, parent, *files)["files"] == []
    assert not (project / "note.txt").exists() and (project / "tracked.txt").read_text() == "old\n"


async def test_a_granted_parent_folder_outside_any_repository_does_not_hide_the_project(runtime, tmp_path):
    manager, session, project = runtime
    outer = tmp_path.resolve()  # Contains the project and is no Git work tree itself.
    session["allowed_directories"] = [str(outer)]
    await run(manager, session, "sed -i.bak 's/old/new/' tracked.txt && rm tracked.txt.bak")
    assert list(listed(manager, session)) == ["tracked.txt"]
    assert baseline_text(manager, session, "tracked.txt") == "old\n"


async def test_a_granted_parent_repository_does_not_hide_the_nested_project_repository(runtime, tmp_path):
    manager, session, _ = runtime
    outer = (tmp_path / "outer").resolve()
    make_repository(outer, {"o.txt": "outer\n"})
    inner = outer / "inner"
    make_repository(inner, {"tracked.txt": "old\n"})
    session.update(workspace=str(inner), allowed_directories=[str(outer)])
    await run(manager, session, f"echo more >> tracked.txt && echo more >> {outer}/o.txt")
    files = listed(manager, session)
    assert sorted(files) == sorted(["tracked.txt", str(outer / "o.txt")])
    assert baseline_text(manager, session, "tracked.txt") == "old\n"
    assert baseline_text(manager, session, str(outer / "o.txt")) == "outer\n"


async def test_files_changed_before_the_command_do_not_crowd_out_its_own_edit(runtime):
    manager, session, project = runtime
    (project / "build").mkdir()
    for number in range(shell_changes.MAX_CANDIDATES + 100):
        (project / "build" / f"{number}.txt").write_text("x")
    await run(manager, session, "sed -i.bak 's/old/new/' tracked.txt && rm tracked.txt.bak")
    assert list(listed(manager, session)) == ["tracked.txt"]
    assert baseline_text(manager, session, "tracked.txt") == "old\n"


@pytest.mark.parametrize("same_command", [True, False])
async def test_a_filter_configured_by_a_command_does_not_run_in_the_apps_status(runtime, tmp_path, same_command):
    # The filter names are read again before each status, not taken from the 60-second cache.
    manager, session, project = runtime
    marker = tmp_path / "markers"
    marker.mkdir()
    script = tmp_path / "program.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}/$1\ncat\n")
    script.chmod(0o755)
    (project / ".gitattributes").write_text("tracked.txt filter=evil\n")
    git(project, "add", ".gitattributes")
    git(project, "commit", "-qm", "attributes")
    configure = f"git config filter.evil.clean '{script} clean-by-app'"
    stat_dirty = "touch -m -t 200101010000 tracked.txt"  # Status then hashes the file.
    if same_command:
        await run(manager, session, f"{configure} && {stat_dirty}")
    else:
        await run(manager, session, configure, "configure")
        os.utime(project / "tracked.txt", (1, 1))
        await run(manager, session, "true", "next")
    assert list(marker.iterdir()) == []


async def test_files_of_the_first_commit_in_a_new_repository_are_listed(runtime, tmp_path):
    manager, session, _ = runtime
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q")
    git(fresh, "config", "user.name", "Test")
    git(fresh, "config", "user.email", "test@example.invalid")
    git(fresh, "config", "commit.gpgsign", "false")
    session["workspace"] = str(fresh)
    await run(manager, session, "echo x > new.txt && git add new.txt && git commit -qm first")
    assert listed(manager, session)["new.txt"]["status"] == "created"
    undo(manager, session, "new.txt")
    assert not (fresh / "new.txt").exists()
