"""Attention: how a turn ended in the conversation list, and optional desktop notifications."""
import asyncio

import httpx
import pytest

from local_agent import notify
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.telemetry import InferenceError
from local_agent.notify import SystemNotifier, attention_message, notification_argv, run_notification, short_title


class Runner:
    """Records notifier commands instead of starting processes."""

    def __init__(self, error=None):
        self.calls, self.error = [], error

    async def __call__(self, argv):
        self.calls.append(argv)
        if self.error:
            raise self.error


class Socket:
    def __init__(self):
        self.messages = []

    async def send_json(self, data):
        self.messages.append(data)


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), model="test-model", env_file="")
    settings.env = {}
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            manager = app.state.manager
            runner = Runner()
            manager.notifier = SystemNotifier(runner=runner, platform="linux", which=lambda name: f"/usr/bin/{name}")
            session = (await client.post("/api/sessions")).json()
            yield client, manager, manager.get(session["id"]), runner


async def run_turn(client, manager, session, outcome):
    """Send a message whose model turn ends with `outcome` (an exception, or None for a reply)."""
    async def run_databricks(session, prompt):
        if outcome:
            raise outcome
        await manager.event(session, "assistant", text="Private reply text")
        return False
    manager.run_databricks = run_databricks
    response = await client.post(f"/api/sessions/{session['id']}/messages", json={"text": "Fix the parser please"})
    assert response.status_code == 200
    await asyncio.wait_for(manager.tasks[session["id"]], 5)
    await asyncio.sleep(0)  # Let a started notification task run.


async def listed(client, session):
    return next(item for item in (await client.get("/api/sessions")).json() if item["id"] == session["id"])


async def test_the_conversation_list_says_how_the_last_turn_ended(runtime):
    client, manager, session, _ = runtime
    await run_turn(client, manager, session, RuntimeError("model failed"))
    summary = await listed(client, session)
    assert summary["status"] == "idle" and summary["terminal_reason"] == "error"
    assert "events" not in summary and "wire" not in summary
    await run_turn(client, manager, session, None)
    assert (await listed(client, session))["terminal_reason"] == "completed"


async def test_the_idle_status_message_carries_how_the_turn_ended(runtime):
    client, manager, session, _ = runtime
    socket = Socket()
    manager.listeners[session["id"]] = {socket}
    await run_turn(client, manager, session, RuntimeError("model failed"))
    statuses = [message for message in socket.messages if message["type"] == "status"]
    assert statuses[0] == {"type": "status", "status": "running"}
    ended = (await listed(client, session))["turn_ended"]
    assert isinstance(ended, float)
    assert statuses[-1] == {"type": "status", "status": "idle", "terminal_reason": "error", "turn_ended": ended}


async def test_the_summary_marks_a_turn_in_progress_and_when_it_ended(runtime):
    client, manager, session, _ = runtime
    during = []

    async def run_databricks(session, prompt):
        during.append(await listed(client, session))
        return False
    manager.run_databricks = run_databricks
    await client.post(f"/api/sessions/{session['id']}/messages", json={"text": "Hello"})
    await asyncio.wait_for(manager.tasks[session["id"]], 5)
    assert during[0]["turn_ended"] is None and during[0]["status"] == "running"
    first = (await listed(client, session))["turn_ended"]
    await run_turn(client, manager, session, None)
    assert (await listed(client, session))["turn_ended"] > first


async def test_desktop_notifications_are_off_by_default(runtime):
    client, manager, session, runner = runtime
    assert manager.settings.values["system_notifications"] is False
    await run_turn(client, manager, session, None)
    assert runner.calls == []


@pytest.mark.parametrize("outcome, expected", [
    (None, "Finished: Fix the parser please"),
    (RuntimeError("secret detail"), "Stopped with an error: Fix the parser please"),
])
async def test_a_finished_or_failed_turn_notifies_once_when_no_page_is_open(runtime, outcome, expected):
    client, manager, session, runner = runtime
    manager.settings.values["system_notifications"] = True
    await run_turn(client, manager, session, outcome)
    assert runner.calls == [["/usr/bin/notify-send", "Local", expected]]


async def test_no_desktop_notification_while_a_page_is_connected(runtime):
    client, manager, session, runner = runtime
    manager.settings.values["system_notifications"] = True
    other = (await client.post("/api/sessions")).json()
    # A page open on any conversation receives the browser's own notifications.
    manager.listeners[other["id"]] = {Socket()}
    await run_turn(client, manager, session, None)
    session = manager.get(session["id"])
    await manager.status(session, "awaiting_approval")
    assert runner.calls == []
    manager.listeners[other["id"]] = set()  # The page was closed.
    await manager.status(session, "running")
    await manager.status(session, "awaiting_approval")
    await asyncio.sleep(0)
    assert runner.calls == [["/usr/bin/notify-send", "Local", "Needs your approval: Fix the parser please"]]


async def test_one_notification_per_state_change(runtime):
    _, manager, session, runner = runtime
    manager.settings.values["system_notifications"] = True
    session["title"] = "Approvals"
    for status in ("running", "awaiting_approval", "awaiting_approval", "running", "compacting", "running"):
        await manager.status(session, status)
    session["terminal_reason"] = "stopped"
    await manager.status(session, "idle")
    await manager.status(session, "compacting")
    session["terminal_reason"] = "error"
    await manager.status(session, "idle")  # The end of a manual compaction is not a reply.
    await asyncio.sleep(0)
    assert [call[-1] for call in runner.calls] == ["Needs your approval: Approvals"]


@pytest.mark.parametrize("failure", [InferenceError("HTTP 429", "rate_limit", 429),
                                     ValueError("Conversation summarization failed: HTTP 429")])
async def test_a_turn_that_fails_during_automatic_compaction_notifies(runtime, failure):
    client, manager, session, runner = runtime
    manager.settings.values["system_notifications"] = True

    async def run_databricks(session, prompt):
        await manager.status(session, "compacting")  # As summarize_context does inside a turn.
        raise failure
    manager.run_databricks = run_databricks
    await client.post(f"/api/sessions/{session['id']}/messages", json={"text": "Long task"})
    await asyncio.wait_for(manager.tasks[session["id"]], 5)
    await asyncio.sleep(0)
    assert runner.calls == [["/usr/bin/notify-send", "Local", "Stopped with an error: Long task"]]


async def test_a_manual_compaction_does_not_notify(runtime):
    client, manager, session, runner = runtime
    manager.settings.values["system_notifications"] = True
    await run_turn(client, manager, session, None)
    runner.calls.clear()
    session = manager.get(session["id"])
    session["wire"] = [{"role": "user", "content": "First"}, {"role": "assistant", "content": "Done"},
                       {"role": "user", "content": "Next"}]  # An earlier turn to compact.
    manager.store.save(session)
    manager.settings.credentials = lambda: (_ for _ in ()).throw(ValueError("No credentials"))
    manager.start_compaction(session["id"])
    await asyncio.wait_for(manager.tasks[session["id"]], 5)
    await asyncio.sleep(0)
    assert manager.get(session["id"])["events"][-1]["text"] == "No credentials"
    assert runner.calls == []


async def test_no_desktop_notification_while_a_page_without_a_chat_is_open(runtime):
    client, manager, session, runner = runtime
    manager.settings.values["system_notifications"] = True
    manager.pages.add(Socket())  # A page on the new-conversation screen.
    await run_turn(client, manager, session, None)
    assert runner.calls == []
    manager.pages.clear()
    await run_turn(client, manager, session, None)
    assert runner.calls == [["/usr/bin/notify-send", "Local", "Finished: Fix the parser please"]]


def test_every_open_page_holds_a_presence_socket(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    app = create_app(settings)
    with TestClient(app) as client:
        token = client.get("/api/bootstrap").json()["token"]
        manager = app.state.manager
        with client.websocket_connect("/api/presence", subprotocols=["local-workspace", token]):
            assert len(manager.pages) == 1
        for _ in range(100):
            if not manager.pages:
                break
            import time
            time.sleep(0.01)
        assert manager.pages == set()
        for protocols in (["local-workspace", "wrong"], ["local-workspace"]):
            with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/presence", subprotocols=protocols) as socket:
                socket.receive_text()
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(
                "/api/presence", subprotocols=["local-workspace", token], headers={"Origin": "https://example.invalid"}) as socket:
            socket.receive_text()
        assert manager.pages == set()


async def test_a_turn_cut_off_by_a_restart_is_marked_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), model="test-model", env_file="")
    first = create_app(settings)
    async with first.router.lifespan_context(first):
        store = first.state.manager.store
        cut, done = store.create(settings.values), store.create(settings.values)
        cut.update(terminal_reason="completed", turn_ended=None, turn_running=True)  # Running when the server stopped.
        done.update(terminal_reason="completed", turn_ended=123.0)
        store.save(cut)
        store.save(done)
    second = create_app(settings)
    async with second.router.lifespan_context(second):
        store = second.state.manager.store
        recovered = store.get(cut["id"])
        assert recovered["terminal_reason"] == "interrupted" and recovered["turn_ended"] > 123
        assert {key: store.get(done["id"])[key] for key in ("terminal_reason", "turn_ended")} == {
            "terminal_reason": "completed", "turn_ended": 123.0}
    assert attention_message({"title": "T", "terminal_reason": "interrupted"}, "running", "idle", True) == "Stopped with an error: T"


async def test_notifier_failures_never_reach_the_conversation(runtime):
    client, manager, session, _ = runtime
    manager.settings.values["system_notifications"] = True
    failing = Runner(OSError("no display"))
    manager.notifier = SystemNotifier(runner=failing, platform="linux", which=lambda name: "/usr/bin/notify-send")
    await run_turn(client, manager, session, None)
    assert len(failing.calls) == 1
    assert (await listed(client, session))["terminal_reason"] == "completed"
    # A notifier that never finishes does not hold back the agent loop.
    started = asyncio.Event()

    async def hang(argv):
        started.set()
        await asyncio.sleep(3600)
    manager.notifier = SystemNotifier(runner=hang, platform="linux", which=lambda name: "/usr/bin/notify-send")
    await asyncio.wait_for(manager.status(session, "awaiting_approval"), 1)
    await asyncio.wait_for(started.wait(), 1)
    for task in manager.notifier.tasks:
        task.cancel()


def test_messages_name_only_the_status_and_a_short_title():
    long_title = "A very long conversation title " * 4
    message = attention_message({"title": long_title, "terminal_reason": "completed"}, "running", "idle", True)
    title = message.removeprefix("Finished: ")
    assert len(title) == 60 and title.endswith("…") and long_title.startswith(title[:-1])
    assert short_title("Line one\nline\ttwo\x07") == "Line one line two"
    assert short_title("") == "Untitled conversation"
    assert attention_message({"title": "T", "terminal_reason": "inference_error"}, "compacting", "idle", True) == "Stopped with an error: T"
    assert attention_message({"title": "T", "terminal_reason": "tool_error"}, "running", "idle", True) == "Finished: T"
    assert attention_message({"title": "T", "terminal_reason": "completed"}, "compacting", "idle") is None  # Manual.
    assert attention_message({"title": "T", "terminal_reason": "stopped"}, "running", "idle", True) is None
    assert attention_message({"title": "T", "is_subagent": True}, "running", "idle", True) is None
    assert attention_message({"title": "T", "is_subagent": True}, "running", "awaiting_approval") == "Needs your approval: T"
    assert attention_message({"title": "T"}, "idle", "running") is None


def test_notify_send_shows_markup_in_titles_as_text(tmp_path):
    message = attention_message({"title": 'Fix a<b && c>d <a href="https://example.invalid">Approve</a>',
                                 "terminal_reason": "completed"}, "running", "idle", True)
    argv = notification_argv(message, "linux", which=lambda name: f"/usr/bin/{name}")
    assert argv[-1] == ("Finished: Fix a&lt;b &amp;&amp; c&gt;d &lt;a href=\"https://example.invalid\"&gt;"
                        "Approve&lt;/a&gt;")
    # osascript shows plain text, so its message is passed unchanged.
    osascript = tmp_path / "osascript"
    osascript.write_text("")
    assert notification_argv(message, "darwin", osascript=str(osascript))[-1] == message


def test_notifier_commands_are_argument_lists_without_a_shell(tmp_path):
    message = 'Finished: "quoted" $(touch x); `id` & end'
    osascript = tmp_path / "osascript"
    osascript.write_text("")
    argv = notification_argv(message, "darwin", osascript=str(osascript))
    assert argv == [str(osascript), "-e", "on run argv", "-e",
                    "display notification (item 2 of argv) with title (item 1 of argv)",
                    "-e", "end run", "Local", message]
    assert notification_argv(message, "darwin", osascript=str(tmp_path / "missing")) is None
    assert notification_argv(message, "linux", which=lambda name: f"/usr/bin/{name}") == [
        "/usr/bin/notify-send", "Local", 'Finished: "quoted" $(touch x); `id` &amp; end']
    assert notification_argv(message, "linux", which=lambda name: None) is None
    assert notification_argv(message, "win32") is None
    assert all("sh" != part and "-c" != part for part in argv)


async def test_the_notifier_process_runs_without_a_shell_and_is_stopped_after_its_timeout(monkeypatch):
    started = []

    class Process:
        returncode = None
        killed = False

        async def wait(self):
            if not self.killed:
                await asyncio.sleep(3600)
            return -9

        def kill(self):
            self.killed = True

    process = Process()

    async def create_subprocess_exec(*argv, **options):
        started.append((argv, options))
        return process
    monkeypatch.setattr(notify.asyncio, "create_subprocess_exec", create_subprocess_exec)
    monkeypatch.setenv("DBRICKS_TOKEN", "not-for-the-notifier")
    await asyncio.wait_for(run_notification(["/usr/bin/notify-send", "Local", "Finished: T"], timeout=0.05), 2)
    (argv, options), = started
    assert argv == ("/usr/bin/notify-send", "Local", "Finished: T") and "shell" not in options
    assert "DBRICKS_TOKEN" not in options["env"] and process.killed

    async def missing(*argv, **options):
        raise FileNotFoundError("notify-send")
    monkeypatch.setattr(notify.asyncio, "create_subprocess_exec", missing)
    await run_notification(["/usr/bin/notify-send", "Local", "Finished: T"])  # Logged, not raised.


def test_desktop_notifications_are_a_validated_setting(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    with TestClient(create_app(settings)) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["settings"]["system_notifications"] is False
        headers = {"X-Local-Token": bootstrap["token"]}
        body = {"workspace": str(tmp_path), "model": "test-model", "env_file": ""}
        for bad in (1, 0, "true"):
            assert client.put("/api/settings", headers=headers, json={**body, "system_notifications": bad}).status_code == 422
        assert client.put("/api/settings", headers=headers, json={**body, "system_notifications": True}).json()["system_notifications"] is True
        # Saving without the field keeps the earlier choice.
        assert client.put("/api/settings", headers=headers, json=body).json()["system_notifications"] is True
    assert Settings(tmp_path / "state").values["system_notifications"] is True
    with pytest.raises(ValueError, match="Desktop notifications"):
        settings.update({"system_notifications": "yes"})
