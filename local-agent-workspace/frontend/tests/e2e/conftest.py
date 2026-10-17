"""End-to-end tests: the real page, the real app and the offline simulator together (M8g3).

Each test gets its own app on a free port of 127.0.0.1, with a fresh state folder and a small Git project, and the
app's Databricks credentials pointed at the simulated gateway (scripts/simulate_databricks.py: start_gateway,
patched_settings, start_app) at speed 0. The browser opens the app's own page, so nothing here is faked between the
page and the server. No network beyond 127.0.0.1, no credentials read, no paid calls.

The tests wait for what the page shows, never for a fixed time. The scripted turns are chosen by words in the
message (see the simulator's docstring): edit, delegate/parallel, ask, rate limit, long.
"""
import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from playwright.sync_api import expect

APP = Path(__file__).resolve().parents[3]
SIMULATOR = APP / "scripts" / "simulate_databricks.py"
MODEL = "databricks-claude-opus-5-5"
# This checkout's app, even when the environment's editable install points at another copy (a Git worktree).
sys.path.insert(0, str(APP / "backend"))


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: the real app and page against the offline simulator (tests/e2e)")


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.resolve().is_relative_to(Path(__file__).resolve().parent):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def simulator():
    import local_agent
    if Path(local_agent.__file__).resolve().parents[1] != APP / "backend":
        pytest.fail(f"The end-to-end tests import the app from {local_agent.__file__}, not from this checkout.")
    spec = importlib.util.spec_from_file_location("simulate_databricks_e2e", SIMULATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class Stack:
    url: str
    manager: object  # The app's AgentManager, to read what the server holds.
    project: Path
    state: Path


@pytest.fixture
def stack(simulator, tmp_path, monkeypatch):
    """The app against the simulated gateway, in its own state folder and Git project (the simulator's demo)."""
    home = tmp_path / "home"
    home.mkdir()
    # As in the backend tests: nothing from the developer's environment or home (default code folder, Python
    # environment, instruction files, skills, output styles). The model is the app's default endpoint.
    monkeypatch.setenv("LOCAL_AGENT_CODING_DIR", "")
    monkeypatch.setenv("LOCAL_AGENT_PYTHON_VENV", "")
    monkeypatch.setenv("LOCAL_AGENT_MODEL", MODEL)
    from local_agent import extensions, instructions
    monkeypatch.setattr(extensions, "USER_HOME", home)
    monkeypatch.setattr(instructions, "user_home", lambda: home)
    gateway, gateway_url = simulator.start_gateway(speed=0.0)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    project = simulator.demo_workspace(tmp_path)
    try:
        with simulator.patched_settings(gateway_url, state, project):
            server, thread, url = simulator.start_app()
            try:
                yield Stack(url, server.config.app.state.manager, project, state)
            finally:
                server.should_exit = True
                thread.join(15)
    finally:
        gateway.shutdown()
        gateway.server_close()


@pytest.fixture
def app_page(browser, stack):
    """The app's page in a fresh browser context (no storage from another test). Uncaught page errors fail the
    test. E2E_CPU_THROTTLE=4 runs the page on a CPU 4 times slower (Chromium's DevTools throttling), as a busy CI
    machine might, to flush out races."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    throttle = float(os.environ.get("E2E_CPU_THROTTLE") or 1)
    if throttle > 1:
        context.new_cdp_session(page).send("Emulation.setCPUThrottlingRate", {"rate": throttle})
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    page.goto(stack.url)
    expect(page.get_by_role("textbox", name="Message")).to_be_editable(timeout=10_000)
    yield page
    context.close()
    assert not errors, f"Uncaught errors in the page: {errors}"
