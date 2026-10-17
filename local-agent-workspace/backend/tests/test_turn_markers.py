"""How a turn's running and ended markers survive a restart, and when a page can see them.

turn_running (internal) tells restart recovery that the answer itself was cut short;
turn_ended is None while a turn runs and records when it ended, for notifications.
"""
import asyncio

import httpx
import pytest

from local_agent.api import create_app
from local_agent.client import result_record
from local_agent.config import Settings


def settings_for(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), model="test-model", env_file="")
    settings.env = {}
    return settings


@pytest.mark.parametrize("record, reason", [
    # Cut off while the model loop ran: Continue is offered.
    ({"turn_running": True}, "interrupted"),
    # Older record without the marker, with a model request still running.
    ({"inference_calls": [{"id": "c", "status": "running", "purpose": "agent"}]}, "interrupted"),
    # Cut off while naming the chat: the answer had finished, so the stale reason is replaced.
    ({"inference_calls": [{"id": "c", "status": "running", "purpose": "title"}]}, "completed"),
    ({}, "completed"),
])
async def test_a_restart_marks_only_an_unfinished_answer_as_interrupted(tmp_path, monkeypatch, record, reason):
    settings = settings_for(tmp_path, monkeypatch)
    first = create_app(settings)
    async with first.router.lifespan_context(first):
        store = first.state.manager.store
        session = store.create(settings.values)
        session.update(terminal_reason="step_limit", turn_ended=None, **record)
        store.save(session)
    second = create_app(settings)
    async with second.router.lifespan_context(second):
        recovered = second.state.manager.store.get(session["id"])
        assert recovered["terminal_reason"] == reason
        assert recovered["turn_ended"] and "turn_running" not in recovered


async def test_a_restart_gives_the_cut_short_turn_its_own_result_record(tmp_path, monkeypatch):
    """Scripts read the header's last_turn_result (client.wait_result): after a restart it is the interrupted
    turn's, counted from its user message, not the previous turn's."""
    settings = settings_for(tmp_path, monkeypatch)
    previous = {"terminal_reason": "completed", "is_error": False, "started": 100.0, "ended": 105.0}
    first = create_app(settings)
    async with first.router.lifespan_context(first):
        store = first.state.manager.store
        session = store.create(settings.values)
        info = {"model": "test-model", "status": "completed", "finish_reason": "stop"}
        session.update(terminal_reason="completed", turn_running=True, turn_ended=None, last_turn_result=previous,
                       events=[{"id": "u1", "type": "user", "text": "First", "created": 100.0, "in_wire": True},
                               {"id": "a1", "type": "assistant", "text": "One.", "created": 104.0, "request_info": info,
                                "turn_result": previous},
                               {"id": "u2", "type": "user", "text": "Second", "created": 200.0, "in_wire": True},
                               {"id": "a2", "type": "assistant", "text": "Part", "created": 202.0,
                                "request_info": {**info, "status": "running"}}],
                       wire=[{"role": "user", "content": "First"}, {"role": "assistant", "content": "One."},
                             {"role": "user", "content": "Second"}],
                       inference_calls=[{"id": f"c{index}", "purpose": "agent", "model": "test-model", "created": created,
                                         "status": "completed", "usage": {"input_tokens": tokens, "output_tokens": 10}}
                                        for index, (created, tokens) in enumerate(((101.0, 1000), (201.0, 2000)))])
        store.save(session)
    second = create_app(settings)
    async with second.router.lifespan_context(second):
        recovered = second.state.manager.store.get(session["id"])
    result = recovered["last_turn_result"]
    assert (result["terminal_reason"], result["is_error"], result["started"]) == ("interrupted", False, 200.0)
    assert result["ended"] == round(recovered["turn_ended"], 3) and recovered["terminal_reason"] == "interrupted"
    assert (result["steps"], result["usage"]["calls"], result["tokens"]) == (1, 1, 2010)
    assert result_record(session["id"], result)["exit_code"] == 3  # Not the previous turn's 0.


async def test_a_page_never_sees_a_new_turn_end_while_the_chat_is_busy(tmp_path, monkeypatch):
    """The sessions list a page polls must not show the turn's end before its idle status."""
    settings = settings_for(tmp_path, monkeypatch)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        manager = app.state.manager
        session = manager.store.create(settings.values)
        seen = []
        broadcast = manager.broadcast

        async def observe(session_id, data):
            summary = next(item for item in manager.store.list() if item["id"] == session_id)
            seen.append((data["type"], data.get("status"), manager.statuses.get(session_id), summary.get("turn_ended")))
            await broadcast(session_id, data)
        manager.broadcast = observe
        started = asyncio.Event()

        async def run_databricks(live, prompt):
            # A tool still running when the turn is stopped is closed in run()'s finally,
            # which broadcasts the update before the idle status.
            await manager.event(live, "tool", name="run_command", input={"command": "sleep 5"}, state="running", output="")
            started.set()
            await asyncio.sleep(30)
        manager.run_databricks = run_databricks
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            assert (await client.post(f"/api/sessions/{session['id']}/messages", json={"text": "Run it"})).status_code == 200
            await asyncio.wait_for(started.wait(), 5)
            await manager.stop(session["id"])
        idle = next(index for index, item in enumerate(seen) if item[:2] == ("status", "idle"))
        assert any(item[0] == "event" for item in seen[:idle])
        assert all(item[3] is None for item in seen[:idle] if item[2] != "idle")
        assert seen[idle][3] is not None
