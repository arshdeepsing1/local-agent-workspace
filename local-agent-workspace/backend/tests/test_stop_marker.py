"""The stop marker (RC02): a Stop, a shutdown or a crash closes the turn in the model history at once,
after completing any interrupted tool exchange, and the 'Stopped.' notice says what was interrupted."""
import asyncio
import contextlib
import json

import httpx
import pytest

from local_agent import history
from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.portability import export_bundle, validate_bundle
from local_agent.store import Store

INTERRUPTED_RESULT = "Execution interrupted; the outcome is unknown. Check the current state before retrying any action."


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(tmp_path / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Stops", title_generated=True, permission_mode="bypassPermissions")
    store.save(session)
    yield manager, session, store, settings
    await manager.jobs.shutdown()
    store.db.close()


def frame(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


class Gateway:
    """Answers streamed requests from a list of factories; records each request body."""

    def __init__(self, monkeypatch, *responses):
        self.requests, self.responses, self.entered = [], list(responses), asyncio.Event()
        client = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
            **kwargs, transport=httpx.MockTransport(self.handle)))

    async def handle(self, request):
        self.requests.append(json.loads(request.content))
        return self.responses.pop(0)(self)


def say(text):
    return lambda gateway: httpx.Response(200, text=frame({"content": text}, "stop") + "data: [DONE]\n\n")


def hang_after(text):
    class Stream(httpx.AsyncByteStream):
        def __init__(self, gateway):
            self.gateway = gateway

        async def __aiter__(self):
            yield frame({"content": text}).encode()
            self.gateway.entered.set()
            await asyncio.Event().wait()
    return lambda gateway: httpx.Response(200, stream=Stream(gateway))


def calls(*items):
    parts = [{"index": index, "id": call_id, "function": {"name": name, "arguments": json.dumps(arguments)}}
             for index, (call_id, name, arguments) in enumerate(items)]
    return lambda gateway: httpx.Response(200, text=frame({"tool_calls": parts}, "tool_calls") + "data: [DONE]\n\n")


def rate_limited(gateway):
    gateway.entered.set()
    return httpx.Response(429, headers={"retry-after": "30"},
                          json={"error_code": "REQUEST_LIMIT_EXCEEDED", "message": "rate limit exceeded"})


async def until(condition, timeout=5):
    async def poll():
        while not condition():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), timeout)


def stopped_notice(session):
    return next(event for event in session["events"] if event["type"] == "notice" and event["text"].startswith("Stopped."))


def no_adjacent_users(messages):
    roles = [message["role"] for message in messages]
    return all(not (first == second == "user") for first, second in zip(roles, roles[1:]))


async def test_stop_while_streaming_keeps_the_partial_text_and_the_marker(runtime, monkeypatch):
    manager, session, store, _ = runtime
    gateway = Gateway(monkeypatch, hang_after("Partial answer"), say("Continued."))
    manager.start(session["id"], "Explain it")
    await asyncio.wait_for(gateway.entered.wait(), 2)
    await until(lambda: (manager.live[session["id"]]["events"][-1].get("text") or "") == "Partial answer")
    await manager.stop(session["id"])
    saved = store.get(session["id"])
    assert saved["wire"] == [{"role": "user", "content": "Explain it"},
                             {"role": "assistant", "content": "Partial answer\n\n" + history.STOP_MARKER}]
    notice = stopped_notice(saved)
    assert notice["text"] == "Stopped. You can continue this conversation." and notice["stop_phase"] == "streaming"
    assert saved["terminal_reason"] == "stopped"
    validate_bundle(export_bundle([saved], saved["id"]))
    await manager.run(saved, "Go on")
    sent = gateway.requests[-1]["messages"]
    assert no_adjacent_users(sent)
    assert sent[-2:] == [{"role": "assistant", "content": "Partial answer\n\n" + history.STOP_MARKER},
                         {"role": "user", "content": "Go on"}]


async def test_stop_during_a_rate_limit_wait_adds_the_marker(runtime, monkeypatch):
    manager, session, store, _ = runtime
    gateway = Gateway(monkeypatch, rate_limited)
    manager.start(session["id"], "Explain it")
    await asyncio.wait_for(gateway.entered.wait(), 2)
    live = manager.live[session["id"]]
    await until(lambda: any(event["type"] == "notice" for event in live["events"]))
    await manager.stop(session["id"])
    saved = store.get(session["id"])
    assert saved["wire"] == [{"role": "user", "content": "Explain it"},
                             {"role": "assistant", "content": history.STOP_MARKER}]
    assert stopped_notice(saved)["stop_phase"] == "waiting"


async def test_stop_while_the_only_tool_runs_completes_the_exchange_first(runtime, monkeypatch):
    manager, session, store, _ = runtime
    gateway = Gateway(monkeypatch, calls(("c1", "run_command", {"command": "sleep 20"})))
    manager.start(session["id"], "Wait")
    live = manager.live[session["id"]]
    await until(lambda: any(event["type"] == "tool" and event.get("job_id") for event in live["events"]))
    await manager.stop(session["id"])
    saved = store.get(session["id"])
    assert [message["role"] for message in saved["wire"]] == ["user", "assistant", "tool", "assistant"]
    assert saved["wire"][2] == {"role": "tool", "tool_call_id": "c1", "content": INTERRUPTED_RESULT}
    assert saved["wire"][3] == {"role": "assistant", "content": history.STOP_MARKER}
    assert stopped_notice(saved)["stop_phase"] == "tools"
    assert len(gateway.requests) == 1


async def test_stop_during_the_second_of_two_tool_calls_is_valid_at_once(runtime, monkeypatch):
    manager, session, store, _ = runtime
    Gateway(monkeypatch, calls(("c1", "list_files", {}), ("c2", "run_command", {"command": "sleep 20"})))
    manager.start(session["id"], "Look and wait")
    live = manager.live[session["id"]]
    await until(lambda: any(event.get("call_id") == "c2" and event.get("job_id") for event in live["events"]))
    await manager.stop(session["id"])
    saved = store.get(session["id"])
    first = next(event for event in saved["events"] if event.get("call_id") == "c1")
    assert first["state"] == "completed"
    wire = saved["wire"]
    assert [message["role"] for message in wire] == ["user", "assistant", "tool", "tool", "assistant"]
    assert [call["id"] for call in wire[1]["tool_calls"]] == ["c1", "c2"]
    assert wire[2] == {"role": "tool", "tool_call_id": "c1", "content": first["output"]}
    assert wire[3] == {"role": "tool", "tool_call_id": "c2", "content": INTERRUPTED_RESULT}
    assert wire[4] == {"role": "assistant", "content": history.STOP_MARKER}
    # Valid before any new turn repairs anything: export, fork and cuts work right away.
    validate_bundle(export_bundle([saved], saved["id"]))
    assert stopped_notice(saved)["stop_phase"] == "tools"


async def test_stop_before_the_prompt_reaches_the_history_never_marks_the_previous_turn(runtime, monkeypatch):
    manager, session, store, _ = runtime
    # The previous turn ended at the step limit: its history ends with tool results.
    session["wire"] = [{"role": "user", "content": "First"},
                       {"role": "assistant", "content": None, "tool_calls": [
                           {"id": "c1", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]},
                       {"role": "tool", "tool_call_id": "c1", "content": "[]"}]
    session["events"] = [{"id": "u1", "type": "user", "text": "First", "in_wire": True},
                         {"id": "t1", "type": "tool", "name": "list_files", "input": {}, "call_id": "c1",
                          "state": "completed", "output": "[]"},
                         {"id": "n1", "type": "notice", "text": "Reached the limit.", "terminal_reason": "step_limit"}]
    session["terminal_reason"] = "step_limit"
    store.save(session)
    discovering = asyncio.Event()

    class Connection:
        async def discover(self):
            discovering.set()
            await asyncio.Event().wait()

    @contextlib.asynccontextmanager
    async def turn():
        yield Connection()

    monkeypatch.setattr(manager.extensions, "turn", turn)
    manager.start(session["id"], "Second")
    await asyncio.wait_for(discovering.wait(), 2)
    await manager.stop(session["id"])
    saved = store.get(session["id"])
    assert saved["wire"] == session["wire"]
    second = [event for event in saved["events"] if event["type"] == "user"][-1]
    assert second["text"] == "Second" and second["in_wire"] is False
    assert stopped_notice(saved)["stop_phase"] == "waiting"


def crashed(store, settings, session, wire, events):
    session.update(wire=wire, events=events, turn_running=True, turn_ended=None)
    store.save(session)
    AgentManager(store, settings)
    return store.get(session["id"])


async def test_a_restart_closes_a_cut_short_turn_once(runtime):
    _, session, store, settings = runtime
    saved = crashed(store, settings, session, [{"role": "user", "content": "Explain it"}], [
        {"id": "u1", "type": "user", "text": "Explain it", "in_wire": True},
        {"id": "a1", "type": "assistant", "text": "Partial", "request_info": {"model": "test-model", "status": "running"}}])
    expected = [{"role": "user", "content": "Explain it"},
                {"role": "assistant", "content": "Partial\n\n" + history.INTERRUPTED_MARKER}]
    assert saved["wire"] == expected
    assert saved["terminal_reason"] == "interrupted"
    AgentManager(store, settings)
    assert store.get(session["id"])["wire"] == expected


async def test_a_restart_after_a_crash_during_the_second_tool_is_valid_at_once(runtime):
    _, session, store, settings = runtime
    call = lambda call_id, name: {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}  # noqa: E731
    saved = crashed(store, settings, session, [
        {"role": "user", "content": "Look and wait"},
        {"role": "assistant", "content": None, "tool_calls": [call("c1", "list_files"), call("c2", "run_command")]},
        {"role": "tool", "tool_call_id": "c1", "content": "[]"}], [
        {"id": "u1", "type": "user", "text": "Look and wait", "in_wire": True},
        {"id": "t1", "type": "tool", "name": "list_files", "input": {}, "call_id": "c1", "state": "completed", "output": "[]"},
        {"id": "t2", "type": "tool", "name": "run_command", "input": {}, "call_id": "c2", "state": "running", "output": ""}])
    assert [message["role"] for message in saved["wire"]] == ["user", "assistant", "tool", "tool", "assistant"]
    assert saved["wire"][3]["content"] == INTERRUPTED_RESULT
    assert saved["wire"][4] == {"role": "assistant", "content": history.INTERRUPTED_MARKER}
    validate_bundle(export_bundle([saved], saved["id"]))


async def test_a_restart_never_marks_a_turn_whose_prompt_was_not_appended(runtime):
    _, session, store, settings = runtime
    wire = [{"role": "user", "content": "First"}, {"role": "assistant", "content": "Done."}]
    saved = crashed(store, settings, session, list(wire), [
        {"id": "u1", "type": "user", "text": "First", "in_wire": True}, {"id": "a1", "type": "assistant", "text": "Done."},
        {"id": "u2", "type": "user", "text": "Second", "in_wire": False}])
    assert saved["wire"] == wire


async def test_stop_during_shutdown_uses_the_interrupted_wording(runtime, monkeypatch):
    manager, session, store, _ = runtime
    gateway = Gateway(monkeypatch, hang_after("Partial"))
    manager.closing = True
    manager.start(session["id"], "Explain it")
    await asyncio.wait_for(gateway.entered.wait(), 2)
    await until(lambda: (manager.live[session["id"]]["events"][-1].get("text") or "") == "Partial")
    await manager.stop(session["id"])
    assert store.get(session["id"])["wire"][-1] == {"role": "assistant",
                                                    "content": "Partial\n\n" + history.INTERRUPTED_MARKER}


async def test_user_events_record_whether_their_text_reached_the_history(runtime, monkeypatch):
    manager, session, store, settings = runtime
    Gateway(monkeypatch, say("Hello."))
    await manager.run(session, "Hi")
    settings.credentials = lambda: (_ for _ in ()).throw(ValueError("Configure Databricks first."))
    await manager.run(session, "Hi again")
    users = [event for event in store.get(session["id"])["events"] if event["type"] == "user"]
    assert [(event["text"], event["in_wire"]) for event in users] == [("Hi", True), ("Hi again", False)]
