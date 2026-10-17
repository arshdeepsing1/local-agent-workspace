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
    if Path("/proc/self/stat").exists():
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
    if not Path("/proc/self/stat").exists():  # macOS: ask ps whether it is a zombie
        try:
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                                   timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return True
        return bool(state) and not state.startswith("Z")
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


@pytest.mark.parametrize("argv", [[], [""], ["a"] * 257, ["x" * 1001], ["echo", "a\0b"], "echo", [1]])
async def test_invalid_argv_is_refused(tmp_path, argv, no_shell):
    with pytest.raises(ValueError, match="without a shell needs 1 to 256 arguments"):
        await run_process("text", tmp_path, argv=argv)


@pytest.mark.parametrize("argv", [[], ["a"] * 65, ["x" * 1001], ["echo", "a\0b"]])
async def test_a_command_given_to_run_without_a_shell_has_at_most_64_tokens(tmp_path, argv):
    """The caller's command is bounded; the prepared argument vector (read-only Git adds
    options and pathspecs) may be longer."""
    with pytest.raises(ValueError, match="without a shell needs 1 to 64 arguments"):
        await prepare_command(argv, workspace=tmp_path, forbidden_roots=[], read_only=False)


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


async def test_a_program_named_by_a_path_keeps_its_name_when_it_is_a_symbolic_link(tmp_path, no_shell):
    """A virtual environment's python is a link to the base interpreter, and finds its
    environment from the name it was started under; so does a multi-call program."""
    script = tmp_path / "real" / "show-name"
    script.parent.mkdir()
    script.write_text('#!/bin/sh\necho "$0"\n')
    script.chmod(0o755)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    link = tmp_path / ".venv" / "bin" / "python"
    link.symlink_to(script)
    argv, env = await prepare_command([".venv/bin/python", "-m", "pytest"], workspace=tmp_path, forbidden_roots=[],
                                      read_only=False, env={"PATH": "/usr/bin:/bin"})
    assert argv == [str(link), "-m", "pytest"]
    result = await run_process(".venv/bin/python -m pytest", tmp_path, argv=argv, env=env)
    assert result["output"] == f"{link}\n"
    # A link to a missing or non-executable target is still not found.
    (tmp_path / "dangling").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="^Command not found: ./dangling$"):
        resolve_executable("./dangling", {}, tmp_path)


async def test_a_symlinked_interpreter_keeps_its_virtual_environment(tmp_path, no_shell):
    """A real interpreter linked into a folder with a pyvenv.cfg runs inside that environment."""
    base = Path(os.path.realpath(sys.executable))
    venv = tmp_path / "project" / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {base.parent}\ninclude-system-site-packages = false\n")
    (venv / "bin" / "python").symlink_to(base)
    command = [".venv/bin/python", "-c", "import sys; print(sys.prefix)"]
    argv, env = await prepare_command(command, workspace=venv.parent, forbidden_roots=[], read_only=False,
                                      env=command_environment())
    result = await run_process(" ".join(command), venv.parent, argv=argv, env=env)
    assert result["exit_code"] == 0, result["output"]
    assert result["output"].strip() == str(venv)


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


async def test_command_tool_passes_settings_variables_to_argv_commands(runtime, no_shell):
    """Settings -> Environment variables for commands reach commands allowed by a rule."""
    manager, session = runtime
    manager.jobs.environment = lambda: {"PASSED_BY_NAME": "visible"}
    result, _ = await run(manager, session, "env", argv=[shutil.which("env")], read_only=False)
    assert result["exit_code"] == 0
    assert "PASSED_BY_NAME=visible" in result["output"].splitlines()


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
    (["git", "log", "--format=%G?"], "This Git option is not allowed in a read-only command."),
    (["git", "show", "--pretty=format:%GG"], "This Git option is not allowed in a read-only command."),
    (["git", "diff", "--submodule=diff"], "This Git option is not allowed in a read-only command."),
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
    external diff and a signature program (asked for by log.showSignature and by a
    format.pretty with %G?). Each touches its own marker file."""
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
    git(repository, "config", "format.pretty", "format:%h %G? %s")
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
    markers["gpg"].unlink()
    # format.pretty with %G? checks the signature even with signature display switched off.
    subprocess.run(["git", "-c", "log.showSignature=false", "show", "--no-show-signature", "--no-ext-diff",
                    "--no-textconv"], cwd=repository, env=env, capture_output=True)
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
    assert str(caught.value) == (f"git resolves to {(repository / 'bin' / 'git').resolve()}, inside the project or "
                                 "a folder granted to this chat, so it does not run automatically.")
    # The same with the planted folder as the only forbidden root, through prepare_command.
    with pytest.raises(ValueError) as caught:
        await prepare_command(["git", "diff"], workspace=repository, forbidden_roots=[repository / "bin"],
                              read_only=True, env={"PATH": f"{repository / 'bin'}{os.pathsep}{os.environ['PATH']}"})
    assert str(caught.value) == (f"git resolves to {(repository / 'bin' / 'git').resolve()}, inside the project or "
                                 "a folder granted to this chat, so it does not run automatically.")
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
    with pytest.raises(ValueError) as caught:
        await run(manager, session, "git status")
    assert str(caught.value) == (f"git resolves to {(granted / 'bin' / 'git').resolve()}, inside the project or "
                                 "a folder granted to this chat, so it does not run automatically.")
    assert not marker.exists()


async def test_no_git_on_path_is_reported(tmp_path):
    with pytest.raises(ValueError) as caught:
        await prepare_command(["git", "status"], workspace=tmp_path, forbidden_roots=[], read_only=True,
                              env={"PATH": str(tmp_path)})
    assert str(caught.value) == "Git is not installed or not on PATH."


@needs_git
async def test_the_hardened_git_command_line(tmp_path, monkeypatch):
    from local_agent.git_tools import DIFF_EXCLUDES, SAFE_OPTIONS, hardened_git_command
    # Only this repository's filters: a machine's global Git configuration (Git LFS on CI) adds its own.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
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
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert ["-c", "diff.submodule=short"] == list(SAFE_OPTIONS[-4:-2])
    assert ["-c", "format.pretty=medium"] == list(SAFE_OPTIONS[-2:])
    show, _ = await hardened_git_command(tmp_path, ["show", "--stat"], [])
    assert show[show.index("show"):show.index("--")] == [
        "show", "--no-ext-diff", "--no-textconv", "--no-show-signature", "--sparse", "--stat"]


@needs_git
async def test_read_only_git_runs_with_many_filters_configured(runtime, tmp_path, no_shell):
    """A Git LFS setup names four filter settings: the prepared command is longer than the
    64 tokens a caller may give, and still runs."""
    manager, session = runtime
    repository = Path(session["workspace"])
    git(repository, "init", "-q")
    (repository / "a.txt").write_text("one\n")
    git(repository, "add", "-A")
    git(repository, "commit", "-qm", "initial")
    (repository / "a.txt").write_text("two\n")
    marker = tmp_path / "filter-ran"
    for name in ("lfs", "other"):
        for key in ("clean", "smudge", "process"):
            git(repository, "config", f"filter.{name}.{key}", str(program(tmp_path / f"{name}-{key}", marker)))
        git(repository, "config", f"filter.{name}.required", "true")
    for command in ("git diff", "git show", "git status --short"):
        result, _ = await run(manager, session, command)
        assert result["exit_code"] == 0 and "a.txt" in result["output"], result["output"]
    assert not marker.exists()


@needs_git
async def test_read_only_git_shows_head_when_it_changes_nothing_shown_here(runtime, tmp_path, no_shell):
    """git show names the commit even when its changes are all outside the workspace or in
    credential files, so the model can tell what HEAD is."""
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
    git(outer, "commit", "-qam", "change outside only")
    session["workspace"] = str(workspace)
    for command in ("git show", "git show --stat"):
        result, _ = await run(manager, session, command)
        assert result["exit_code"] == 0 and "change outside only" in result["output"], command
        assert "outside.txt" not in result["output"]
    (outer / ".env").write_text("SETTING=secret-in-head-p4\n")
    git(outer, "add", "-A")
    git(outer, "commit", "-qm", "credential only")
    session["workspace"] = str(outer)
    result, _ = await run(manager, session, "git show")
    assert "credential only" in result["output"] and "secret-in-head-p4" not in result["output"]


@needs_git
async def test_read_only_git_leaves_out_env_files_but_not_env_example(runtime, no_shell):
    """The same rule as the file tools: .env.example is read and edited, other .env.* are not shown."""
    manager, session = runtime
    repository = Path(session["workspace"])
    git(repository, "init", "-q")
    # The upper-case name has its own folder: macOS file systems treat .ENV.Example and .env.example as one file.
    names = [".env.example", "upper/.ENV.Example", "nested/.env.example", ".env.local", ".ENV.PROD", ".env.e",
             ".env.exampl", ".env.examples", ".env.example.bak", ".env.", "nested/.env.d/inner", "app.env"]
    for name in names:
        (repository / name).parent.mkdir(parents=True, exist_ok=True)
        (repository / name).write_text("SETTING=first\n")
    git(repository, "add", "-A")
    git(repository, "commit", "-qm", "initial")
    for name in names:
        (repository / name).write_text("SETTING=changed\n")
    visible = {".env.example", "upper/.ENV.Example", "nested/.env.example"}
    for command in ("git status --short", "git diff --name-only", "git ls-files"):
        result, _ = await run(manager, session, command)
        listed = {line.split()[-1] for line in result["output"].splitlines() if line.strip()}
        assert listed == visible, command


# A promisor remote whose configured upload-pack program leaves a marker, then runs Git's own.
UPLOAD_PACK = 'exec git-upload-pack "$@"'


def partial_clone(tmp_path, name):
    """A partial clone (blob:none, nothing checked out) whose remote names an upload-pack
    program: fetching a missing object runs it."""
    origin = tmp_path / "origin"
    if not origin.exists():
        origin.mkdir()
        git(origin, "init", "-q")
        (origin / "a.txt").write_text("one\n")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", "first")
        (origin / "a.txt").write_text("two\n")
        git(origin, "commit", "-qam", "second")
        git(origin, "config", "uploadpack.allowFilter", "true")
    clone = tmp_path / name
    subprocess.run(["git", "-c", "protocol.file.allow=always", "clone", "-q", "--no-checkout", "--filter=blob:none",
                    f"file://{origin}", str(clone)], check=True, capture_output=True)
    marker = tmp_path / f"{name}-upload-pack-ran"
    git(clone, "config", "remote.origin.uploadpack", str(program(tmp_path / f"{name}-upload-pack", marker, UPLOAD_PACK)))
    return clone, marker


@needs_git
async def test_read_only_git_never_fetches_missing_objects_in_a_partial_clone(runtime, tmp_path, monkeypatch, no_shell):
    from local_agent import git_tools
    manager, session = runtime
    control, control_marker = partial_clone(tmp_path, "control")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    subprocess.run(["git", "show"], cwd=control, env=env, capture_output=True)
    assert control_marker.exists()  # CONTROL: plain Git fetches, running the configured program.
    probe, probe_marker = partial_clone(tmp_path, "probe")
    subprocess.run(["git", "show"], cwd=probe, env={**env, "GIT_NO_LAZY_FETCH": "1"}, capture_output=True)
    if probe_marker.exists():
        pytest.skip("This Git does not honour GIT_NO_LAZY_FETCH")
    clone, marker = partial_clone(tmp_path, "clone")
    session["workspace"] = str(clone)

    async def recent(workspace, program):
        return (2, 45, 1)
    monkeypatch.setattr(git_tools, "git_version", recent)
    for command in ("git show", "git show --stat", "git diff --cached", "git log --oneline", "git status"):
        await run(manager, session, command)
        assert not marker.exists(), command


@needs_git
async def test_read_only_git_does_not_run_in_a_partial_clone_with_an_older_git(runtime, hostile, tmp_path, monkeypatch,
                                                                             no_shell):
    """A Git without GIT_NO_LAZY_FETCH would fetch, so a partial clone is refused; other
    repositories still run."""
    from local_agent import git_tools
    manager, session = runtime
    clone, marker = partial_clone(tmp_path, "clone")

    async def older(workspace, program):
        return (2, 43, 0)
    monkeypatch.setattr(git_tools, "git_version", older)
    session["workspace"] = str(clone)
    with pytest.raises(ValueError) as caught:
        await run(manager, session, "git show")
    assert str(caught.value) == git_tools.PARTIAL_CLONE_REFUSED
    assert not marker.exists() and manager.jobs.list() == []
    repository, markers = hostile
    session["workspace"] = str(repository)
    result, _ = await run(manager, session, "git show")
    assert result["exit_code"] == 0 and "second change" in result["output"]


@pytest.mark.parametrize("version, honoured", [
    ((2, 38, 9), False), ((2, 39, 3), False), ((2, 39, 4), True), ((2, 42, 1), False), ((2, 42, 2), True),
    ((2, 43, 0), False), ((2, 43, 4), True), ((2, 44, 0), False), ((2, 44, 1), True), ((2, 45, 0), False),
    ((2, 45, 1), True), ((2, 46, 0), True), ((3, 0, 0), True), ((0, 0, 0), False),
])
def test_which_git_versions_honour_no_lazy_fetch(version, honoured):
    from local_agent.git_tools import honours_no_lazy_fetch
    assert honours_no_lazy_fetch(version) is honoured


@needs_git
async def test_the_git_version_is_read_from_the_program(tmp_path):
    from local_agent import git_tools
    git_tools._versions.clear()
    found = shutil.which("git")
    version = await git_tools.git_version(tmp_path, found)
    expected = subprocess.run([found, "version"], capture_output=True, text=True).stdout
    assert ".".join(map(str, version[:2])) in expected and version >= (1, 0, 0)


@pytest.fixture
def hostile_submodule(tmp_path):
    """A superproject with diff.submodule=diff, whose submodule's own configuration names a
    textconv program and an external diff. The submodule commit changes in HEAD, in the
    index and in the working tree."""
    base = tmp_path / "submodule-case"
    source = base / "source"
    source.mkdir(parents=True)
    git(source, "init", "-q")
    (source / ".gitattributes").write_text("* diff=x\n")
    (source / "f.txt").write_text("one\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "s1")
    superproject = base / "super"
    superproject.mkdir()
    git(superproject, "init", "-q")
    (superproject / "r.txt").write_text("r\n")
    git(superproject, "add", "-A")
    git(superproject, "commit", "-qm", "r")
    git(superproject, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "sub")
    git(superproject, "commit", "-qm", "add sub")
    sub = superproject / "sub"
    marks = base / "marks"
    marks.mkdir()
    markers = {name: marks / name for name in ("textconv", "external")}
    git(sub, "config", "diff.x.textconv", str(program(base / "textconv", markers["textconv"], 'cat "$1"')))
    git(superproject, "config", "diff.submodule", "diff")
    for text, staged in (("two", "commit"), ("three", "stage"), ("four", None)):
        (sub / "f.txt").write_text(text + "\n")
        git(sub, "commit", "-qam", text)
        if staged:
            git(superproject, "add", "sub")
        if staged == "commit":
            git(superproject, "commit", "-qm", "bump sub")
    return superproject, markers, base


@needs_git
async def test_the_submodule_fixture_runs_its_programs_when_git_is_not_hardened(hostile_submodule):
    """CONTROL: --no-textconv and --no-ext-diff do not reach the diff Git runs inside the submodule."""
    superproject, markers, base = hostile_submodule
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    for command in (["diff"], ["diff", "--cached"], ["show"]):
        subprocess.run(["git", *command, "--no-textconv", "--no-ext-diff"], cwd=superproject, env=env,
                       capture_output=True, check=True)
        assert touched(markers) == ["textconv"], command
        markers["textconv"].unlink()
    git(superproject / "sub", "config", "diff.external", str(program(base / "external", markers["external"])))
    subprocess.run(["git", "diff", "--cached", "--no-textconv", "--no-ext-diff"], cwd=superproject, env=env,
                   capture_output=True, check=True)
    assert touched(markers) == ["external"]


@needs_git
@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("command", READ_ONLY_GIT)
async def test_read_only_git_runs_no_program_named_in_a_submodule(runtime, hostile_submodule, command, external,
                                                                   no_shell):
    manager, session = runtime
    superproject, markers, base = hostile_submodule
    if external:
        git(superproject / "sub", "config", "diff.external", str(program(base / "external", markers["external"])))
    session["workspace"] = str(superproject)
    result, _ = await run(manager, session, command)
    assert result["exit_code"] == 0, result["output"]
    assert touched(markers) == []
    if command in ("git diff", "git diff --cached", "git show"):
        assert "Subproject commit" in result["output"]


async def test_a_stop_before_the_job_task_starts_cancels_the_job_not_its_waiter(store, tmp_path):
    """Terminal → Stop job right after a foreground command started (its task not yet run): the turn waiting for
    the command sees a cancelled job; before, the job task's CancelledError reached the turn and stopped it."""
    manager = JobManager(store, lambda text: text, None)
    job = await manager.start("sleep 30", tmp_path)  # Its task is scheduled, not yet run.
    waiter = asyncio.create_task(manager.wait(job["id"]))
    stopped = await manager.stop(job["id"])  # Cancels the job's task before its first step.
    waited = await asyncio.wait_for(waiter, 10)
    assert waited["state"] == stopped["state"] == "cancelled" and not waiter.cancelled()
