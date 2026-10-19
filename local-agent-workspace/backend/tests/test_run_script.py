"""run.py: --open opens the app in the browser once its port accepts connections (M8g1)."""
import importlib.util
import os
import socket
import stat
import sys
import threading
import time
import webbrowser
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
    monkeypatch.setattr(run_script, "open_when_ready", lambda port: started.append(port) or True)
    monkeypatch.setattr(run_script, "desktop_browser", lambda: True)
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


@pytest.mark.parametrize(("platform", "environ", "desktop"), [
    ("linux", {"TERM": "xterm-256color"}, False), ("linux", {"DISPLAY": ":0"}, True),
    ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, True), ("linux", {"BROWSER": "firefox"}, True),
    ("freebsd14", {}, False), ("darwin", {}, True), ("win32", {}, True)])
def test_a_browser_opens_only_where_a_graphical_one_can(run_script, platform, environ, desktop):
    # Without a display Python's webbrowser picks a text browser (w3m, lynx…), run in the server's own terminal.
    assert run_script.desktop_browser(environ, platform) is desktop


def test_open_without_a_desktop_says_where_the_app_is(run_script, monkeypatch, capsys):
    from local_agent import serve
    started = []
    monkeypatch.setattr(serve, "run", lambda app, port, reload=False: None)
    monkeypatch.setattr(run_script, "open_when_ready", lambda port: started.append(port))
    monkeypatch.setattr(run_script, "desktop_browser", lambda: False)
    port = free_port()
    run_script.main(["--port", str(port), "--open"])
    assert started == []
    assert f"open http://127.0.0.1:{port}/ in your browser" in capsys.readouterr().out


def test_with_a_display_the_default_browser_opens_the_app(run_script, monkeypatch):
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    for platform, environ in (("linux", {"DISPLAY": ":0"}), ("linux", {"WAYLAND_DISPLAY": "wayland-0"}),
                              ("darwin", {}), ("win32", {})):
        assert run_script.open_browser("http://127.0.0.1:8765/", environ, platform) is True
    assert len(opened) == 4
    # Without one, and with nothing in BROWSER, no browser is tried.
    assert run_script.open_browser("http://127.0.0.1:8765/", {"TERM": "xterm-256color"}, "linux") is False
    assert len(opened) == 4


@pytest.fixture
def no_display(monkeypatch, tmp_path):
    """A Linux machine reached over SSH: a terminal (TERM), no display, and on PATH only the programs a test puts in
    the folder returned. Python's webbrowser keeps the browsers it found on first use, so it looks again here."""
    folder = tmp_path / "bin"
    folder.mkdir()
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.setenv("TERM", "xterm-256color")
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(webbrowser, "_tryorder", None)
    monkeypatch.setattr(webbrowser, "_browsers", {})
    monkeypatch.setattr(webbrowser, "_os_preferred_browser", None)
    return folder


def fake_program(folder, name, body):
    path = folder / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def serve_until_opened(monkeypatch):
    """serve.run as a server that listens on its port until --open's wait (its own thread) has finished."""
    from local_agent import serve
    before = set(threading.enumerate())

    def run(app, port, reload=False):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and any(thread.is_alive() for thread in set(threading.enumerate()) - before):
                time.sleep(0.05)
    monkeypatch.setattr(serve, "run", run)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="a Linux machine reached over SSH")
def test_a_browser_named_in_browser_that_cannot_start_runs_no_text_browser_and_the_address_is_printed(
        run_script, no_display, monkeypatch, tmp_path, capsys):
    # The profile exports BROWSER=firefox, as on the desktop; over SSH firefox cannot start without a display.
    # Python's webbrowser would then go on to the text browsers it found (w3m here), run in the server's terminal.
    tried, ran = tmp_path / "firefox-tried", tmp_path / "w3m-ran"
    fake_program(no_display, "firefox", f'echo "$@" >> "{tried}"\necho "Error: no DISPLAY environment variable specified" >&2\nexit 1')
    fake_program(no_display, "w3m", f'echo "$@" >> "{ran}"')
    monkeypatch.setenv("BROWSER", "firefox")
    serve_until_opened(monkeypatch)
    port = free_port()
    run_script.main(["--port", str(port), "--open"])
    assert tried.read_text() == f"http://127.0.0.1:{port}/\n"  # The browser named in BROWSER is tried.
    assert not ran.exists(), f"a text browser ran in the server's terminal: w3m {ran.read_text().strip()}"
    assert f"open http://127.0.0.1:{port}/ in your browser" in capsys.readouterr().out
