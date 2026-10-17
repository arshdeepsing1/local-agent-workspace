"""Scripting the app (M8b): the NDJSON stream of a chat, the stdlib client (local_agent.client) and
scripts/agent_run.py, driven against the app and the offline simulator (scripts/simulate_databricks.py) on free
ports. No network beyond 127.0.0.1 and no paid calls."""
import asyncio
import importlib.util
import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from local_agent import client as client_module
from local_agent import stream_api
from local_agent.client import Allow, Deny, LocalAgentClient, LocalAgentError, exit_code

APP = Path(__file__).resolve().parents[2]
SCRIPT = APP / "scripts" / "simulate_databricks.py"
RUNNER = APP / "scripts" / "agent_run.py"


@pytest.fixture(scope="module")
def simulator():
    spec = importlib.util.spec_from_file_location("simulate_databricks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def served(simulator, tmp_path):
    """The app on a free port, against the simulated gateway: (url, its AgentManager, the project folder)."""
    gateway, gateway_url = simulator.start_gateway(speed=0.0)
    state, workspace = tmp_path / "state", tmp_path / "project"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Demo\n\nA project for scripted runs.\n")
    with simulator.patched_settings(gateway_url, state, workspace):
        server, thread, url = simulator.start_app()
        try:
            yield url, server.config.app.state.manager, workspace
        finally:
            server.should_exit = True
            thread.join(15)
            gateway.shutdown()
            gateway.server_close()


def last(messages, kind):
    return [message for message in messages if message.get("type") == kind][-1]


# The NDJSON stream

def test_the_stream_needs_the_local_token(served):
    url, _, _ = served
    session = LocalAgentClient(url).create_session()
    response = httpx.get(f"{url}/api/sessions/{session['id']}/stream.ndjson")
    assert response.status_code == 403 and response.json()["code"] == "reconnect_required"


def test_the_stream_starts_with_the_snapshot_and_ends_a_turn_with_its_result(served):
    url, manager, _ = served
    client = LocalAgentClient(url)
    session = client.create_session(permission_mode="manual")
    seen, done = [], threading.Event()

    def read():
        for message in client.events(session["id"], stop=done):
            seen.append(message)
            if message.get("type") == "status" and message.get("turn_result"):
                done.set()
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.time() + 10
    while not seen and time.time() < deadline:
        time.sleep(0.02)
    client.send(session["id"], "Hello there")
    assert done.wait(20)
    reader.join(5)
    assert seen[0]["type"] == "snapshot" and seen[0]["session"]["id"] == session["id"]
    assert seen[0]["session"]["status"] == "idle" and "usage_summary" not in seen[0]["session"]
    assert any(message["type"] == "delta" for message in seen)
    end = seen[-1]
    assert end["status"] == "idle" and end["terminal_reason"] == "completed"
    assert end["turn_result"]["steps"] == 1 and end["turn_result"]["usage"]["calls"] == 2  # The reply and the title.
    # The reader is dropped once its connection closes (seen at its next keep-alive at the latest).
    deadline = time.time() + 20
    while manager.listeners.get(session["id"]) and time.time() < deadline:
        time.sleep(0.05)
    assert not manager.listeners.get(session["id"])


def test_a_quiet_stream_sends_keep_alive_lines(served, monkeypatch):
    url, _, _ = served
    monkeypatch.setattr(stream_api, "STREAM_KEEPALIVE_SECONDS", 0.05)
    client = LocalAgentClient(url)
    session = client.create_session()
    kinds = []
    for message in client._lines(session["id"]):
        kinds.append(message["type"])
        if kinds.count("keep_alive") >= 2:
            break
    assert kinds[0] == "snapshot" and kinds.count("keep_alive") == 2


async def test_a_reader_that_falls_behind_is_dropped():
    listener = stream_api.StreamListener(limit=2)
    await listener.send_json({"n": 1})
    await listener.send_json({"n": 2})
    with pytest.raises(ConnectionError):
        await listener.send_json({"n": 3})
    assert listener.closed == "slow_reader"
    with pytest.raises(ConnectionError):
        await listener.send_json({"n": 4})
    # Closed after a failed send: a full queue is read to its end; an empty one wakes its reader at once.
    await listener.close(1011)
    assert listener.queue.qsize() == 2
    quiet = stream_api.StreamListener()
    await quiet.close(1011)
    assert quiet.closed == "closed" and quiet.queue.get_nowait() is None


def test_query_reconnects_after_the_app_dropped_a_slow_reader(monkeypatch):
    """A dropped stream (stream_closed slow_reader) is opened again; its fresh snapshot shows the turn ended."""
    client = LocalAgentClient("http://127.0.0.1:9")
    previous = {"started": 1.0, "terminal_reason": "completed", "steps": 1}
    ours = {"started": 5.0, "terminal_reason": "completed", "steps": 2}
    reply = {"id": "r1", "type": "assistant", "text": "All done.", "created": 6.0}
    streams = iter([
        [{"type": "snapshot", "session": {"id": "s1", "status": "idle", "events": [], "last_turn_result": previous}},
         {"type": "status", "status": "running"}, {"type": "stream_closed", "reason": "slow_reader"}],
        [{"type": "snapshot", "session": {"id": "s1", "status": "idle", "events": [reply], "last_turn_result": ours}}],
    ])
    monkeypatch.setattr(client, "_lines", lambda chat_id, stop=None, opened=None: iter(next(streams)))
    sent = []
    monkeypatch.setattr(client, "send", lambda chat_id, text, verbatim=False: sent.append(text))
    messages = list(client.query("Go", session_id="s1"))
    assert sent == ["Go"] and [message["type"] for message in messages] == ["snapshot", "status", "snapshot", "result"]
    assert messages[-1]["steps"] == 2 and messages[-1]["result"] == "All done."


# The client

def test_query_ends_with_a_result_record(served):
    url, _, workspace = served
    client = LocalAgentClient(url)
    messages = list(client.query("read the project", permission_mode="plan"))
    result = messages[-1]
    assert result["type"] == "result" and result["terminal_reason"] == "completed" and result["exit_code"] == 0
    assert result["result"].startswith("I looked at the project overview and the README")
    assert (result["steps"], result["tool_calls"]) == (3, 2) and result["is_error"] is False
    assert result["usage"]["calls"] == 4 and result["tokens"] > 0 and result["changed_files"] == []  # 3 steps, a title.
    assert messages[0]["type"] == "snapshot" and all(message["session_id"] == result["session_id"] for message in messages)
    info = client.info(result["session_id"])
    assert info["workspace"] == str(workspace) and info["permission_mode"] == "plan"
    assert info["last_turn_result"]["steps"] == 3


def test_the_callback_allows_or_denies_the_edit_turn_in_manual(served):
    url, _, workspace = served
    client = LocalAgentClient(url)
    asked = []

    def allow(name, arguments, context):
        asked.append((name, context["subagent"], bool(context["event_id"])))
        return Allow()
    result = list(client.query("edit a note", permission_mode="manual", can_use_tool=allow))[-1]
    assert [name for name, *_ in asked] == ["write_file", "edit_file", "edit_file"]
    assert all(subagent is False and has_id for _, subagent, has_id in asked)
    assert result["terminal_reason"] == "completed" and len(result["changed_files"]) == 1
    written = workspace / result["changed_files"][0]
    assert written.read_text().endswith("status: step 3\n")
    written.unlink()  # The next run may pick the same name (it has the time to the second): write_file then asks again.

    def deny(name, arguments, context):
        return Deny("Not in this run.")
    result = list(client.query("edit a note", permission_mode="manual", can_use_tool=deny))[-1]
    # The later edits of a file that was never written fail: the turn still completed (exit code 0).
    assert result["terminal_reason"] in ("completed", "tool_error") and result["exit_code"] == 0
    assert result["changed_files"] == []
    session = client.session(result["session_id"])
    tools = [event for event in session["events"] if event["type"] == "tool"]
    assert tools[0]["approval"] == "declined" and tools[0]["decline_note"] == "Not in this run."


def test_a_subagents_messages_and_cards_come_with_its_parents(served):
    url, _, _ = served
    client = LocalAgentClient(url)
    asked = []

    def allow(name, arguments, context):
        asked.append(name)
        return Allow()
    messages = list(client.query("delegate a survey", permission_mode="manual", can_use_tool=allow))
    assert asked == ["delegate_task"]  # The explore subagent only reads: no card of its own.
    children = {message["session_id"] for message in messages if message.get("subagent")}
    assert len(children) == 1 and messages[-1]["subagents"]["count"] == 1
    assert any(message.get("subagent") and message["type"] == "snapshot" for message in messages)
    assert messages[-1]["result"].startswith("Subagent reports received")


def test_without_a_callback_cards_are_declined_and_questions_skipped_at_once(served):
    url, manager, _ = served
    client = LocalAgentClient(url)
    manager.settings.values["approval_timeout_minutes"] = 0  # A card nobody answered would wait forever.
    started = time.time()
    result = list(client.query("edit a note", permission_mode="manual"))[-1]
    assert result["exit_code"] == 0 and result["changed_files"] == []
    tools = [event for event in client.session(result["session_id"])["events"] if event["type"] == "tool"]
    assert tools[0]["approval"] == "declined" and tools[0]["decline_note"] == client_module.NO_APPROVER
    result = list(client.query("ask me something", permission_mode="manual"))[-1]
    question = next(event for event in client.session(result["session_id"])["events"] if event.get("name") == "ask_user")
    assert question["question_outcome"] == "skipped" and result["terminal_reason"] == "completed"
    assert time.time() - started < 30


def test_on_question_answers_ask_user(served):
    url, _, _ = served
    client = LocalAgentClient(url)

    def answer(questions, context):
        return [{"selected": [questions[0]["options"][1]["label"]], "other": None}]
    result = list(client.query("ask me something", permission_mode="manual", on_question=answer))[-1]
    assert "Fast" in result["result"]


def test_interrupt_stops_the_turn(served):
    url, _, _ = served
    client = LocalAgentClient(url)
    holder = {}

    def stop(name, arguments, context):
        client.interrupt(context["session_id"])
        holder["session"] = context["session_id"]
        return Allow()
    result = list(client.query("edit a note", permission_mode="manual", can_use_tool=stop))[-1]
    assert result["terminal_reason"] == "stopped" and result["exit_code"] == 3
    assert client.wait_result(holder["session"])["terminal_reason"] == "stopped"  # Its last turn, from the snapshot.


def test_a_busy_chat_and_a_missing_app_are_errors(served):
    url, manager, _ = served
    client = LocalAgentClient(url)
    session = client.create_session(permission_mode="manual")
    manager.statuses[session["id"]] = "running"
    with pytest.raises(LocalAgentError, match="busy"):
        list(client.query("Hello", session_id=session["id"]))
    manager.statuses[session["id"]] = "idle"
    with pytest.raises(LocalAgentError, match="No app answered"):
        LocalAgentClient("http://127.0.0.1:9").info("x")
    with pytest.raises(ValueError, match="this computer only"):
        LocalAgentClient("http://example.com:8765")
    # A token from before a restart is fetched again.
    client.token = "stale"
    assert client.info(session["id"])["id"] == session["id"]


def test_exit_codes_follow_the_terminal_reason():
    assert [exit_code(reason) for reason in ("completed", "tool_error", "error", "inference_error", "hook_blocked",
                                             "step_limit", "spending_limit", "budget_limit", "stopped", "interrupted")] \
        == [0, 0, 1, 1, 1, 2, 2, 2, 3, 3]


# The workspace a script chooses

def test_a_new_chat_can_name_its_project_folder_checked_like_settings(served, tmp_path):
    url, manager, workspace = served
    client = LocalAgentClient(url)
    other = tmp_path / "other"
    other.mkdir()
    session = client.create_session(workspace=str(other), max_steps=3, turn_token_limit=50_000)
    assert session["workspace"] == str(other) and session["max_agent_steps"] == 3
    assert session["turn_token_limit"] == 50_000
    for value, message in ((str(manager.settings.state_dir), "state folder"),
                           (str(manager.settings.state_dir / "conversations"), "state folder"),
                           ("relative/path", "absolute path"), (str(tmp_path / "missing"), "existing project")):
        with pytest.raises(LocalAgentError, match=message):
            client.create_session(workspace=value)
    with pytest.raises(LocalAgentError) as refused:
        client.request("POST", "/api/sessions", {"max_agent_steps": 65})
    assert refused.value.status == 422
    assert client.create_session()["workspace"] == str(workspace)  # The Settings workspace by default.


def test_a_chats_own_agent_steps_end_its_turn(served):
    url, _, _ = served
    result = list(LocalAgentClient(url).query("read the project", max_steps=1))[-1]
    assert result["terminal_reason"] == "step_limit" and result["exit_code"] == 2 and result["steps"] == 1


# scripts/agent_run.py

def run(url, *args, stdin=None):
    return subprocess.run([sys.executable, str(RUNNER), "--url", url, *args], capture_output=True, text=True,
                          input=stdin, timeout=60)


def test_the_runner_prints_json_and_exits_with_the_turns_code(served):
    url, _, _ = served
    done = run(url, "--output", "json", "read the project")
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["terminal_reason"] == "completed" and result["result"].startswith("I looked at")
    limited = run(url, "--output", "json", "--max-steps", "1", "-", stdin="read the project")
    assert limited.returncode == 2 and json.loads(limited.stdout)["terminal_reason"] == "step_limit"


def test_the_runner_streams_json_lines_and_prints_text(served):
    url, _, _ = served
    streamed = run(url, "--output", "stream-json", "--mode", "manual", "edit a note")
    lines = [json.loads(line) for line in streamed.stdout.splitlines()]
    assert streamed.returncode == 0 and lines[0]["type"] == "snapshot" and lines[-1]["type"] == "result"
    assert lines[-1]["changed_files"] == []  # --approve deny (the default) declined every card.
    text = run(url, "Hello")
    assert text.returncode == 0 and text.stdout.startswith("Simulated reply") and "completed · 1 steps" in text.stderr


def test_the_runner_reports_a_missing_app_and_refuses_ask_without_a_terminal(served):
    missing = run("http://127.0.0.1:9", "Hello")
    assert missing.returncode == 1 and "No app answered" in missing.stderr
    asked = run(served[0], "--approve", "ask", "Hello")
    assert asked.returncode == 2 and "needs a terminal" in asked.stderr  # argparse's usage error.
    bypass = run(served[0], "--mode", "bypassPermissions", "Hello")
    assert bypass.returncode == 2 and "--confirm-bypass" in bypass.stderr


async def test_the_simulator_helpers_restore_the_settings(simulator, tmp_path):
    from local_agent import config
    before = config.Settings.credentials
    with simulator.patched_settings("http://127.0.0.1:1", tmp_path / "s", tmp_path):
        assert config.Settings(tmp_path / "s").credentials() == ("http://127.0.0.1:1", "simulated-token")
    assert config.Settings.credentials is before
    await asyncio.sleep(0)


def test_ctrl_c_stops_the_turn_and_prints_its_result(served, simulator, monkeypatch):
    url, _, _ = served
    monkeypatch.setattr(simulator.Gateway, "speed", 1.0)  # The edit turn pauses 4 seconds between its steps.
    process = subprocess.Popen([sys.executable, str(RUNNER), "--url", url, "--output", "json", "--mode", "manual", "edit a note"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    client = LocalAgentClient(url)
    deadline = time.time() + 20
    while time.time() < deadline and not any(item["status"] != "idle" for item in client.request("GET", "/api/sessions")):
        time.sleep(0.05)
    process.send_signal(signal.SIGINT)
    out, err = process.communicate(timeout=30)
    assert process.returncode == 3, err
    assert "Stopping the turn" in err and json.loads(out)["terminal_reason"] == "stopped"
