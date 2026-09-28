import asyncio
import copy
import json
import multiprocessing
from pathlib import Path

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.drafts import StreamDraft
from local_agent.store import Store


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(tmp_path / "sessions.db")
    manager = AgentManager(store, settings)
    yield manager, store.create(settings.values)
    store.db.close()


def hanging_gateway(monkeypatch, entered, text="Partial answer", usage=None, tool_calls=None):
    class HangingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            delta = {"content": text}
            if tool_calls:
                delta["tool_calls"] = tool_calls
            chunk = {"choices": [{"delta": delta, "finish_reason": None}], "usage": usage}
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            entered.set()
            await asyncio.Event().wait()

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(**kwargs,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=HangingStream()))))


def request_event(session):
    return next(event for event in reversed(session["events"]) if "request_info" in event)


def _draft_writer_process(database, state_dir, workspace, ready):
    """A real process that the test kills without running cancellation cleanup."""
    from local_agent import drafts
    drafts.DRAFT_INTERVAL_SECONDS = 0.02

    async def write():
        settings = Settings(Path(state_dir))
        settings.values.update(workspace=workspace, env_file="")
        store = Store(Path(database))
        manager = AgentManager(store, settings)
        session = store.create(settings.values)
        event = await manager.event(session, "assistant", text="",
                                    request_info={"model": session["model"], "status": "running"})

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                chunk = {"choices": [{"delta": {"content": "Survives hard crash", "tool_calls": [
                    {"index": 0, "id": "partial", "function": {"name": "write_file", "arguments": '{"path":'}}]},
                    "finish_reason": None}], "usage": {"prompt_tokens": 9, "completion_tokens": 3}}
                yield ("data: " + json.dumps(chunk) + "\n\n").encode()
                while request_event(store.get(session["id"]))["text"] != "Survives hard crash":
                    await asyncio.sleep(0.01)
                ready.send(session["id"])
                await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=Stream()))) as client:
            await manager.model_response(session, event, client, "https://gateway.example/invocations", {}, {})

    asyncio.run(write())


def test_hard_killed_process_restores_durable_partial_reply_without_executable_calls(tmp_path):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    database, state_dir = tmp_path / "crash.db", tmp_path / "state"
    process = context.Process(target=_draft_writer_process,
                              args=(str(database), str(state_dir), str(tmp_path), sender))
    process.start()
    sender.close()
    try:
        assert receiver.poll(5), "Writer did not persist its streamed draft"
        session_id = receiver.recv()
        process.kill()
        process.join(3)
        assert not process.is_alive()
        store = Store(database)
        try:
            AgentManager(store, Settings(state_dir))
            saved = store.get(session_id)
            event = request_event(saved)
            assert event["text"] == "Survives hard crash"
            assert event["request_info"]["status"] == "interrupted"
            assert event["request_info"]["usage"] == {"input_tokens": 9, "output_tokens": 3}
            assert saved["wire"] == []
        finally:
            store.db.close()
    finally:
        if process.is_alive():
            process.kill()
            process.join(3)
        receiver.close()


async def test_draft_checkpoints_are_throttled_by_size_and_flush_small_stalled_changes(runtime, monkeypatch):
    manager, session = runtime
    monkeypatch.setattr("local_agent.drafts.DRAFT_BYTES", 100)
    monkeypatch.setattr("local_agent.drafts.DRAFT_INTERVAL_SECONDS", 0.02)
    saved = []
    original = manager.store.save

    def save(value):
        saved.append(copy.deepcopy(value))
        original(value)

    monkeypatch.setattr(manager.store, "save", save)
    draft = StreamDraft(manager.store, session)
    try:
        for _ in range(10):
            draft.changed(10)
        assert len(saved) == 1
        await asyncio.sleep(0.05)
        assert len(saved) == 1
        draft.changed(1)
        await asyncio.sleep(0.05)
        assert len(saved) == 2
    finally:
        await draft.close()
    assert draft.worker.done()


async def test_draft_cleanup_does_not_suppress_unexpected_worker_errors(runtime, monkeypatch):
    manager, session = runtime
    monkeypatch.setattr("local_agent.drafts.DRAFT_INTERVAL_SECONDS", 0.01)

    def broken_save(value):
        raise TypeError("Unexpected serialization defect")

    monkeypatch.setattr(manager.store, "save", broken_save)
    draft = StreamDraft(manager.store, session)
    draft.changed()
    await asyncio.sleep(0.03)
    with pytest.raises(TypeError, match="Unexpected serialization defect"):
        await draft.close()
    assert draft.worker.done()


async def test_stalled_stream_draft_survives_restart_with_usage_and_no_executable_partial_calls(runtime, monkeypatch, tmp_path):
    manager, session = runtime
    monkeypatch.setattr("local_agent.drafts.DRAFT_INTERVAL_SECONDS", 0.02)
    entered = asyncio.Event()
    hanging_gateway(monkeypatch, entered, usage={"prompt_tokens": 25, "completion_tokens": 0},
                    tool_calls=[{"index": 0, "id": "unfinished", "function": {"name": "write_file", "arguments": '{"path":'}}])
    manager.start(session["id"], "Inspect this project")
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(50):
            saved = manager.store.get(session["id"])
            if request_event(saved).get("text") == "Partial answer":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("The stalled stream did not persist its partial answer")
        assert request_event(saved)["request_info"]["status"] == "running"
        reopened = Store(tmp_path / "sessions.db")
        try:
            AgentManager(reopened, manager.settings)
            restored = reopened.get(session["id"])
            event = request_event(restored)
            assert event["text"] == "Partial answer"
            assert event["request_info"]["status"] == "interrupted"
            assert event["request_info"]["usage"] == {"input_tokens": 25, "output_tokens": 0}
            assert not any(item.get("tool_calls") for item in restored["wire"])
        finally:
            reopened.db.close()
    finally:
        await manager.stop(session["id"])


async def test_stop_flushes_small_draft_without_waiting_for_periodic_checkpoint(runtime, monkeypatch):
    manager, session = runtime
    monkeypatch.setattr("local_agent.drafts.DRAFT_INTERVAL_SECONDS", 60)
    entered = asyncio.Event()
    hanging_gateway(monkeypatch, entered, text="Last partial words", usage={"total_tokens": 7})
    manager.start(session["id"], "Continue")
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(manager.stop(session["id"]), 1)
    saved = manager.store.get(session["id"])
    assert request_event(saved)["text"] == "Last partial words"
    assert request_event(saved)["request_info"]["status"] == "cancelled"
    assert request_event(saved)["request_info"]["usage"] == {"total_tokens": 7}
    assert not any(item.get("tool_calls") for item in saved["wire"])
    assert manager.statuses[session["id"]] == "idle"


async def test_completed_stream_final_flush_keeps_exact_text_and_does_not_duplicate_updates(runtime, monkeypatch):
    manager, session = runtime
    monkeypatch.setattr("local_agent.drafts.DRAFT_INTERVAL_SECONDS", 60)
    chunks = [
        {"choices": [{"delta": {"content": "First "}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "second"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 2}},
    ]
    body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(**kwargs,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))))
    messages = []

    async def broadcast(session_id, data):
        messages.append(copy.deepcopy(data))

    manager.broadcast = broadcast
    await manager.run_databricks(session, "Continue")
    saved = manager.store.get(session["id"])
    assert request_event(saved)["text"] == "First second"
    assert request_event(saved)["request_info"]["status"] == "completed"
    assert request_event(saved)["request_info"]["usage"] == {"input_tokens": 11, "output_tokens": 2}
    assert saved["wire"][-1] == {"role": "assistant", "content": "First second"}
    assert [item["text"] for item in messages if item["type"] == "delta"] == ["First ", "second"]
    completed = [item for item in messages if item.get("event", {}).get("request_info", {}).get("status") == "completed"]
    assert len(completed) == 1
    assert not any(task.get_coro().__qualname__ == "StreamDraft._periodic" for task in asyncio.all_tasks())


@pytest.mark.parametrize("threshold", [1, 16384])
async def test_persistence_failure_aborts_stream_and_reports_once_even_when_database_stays_unavailable(runtime, monkeypatch, threshold):
    manager, session = runtime
    monkeypatch.setattr("local_agent.drafts.DRAFT_BYTES", threshold)
    monkeypatch.setattr("local_agent.drafts.DRAFT_INTERVAL_SECONDS", 0.02)
    messages = []

    async def broadcast(session_id, data):
        messages.append(copy.deepcopy(data))

    manager.broadcast = broadcast
    original = manager.store.save

    def fail_draft(value):
        if any(event.get("text") == "Unsaved partial" for event in value["events"]):
            raise OSError("disk full")
        original(value)

    monkeypatch.setattr(manager.store, "save", fail_draft)
    hanging_gateway(monkeypatch, asyncio.Event(), text="Unsaved partial")
    manager.start(session["id"], "Continue")
    await asyncio.wait_for(manager.tasks[session["id"]], 1)
    assert manager.statuses[session["id"]] == "idle"
    assert session["id"] not in manager.live
    assert manager.pending == {}
    errors = [item["event"] for item in messages if item.get("event", {}).get("type") == "error"]
    assert len(errors) == 1
    assert "Local history could not be saved" in errors[0]["text"]
    assert "recent text may be missing" in errors[0]["text"]
    deltas = [item for item in messages if item["type"] == "delta"]
    assert len(deltas) <= 1
    updates = [item["event"] for item in messages if item.get("event", {}).get("request_info")]
    assert updates[-1]["request_info"]["status"] == "error"
