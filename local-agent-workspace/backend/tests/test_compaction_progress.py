"""Compaction progress (M5i): before each summary or condensing request the chat gets its phase, request number,
start times and trigger, in a 'compaction' message and in its snapshot while it compacts; GET /context returns
the meter without building a request."""
import json

import httpx
import pytest

from local_agent.agents import AgentManager, public_session
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.context import SUMMARY_SECTIONS, prepare_context
from local_agent.store import Store

REAL_CLIENT = httpx.AsyncClient
HANDOFF = "\n\n".join(f"## {name}\n" + ("Detail line about the work. " * 700 if index == 0 else "None.")
                      for index, name in enumerate(SUMMARY_SECTIONS))
SHORT = "\n\n".join(f"## {name}\nShort." for name in SUMMARY_SECTIONS)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", compaction_handoffs=True, context_window=65_536)
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Progress", title_generated=True, permission_mode="bypassPermissions")
    store.save(session)
    yield AgentManager(store, settings), session
    store.db.close()


def gateway(monkeypatch, manager, session, seen):
    """Agent requests answer "Done."; a summary request a long handoff, a condensing request a short summary. seen
    gets the chat's compaction_view at each compaction request."""
    async def handle(request):
        body = json.loads(request.content)
        if body.get("stream") is True:
            chunk = {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}]}
            return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        seen.append(manager.compaction_view(session["id"]))
        condense = "Summary to shorten:\n" in body["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": SHORT if condense else HANDOFF},
                                                      "finish_reason": "stop"}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(handle)))


def recorded(monkeypatch, manager):
    messages = []
    broadcast = manager.broadcast

    async def record(session_id, data):
        messages.append(data)
        await broadcast(session_id, data)
    monkeypatch.setattr(manager, "broadcast", record)
    return messages


async def test_a_handoff_then_a_condense_request_are_numbered_in_order(runtime, monkeypatch):
    manager, session = runtime
    seen = []
    gateway(monkeypatch, manager, session, seen)
    await manager.run(session, "First " * 50)
    await manager.run(session, "Second")
    messages = recorded(monkeypatch, manager)
    await manager.manual_compact(manager.get(session["id"]))
    progress = [data["progress"] for data in messages if data["type"] == "compaction"]
    assert [(item["phase"], item["request"], item["trigger"]) for item in progress] == [
        ("handoff", 1, "manual"), ("condense", 2, "manual")]
    assert progress[0]["started"] == progress[1]["started"] <= progress[0]["request_started"] <= progress[1]["request_started"]
    # Each request's progress was the chat's snapshot while it ran.
    assert seen == progress
    statuses = [data["status"] for data in messages if data["type"] == "status"]
    assert statuses[0] == "compacting" and statuses[-1] == "idle"
    # It ends with the compaction: a later snapshot carries none.
    assert manager.compactions == {} and manager.compaction_view(session["id"]) is None
    assert "compaction_progress" not in public_session(manager.get(session["id"]), "idle",
                                                        manager.compaction_view(session["id"]))
    assert "Detailed handoff" not in json.dumps(progress)  # Phases and numbers only, never text.


async def test_an_automatic_compaction_says_why_it_runs(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values.update(compaction_handoffs=False, auto_compact_percent=50)
    seen = []
    gateway(monkeypatch, manager, session, seen)
    for turn in range(12):
        await manager.run(session, f"Turn {turn}: " + "words of the conversation " * 400)
        if seen:
            break
    assert seen and seen[0]["trigger"] == "auto" and seen[0]["phase"] == "summary" and seen[0]["request"] == 1
    assert public_session(session, "compacting", seen[0])["compaction_progress"] == seen[0]
    assert manager.compactions == {}  # The turn's next status ("running") ended it.


async def test_prepare_context_names_the_trigger_once_the_cut_is_chosen():
    system = {"role": "system", "content": "s"}
    events = []

    async def summarize(previous, chunk, limit):
        events.append("summary")
        return "## Goal and current status\nWork."
    turns = []
    for number in range(6):
        turns += [{"role": "user", "content": f"Turn {number} " + "x" * 6_000}, {"role": "assistant", "content": "ok"}]
    turns.append({"role": "user", "content": "Latest"})
    await prepare_context(turns, {}, system, [], 16_384, summarize, reply_reserve=2_048,
                          on_compaction=lambda trigger: events.append(trigger))
    assert events[:2] == ["auto", "summary"]
    events.clear()
    await prepare_context(turns, {}, system, [], 65_536, summarize, reply_reserve=2_048, force_compact=True,
                          on_compaction=lambda trigger: events.append(trigger))
    assert events[:2] == ["manual", "summary"]
    events.clear()
    # One turn that outgrew the budget by itself: its earlier tool exchanges are summarized.
    wire = [{"role": "user", "content": "Read everything"}]
    for number in range(12):
        wire += [{"role": "assistant", "content": None, "tool_calls": [{"id": f"c{number}", "type": "function",
                  "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{number}.txt"})}}]},
                 {"role": "tool", "tool_call_id": f"c{number}", "content": "y" * 12_000}]
    await prepare_context(wire, {}, system, [], 32_768, summarize, reply_reserve=4_096,
                          on_compaction=lambda trigger: events.append(trigger))
    assert events[:2] == ["in_turn", "summary"]
    # Nothing to compact: never called.
    events.clear()
    await prepare_context([{"role": "user", "content": "Hi"}], {}, system, [], 65_536, summarize,
                          on_compaction=lambda trigger: events.append(trigger))
    assert events == []


async def test_the_context_route_returns_the_stored_meter(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            session_id = (await client.post("/api/sessions")).json()["id"]
            assert (await client.get(f"/api/sessions/{session_id}/context")).json() == {
                "status": "idle", "context_info": None}
            manager = app.state.manager
            session = manager.get(session_id)
            info = {"estimated_tokens": 1_200, "input_budget": 100_000, "breakdown": {"tool_definitions": 900},
                    "tool_groups": {"Built-in tools": 700, "MCP docs": 200}}
            session["context_info"] = info
            manager.store.save(session)
            requests = []
            monkeypatch.setattr(manager, "context_inputs", lambda *args, **kwargs: requests.append(args))
            body = (await client.get(f"/api/sessions/{session_id}/context")).json()
            assert body == {"status": "idle", "context_info": info} and requests == []  # Nothing was built.
            manager.statuses[session_id] = "compacting"
            manager.compactions[session_id] = {"trigger": "auto", "started": 1.0, "request": 1, "phase": "summary",
                                               "request_started": 2.0}
            body = (await client.get(f"/api/sessions/{session_id}/context")).json()
            assert body["status"] == "compacting" and body["compaction_progress"]["phase"] == "summary"
            assert (await client.get(f"/api/sessions/{session_id}")).json()["compaction_progress"]["request"] == 1
            manager.statuses[session_id] = "idle"
            assert "compaction_progress" not in (await client.get(f"/api/sessions/{session_id}")).json()
            assert (await client.get("/api/sessions/missing/context")).status_code == 404
