"""Commands run from an argument vector without a shell, and hardened read-only commands.

run_process(argv=) never calls shell_argv; prepare_command resolves programs on the
absolute PATH entries only; read-only Git runs with the programs a repository's
configuration can name switched off, credential files left out, and output kept inside
the workspace."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from local_agent import jobs
from local_agent.agents import AgentManager
from local_agent.command_exec import absolute_path_entries, prepare_command, resolve_executable
from local_agent.config import Settings
from local_agent.jobs import CommandTimeout, JobManager, command_environment, run_process
from local_agent.store import Store

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="Git is not installed")

# P1's rules.READ_ONLY_COMMANDS: the pinned list of read-only commands.
READ_ONLY_GIT = [
    "git status", "git status --short", "git status -s", "git status --porcelain",
    "git diff", "git diff --stat", "git diff --name-only", "git diff --cached", "git diff --staged",
    "git diff --cached --stat", "git log --oneline", "git log --oneline -n 20", "git show", "git show --stat",
    "git branch", "git branch --show-current", "git rev-parse HEAD", "git rev-parse --abbrev-ref HEAD",
    "git rev-parse --show-toplevel", "git ls-files",
]
READ_ONLY_OTHER = ["pwd", "ls", "ls -l", "ls -a", "ls -la", "ls -al", "ls -1", "/bin/pwd", "/bin/ls"]
NON_EMPTY = ("status", "diff", "log", "show", "ls-files")


@pytest.fixture
def no_shell(monkeypatch):
    """Any use of the shell fails the test."""
    def refused(command):
        raise AssertionError(f"shell_argv was called for {command!r}")
    monkeypatch.setattr(jobs, "shell_argv", refused)


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "jobs.sqlite3")
    yield instance
    instance.db.close()


def alive(pid):
    """Whether a process runs (a zombie waiting to be reaped does not count)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state not in ("Z", "X")
    except FileNotFoundError:
        return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def gone(pid, seconds=3):
    async with asyncio.timeout(seconds):
        while alive(pid):
            await asyncio.sleep(.05)


# A Python program (run directly, not through sh) that starts a sleeping child and prints its pid.
PARENT_OF_SLEEPER = ("import subprocess, sys, time\n"
                     "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                     "print(child.pid, flush=True)\n"
                     "time.sleep(60)\n")


# --- run_process and JobManager with argv ---------------------------------------------------------

async def test_argv_runs_without_a_shell_and_prints_shell_syntax_literally(tmp_path, no_shell):
    result = await run_process("echo 'a;b $(id)'", tmp_path, argv=["/bin/echo", "a;b $(id)"])
    assert result == {"exit_code": 0, "output": "a;b $(id)\n", "truncated": False}


async def test_argv_jobs_see_only_the_filtered_environment(tmp_path, monkeypatch, no_shell):
    monkeypatch.setenv("UNRELATED_SECRET", "not-for-commands")
    monkeypatch.setenv("DATABRICKS_TOKEN", "nor-this")
    env_program = shutil.which("env")
    result = await run_process("env", tmp_path, argv=[env_program])
    assert result["exit_code"] == 0
    names = {line.split("=", 1)[0] for line in result["output"].splitlines()}
    assert "UNRELATED_SECRET" not in names and "DATABRICKS_TOKEN" not in names
    assert names <= jobs.ENVIRONMENT_KEYS
    assert f"PATH={os.environ['PATH']}" in result["output"].splitlines()
    assert command_environment() == {key: value for key, value in os.environ.items() if key in jobs.ENVIRONMENT_KEYS}


async def test_argv_jobs_keep_the_variables_passed_through_by_name(store, tmp_path, no_shell):
    manager = JobManager(store, lambda text: text, None, environment=lambda: {"PASSED_BY_NAME": "visible"})
    env = manager.command_environment()
    assert env["PASSED_BY_NAME"] == "visible"
    job = await manager.start("env", tmp_path, argv=[shutil.which("env")], env=env)
    job = await manager.wait(job["id"])
    assert "PASSED_BY_NAME=visible" in job["output"].splitlines()


@pytest.mark.parametrize("argv", [[], [""], ["a"] * 65, ["x" * 1001], ["echo", "a\0b"], "echo", [1]])
async def test_invalid_argv_is_refused(tmp_path, argv, no_shell):
    with pytest.raises(ValueError, match="without a shell needs 1 to 64 arguments"):
        await run_process("text", tmp_path, argv=argv)


async def test_a_timeout_stops_the_process_group_of_an_argv_job(tmp_path, killpg_answer, no_shell):
    seen = asyncio.Event()
    lines = []

    async def output(text, truncated):
        if text.strip():
            lines.append(text)
            seen.set()

    task = asyncio.create_task(run_process("parent of a sleeper", tmp_path, timeout_seconds=1, on_output=output,
                                           argv=[sys.executable, "-c", PARENT_OF_SLEEPER]))
    await asyncio.wait_for(seen.wait(), 5)
    child = int(lines[0].split()[0])
    assert alive(child)
    with pytest.raises(CommandTimeout):
        await asyncio.wait_for(task, 10)
    await gone(child)


async def test_stop_ends_the_process_group_of_an_argv_job(store, tmp_path, no_shell):
    manager = JobManager(store, lambda text: text, None)
    job = await manager.start("parent of a sleeper", tmp_path, background=True,
                              argv=[sys.executable, "-c", PARENT_OF_SLEEPER])
    async with asyncio.timeout(5):
        while not manager.get(job["id"])["output"].strip():
            await asyncio.sleep(.05)
    child = int(manager.get(job["id"])["output"].split()[0])
    assert alive(child)
    stopped = await asyncio.wait_for(manager.stop(job["id"]), 10)
    assert stopped["state"] == "cancelled"
    await gone(child)


async def test_argv_job_records_say_without_shell_and_never_hold_argv_or_env(store, tmp_path, no_shell):
    session = {"id": "chat", "title": "Commands", "created": 1, "updated": 1, "workspace": str(tmp_path),
               "model": "model", "events": [], "wire": []}
    store.save(session)
    published = []

    async def notify(job):
        published.append(json.dumps(job))

    manager = JobManager(store, lambda text: text, notify)
    env = {**command_environment(), "ARGV_ONLY_VARIABLE": "env-value-never-stored"}
    job = await manager.start("echo shown", tmp_path, session_id="chat",
                              argv=["/bin/echo", "argv-token-never-stored"], env=env)
    assert job["without_shell"] is True and job["command"] == "echo shown"
    job = await manager.wait(job["id"])
    assert job["output"] == "argv-token-never-stored\n"  # The output is the command's own, as always.
    saved = store.get("chat")["command_jobs"]
    assert saved == [job]
    stored = [row[0] for row in store.db.execute("SELECT data FROM jobs")]
    mirror = (store.conversations / "chat.jsonl").read_text()
    for text in [*published, *stored, mirror]:
        assert "env-value-never-stored" not in text and "ARGV_ONLY_VARIABLE" not in text
        assert '"argv"' not in text and '"env"' not in text
    assert "argv-token-never-stored" not in published[0]  # Published before the command ran.
    shell_job = await manager.start("printf plain", tmp_path)
    assert "without_shell" not in shell_job


# --- prepare_command: programs on PATH ------------------------------------------------------------

def planted(path, marker):
    path.write_text(f"#!/bin/sh\ntouch '{marker}'\necho planted-program-output\n")
    path.chmod(0o755)
    return path


def test_absolute_path_entries_drop_empty_and_relative_entries():
    env = {"PATH": os.pathsep.join(["", ".", "bin", "/usr/bin", "./tools", "/bin"])}
    assert absolute_path_entries(env) == ["/usr/bin", "/bin"]
    assert absolute_path_entries({}) == []


def test_relative_path_entries_never_find_a_planted_program(tmp_path):
    marker = tmp_path / "planted-ran"
    planted(tmp_path / "planted-tool-p4", marker)
    (tmp_path / "bin").mkdir()
    planted(tmp_path / "bin" / "planted-tool-p4", marker)
    for path in (f".{os.pathsep}{os.environ['PATH']}", f"bin{os.pathsep}{os.environ['PATH']}",
                 f"{os.pathsep}{os.environ['PATH']}"):
        with pytest.raises(ValueError) as caught:
            resolve_executable("planted-tool-p4", {"PATH": path}, tmp_path)
        assert str(caught.value) == "Command not found on PATH: planted-tool-p4"
    assert resolve_executable("planted-tool-p4", {"PATH": str(tmp_path / "bin")}, tmp_path) == str(
        tmp_path / "bin" / "planted-tool-p4")


def test_not_found_messages_are_exact(tmp_path):
    (tmp_path / "plain.txt").write_text("not a program")
    for name in ("./missing", "./plain.txt", "sub/missing"):
        with pytest.raises(ValueError) as caught:
            resolve_executable(name, {"PATH": "/usr/bin"}, tmp_path)
        assert str(caught.value) == f"Command not found: {name}"
    with pytest.raises(ValueError) as caught:
        resolve_executable("no-such-program-p4", {"PATH": "/usr/bin:/bin"}, tmp_path)
    assert str(caught.value) == "Command not found on PATH: no-such-program-p4"
    script = planted(tmp_path / "script.sh", tmp_path / "unused")
    assert resolve_executable("./script.sh", {}, tmp_path) == str(script)


async def test_prepare_command_resolves_programs_for_rule_allowed_commands(tmp_path):
    env = {"PATH": "/usr/bin:/bin"}
    argv, prepared = await prepare_command(["echo", "a b"], workspace=tmp_path, forbidden_roots=[tmp_path],
                                           read_only=False, env=env)
    assert argv == [shutil.which("echo", path="/usr/bin:/bin"), "a b"] and prepared is env


# --- AgentManager.command_tool --------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.env = {}
    workspace = tmp_path / "project"
    workspace.mkdir()
    settings.values.update(workspace=str(workspace), env_file="")
    settings.credentials = lambda: ("https://fake.example", "synthetic-secret-token")
    return settings


@pytest.fixture
async def runtime(settings):
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    manager.live[session["id"]] = session
    yield manager, session
    await manager.jobs.shutdown()
    store.db.close()


def tool_event(session, command):
    event = {"id": f"tool-{len(session['events'])}", "type": "tool", "name": "run_command",
             "arguments": {"command": command}, "status": "running"}
    session["events"].append(event)
    return event


async def run(manager, session, command, *, read_only=True, argv=None, **options):
    event = tool_event(session, command)
    result = await manager.command_tool(session, event, {"command": command, **options},
                                        argv=argv if argv is not None else command.split(), read_only=read_only)
    return result, event


async def test_command_tool_runs_argv_without_a_shell(runtime, no_shell):
    manager, session = runtime
    result, event = await run(manager, session, "echo 'a;b'", argv=["echo", "a;b"], read_only=False)
    assert result["exit_code"] == 0 and result["output"] == "a;b\n"
    assert event["without_shell"] is True and event["job_id"] == result["job_id"]
    assert manager.jobs.get(result["job_id"])["without_shell"] is True


async def test_command_tool_without_argv_is_unchanged(runtime):
    manager, session = runtime
    event = tool_event(session, "printf '%s' shell")
    result = await manager.command_tool(session, event, {"command": "printf '%s' shell"})
    assert result["output"] == "shell"
    assert "without_shell" not in event and "without_shell" not in manager.jobs.get(result["job_id"])


async def test_command_tool_reports_a_program_not_on_path(runtime, no_shell):
    manager, session = runtime
    with pytest.raises(ValueError, match="^Command not found on PATH: no-such-program-p4$"):
        await run(manager, session, "no-such-program-p4 x", read_only=False)
    assert manager.jobs.list() == []


@pytest.mark.parametrize("argv, message", [
    (["cat", "x"], "Not a read-only command."),
    (["bash", "-c", "id"], "Not a read-only command."),
    (["./ls"], "Not a read-only command."),
    (["git", "push"], "Not a read-only Git command."),
    (["git", "remote", "-v"], "Not a read-only Git command."),
    (["git"], "Not a read-only Git command."),
    (["git", "diff", "--ext-diff"], "This Git option is not allowed in a read-only command."),
    (["git", "log", "--show-signature"], "This Git option is not allowed in a read-only command."),
    (["git", "diff", "--output=/srv/x"], "This Git option is not allowed in a read-only command."),
    (["git", "diff", "--", "../outside"], "This Git option is not allowed in a read-only command."),
])
async def test_read_only_refuses_other_commands(runtime, argv, message, no_shell):
    manager, session = runtime
    if argv[0] == "git" and shutil.which("git") is None:
        pytest.skip("Git is not installed")
    with pytest.raises(ValueError) as caught:
        await run(manager, session, " ".join(argv), argv=argv)
    assert str(caught.value) == message
    assert manager.jobs.list() == []


async def test_read_only_commands_run_in_the_foreground_only(runtime, no_shell):
    manager, session = runtime
    with pytest.raises(ValueError, match="^Read-only commands run in the foreground only.$"):
        await run(manager, session, "ls", background=True)
    assert manager.jobs.list() == []


@pytest.mark.parametrize("command", READ_ONLY_OTHER)
async def test_read_only_pwd_and_ls_run_from_bin(runtime, command, no_shell):
    manager, session = runtime
    workspace = Path(session["workspace"])
    (workspace / "listed.txt").write_text("x")
    result, event = await run(manager, session, command)
    assert result["exit_code"] == 0 and event["without_shell"] is True
    if command.endswith("pwd"):
        assert result["output"] == f"{workspace.resolve()}\n"
    else:
        assert "listed.txt" in result["output"]


async def test_a_planted_ls_first_on_path_is_not_run(runtime, monkeypatch, tmp_path, no_shell):
    manager, session = runtime
    workspace = Path(session["workspace"])
    (workspace / "bin").mkdir()
    marker = tmp_path / "planted-ls-ran"
    planted(workspace / "bin" / "ls", marker)
    planted(workspace / "bin" / "pwd", marker)
    monkeypatch.setenv("PATH", f"{workspace / 'bin'}{os.pathsep}{os.environ['PATH']}")
    for command in ("ls", "ls -la", "pwd"):
        result, _ = await run(manager, session, command)
        assert result["exit_code"] == 0 and "planted-program-output" not in result["output"]
    assert not marker.exists()


async def test_read_only_ls_without_bin_ls_does_not_run(monkeypatch, tmp_path):
    from local_agent import command_exec
    monkeypatch.setitem(command_exec.READ_ONLY_PROGRAMS, "ls", str(tmp_path / "no-ls"))
    with pytest.raises(ValueError) as caught:
        await prepare_command(["ls"], workspace=tmp_path, forbidden_roots=[], read_only=True)
    assert str(caught.value) == f"{tmp_path / 'no-ls'} is not available, so this read-only command does not run automatically."


# --- Hardened Git ---------------------------------------------------------------------------------

def git(workspace, *arguments, env=None):
    """Set-up Git: the test's identity, and no commit signing from a global configuration."""
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false",
                           "-c", "core.hooksPath=/dev/null", "-C", str(workspace), *arguments],
                          check=True, capture_output=True, env=env)


def program(path, marker, body=""):
    """A script that leaves `marker` behind when Git runs it."""
    path.write_text(f"#!/bin/sh\ntouch '{marker}'\n{body}\n")
    path.chmod(0o755)
    return path


SECRETS = ("committed-env-value-p4", "committed-credential-value-p4", "staged-env-value-p4",
           "staged-credential-value-p4", "working-env-value-p4")


def sign(repository):
    """Give HEAD a gpgsig header, written offline (no signing program runs)."""
    raw = git(repository, "cat-file", "commit", "HEAD").stdout.decode()
    head, _, message = raw.partition("\n\n")
    signature = ("gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEEsynthetic0signature0for0tests\n"
                 " -----END PGP SIGNATURE-----")
    crafted = f"{head}\n{signature}\n\n{message}"
    result = subprocess.run(["git", "-C", str(repository), "hash-object", "-t", "commit", "-w", "--literally", "--stdin"],
                            input=crafted.encode(), check=True, capture_output=True)
    git(repository, "update-ref", "HEAD", result.stdout.decode().strip())


@pytest.fixture
def hostile(tmp_path):
    """A repository whose configuration names a program for every hook Git offers to the
    commands on the read-only list: fsmonitor, clean and smudge filters, textconv, an
    external diff and a signature program. Each touches its own marker file."""
    repository = tmp_path / "project"
    repository.mkdir(exist_ok=True)
    git(repository, "init", "-q")
    (repository / ".gitattributes").write_text("* filter=x diff=x\n")
    (repository / "data.txt").write_text("one\n")
    (repository / ".env").write_text("SETTING=first\n")
    (repository / "config").mkdir()
    (repository / "config" / "credentials.json").write_text('{"value": "first"}\n')
    git(repository, "add", "-A")
    git(repository, "commit", "-qm", "initial")
    (repository / "data.txt").write_text("two\n")
    (repository / ".env").write_text("SETTING=committed-env-value-p4\n")
    (repository / "config" / "credentials.json").write_text('{"value": "committed-credential-value-p4"}\n')
    git(repository, "add", "-A")
    git(repository, "commit", "-qm", "second change")
    sign(repository)
    (repository / "data.txt").write_text("three\n")
    (repository / ".env").write_text("SETTING=staged-env-value-p4\n")
    (repository / "config" / "credentials.json").write_text('{"value": "staged-credential-value-p4"}\n')
    git(repository, "add", "-A")
    programs = tmp_path / "programs"
    marks = tmp_path / "marks"
    programs.mkdir()
    marks.mkdir()
    markers = {name: marks / name for name in ("fsmonitor", "clean", "smudge", "textconv", "external", "gpg")}
    settings = {
        "core.fsmonitor": program(programs / "fsmonitor", markers["fsmonitor"], "exit 1"),
        "filter.x.clean": program(programs / "clean", markers["clean"], "cat"),
        "filter.x.smudge": program(programs / "smudge", markers["smudge"], "cat"),
        "diff.x.textconv": program(programs / "textconv", markers["textconv"], 'cat "$1"'),
        "diff.external": program(programs / "external", markers["external"], "exit 0"),
        "gpg.program": program(programs / "gpg", markers["gpg"], "exit 1"),
    }
    for key, value in settings.items():
        git(repository, "config", key, str(value))
    git(repository, "config", "log.showSignature", "true")
    (repository / "data.txt").write_text("four\n")
    (repository / ".env").write_text("SETTING=working-env-value-p4\n")
    return repository, markers


def touched(markers):
    return sorted(name for name, path in markers.items() if path.exists())


@needs_git
async def test_the_hostile_fixture_runs_every_program_when_git_is_not_hardened(hostile):
    """CONTROL: plain Git runs each configured program, so the hardened runs below can fail."""
    repository, markers = hostile
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    plain = subprocess.run(["git", "diff"], cwd=repository, env=env, capture_output=True)
    assert plain.returncode == 0
    assert {"fsmonitor", "clean", "external"} <= set(touched(markers))
    internal = subprocess.run(["git", "diff", "--no-ext-diff"], cwd=repository, env=env, capture_output=True)
    assert "textconv" in touched(markers)
    assert b"working-env-value-p4" in internal.stdout  # Plain Git shows credential files.
    subprocess.run(["git", "log", "--show-signature", "-1"], cwd=repository, env=env, capture_output=True)
    assert "gpg" in touched(markers)
    shown = subprocess.run(["git", "show", "--no-ext-diff"], cwd=repository, env=env, capture_output=True)
    assert b"committed-credential-value-p4" in shown.stdout


@needs_git
@pytest.mark.parametrize("command", READ_ONLY_GIT)
async def test_read_only_git_runs_no_repository_program(runtime, hostile, command, no_shell):
    manager, session = runtime
    repository, markers = hostile
    session["workspace"] = str(repository)
    result, event = await run(manager, session, command)
    assert result["exit_code"] == 0, result["output"]
    assert touched(markers) == []
    assert event["without_shell"] is True
    if command.split()[1] in NON_EMPTY:
        assert result["output"].strip()
    for secret in SECRETS:
        assert secret not in result["output"]
    if command in ("git diff", "git diff --cached", "git show"):
        assert "data.txt" in result["output"]
        assert ".env" not in result["output"] and "credentials.json" not in result["output"]


@needs_git
async def test_read_only_git_output_shows_the_expected_changes(runtime, hostile, no_shell):
    manager, session = runtime
    repository, markers = hostile
    session["workspace"] = str(repository)
    diff, _ = await run(manager, session, "git diff")
    assert "-three\n+four" in diff["output"]
    staged, _ = await run(manager, session, "git diff --cached")
    assert "-two\n+three" in staged["output"]
    shown, _ = await run(manager, session, "git show")
    assert "second change" in shown["output"] and "-one\n+two" in shown["output"]
    log, _ = await run(manager, session, "git log --oneline")
    assert "second change" in log["output"] and "initial" in log["output"]
    files, _ = await run(manager, session, "git ls-files")
    assert files["output"].split() == [".gitattributes", "data.txt"]
    assert touched(markers) == []


@needs_git
async def test_read_only_git_stays_inside_a_workspace_that_is_a_subfolder(runtime, tmp_path, no_shell):
    manager, session = runtime
    outer = tmp_path / "outer"
    workspace = outer / "sub"
    workspace.mkdir(parents=True)
    git(outer, "init", "-q")
    (outer / "outside.txt").write_text("outside\n")
    (workspace / "inside.txt").write_text("inside\n")
    git(outer, "add", "-A")
    git(outer, "commit", "-qm", "initial")
    (outer / "outside.txt").write_text("outside changed\n")
    (workspace / "inside.txt").write_text("inside changed\n")
    session["workspace"] = str(workspace)
    for command in ("git status --short", "git status --porcelain", "git diff", "git diff --stat", "git ls-files"):
        result, _ = await run(manager, session, command)
        assert result["exit_code"] == 0
        assert "inside" in result["output"], command
        assert "outside" not in result["output"], command


@needs_git
async def test_a_git_planted_in_the_project_never_runs(runtime, hostile, tmp_path, monkeypatch, no_shell):
    manager, session = runtime
    repository, markers = hostile
    session["workspace"] = str(repository)
    marker = tmp_path / "planted-git-ran"
    (repository / "bin").mkdir()
    planted(repository / "bin" / "git", marker)
    monkeypatch.setenv("PATH", f"{repository / 'bin'}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(ValueError) as caught:
        await run(manager, session, "git status")
    assert str(caught.value) == "git resolves to a program inside the project, so it does not run automatically."
    # The same with the planted folder as the only forbidden root, through prepare_command.
    with pytest.raises(ValueError) as caught:
        await prepare_command(["git", "diff"], workspace=repository, forbidden_roots=[repository / "bin"],
                              read_only=True, env={"PATH": f"{repository / 'bin'}{os.pathsep}{os.environ['PATH']}"})
    assert str(caught.value) == "git resolves to a program inside the project, so it does not run automatically."
    assert not marker.exists()
    assert manager.jobs.list() == []
    assert touched(markers) == []


@needs_git
async def test_a_git_in_a_granted_folder_never_runs(runtime, tmp_path, monkeypatch, no_shell):
    manager, session = runtime
    granted = tmp_path / "granted"
    (granted / "bin").mkdir(parents=True)
    marker = tmp_path / "granted-git-ran"
    planted(granted / "bin" / "git", marker)
    session["allowed_directories"] = [str(granted)]
    monkeypatch.setenv("PATH", f"{granted / 'bin'}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(ValueError, match="inside the project"):
        await run(manager, session, "git status")
    assert not marker.exists()


async def test_no_git_on_path_is_reported(tmp_path):
    with pytest.raises(ValueError) as caught:
        await prepare_command(["git", "status"], workspace=tmp_path, forbidden_roots=[], read_only=True,
                              env={"PATH": str(tmp_path)})
    assert str(caught.value) == "Git is not installed or not on PATH."


@needs_git
async def test_the_hardened_git_command_line(tmp_path):
    from local_agent.git_tools import DIFF_EXCLUDES, SAFE_OPTIONS, hardened_git_command
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "filter.x.clean", "/srv/x/clean")
    argv, env = await hardened_git_command(tmp_path, ["diff", "--stat"], [tmp_path])
    assert argv[0] == shutil.which("git", path=os.pathsep.join(absolute_path_entries(os.environ)))
    assert argv[1:] == [*SAFE_OPTIONS, "-c", "log.showSignature=false", "-c", "filter.x.clean=",
                        "--no-optional-locks", "diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=dirty",
                        "--stat", "--", ".", *DIFF_EXCLUDES]
    assert env["GIT_OPTIONAL_LOCKS"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "filter.x.clean" and env["GIT_CONFIG_VALUE_0"] == ""
    log, _ = await hardened_git_command(tmp_path, ["log", "--oneline"], [])
    assert log[-3:] == ["log", "--no-show-signature", "--oneline"]
