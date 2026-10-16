import asyncio
import errno
import os
import shutil
import signal

import pytest


@pytest.fixture(autouse=True)
def isolated_defaults(monkeypatch):
    """No default code folder or Python environment from the developer's environment, and a
    model without a built-in profile (131,072 and 8,192). Tests of the default model delenv
    LOCAL_AGENT_MODEL."""
    monkeypatch.setenv("LOCAL_AGENT_CODING_DIR", "")
    monkeypatch.setenv("LOCAL_AGENT_PYTHON_VENV", "")
    monkeypatch.setenv("LOCAL_AGENT_MODEL", "test-endpoint")


@pytest.fixture(scope="session")
def empty_home(tmp_path_factory):
    return tmp_path_factory.mktemp("home")


@pytest.fixture(autouse=True)
def isolated_user_folders(monkeypatch, empty_home):
    """The user's own skill and command folders (~/.agents, ~/.claude) are read from an empty folder, never
    the developer's home. Tests of those folders set extensions.USER_HOME to their own."""
    from local_agent import extensions
    monkeypatch.setattr(extensions, "USER_HOME", empty_home)


@pytest.fixture(autouse=True)
def no_ripgrep(monkeypatch):
    """Search without ripgrep by default, so results do not depend on whether it is
    installed. Tests that need it use real_ripgrep."""
    from local_agent import search
    monkeypatch.setattr(search, "ripgrep", lambda: None)


@pytest.fixture(autouse=True)
def mac_shell_prompt(monkeypatch):
    """A system prompt that names zsh, as on a Mac, whatever shell this machine has, so that a
    test near a token threshold gives the same result on every runner. Commands still run with
    this machine's shell; tests of the prompt's own choice patch agents.shell_note back."""
    from local_agent import agents, jobs
    note = " ".join((jobs.SHELL_NOTES["/bin/zsh"], jobs.POLLING_NOTE))
    monkeypatch.setattr(agents, "shell_note", lambda: note)


@pytest.fixture
def real_ripgrep(monkeypatch):
    """Search with the installed rg; the test is skipped when rg is not installed."""
    from local_agent import search
    rg = shutil.which("rg")
    if not rg:
        pytest.skip("ripgrep (rg) is not installed")
    monkeypatch.setattr(search, "ripgrep", lambda: rg)
    return rg


@pytest.fixture(params=["as-is", "eperm"])
def killpg_answer(request, monkeypatch):
    """Run a test twice: with os.killpg as it is, and as macOS answers when a group's
    processes have all exited but are not yet reaped (EPERM instead of ESRCH).

    The EPERM variant still sends the signal, so a test can also check that nothing
    the command started survives.
    """
    if request.param == "eperm":
        killpg = os.killpg

        def not_permitted(pid, sig):
            try:
                killpg(pid, sig)
            except ProcessLookupError:
                pass
            raise PermissionError(errno.EPERM, "Operation not permitted")
        monkeypatch.setattr(os, "killpg", not_permitted)
    return request.param


@pytest.fixture
async def unkillable_group(monkeypatch):
    """Make os.killpg answer EPERM without signalling, as for a command whose running
    processes all belong to another user (for example through sudo), and shorten
    the grace periods after SIGTERM and SIGKILL. The groups are killed for real afterwards."""
    from local_agent import jobs
    killpg = os.killpg
    groups = set()

    def not_permitted(pid, sig):
        groups.add(pid)
        raise PermissionError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(os, "killpg", not_permitted)
    monkeypatch.setattr(jobs, "KILL_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(jobs, "TERM_GRACE_SECONDS", 0.2)
    yield groups
    for pid in groups:
        try:
            killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    await asyncio.sleep(0.2)  # Let the event loop reap them before it closes.
