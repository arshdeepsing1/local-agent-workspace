import errno
import os

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
