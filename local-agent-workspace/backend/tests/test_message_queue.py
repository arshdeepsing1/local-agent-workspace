"""Messages sent while the agent works: queued, steered into the turn at a step boundary,
or sent as the next turn; paused after a stop or failure (M4b)."""
import asyncio
import json

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.composer_api import STEER_PREFIX
from local_agent.config import Settings
from local_agent.context import OUTPUT_LIMIT_CONTINUATION, context_boundary, is_continuation, prepare_context
from local_agent.portability import _validate_wire
from local_agent.store import Store


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", compaction_handoffs=False)
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Queue", title_generated=True)
    store.save(session)
    yield manager, session, project
    store.db.close()


def frame(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


def reply(text="Done.", finish="stop"):
    return httpx.Response(200, text=frame({"content": text}, finish) + "data: [DONE]\n\n")


def tool_reply(*calls):
    parts = [{"index": index, "id": f"call-{name}-{index}", "function": {"name": name, "arguments": json.dumps(arguments)}}
             for index, (name, arguments) in enumerate(calls)]
    return httpx.Response(200, text=frame({"content": "", "tool_calls": parts}, "tool_calls") + "data: [DONE]\n\n")


def summary(text="Summary of the earlier turns."):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})


def gateway(monkeypatch, steps):
    """Answer requests with the given callables in order: each gets the request body and returns a
    response (or a coroutine of one). The bodies are recorded."""
    requests = []
    client = httpx.AsyncClient

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        result = steps[min(len(requests), len(steps)) - 1](body)
        return await result if asyncio.iscoroutine(result) else result
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**{"transport": httpx.MockTransport(handle), **kwargs}))
    return requests


async def settle(manager, session_id):
    """Wait until no task of the chat runs and no queued-message start is pending."""
    for _ in range(500):
        task = manager.tasks.get(session_id)
        pending = [item for item in manager.__dict__.get("_queue_tasks", ()) if not item.done()]
        if (task is None or task.done()) and not pending:
            await asyncio.sleep(0.02)  # Done-callbacks run on the next loop iterations.
            task = manager.tasks.get(session_id)
            pending = [item for item in manager.__dict__.get("_queue_tasks", ()) if not item.done()]
            if (task is None or task.done()) and not pending:
                return
        await asyncio.sleep(0.01)
    raise AssertionError("The conversation did not settle.")


async def until(condition):
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("The condition was not met.")


async def approve_next(manager, session_id):
    await until(lambda: any(key[0] == session_id for key in manager.pending))
    (_, event_id), = [key for key in manager.pending if key[0] == session_id]
    manager.decide(session_id, event_id, True)


def users(session):
    return [event for event in session["events"] if event["type"] == "user"]


def roles(messages):
    return [message["role"] for message in messages]


def no_adjacent_users(messages):
    names = roles(messages)
    return all(not (first == second == "user") for first, second in zip(names, names[1:]))


async def test_a_message_queued_during_an_approval_steers_the_same_turn(runtime, monkeypatch):
    manager, session, project = runtime
    sid = session["id"]
    requests = gateway(monkeypatch, [lambda body: tool_reply(("write_file", {"path": "a.txt", "content": "x"})),
                                     lambda body: reply("Done, and I read your note.")])
    ended = []
    status = manager.status

    async def record(session, value, turn_ended=False, **extra):
        ended.append(turn_ended)
        await status(session, value, turn_ended, **extra)
    monkeypatch.setattr(manager, "status", record)
    manager.start(sid, "Write a.txt")
    await until(lambda: manager.pending)
    queued = await manager.submit_message(sid, "Also put a date in it")
    assert queued["queued"] is True and [item["text"] for item in queued["queued_messages"]] == ["Also put a date in it"]
    assert queued["queued_messages"][0]["paused"] is False
    await approve_next(manager, sid)
    await settle(manager, sid)

    assert len(requests) == 2
    last = requests[1]["messages"][-3:]
    assert roles(last) == ["assistant", "tool", "user"]
    assert last[0]["tool_calls"][0]["id"] == last[1]["tool_call_id"]
    assert last[2]["content"] == STEER_PREFIX + "Also put a date in it"
    live = manager.get(sid)
    assert ended.count(True) == 1 and live["terminal_reason"] == "completed"
    _validate_wire(live["wire"], complete=True)
    steered = users(live)[-1]
    assert steered["text"] == "Also put a date in it" and steered["steered"] is True and steered["in_wire"] is True
    # m4-history-branching records in_wire on every user event once its prompt is sent.
    assert users(live)[0]["in_wire"] is True and live.get("queued_messages") == []
    assert (project / "a.txt").read_text() == "x"


async def test_a_steered_message_comes_after_every_result_of_its_step(runtime, monkeypatch):
    manager, session, project = runtime
    sid = session["id"]
    (project / "b.txt").write_text("bee")
    requests = gateway(monkeypatch, [
        lambda body: tool_reply(("write_file", {"path": "a.txt", "content": "x"}), ("read_file", {"path": "b.txt"})),
        lambda body: reply("Done.")])
    manager.start(sid, "Write and read")
    await until(lambda: manager.pending)
    await manager.submit_message(sid, "Stop after this")
    await approve_next(manager, sid)
    await settle(manager, sid)
    last = requests[1]["messages"][-4:]
    assert roles(last) == ["assistant", "tool", "tool", "user"]
    assert [message["tool_call_id"] for message in last[1:3]] == [call["id"] for call in last[0]["tool_calls"]]
    assert last[3]["content"] == STEER_PREFIX + "Stop after this"
    _validate_wire(manager.get(sid)["wire"], complete=True)


async def test_a_steered_message_never_follows_an_output_limit_continuation(runtime, monkeypatch):
    manager, session, project = runtime
    sid = session["id"]

    async def cut_off(body):
        await manager.submit_message(sid, "Use small steps")
        return reply("Very long", finish="length")
    requests = gateway(monkeypatch, [cut_off, lambda body: tool_reply(("list_files", {})), lambda body: reply("Done.")])
    manager.start(sid, "Write a long file")
    await settle(manager, sid)
    assert len(requests) == 3
    second, third = requests[1]["messages"], requests[2]["messages"]
    assert second[-1] == {"role": "user", "content": OUTPUT_LIMIT_CONTINUATION}
    assert not any(STEER_PREFIX in str(message.get("content")) for message in second)
    assert roles(third[-3:]) == ["assistant", "tool", "user"] and third[-1]["content"].startswith(STEER_PREFIX)
    assert no_adjacent_users(third) and no_adjacent_users(manager.get(sid)["wire"])


async def test_a_message_queued_during_the_final_reply_starts_the_next_turn(runtime, monkeypatch):
    manager, session, _ = runtime
    sid = session["id"]

    async def final(body):
        await manager.submit_message(sid, "First follow-up")
        await manager.submit_message(sid, "Second follow-up")
        return reply("Answer one.")
    requests = gateway(monkeypatch, [final, lambda body: reply("Answer two.")])
    manager.start(sid, "Question")
    await settle(manager, sid)
    assert len(requests) == 2
    assert requests[1]["messages"][-1] == {"role": "user", "content": "First follow-up\n\nSecond follow-up"}
    live = manager.get(sid)
    assert [event["text"] for event in users(live)] == ["Question", "First follow-up\n\nSecond follow-up"]
    assert not users(live)[1].get("steered") and live["queued_messages"] == []
    assert live["terminal_reason"] == "completed"


async def test_a_turn_with_a_failed_tool_still_sends_the_queue(runtime, monkeypatch):
    manager, session, _ = runtime
    sid = session["id"]

    async def final(body):
        await manager.submit_message(sid, "Then continue")
        return reply("The file is missing.")
    requests = gateway(monkeypatch, [lambda body: tool_reply(("read_file", {"path": "missing.txt"})), final,
                                     lambda body: reply("Continued.")])
    manager.start(sid, "Read missing.txt")
    await until(lambda: len(requests) >= 3)
    await settle(manager, sid)
    assert len(requests) == 3 and requests[2]["messages"][-1]["content"] == "Then continue"
    assert manager.get(sid)["queued_messages"] == []


async def test_at_most_three_messages_and_50000_characters_can_wait(runtime):
    manager, session, _ = runtime
    sid = session["id"]
    blocker = asyncio.Event()
    manager.live[sid] = session
    manager.tasks[sid] = asyncio.create_task(blocker.wait())
    try:
        for number in range(3):
            await manager.submit_message(sid, f"message {number}")
        with pytest.raises(ValueError, match="At most 3 messages can wait. Remove one or wait for the reply."):
            await manager.submit_message(sid, "fourth")
        # The blank line that joins queued messages counts: the joined text must fit one message.
        session["queued_messages"] = [{"id": "big", "text": "x" * 49_997, "created": 1, "paused": True}]
        with pytest.raises(ValueError, match="at most 50,000 characters in total"):
            await manager.submit_message(sid, "xy")
        await manager.submit_message(sid, "y")
        assert [len(item["text"]) for item in session["queued_messages"]] == [49_997, 1]
        assert len("\n\n".join(item["text"] for item in session["queued_messages"])) == 50_000
        with pytest.raises(KeyError):
            await manager.remove_queued(sid, "missing")
        assert [item["text"] for item in await manager.remove_queued(sid, "big")] == ["y"]
    finally:
        blocker.set()
        await settle(manager, sid)


async def test_queued_messages_must_fit_one_message_together(runtime):
    manager, session, project = runtime
    sid = session["id"]
    for number in range(6):
        (project / f"f{number}.py").write_text(f"x = {number}\n")
    blocker = asyncio.Event()
    manager.live[sid] = session
    manager.tasks[sid] = asyncio.create_task(blocker.wait())
    try:
        await manager.submit_message(sid, "Look at @f0.py @f1.py @f2.py")
        with pytest.raises(ValueError, match=r"^Queued messages are sent as one message\. Attach at most 5 files to one message\.$"):
            await manager.submit_message(sid, "And @f3.py @f4.py @f5.py")
        assert [item["text"] for item in session["queued_messages"]] == ["Look at @f0.py @f1.py @f2.py"]
        await manager.submit_message(sid, "And @f3.py @f4.py, again @f0.py")  # Five distinct files.
        assert len(session["queued_messages"]) == 2
    finally:
        blocker.set()
        await settle(manager, sid)


async def test_stop_while_the_title_is_generated_sends_no_queued_message(runtime, monkeypatch):
    manager, session, _ = runtime
    sid = session["id"]
    session.update(title="New conversation", title_generated=False)
    manager.store.save(session)
    hold = asyncio.Event()

    async def final(body):
        await manager.submit_message(sid, "Queued while answering")
        return reply("Answer.")

    async def naming(body):
        await hold.wait()
    requests = gateway(monkeypatch, [final, naming, lambda body: reply("Not wanted.")])
    manager.start(sid, "Hello there, please explain the project")
    await until(lambda: manager.statuses.get(sid) == "naming")
    await manager.stop(sid)
    await settle(manager, sid)
    stored = manager.get(sid)
    assert len(requests) == 2 and [event["text"] for event in users(stored)] == ["Hello there, please explain the project"]
    assert [(item["text"], item["paused"]) for item in stored["queued_messages"]] == [("Queued while answering", False)]
    assert not manager.stop_requested


async def test_stop_leaves_the_queue_and_the_next_message_pauses_it(runtime, monkeypatch):
    manager, session, _ = runtime
    sid = session["id"]
    hold = asyncio.Event()

    async def blocked(body):
        await manager.submit_message(sid, "Leftover")
        await hold.wait()
    requests = gateway(monkeypatch, [blocked, lambda body: tool_reply(("list_files", {})), lambda body: reply("Fresh done."),
                                     lambda body: reply("Leftover done.")])
    manager.start(sid, "Long task")
    await until(lambda: manager.get(sid).get("queued_messages"))
    await manager.stop(sid)
    await settle(manager, sid)
    stored = manager.get(sid)
    assert stored["terminal_reason"] == "stopped" and len(requests) == 1
    assert [(item["text"], item["paused"]) for item in stored["queued_messages"]] == [("Leftover", False)]

    manager.start(sid, "Fresh request")  # Idle: the leftover is paused, never steered or sent after it.
    assert [item["paused"] for item in manager.get(sid)["queued_messages"]] == [True]
    await settle(manager, sid)
    assert len(requests) == 3
    assert requests[2]["messages"][-1]["role"] == "tool"
    assert not any(STEER_PREFIX in str(message.get("content")) for body in requests for message in body["messages"])
    stored = manager.get(sid)
    assert stored["terminal_reason"] == "completed" and [item["text"] for item in stored["queued_messages"]] == ["Leftover"]

    await manager.send_queued(sid)  # 'Send queued messages' sends paused ones too.
    await settle(manager, sid)
    assert len(requests) == 4 and requests[3]["messages"][-1]["content"] == "Leftover"
    assert manager.get(sid)["queued_messages"] == []


@pytest.mark.parametrize("ending", ["error", "step_limit"])
async def test_an_error_or_the_step_limit_does_not_send_the_queue(runtime, monkeypatch, ending):
    manager, session, _ = runtime
    sid = session["id"]
    manager.settings.values["max_agent_steps"] = 2

    async def queue_then(response):
        await manager.submit_message(sid, "Queued")
        return response
    if ending == "error":
        steps = [lambda body: queue_then(httpx.Response(400, json={"error": "bad request"}))]
    else:
        steps = [lambda body: tool_reply(("list_files", {})),
                 lambda body: queue_then(tool_reply(("list_files", {})))]
    requests = gateway(monkeypatch, steps)
    manager.start(sid, "Work")
    await settle(manager, sid)
    stored = manager.get(sid)
    assert stored["terminal_reason"] == ("inference_error" if ending == "error" else "step_limit")
    assert len(requests) == len(steps)
    # The last step has no boundary after it: nothing is steered into a step that will not run.
    assert not any(STEER_PREFIX in str(message.get("content")) for message in stored["wire"])
    assert [(item["text"], item["paused"]) for item in stored["queued_messages"]] == [("Queued", False)]
    gateway(monkeypatch, [lambda body: reply("Next.")])
    manager.start(sid, "Next")
    assert [item["paused"] for item in manager.get(sid)["queued_messages"]] == [True]
    await settle(manager, sid)


def earlier_turns(session):
    session["wire"] = [{"role": "user", "content": "First"}, {"role": "assistant", "content": "One."},
                       {"role": "user", "content": "Second"}, {"role": "assistant", "content": "Two."}]
    session["events"] = [{"id": "u1", "type": "user", "text": "First", "created": 1},
                         {"id": "u2", "type": "user", "text": "Second", "created": 2}]


@pytest.mark.parametrize("stopped", [False, True])
async def test_a_message_queued_during_manual_compaction_starts_afterwards_unless_stopped(runtime, monkeypatch, stopped):
    manager, session, _ = runtime
    sid = session["id"]
    earlier_turns(session)
    manager.store.save(session)
    hold = asyncio.Event()

    async def compaction(body):
        await manager.submit_message(sid, "After compaction")
        if stopped:
            await hold.wait()
        return summary()
    requests = gateway(monkeypatch, [compaction, lambda body: reply("Answered.")])
    manager.start_compaction(sid)
    if stopped:
        await until(lambda: manager.get(sid).get("queued_messages"))
        await manager.stop(sid)
    await settle(manager, sid)
    stored = manager.get(sid)
    if stopped:
        assert len(requests) == 1 and [item["text"] for item in stored["queued_messages"]] == ["After compaction"]
    else:
        assert len(requests) == 2 and requests[1]["stream"] is True
        assert requests[1]["messages"][-1] == {"role": "user", "content": "After compaction"}
        assert stored["queued_messages"] == []


async def test_a_queue_that_cannot_start_is_paused_with_its_error(runtime, monkeypatch):
    manager, session, _ = runtime
    sid = session["id"]
    sent = []
    broadcast = manager.broadcast

    async def record(session_id, data):
        sent.append(data)
        await broadcast(session_id, data)
    monkeypatch.setattr(manager, "broadcast", record)
    available = manager.workspace_available

    async def final(body):
        await manager.submit_message(sid, "Next")

        def removing(workspace):
            raise ValueError("This managed worktree is being removed. Choose another workspace.")
        monkeypatch.setattr(manager, "workspace_available", removing)
        return reply("Done.")
    requests = gateway(monkeypatch, [final])
    manager.start(sid, "Work")
    await settle(manager, sid)
    monkeypatch.setattr(manager, "workspace_available", available)
    assert len(requests) == 1
    assert [(item["text"], item["paused"]) for item in manager.get(sid)["queued_messages"]] == [("Next", True)]
    errors = [data for data in sent if data.get("type") == "queue" and data.get("error")]
    assert errors and errors[-1]["error"] == "This managed worktree is being removed. Choose another workspace."
    assert errors[-1]["queued_messages"][0]["paused"] is True


async def test_a_queued_file_whose_grant_was_removed_is_sent_with_a_note(runtime, monkeypatch, tmp_path):
    manager, session, _ = runtime
    sid = session["id"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.txt").write_text("outside notes\n")
    session["allowed_directories"] = [str(outside)]
    manager.store.save(session)

    async def final(body):
        await manager.submit_message(sid, f"Compare @{outside}/notes.txt")  # Granted: accepted when queued.
        manager.get(sid)["allowed_directories"] = []
        return reply("Done.")
    requests = gateway(monkeypatch, [final, lambda body: reply("Compared.")])
    manager.start(sid, "Work")
    await settle(manager, sid)
    assert len(requests) == 2
    content = requests[1]["messages"][-1]["content"]
    assert content.startswith(f"Compare @{outside}/notes.txt\n\n---\nContext attached by the app")
    assert f"(Could not attach @{outside}/notes.txt: This folder needs access approval" in content
    assert "Use Folder access above the message box first.)" in content and "outside notes" not in content
    assert "attachments" not in users(manager.get(sid))[-1]


async def test_the_queue_survives_a_restart_in_the_sidecar_and_is_not_started(runtime):
    manager, session, _ = runtime
    sid = session["id"]
    blocker = asyncio.Event()
    manager.live[sid] = session
    manager.tasks[sid] = asyncio.create_task(blocker.wait())
    await manager.submit_message(sid, "Survive a restart")
    session["pending_context"] = [{"kind": "shell", "command": "ls", "exit_code": 0, "text": "a.txt"}]
    manager.store.save(session)
    blocker.set()
    manager.tasks[sid].cancel()  # As if the server stopped: no follow-up.
    await asyncio.sleep(0.05)

    lines = [json.loads(line) for line in manager.store._path(sid).read_text().splitlines()]
    assert not any(line.get("name") in ("queued_messages", "pending_context") for line in lines)
    assert "queued_messages" not in lines[0]["data"] and "pending_context" not in lines[0]["data"]
    state = json.loads(manager.store._state_path(sid).read_text())
    assert [item["text"] for item in state["queued_messages"]] == ["Survive a restart"]

    store = Store(manager.settings.state_dir / "tests.sqlite3")
    try:
        restarted = AgentManager(store, manager.settings)
        reloaded = restarted.get(sid)
        assert [item["text"] for item in reloaded["queued_messages"]] == ["Survive a restart"]
        assert reloaded["pending_context"][0]["command"] == "ls"
        assert not restarted.tasks
        assert "queued_messages" not in store.list()[0]
    finally:
        store.db.close()


async def test_compaction_after_a_steered_message_ends_on_a_valid_boundary(runtime, monkeypatch):
    manager, session, _ = runtime
    sid = session["id"]
    earlier_turns(session)
    manager.store.save(session)
    gateway(monkeypatch, [lambda body: tool_reply(("write_file", {"path": "a.txt", "content": "x"})),
                          lambda body: reply("Done.")])
    manager.start(sid, "Write a.txt")
    await until(lambda: manager.pending)
    await manager.submit_message(sid, "And then stop")
    await approve_next(manager, sid)
    await settle(manager, sid)
    requests = gateway(monkeypatch, [lambda body: summary()])
    manager.start_compaction(sid)
    await settle(manager, sid)
    stored = manager.get(sid)
    through = stored["context_state"]["through"]
    assert len(requests) == 1 and through > 0 and context_boundary(stored["wire"], through)
    assert stored["wire"][through]["role"] == "user" and not is_continuation(stored["wire"][through])
    _validate_wire(stored["wire"], complete=True)


@pytest.mark.parametrize("after", [1, 4])
async def test_in_turn_compaction_keeps_the_request_and_the_steered_message_verbatim(after):
    """A steered message is not a turn start for compaction (u-large-inputs item 6): the summary
    ends inside the turn, and both the turn's request and the steered message stay verbatim."""
    big = "data " * 3000
    wire = [{"role": "user", "content": "Inspect the files"}]
    for number in range(2 + after):
        wire += [{"role": "assistant", "content": None, "tool_calls": [{"id": f"c{number}", "type": "function",
                  "function": {"name": "read_file", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": f"c{number}", "content": big}]
        if number == 1:
            wire.append({"role": "user", "content": STEER_PREFIX + "Focus on the parser"})
    steered = next(index for index, message in enumerate(wire) if STEER_PREFIX in str(message.get("content")))

    async def summarize(previous, chunk, limit):
        return "Summary."
    messages, state, info = await prepare_context(wire, {}, {"role": "system", "content": "s"}, [], 16384, summarize,
                                                  reply_reserve=1024)
    through = state["through"]
    assert context_boundary(wire, through) and through > steered and wire[through]["role"] == "assistant"
    assert info.get("compacted_in_turn") is True
    assert [message["content"] for message in messages[2:4]] == [wire[0]["content"], wire[steered]["content"]]


async def test_the_api_queues_while_busy_and_guards_the_queue_routes(runtime, monkeypatch):
    manager, _, _ = runtime
    app = create_app(manager.settings)
    agents = app.state.manager
    session = agents.store.create(manager.settings.values)
    sid = session["id"]
    hold = asyncio.Event()

    async def blocked(body):
        await hold.wait()
        return reply("Done.")
    requests = gateway(monkeypatch, [blocked])
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            token = (await client.get("/api/bootstrap")).json()["token"]
            headers = {"X-Local-Token": token}
            assert (await client.post(f"/api/sessions/{sid}/messages", headers=headers, json={"text": "Go"})).json() == {"ok": True}
            await until(lambda: requests)
            queued = (await client.post(f"/api/sessions/{sid}/messages", headers=headers, json={"text": "Later"})).json()
            assert queued["ok"] is True and queued["queued"] is True and queued["queued_messages"][0]["text"] == "Later"
            assert (await client.post(f"/api/sessions/{sid}/queue/send", headers=headers)).status_code == 409
            item = queued["queued_messages"][0]["id"]
            gone = await client.delete(f"/api/sessions/{sid}/queue/unknown", headers=headers)
            assert gone.status_code == 404 and gone.json()["detail"] == "This queued message was already sent."
            # Deleting the chat stops its turn; the queue starts nothing.
            assert (await client.delete(f"/api/sessions/{sid}", headers=headers)).status_code == 200
            await settle(agents, sid)
            assert len(requests) == 1 and agents.get(sid) is None
            assert item
    finally:
        hold.set()
        agents.store.db.close()


async def test_the_queue_routes_remove_and_send(runtime, monkeypatch):
    manager, _, _ = runtime
    app = create_app(manager.settings)
    agents = app.state.manager
    session = agents.store.create(manager.settings.values)
    session.update(title="Queue", title_generated=True,
                   queued_messages=[{"id": "q1", "text": "One", "created": 1, "paused": True},
                                    {"id": "q2", "text": "Two", "created": 2, "paused": True}])
    agents.store.save(session)
    sid = session["id"]
    requests = gateway(monkeypatch, [lambda body: reply("Done.")])
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = {"X-Local-Token": (await client.get("/api/bootstrap")).json()["token"]}
            removed = await client.delete(f"/api/sessions/{sid}/queue/q1", headers=headers)
            assert [item["id"] for item in removed.json()["queued_messages"]] == ["q2"]
            assert (await client.post(f"/api/sessions/{sid}/queue/send", headers=headers)).json() == {"ok": True}
            await settle(agents, sid)
            assert requests[0]["messages"][-1]["content"] == "Two" and agents.get(sid)["queued_messages"] == []
            empty = await client.post(f"/api/sessions/{sid}/queue/send", headers=headers)
            assert empty.status_code == 400 and empty.json()["detail"] == "No messages are queued."
    finally:
        agents.store.db.close()
