"""GET /api/git: no shell and no programs named by the repository's configuration."""
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from local_agent.api import create_app
from local_agent.config import Settings

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is not installed")


def git(workspace, *arguments):
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "-C", str(workspace), *arguments],
                   check=True, capture_output=True)


def program(path, marker):
    """A script that leaves `marker` behind when Git runs it."""
    path.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(workspace), env_file="")
    with TestClient(create_app(settings)) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        yield client, workspace


@pytest.fixture
def repository(client, tmp_path):
    client, workspace = client
    git(workspace, "init", "-q")
    (workspace / "app.py").write_text("print('old')\n")
    (workspace / ".env").write_text("TOKEN=old\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "initial")
    return client, workspace


def test_repository_fsmonitor_program_does_not_run(repository, tmp_path):
    client, workspace = repository
    marker = tmp_path / "fsmonitor-ran"
    git(workspace, "config", "core.fsmonitor", str(program(tmp_path / "fsmonitor.sh", marker)))
    (workspace / "app.py").write_text("print('new')\n")

    result = client.get("/api/git").json()

    assert not marker.exists()
    assert result["output"].startswith(" M app.py\n")
    assert "+print('new')" in result["output"]
    assert result["exit_code"] == 0 and result["truncated"] is False


def test_repository_clean_filter_program_does_not_run(repository, tmp_path):
    client, workspace = repository
    marker = tmp_path / "filter-ran"
    git(workspace, "config", "filter.demo.clean", str(program(tmp_path / "clean.sh", marker)))
    git(workspace, "config", "filter.demo.required", "true")
    (workspace / ".gitattributes").write_text("*.py filter=demo\n")
    (workspace / "app.py").write_text("print('filtered')\n")

    result = client.get("/api/git").json()

    assert not marker.exists()
    assert "+print('filtered')" in result["output"]


def test_credential_files_stay_out_of_the_diff(repository):
    client, workspace = repository
    (workspace / ".env").write_text("TOKEN=new-secret\n")
    output = client.get("/api/git").json()["output"]
    assert " M .env" in output  # status names the file
    assert "new-secret" not in output


def test_a_folder_that_is_not_a_repository_gets_a_readable_message(client):
    client, _ = client
    response = client.get("/api/git")
    assert response.status_code == 200
    result = response.json()
    assert "not a git repository" in result["output"] and result["exit_code"] != 0


def report_kills_as_not_permitted(monkeypatch):
    """Answer os.killpg as macOS does for a group whose leader has exited but is not yet reaped."""
    import errno
    import os
    killpg = os.killpg

    def not_permitted(pid, sig):
        try:
            killpg(pid, sig)
        except ProcessLookupError:
            pass
        raise PermissionError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(os, "killpg", not_permitted)


@pytest.mark.parametrize("macos", [False, True], ids=["killpg", "killpg-not-permitted"])
async def test_output_is_bounded(tmp_path, monkeypatch, macos):
    from local_agent import git_tools
    if macos:
        report_kills_as_not_permitted(monkeypatch)
    workspace = tmp_path / "big"
    workspace.mkdir()
    git(workspace, "init", "-q")
    for index in range(40):
        (workspace / f"file-{index:02d}-{'x' * 100}.txt").write_text("data\n")
    monkeypatch.setattr(git_tools, "OUTPUT_LIMIT", 1000)
    result = await git_tools.working_tree_changes(workspace)
    assert result["truncated"] is True and result["exit_code"] is None
    assert result["output"].endswith("[Output limit reached: showing the first 1 KB.]")
    assert len(result["output"].encode()) < 1100


async def test_a_timeout_is_reported_even_when_macos_says_the_exited_git_is_not_permitted(tmp_path, monkeypatch):
    from local_agent import git_tools
    report_kills_as_not_permitted(monkeypatch)
    workspace = tmp_path / "project"
    workspace.mkdir()
    git(workspace, "init", "-q")
    with pytest.raises(TimeoutError):
        await git_tools.run_git(workspace, ["status"], timeout=0)


async def test_git_runs_without_a_shell_and_with_a_limited_environment(tmp_path, monkeypatch):
    import asyncio

    from local_agent import git_tools
    monkeypatch.setenv("DATABRICKS_TOKEN", "synthetic-secret")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    calls = []
    original = asyncio.create_subprocess_exec

    async def capture(*argv, **options):
        calls.append((argv, options))
        return await original(*argv, **options)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    await git_tools.working_tree_changes(tmp_path)
    assert calls
    for argv, options in calls:
        assert argv[0].endswith("git") and "core.fsmonitor=false" in argv and "core.hooksPath=/dev/null" in argv
        assert "core.pager=cat" in argv and "submodule.recurse=false" in argv
        assert "DATABRICKS_TOKEN" not in options["env"] and "GIT_DIR" not in options["env"]
        assert options["env"]["GIT_CONFIG_NOSYSTEM"] == "1" and options["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert any("--no-optional-locks" in argv and "status" in argv for argv, _ in calls)


def test_a_filter_whose_driver_name_contains_an_equals_sign_does_not_run(repository, tmp_path):
    """`-c filter.a=b.clean=` would set `filter.a`; the override must reach `filter.a=b.clean`."""
    client, workspace = repository
    marker = tmp_path / "filter-ran"
    git(workspace, "config", "filter.a=b.clean", str(program(tmp_path / "clean.sh", marker)))
    (workspace / ".gitattributes").write_text("*.py filter=a=b\n")
    (workspace / "app.py").write_text("print('filtered')\n")

    result = client.get("/api/git").json()

    assert not marker.exists()
    assert "+print('filtered')" in result["output"]


def test_a_filter_configured_inside_a_submodule_does_not_run(repository, tmp_path):
    client, workspace = repository
    library = tmp_path / "library"
    library.mkdir()
    git(library, "init", "-q")
    (library / "a.txt").write_text("one\n")
    (library / ".gitattributes").write_text("* filter=demo\n")
    git(library, "add", "-A")
    git(library, "commit", "-qm", "library")
    git(workspace, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(library), "sub")
    git(workspace, "commit", "-qm", "add submodule")
    marker = tmp_path / "submodule-filter-ran"
    git(workspace / "sub", "config", "filter.demo.clean", str(program(tmp_path / "clean.sh", marker)))
    (workspace / "sub" / "a.txt").write_text("two\n")
    (workspace / "app.py").write_text("print('new')\n")

    result = client.get("/api/git").json()

    assert not marker.exists()
    assert " M app.py" in result["output"]


def test_credential_files_at_any_depth_stay_out_of_the_diff(repository):
    client, workspace = repository
    names = ["config/.env", "config/.env.production", "config/env_vars.txt", "config/prod.env",
             "deploy/PROD.ENV", "release/prod.Env", "TOP.ENV", "config/.ENV.local",
             "deploy/credentials.json", "keys/ID_RSA", "credentials/token.txt", ".netrc"]
    for name in names:
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_text("TOKEN=old\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-qm", "credentials")
    for name in names:
        (workspace / name).write_text("TOKEN=new-secret\n")
    (workspace / "app.py").write_text("print('new')\n")

    output = client.get("/api/git").json()["output"]

    assert " M config/.env.production" in output  # status names the files
    assert "+print('new')" in output
    assert "new-secret" not in output
