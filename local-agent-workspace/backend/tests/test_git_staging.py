"""M8f3: Stage, Unstage and Commit in the Changes view (POST /api/git/stage, /api/git/commit) and the pull
request chip (GET /api/git/pr, git_tools.pr_status) from a fake gh: no shell, no repository programs (hooks,
fsmonitor, filters, signing), no credential files, nothing while a chat runs, and no network."""
import asyncio
import json
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from local_agent import git_tools
from local_agent.api import create_app
from local_agent.config import Settings

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is not installed")


def git(folder, *arguments):
    return subprocess.run(["git", "-c", "init.defaultBranch=main", "-C", str(folder), *arguments], check=True,
                          capture_output=True, text=True).stdout


def program(path, marker, body="cat"):
    path.write_text(f"#!/bin/sh\ntouch '{marker}'\n{body}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    git(workspace, "init", "-q")
    git(workspace, "config", "user.name", "Test")
    git(workspace, "config", "user.email", "test@localhost")
    (workspace / "app.py").write_text("print('old')\n")
    git(workspace, "add", "app.py")
    git(workspace, "commit", "-qm", "initial")
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(workspace), env_file="")
    with TestClient(create_app(settings)) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        yield client, workspace


def areas(status):
    return {(entry["area"], entry["path"]) for entry in status["files"]}


def stage(client, paths, staged=True):
    return client.post("/api/git/stage", json={"paths": paths, "staged": staged})


def test_stage_and_unstage_chosen_files(client):
    client, workspace = client
    (workspace / "app.py").write_text("print('new')\n")
    (workspace / "new.py").write_text("x = 1\n")
    (workspace / "other.py").write_text("y = 2\n")
    response = stage(client, ["app.py", "new.py"])
    assert response.status_code == 200, response.text
    assert areas(response.json()) == {("staged", "app.py"), ("staged", "new.py"), ("untracked", "other.py")}
    assert git(workspace, "diff", "--cached", "--name-only").split() == ["app.py", "new.py"]
    response = stage(client, ["new.py"], staged=False)
    assert areas(response.json()) == {("staged", "app.py"), ("untracked", "new.py"), ("untracked", "other.py")}
    # A deletion stages as a deletion; unstaging a rename names both of its sides.
    git(workspace, "mv", "app.py", "main.py")
    assert stage(client, ["main.py", "app.py"], staged=False).status_code == 200
    (workspace / "other.py").unlink()
    assert areas(stage(client, ["main.py", "app.py"]).json()) >= {("staged", "main.py")}


def test_unstage_in_a_repository_without_commits(tmp_path, monkeypatch, client):
    client, workspace = client
    shutil.rmtree(workspace / ".git")
    git(workspace, "init", "-q")
    assert stage(client, ["app.py"]).json()["files"][0]["area"] == "staged"
    assert stage(client, ["app.py"], staged=False).json()["files"][0]["area"] == "untracked"
    # Also after the file was edited again (staged content then differs from both the file and HEAD).
    assert stage(client, ["app.py"]).status_code == 200
    (workspace / "app.py").write_text("print('edited after staging')\n")
    response = stage(client, ["app.py"], staged=False)
    assert response.status_code == 200, response.text
    assert areas(response.json()) == {("untracked", "app.py")}
    assert (workspace / "app.py").read_text() == "print('edited after staging')\n"


def test_git_errors_keep_the_rest_of_their_message():
    output = (b"error: the following file has staged content different from both the\nfile and the HEAD:\n"
              b"    app.py\n(use -f to force removal)\nhint: never shown\n")
    assert git_tools.git_error(output, 1) == ("Git: error: the following file has staged content different from both "
                                              "the file and the HEAD: app.py (use -f to force removal)")
    assert git_tools.git_error(b"fatal: one\nfatal: two\nfatal: three\n", 128) == "Git: fatal: one fatal: two"


def test_a_submodule_whose_commit_changed_is_listed_but_not_stageable(client, tmp_path):
    client, workspace = client
    library = tmp_path / "library"
    library.mkdir()
    git(library, "init", "-q")
    identity = ("-c", "user.name=Test", "-c", "user.email=test@localhost")
    git(library, *identity, "commit", "-q", "--allow-empty", "-m", "one")
    git(workspace, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(library), "lib")
    git(workspace, "commit", "-qm", "Add the submodule")
    git(workspace / "lib", *identity, "commit", "-q", "--allow-empty", "-m", "two")
    (workspace / "app.py").write_text("print('new')\n")
    files = {(entry["area"], entry["path"]): entry for entry in client.get("/api/git/status").json()["files"]}
    assert files[("unstaged", "lib")]["submodule"] is True and "submodule" not in files[("unstaged", "app.py")]
    # The server stages files only; the view gives the submodule row no button, so Stage all leaves it out.
    assert stage(client, ["lib"]).json()["detail"] == "Choose one changed file, not a folder."
    assert stage(client, ["app.py"]).status_code == 200


@pytest.mark.parametrize("path, message", [
    (".env", "Credential files are not staged."), ("config/prod.env", "Credential files are not staged."),
    ("../outside.py", "Choose a file inside this project's repository."),
    ("/etc/passwd", "Choose a file inside this project's repository."), ("~/x", "Choose a file inside"),
    ("src", "Choose one changed file, not a folder."), (".", "Choose one changed file, not a folder."),
    ("node_modules/pkg/index.js", "Files in generated folders are not staged here.")])
def test_credential_files_folders_generated_files_and_paths_outside_are_refused(client, path, message):
    client, workspace = client
    (workspace / ".env").write_text("TOKEN=x\n")
    (workspace / "config").mkdir()
    (workspace / "config" / "prod.env").write_text("KEY=1\n")
    (workspace / "src").mkdir()
    (workspace / "src" / "a.py").write_text("a\n")
    response = stage(client, [path])
    assert response.status_code == 400 and response.json()["detail"].startswith(message)
    assert git(workspace, "diff", "--cached", "--name-only") == ""


def test_an_env_example_file_is_staged_shown_and_committed(client):
    """.env.example is not a credential file, as for the file tools and the commands' credential cards."""
    client, workspace = client
    (workspace / ".env.example").write_text("TOKEN=<your token>\n")
    response = stage(client, [".env.example"])
    assert response.status_code == 200, response.text
    entry = next(entry for entry in response.json()["files"] if entry["path"] == ".env.example")
    assert entry["area"] == "staged" and entry["hidden"] is None and entry["added"] == 1
    diff = client.get("/api/git/diff", params={"path": ".env.example", "area": "staged"})
    assert diff.status_code == 200 and diff.json()["added"] == 1
    assert client.post("/api/git/commit", json={"message": "Document the variables"}).status_code == 200
    assert git(workspace, "ls-files", ".env.example").strip() == ".env.example"


def test_a_staged_deletion_of_a_credential_file_commits_but_a_rename_of_one_does_not(client):
    """git rm --cached .env (to stop tracking a committed secret) can be committed; moving its content to
    another name cannot."""
    client, workspace = client
    (workspace / ".env").write_text("TOKEN=x\n")
    git(workspace, "add", "-f", ".env")
    git(workspace, "commit", "-qm", "Track the credentials by mistake")
    git(workspace, "mv", ".env", "notes.txt")
    renamed = client.post("/api/git/commit", json={"message": "Rename"})
    assert renamed.status_code == 400
    assert renamed.json()["detail"].startswith("notes.txt is staged as a rename or copy of the credential file .env.")
    git(workspace, "mv", "notes.txt", ".env")
    git(workspace, "rm", "-q", "--cached", ".env")
    response = client.post("/api/git/commit", json={"message": "Stop tracking .env"})
    assert response.status_code == 200, response.text
    assert git(workspace, "ls-files", ".env") == "" and (workspace / ".env").read_text() == "TOKEN=x\n"


def test_the_configured_credential_file_is_never_staged_under_another_name(client, tmp_path):
    client, workspace = client
    (workspace / "creds.txt").write_text("DBRICKS_TOKEN=x\n")
    settings = client.app.state.settings
    settings.values["env_file"] = str(workspace / "creds.txt")
    response = stage(client, ["creds.txt"])
    assert response.status_code == 400 and "Credential" in response.json()["detail"]
    assert git(workspace, "diff", "--cached", "--name-only") == ""


def test_one_request_stages_at_most_100_kb_of_paths(client):
    client, workspace = client
    response = stage(client, [f"{'a' * 200}{index}.py" for index in range(600)])
    assert response.status_code == 400 and response.json()["detail"] == "Too many files at once: stage fewer."
    assert stage(client, [f"{index}.py" for index in range(4_001)]).status_code == 422


def test_ignored_files_are_not_forced_in(client):
    client, workspace = client
    (workspace / ".gitignore").write_text("*.log\n")
    (workspace / "debug.log").write_text("x\n")
    response = stage(client, ["debug.log"])
    assert response.status_code == 400
    assert response.json()["detail"].startswith("Git: The following paths are ignored by one of your .gitignore files")
    assert "hint" not in response.json()["detail"]
    assert "debug.log" not in git(workspace, "diff", "--cached", "--name-only")


def test_repository_programs_never_run_and_a_filtered_file_is_refused(client, tmp_path):
    client, workspace = client
    markers = {name: tmp_path / f"{name}-ran" for name in ("fsmonitor", "hook", "hooks-path", "filter", "gpg")}
    git(workspace, "config", "core.fsmonitor", str(program(tmp_path / "fsmonitor.sh", markers["fsmonitor"])))
    for name in ("pre-commit", "commit-msg", "post-commit", "post-index-change"):
        program(workspace / ".git" / "hooks" / name, markers["hook"], "exit 0")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("pre-commit", "post-commit"):
        program(hooks / name, markers["hooks-path"], "exit 0")
    git(workspace, "config", "core.hooksPath", str(hooks))
    git(workspace, "config", "commit.gpgSign", "true")
    git(workspace, "config", "gpg.program", str(program(tmp_path / "gpg.sh", markers["gpg"], "exit 1")))
    git(workspace, "config", "filter.demo.clean", str(program(tmp_path / "clean.sh", markers["filter"])))
    (workspace / ".gitattributes").write_text("*.bin filter=demo\n")
    (workspace / "app.py").write_text("print('new')\n")
    (workspace / "data.bin").write_text("raw\n")
    assert stage(client, ["app.py", ".gitattributes"]).status_code == 200
    refused = stage(client, ["data.bin"])
    assert refused.status_code == 400 and refused.json()["detail"].startswith(
        "data.bin uses a Git filter (such as Git LFS), whose program the app does not run")
    response = client.post("/api/git/commit", json={"message": "Update the app\n\nWith a body."})
    assert response.status_code == 200, response.text
    commit = response.json()["commit"]
    assert commit["subject"] == "Update the app" and len(commit["hash"]) >= 7
    assert git(workspace, "log", "-1", "--format=%s%n%b").split("\n")[:3] == ["Update the app", "With a body.", ""]
    assert all(not marker.exists() for marker in markers.values())
    assert ("staged", "app.py") not in areas(response.json()["status"])


def test_a_file_whose_filter_has_no_visible_program_is_refused_too(client):
    """Git LFS set up in the system configuration (which the app's Git does not read) would otherwise be
    staged unfiltered: the whole file instead of its pointer."""
    client, workspace = client
    (workspace / ".gitattributes").write_text("*.bin filter=lfs\n*.txt -filter\n")
    (workspace / "model.bin").write_text("weights\n")
    (workspace / "notes.txt").write_text("notes\n")
    refused = stage(client, ["model.bin"])
    assert refused.status_code == 400 and refused.json()["detail"].startswith("model.bin uses a Git filter")
    assert stage(client, ["notes.txt", ".gitattributes"]).status_code == 200
    assert git(workspace, "diff", "--cached", "--name-only").split() == [".gitattributes", "notes.txt"]


def test_a_commit_without_a_git_identity_says_what_to_set(client, tmp_path, monkeypatch):
    client, workspace = client
    git(workspace, "config", "--unset", "user.name")
    git(workspace, "config", "--unset", "user.email")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # No ~/.gitconfig with an identity.
    (workspace / "app.py").write_text("print('new')\n")
    stage(client, ["app.py"])
    git(workspace, "config", "user.useConfigOnly", "true")
    response = client.post("/api/git/commit", json={"message": "No identity"})
    assert response.status_code == 400 and response.json()["detail"].startswith("Git does not know who you are")


def test_commit_needs_a_message_and_staged_files_inside_the_project_without_credentials(client, tmp_path):
    client, workspace = client
    assert client.post("/api/git/commit", json={"message": "   "}).status_code == 400
    assert client.post("/api/git/commit", json={"message": ""}).status_code == 422
    nothing = client.post("/api/git/commit", json={"message": "Empty"})
    assert nothing.status_code == 400 and nothing.json()["detail"] == "Nothing is staged. Stage the files to commit first."
    (workspace / ".env").write_text("TOKEN=x\n")
    git(workspace, "add", "-f", ".env")  # Staged in a terminal.
    secret = client.post("/api/git/commit", json={"message": "Oops"})
    assert secret.status_code == 400 and secret.json()["detail"].startswith(".env is a credential file and it is staged.")
    git(workspace, "rm", "-q", "--cached", ".env")
    # A chat on a folder inside the repository commits only what is inside that folder.
    (workspace / "sub").mkdir()
    (workspace / "sub" / "a.py").write_text("a\n")
    (workspace / "top.py").write_text("t\n")
    git(workspace, "add", "sub/a.py", "top.py")
    session = client.post("/api/sessions", json={}).json()
    manager = client.app.state.manager
    manager.store.get(session["id"])
    stored = manager.get(session["id"])
    stored["workspace"] = str(workspace / "sub")
    manager.store.save(stored)
    outside = client.post(f"/api/git/commit?session_id={session['id']}", json={"message": "Sub only"})
    assert outside.status_code == 400 and outside.json()["detail"].startswith("1 staged file is outside this project folder (top.py)")
    git(workspace, "rm", "-q", "--cached", "top.py")
    inside = client.post(f"/api/git/commit?session_id={session['id']}", json={"message": "Sub only"})
    assert inside.status_code == 200, inside.text
    assert git(workspace, "show", "--name-only", "--format=", "HEAD").split() == ["sub/a.py"]


def test_staging_and_committing_wait_while_a_chat_in_the_project_runs(client):
    client, workspace = client
    session = client.post("/api/sessions", json={}).json()
    manager = client.app.state.manager
    manager.statuses[session["id"]] = "running"
    (workspace / "app.py").write_text("print('new')\n")
    for response in (stage(client, ["app.py"]), client.post("/api/git/commit", json={"message": "x"})):
        assert response.status_code == 409
        assert "is still running" in response.json()["detail"]["message"]
    manager.statuses[session["id"]] = "idle"
    assert stage(client, ["app.py"]).status_code == 200


# The pull request chip: a fake gh on PATH, outside the project.

FAKE_GH = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
env > "{env}"
case "$(cat "{mode}")" in
  open) printf '%s' '{open}' ;;
  draft) printf '%s' '{draft}' ;;
  none) echo 'no pull requests found for branch "main"' >&2; exit 1 ;;
  login) echo 'To get started with GitHub CLI, please run:  gh auth login' >&2; exit 4 ;;
  slow) sleep 30 ;;
  gitlab) echo 'none of the git remotes configured for this repository point to a known GitHub host. To tell gh about a new GitHub host, please use `gh auth login`' >&2; exit 1 ;;
  odd) echo 'unexpected failure {token}' >&2; exit 1 ;;
esac
"""
OPEN = {"number": 12, "title": "Add the exporter", "state": "OPEN", "isDraft": False, "url": "https://example.com/o/r/pull/12",
        "reviewDecision": "REVIEW_REQUIRED", "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"__typename": "CheckRun", "name": "build", "status": "IN_PROGRESS", "conclusion": ""},
            {"__typename": "StatusContext", "context": "ci", "state": "SUCCESS"}]}


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    folder = tmp_path / "gh-bin"
    folder.mkdir()
    paths = {name: tmp_path / f"gh-{name}" for name in ("log", "env", "mode")}
    draft = {**OPEN, "isDraft": True, "statusCheckRollup": [], "url": "javascript:alert(1)"}
    (folder / "gh").write_text(FAKE_GH.format(open=json.dumps(OPEN), draft=json.dumps(draft),
                                              token="dapi" + "0123456789abcdef" * 2, **paths))
    (folder / "gh").chmod(0o755)
    paths["mode"].write_text("open")
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("DBRICKS_TOKEN", "never-for-gh")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    return paths


def pr(client, **params):
    response = client.get("/api/git/pr", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_chip_reads_gh_pr_view_and_is_cached(client, fake_gh):
    client, workspace = client
    result = pr(client)
    assert result == {"available": True, "number": 12, "title": "Add the exporter", "state": "open", "draft": False,
                      "url": "https://example.com/o/r/pull/12", "review_decision": "review_required",
                      "checks": {"total": 4, "passed": 2, "failed": 1, "pending": 1}}
    assert fake_gh["log"].read_text() == f"pr view --json {git_tools.PR_FIELDS}\n"
    env = dict(line.split("=", 1) for line in fake_gh["env"].read_text().splitlines() if "=" in line)
    assert "DBRICKS_TOKEN" not in env and env["GH_PROMPT_DISABLED"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
    # Where gh keeps its sign-in on a Linux desktop (the keyring, over the session bus).
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
    assert env["GIT_CONFIG_KEY_0"] == "core.fsmonitor" and env["GIT_CONFIG_KEY_1"] == "core.hooksPath"
    assert env["PATH"].split(os.pathsep)[0] == os.path.dirname(shutil.which("git"))
    # Within a minute (and within 5 s with Refresh) gh is not asked again for the same branch.
    fake_gh["mode"].write_text("none")
    assert pr(client) == result and pr(client, refresh="true") == result
    assert len(fake_gh["log"].read_text().splitlines()) == 1
    # Another branch (the Changes view names the one it shows) is asked at once.
    assert pr(client, branch="feature/next")["code"] == "no_pr"
    assert pr(client, branch="feature/next")["code"] == "no_pr" and len(fake_gh["log"].read_text().splitlines()) == 2
    assert client.get("/api/git/pr", params={"branch": "b" * 301}).status_code == 422


@pytest.mark.parametrize("mode, expected", [
    ("draft", {"available": True, "draft": True, "url": None, "checks": {"total": 0, "passed": 0, "failed": 0, "pending": 0}}),
    ("none", {"available": False, "code": "no_pr", "reason": "No pull request for this branch."}),
    ("login", {"available": False, "code": "not_signed_in"}),
    # gh's message for a remote that is not on GitHub also names gh auth login: no note in the Changes view.
    ("gitlab", {"available": False, "code": "no_remote", "reason": "This repository has no GitHub remote."}),
    ("odd", {"available": False, "code": "error", "reason": "gh pr view failed: unexpected failure [REDACTED]"}),
])
async def test_draft_no_pull_request_signed_out_and_other_failures(tmp_path, fake_gh, mode, expected):
    fake_gh["mode"].write_text(mode)
    (tmp_path / "work").mkdir()
    result = await git_tools.pr_status(tmp_path / "work", env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    assert {key: result[key] for key in expected} == expected


async def test_a_missing_slow_or_planted_gh(tmp_path, fake_gh):
    work = tmp_path / "work"
    work.mkdir()
    # A folder with only Git in it (a CI runner may have a real gh next to its git).
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git"))
    missing = await git_tools.pr_status(work, env={"PATH": str(only_git)})
    assert missing["available"] is False and missing["code"] == "no_gh"
    fake_gh["mode"].write_text("slow")
    started = asyncio.get_running_loop().time()
    slow = await git_tools.pr_status(work, env={"PATH": os.environ["PATH"]}, timeout=1)
    assert slow == {"available": False, "code": "timeout", "reason": "GitHub CLI did not answer within 1 seconds."}
    assert asyncio.get_running_loop().time() - started < 10
    # A gh inside the project or a granted folder never runs.
    fake_gh["log"].unlink()
    planted = await git_tools.pr_status(work, env={"PATH": os.environ["PATH"]}, forbidden_roots=[tmp_path / "gh-bin"])
    assert planted["available"] is False and planted["code"] == "refused" and "inside the project" in planted["reason"]
    assert not fake_gh["log"].exists()
