import asyncio
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.store import Store
from local_agent.worktrees import WorktreeManager


def git(path, *arguments):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(path), *arguments],
                          check=True, text=True, capture_output=True).stdout.strip()


@pytest.fixture
def worktrees(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Isolated Test")
    git(repo, "config", "user.email", "isolated@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "tracked.txt").write_text("committed content")
    (repo / ".gitignore").write_text("ignored.txt\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    state = tmp_path / "state"
    state.mkdir()
    store = Store(state / "state.sqlite3")
    yield WorktreeManager(store, state), repo, store
    store.db.close()


async def test_create_uses_head_without_mutating_dirty_original_and_retains_branch(worktrees):
    manager, repo, _ = worktrees
    original_head = git(repo, "rev-parse", "HEAD")
    (repo / "tracked.txt").write_text("dirty source")
    (repo / "untracked.txt").write_text("source only")
    record = await manager.create(str(repo), "codex/isolated")
    target = Path(record["path"])
    assert target.parent == manager.base
    assert (target / "tracked.txt").read_text() == "committed content"
    assert not (target / "untracked.txt").exists()
    assert git(target, "branch", "--show-current") == "codex/isolated"
    assert git(target, "rev-parse", "HEAD") == original_head
    assert git(repo, "branch", "--show-current") == "main"
    assert (repo / "tracked.txt").read_text() == "dirty source"
    assert manager.list(str(repo)) == [record]
    assert manager.list(str(target)) == [record]
    assert await manager.remove(record["id"]) == record
    assert not target.exists() and manager.list() == []
    assert git(repo, "rev-parse", "refs/heads/codex/isolated") == original_head


async def test_clean_local_commits_survive_worktree_removal(worktrees):
    manager, repo, _ = worktrees
    record = await manager.create(repo, "my-exact-branch")
    target = Path(record["path"])
    (target / "tracked.txt").write_text("new commit")
    git(target, "add", "tracked.txt")
    git(target, "commit", "-m", "local work")
    committed = git(target, "rev-parse", "HEAD")
    await manager.remove(record["id"])
    assert git(repo, "rev-parse", "refs/heads/my-exact-branch") == committed


@pytest.mark.parametrize("change", ["modified", "untracked", "ignored"])
async def test_remove_refuses_data_loss_for_dirty_files(worktrees, change):
    manager, repo, _ = worktrees
    record = await manager.create(repo, "codex/dirty")
    target = Path(record["path"])
    filename = {"modified": "tracked.txt", "untracked": "new.txt", "ignored": "ignored.txt"}[change]
    (target / filename).write_text("must survive")
    with pytest.raises(ValueError, match="modified, untracked, or ignored"):
        await manager.remove(record["id"])
    assert (target / filename).read_text() == "must survive"
    assert manager.list() == [record]
    assert manager.removing_paths == set()


@pytest.mark.parametrize("branch", ["", "-bad", "with space", "bad..name", "refs/heads/", "a" * 201])
async def test_invalid_branch_names_are_rejected_without_shell_execution(worktrees, branch):
    manager, repo, _ = worktrees
    with pytest.raises(ValueError):
        await manager.create(repo, branch)
    assert manager.list() == []
    assert git(repo, "branch", "--show-current") == "main"


async def test_shell_syntax_is_a_literal_branch_argument(worktrees):
    manager, repo, _ = worktrees
    record = await manager.create(repo, "codex/safe;touch-NOT-A-COMMAND")
    assert git(Path(record["path"]), "branch", "--show-current") == "codex/safe;touch-NOT-A-COMMAND"
    assert not (repo / "NOT-A-COMMAND").exists()
    await manager.remove(record["id"])


async def test_existing_and_unborn_branches_are_rejected(worktrees, tmp_path):
    manager, repo, _ = worktrees
    with pytest.raises(ValueError, match="already exists"):
        await manager.create(repo, "main")
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    git(unborn, "init", "-b", "main")
    with pytest.raises(ValueError, match="unborn"):
        await manager.create(unborn, "codex/new")
    assert manager.list() == []


async def test_remove_rejects_unknown_and_symlinked_owned_paths(worktrees, tmp_path):
    manager, repo, _ = worktrees
    with pytest.raises(ValueError, match="not found"):
        await manager.remove("not-owned")
    record = await manager.create(repo, "codex/swapped")
    target = Path(record["path"])
    saved = tmp_path / "saved-worktree"
    target.rename(saved)
    target.symlink_to(repo, target_is_directory=True)
    with pytest.raises(ValueError, match="app-owned"):
        await manager.remove(record["id"])
    with pytest.raises(ValueError, match="app-owned"):
        manager.validate(record["id"])
    assert (repo / "tracked.txt").read_text() == "committed content"
    target.unlink()
    saved.rename(target)
    await manager.remove(record["id"])


async def test_symlinked_managed_base_is_rejected(worktrees, tmp_path):
    manager, repo, _ = worktrees
    other = tmp_path / "other"
    other.mkdir()
    manager.base.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="app-owned"):
        await manager.create(repo, "codex/blocked")
    assert list(other.iterdir()) == []


async def test_remove_blocks_sessions_and_running_jobs_in_subdirectories(worktrees):
    manager, repo, store = worktrees
    record = await manager.create(repo, "codex/used")
    target = Path(record["path"])
    nested = target / "nested"
    nested.mkdir()
    session = store.create({"workspace": str(nested), "model": "fake"})
    with pytest.raises(ValueError, match="conversation or running command"):
        await manager.remove(record["id"])
    store.delete(session["id"])
    store.db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, session_id TEXT, workspace TEXT, data TEXT)")
    job = {"id": "job", "workspace": str(nested), "state": "running"}
    store.db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?)", ("job", None, str(nested), json.dumps(job)))
    store.db.commit()
    with pytest.raises(ValueError, match="conversation or running command"):
        await manager.remove(record["id"])
    job["state"] = "completed"
    store.db.execute("UPDATE jobs SET data=? WHERE id='job'", (json.dumps(job),))
    store.db.commit()
    nested.rmdir()
    await manager.remove(record["id"])


async def test_metadata_survives_manager_restart(worktrees):
    manager, repo, store = worktrees
    record = await manager.create(repo, "codex/restart")
    restarted = WorktreeManager(store, manager.base.parent)
    assert restarted.list() == [record]
    await restarted.remove(record["id"])


async def test_hooks_and_checkout_filters_do_not_execute(worktrees):
    manager, repo, _ = worktrees
    hook_marker = repo.parent / "hook-ran"
    filter_marker = repo.parent / "filter-ran"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(hook_marker))}\n")
    hook.chmod(0o755)
    (repo / ".gitattributes").write_text("tracked.txt filter=tripwire\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-m", "attributes")
    git(repo, "config", "filter.tripwire.smudge", f"touch {shlex.quote(str(filter_marker))}; cat")
    git(repo, "config", "filter.tripwire.clean", f"touch {shlex.quote(str(filter_marker))}; cat")
    record = await manager.create(repo, "codex/no-hooks")
    assert not hook_marker.exists() and not filter_marker.exists()
    await manager.remove(record["id"])
    assert not hook_marker.exists() and not filter_marker.exists()


async def test_git_environment_omits_gateway_credentials(worktrees, monkeypatch):
    manager, repo, _ = worktrees
    monkeypatch.setenv("DBRICKS_TOKEN", "synthetic-secret")
    monkeypatch.setenv("DATABRICKS_TOKEN", "synthetic-secret")
    original = asyncio.create_subprocess_exec
    seen = []

    async def capture(*arguments, **options):
        seen.append(options["env"])
        return await original(*arguments, **options)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    record = await manager.create(repo, "codex/environment")
    await manager.remove(record["id"])
    assert seen and all("DBRICKS_TOKEN" not in env and "DATABRICKS_TOKEN" not in env for env in seen)


async def test_cancelled_creation_still_registers_completed_worktree(worktrees, monkeypatch):
    manager, repo, _ = worktrees
    created, release = asyncio.Event(), asyncio.Event()
    original = manager._git

    async def gated(workspace, arguments, **options):
        result = await original(workspace, arguments, **options)
        if arguments[:2] == ["worktree", "add"]:
            created.set()
            await release.wait()
        return result

    monkeypatch.setattr(manager, "_git", gated)
    task = asyncio.create_task(manager.create(repo, "codex/cancelled"))
    try:
        await asyncio.wait_for(created.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    record, = manager.list()
    assert Path(record["path"]).is_dir()
    await manager.remove(record["id"])


async def test_open_worktree_api_rejects_symlink_replacement(worktrees, tmp_path, monkeypatch):
    _, repo, _ = worktrees
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "api-state")
    settings.env = {}
    settings.values.update(workspace=str(repo), env_file="")
    settings.credentials = lambda: ("https://fake.example", "synthetic-test-token")
    app = create_app(settings)
    manager = app.state.manager.worktrees
    record = await manager.create(repo, "codex/api-owned")
    target = Path(record["path"])
    saved = tmp_path / "saved"
    target.rename(saved)
    target.symlink_to(repo, target_is_directory=True)
    try:
        with TestClient(app) as client:
            headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
            response = client.post(f"/api/worktrees/{record['id']}/session", headers=headers)
            assert response.status_code == 400
            assert "app-owned" in response.json()["detail"]
            assert client.get("/api/sessions", headers=headers).json() == []
    finally:
        target.unlink()
        saved.rename(target)
        # The TestClient lifespan closed its database; Git cleanup is sufficient.
        git(repo, "worktree", "remove", str(target))
