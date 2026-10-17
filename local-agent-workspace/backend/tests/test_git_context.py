"""M8f1: the bounded, hardened Git snapshot in each turn's first message (git_tools.git_context,
AgentManager.git_context_block), Settings → Git status in messages, and the shipped commit, pr, review
and security-review skills."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from local_agent import git_tools
from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.context import GIT_MARKER, request_text
from local_agent.extensions import SKILL_INDEX_DESCRIPTION, ExtensionManager, skill_index
from local_agent.store import Store
from local_agent.tools import WorkspaceTools

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is not installed")
APP_SKILLS = Path(__file__).resolve().parents[2] / "skills"
TOKEN = "dapi" + "0123456789abcdef" * 2  # A token shape, built here so the source holds none.


def git(folder, *arguments):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "init.defaultBranch=main",
                           "-C", str(folder), *arguments], check=True, capture_output=True, text=True).stdout


def program(path, marker):
    path.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def repository(tmp_path):
    """A repository on main tracking a local bare upstream (no network), one commit ahead of it."""
    upstream, workspace = tmp_path / "upstream.git", tmp_path / "project"
    git(tmp_path, "init", "-q", "--bare", str(upstream))
    workspace.mkdir()
    git(workspace, "init", "-q")
    git(workspace, "checkout", "-q", "-b", "main")
    (workspace / "app.py").write_text("print('one')\n")
    git(workspace, "add", "app.py")
    git(workspace, "commit", "-qm", "First commit")
    git(workspace, "remote", "add", "origin", str(upstream))
    git(workspace, "push", "-q", "-u", "origin", "main")
    (workspace / "lib.py").write_text("x = 1\n")
    git(workspace, "add", "lib.py")
    git(workspace, "commit", "-qm", "Add lib")
    return workspace


async def test_the_snapshot_names_branch_upstream_counts_files_and_commits(repository):
    (repository / "app.py").write_text("print('two')\n")  # unstaged
    (repository / "staged.py").write_text("y = 2\n")
    git(repository, "add", "staged.py")  # staged
    (repository / "notes.md").write_text("todo\n")  # untracked
    (repository / ".env").write_text("TOKEN=abc\n")  # a credential file: never named
    (repository / "config").mkdir()
    (repository / "config" / "prod.env").write_text("KEY=1\n")
    text = await git_tools.git_context(repository)
    lines = text.splitlines()
    assert lines[0] == git_tools.CONTEXT_HEADER
    assert "Branch: main" in lines
    assert "Upstream: origin/main, 1 ahead, 0 behind" in lines
    assert "Changes: 1 staged, 1 unstaged, 1 untracked" in lines
    assert "Changed files: app.py, staged.py, notes.md" in lines
    assert lines[-3:] == ["Recent commits:", lines[-2], lines[-1]]
    assert lines[-2].endswith(" Add lib") and lines[-1].endswith(" First commit")
    assert ".env" not in text and "prod.env" not in text
    assert ("\n\n---\n" + git_tools.CONTEXT_HEADER).startswith(GIT_MARKER)  # The app record finds the block.


async def test_a_clean_tree_without_upstream_and_a_workspace_inside_the_repository(repository):
    git(repository, "branch", "--unset-upstream")
    text = await git_tools.git_context(repository)
    assert "Upstream: none" in text and "Changes: none (clean)" in text and "Changed files" not in text
    (repository / "sub").mkdir()
    (repository / "sub" / "kept.py").write_text("k\n")
    git(repository, "add", "sub/kept.py")
    git(repository, "commit", "-qm", "Sub")
    (repository / "sub" / "a.py").write_text("a\n")
    (repository / "outside.py").write_text("b\n")
    inner = await git_tools.git_context(repository / "sub")
    assert "Changes in sub/: 1 untracked" in inner and "Changed files: a.py" in inner and "outside.py" not in inner
    # A folder Git lists as untracked as a whole is named as such, also when it is the workspace itself.
    (repository / "new").mkdir()
    (repository / "new" / "b.py").write_text("b\n")
    assert "new/" in await git_tools.git_context(repository)
    assert "Changed files: ./" in await git_tools.git_context(repository / "new")


async def test_detached_head_and_a_repository_without_commits(tmp_path, repository):
    git(repository, "checkout", "-q", "--detach")
    assert "Branch: detached HEAD at " in await git_tools.git_context(repository)
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    text = await git_tools.git_context(empty)
    assert "(no commits yet)" in text and "Recent commits" not in text


async def test_no_snapshot_outside_a_repository_or_without_a_safe_git(tmp_path, repository, monkeypatch):
    folder = tmp_path / "plain"
    folder.mkdir()
    assert await git_tools.git_context(folder) == ""
    assert await git_tools.git_context(repository, env={"PATH": str(tmp_path / "no-bin")}) == ""
    # A git inside the project (or a granted folder) never runs.
    planted = repository / "bin"
    planted.mkdir()
    marker = tmp_path / "planted-git-ran"
    program(planted / "git", marker)
    assert await git_tools.git_context(repository, env={"PATH": str(planted)}) == ""
    assert not marker.exists()


async def test_a_repository_git_cannot_answer_in_time_is_skipped_for_a_while(repository, tmp_path, monkeypatch):
    slow = tmp_path / "slow-bin"
    slow.mkdir()
    (slow / "git").write_text("#!/bin/sh\nsleep 5\n")
    (slow / "git").chmod(0o755)
    monkeypatch.setattr(git_tools, "_slow_repositories", {})
    assert await git_tools.git_context(repository, env={"PATH": str(slow)}, timeout=0.5) == ""
    assert await git_tools.git_context(repository) == ""  # Not asked again for CONTEXT_RETRY_SECONDS.
    monkeypatch.setattr(git_tools, "CONTEXT_RETRY_SECONDS", 0)
    assert "Branch: main" in await git_tools.git_context(repository)


async def test_fsmonitor_hooks_and_filters_configured_in_the_repository_never_run(repository, tmp_path):
    monitor, filtered = tmp_path / "fsmonitor-ran", tmp_path / "filter-ran"
    git(repository, "config", "core.fsmonitor", str(program(tmp_path / "fsmonitor.sh", monitor)))
    git(repository, "config", "filter.demo.clean", str(program(tmp_path / "clean.sh", filtered)))
    git(repository, "config", "filter.demo.required", "true")
    (repository / ".gitattributes").write_text("*.py filter=demo\n")
    (repository / "app.py").write_text("print('filtered')\n")
    assert "app.py" in await git_tools.git_context(repository)
    assert not monitor.exists() and not filtered.exists()


async def test_the_snapshot_is_bounded_and_redacted(repository):
    for index in range(40):
        (repository / f"{'long-name-' * 6}{index}.py").write_text("x\n")
    for index in range(5):
        git(repository, "commit", "-q", "--allow-empty", "-m",
            f"Commit {index} with {TOKEN} and secret-value {'word ' * 30}")
    text = await git_tools.git_context(repository, redact=lambda value: value.replace("secret-value", "[REDACTED]"))
    assert len(text.encode("utf-8")) <= git_tools.CONTEXT_LIMIT
    assert TOKEN[:12] not in text and "secret-value" not in text and "[REDACTED]" in text
    assert "Changes: 40 untracked" in text and "more" in text
    assert git_tools.context_text({"head": "main"}, [], ["x" * 2000], limit=200).count("\n") <= 3


# The block in the model request: once per turn, in the turn's first message, never the system message.

@pytest.fixture
def runtime(tmp_path, monkeypatch, repository):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(repository), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "git-context.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(permission_mode="acceptEdits", title_generated=True)
    store.save(session)
    yield manager, session, repository
    store.db.close()


def gateway(monkeypatch, replies):
    """Answers each request with the next reply: a tool call (a dict) or text; records the request bodies."""
    requests, client = [], httpx.AsyncClient

    async def handler(request):
        requests.append(json.loads(request.content))
        reply = replies[min(len(requests), len(replies)) - 1]
        delta = {"content": reply} if isinstance(reply, str) else {"tool_calls": [{"index": 0, "id": f"call-{len(requests)}",
                                                                                  "type": "function", "function": reply}]}
        chunk = {"choices": [{"delta": delta, "finish_reason": "stop" if isinstance(reply, str) else "tool_calls"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))
    return requests


async def test_the_block_goes_once_into_each_turns_first_message(runtime, monkeypatch):
    manager, session, repository = runtime
    requests = gateway(monkeypatch, [{"name": "list_files", "arguments": "{}"}, "Listed.", "Second answer."])
    await manager.run(session, "Look around")
    assert session["terminal_reason"] == "completed" and len(requests) == 2
    for body in requests:
        assert git_tools.CONTEXT_HEADER not in body["messages"][0]["content"]  # Not in the system message.
        users = [message for message in body["messages"] if message["role"] == "user"]
        assert [message["content"].count(git_tools.CONTEXT_HEADER) for message in users] == [1]
        assert users[0]["content"].startswith("Look around" + GIT_MARKER)
    # The same first message on the second step: the cached prefix holds.
    assert requests[0]["messages"][1] == requests[1]["messages"][1]
    turn = next(event for event in session["events"] if event["type"] == "user")
    assert turn["git_context"] is True
    # The app record (compaction) keeps only the typed words.
    assert request_text({"role": "user", "content": requests[0]["messages"][1]["content"]}) == "Look around"
    (repository / "new.py").write_text("n\n")
    await manager.run(session, "And now?")
    users = [message for message in requests[-1]["messages"] if message["role"] == "user"]
    assert [message["content"].count(git_tools.CONTEXT_HEADER) for message in users] == [1, 1]
    assert "new.py" in users[1]["content"] and "new.py" not in users[0]["content"]


async def test_the_setting_and_subagents_send_no_block(runtime, monkeypatch):
    manager, session, _ = runtime
    requests = gateway(monkeypatch, ["Done."])
    manager.settings.values["git_context"] = False
    await manager.run(session, "Hello")
    assert git_tools.CONTEXT_HEADER not in json.dumps(requests[-1])
    assert "git_context" not in next(event for event in session["events"] if event["type"] == "user")
    manager.settings.values["git_context"] = True
    child = manager.store.create(manager.settings.values)
    child.update(is_subagent=True, parent_session_id=session["id"], title_generated=True)
    assert await manager.git_context_block(child, manager.session_tools(child)) == ""
    assert (await manager.git_context_block(session, manager.session_tools(session))).startswith(GIT_MARKER)


async def test_the_configured_credential_file_is_left_out_under_any_name(runtime):
    manager, session, repository = runtime
    (repository / "creds.txt").write_text("DBRICKS_TOKEN=x\n")
    (repository / "other.txt").write_text("o\n")
    manager.settings.values["env_file"] = str(repository / "creds.txt")
    block = await manager.git_context_block(session, manager.session_tools(session))
    assert "other.txt" in block and "creds.txt" not in block


# The shipped Git skills.

@pytest.mark.parametrize("skill_id,needle", [
    ("commit", "Never `git add -A`"), ("pr", "Never merge"), ("review", "report_findings"),
    ("security-review", "exploit path")])
def test_the_app_ships_commit_pr_and_review_skills(tmp_path, skill_id, needle):
    manager = ExtensionManager(SimpleNamespace(state_dir=tmp_path, redact=lambda text: text), app_skills=APP_SKILLS)
    tools = WorkspaceTools(str(tmp_path))
    skill = next(skill for skill in manager.skills(tools) if skill["id"] == skill_id)
    assert skill["description"].startswith("Use when") and len(skill["description"]) <= SKILL_INDEX_DESCRIPTION
    text = manager.load_skill(tools, skill_id)["text"]
    assert needle in text and len(text.encode("utf-8")) <= 4000
    assert f"- {skill_id}: Use when" in skill_index(manager.skills(tools))


def test_a_project_skill_of_the_same_id_overrides_a_shipped_one(tmp_path):
    manager = ExtensionManager(SimpleNamespace(state_dir=tmp_path, redact=lambda text: text), app_skills=APP_SKILLS)
    (tmp_path / ".agents/skills/review").mkdir(parents=True)
    (tmp_path / ".agents/skills/review/SKILL.md").write_text("---\ndescription: Our own review\n---\nHouse rules.")
    tools = WorkspaceTools(str(tmp_path))
    reviews = [skill for skill in manager.skills(tools) if skill["id"] == "review"]
    assert [skill["path"] for skill in reviews] == [".agents/skills/review/SKILL.md"]
    assert manager.load_skill(tools, "review")["text"].endswith("House rules.")
