import asyncio
import errno
import os
import signal

import pytest


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
    the grace period after a kill. The groups are killed for real afterwards."""
    from local_agent import jobs
    killpg = os.killpg
    groups = set()

    def not_permitted(pid, sig):
        groups.add(pid)
        raise PermissionError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(os, "killpg", not_permitted)
    monkeypatch.setattr(jobs, "KILL_GRACE_SECONDS", 0.5)
    yield groups
    for pid in groups:
        try:
            killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    await asyncio.sleep(0.2)  # Let the event loop reap them before it closes.
