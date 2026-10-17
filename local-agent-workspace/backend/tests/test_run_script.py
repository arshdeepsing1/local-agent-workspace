"""run.py: --open opens the app in the browser once its port accepts connections (M8g1)."""
import importlib.util
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

RUN = Path(__file__).resolve().parents[2] / "run.py"


@pytest.fixture
def run_script(monkeypatch):
    # Loading run.py puts the backend first on sys.path and the interpreter's folder first on PATH: both are put
    # back after the test, so later tests see the environment they started with.
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("run_script", RUN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_browser_opens_once_the_port_accepts_connections(run_script):
    port = free_port()
    opened = []
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def start_later():
        listener.bind(("127.0.0.1", port))
        listener.listen()
    timer = threading.Timer(0.3, start_later)
    timer.start()
    try:
        assert run_script.open_when_ready(port, timeout=5, opener=lambda url: opened.append(url) or True) is True
    finally:
        timer.join()
        listener.close()
    assert opened == [f"http://127.0.0.1:{port}/"]


def test_no_browser_opens_when_the_app_never_starts(run_script):
    opened = []
    assert run_script.open_when_ready(free_port(), timeout=0.3, opener=opened.append) is False
    assert opened == []


def test_open_starts_the_wait_only_for_a_free_port(run_script, monkeypatch):
    from local_agent import serve
    started, served = [], []
    monkeypatch.setattr(serve, "run", lambda app, port, reload=False: served.append((app, port, reload)))
    monkeypatch.setattr(run_script, "open_when_ready", lambda port: started.append(port))
    port = free_port()
    run_script.main(["--port", str(port), "--open"])
    run_script.main(["--port", str(port)])
    with socket.socket() as busy:  # Another program on the port: the start fails, and no browser opens for it.
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        run_script.main(["--port", str(busy.getsockname()[1]), "--open"])
    deadline = time.monotonic() + 5
    while not started and time.monotonic() < deadline:  # The wait runs in its own thread.
        time.sleep(0.01)
    assert started == [port]
    assert [entry[1] for entry in served] == [port, port, served[2][1]]
    assert all(entry[0] == "local_agent.api:create_app" and entry[2] is False for entry in served)
