import asyncio
import copy
import json

import httpx
import pytest

from local_agent.agents import AgentManager, public_session
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.context import (
    DEFAULT_CONTEXT_WINDOW, MIN_CONTEXT_WINDOW, SUMMARY_MAX_TOKENS, build_summary_messages, estimate_tokens,
    handoff_output_tokens, prepare_context,
)
from local_agent.instructions import load_project_instructions
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["wire"] = [
        {"role": "user", "content": "Earlier decision: preserve the migration."},
        {"role": "assistant", "content": "Migration complete."},
        {"role": "user", "content": "Read current status."},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "read-1", "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"status.txt"}'}}]},
        {"role": "tool", "tool_call_id": "read-1", "content": "Verified status."},
        {"role": "assistant", "content": "Status confirmed."},
        {"role": "user", "content": "What is next?"},
        {"role": "assistant", "content": "Run tests."},
    ]
    session["events"] = [{"id": "old", "type": "user", "text": "Existing display history"}]
    session["context_state"] = {"summary": "Prior decisions", "through": 0, "compactions": 2,
        "tool_definitions": [{"type": "function", "function": {"name": "mcp__test", "parameters": {"type": "object"}}}]}
    session["context_info"] = {"existing": "kept until success"}
    store.save(session)
    yield manager, session, project
    store.db.close()


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(gateway)))


@pytest.mark.parametrize("saved_definitions", [True, False])
async def test_manual_compaction_preserves_archive_recent_exchanges_and_never_runs_tools(runtime, monkeypatch, saved_definitions):
    manager, session, project = runtime
    (project / "AGENTS.md").write_text("Keep migrations compatible.")
    if not saved_definitions:
        session["context_state"].pop("tool_definitions")
    manager.store.save(session)
    original = copy.deepcopy(session)
    requests, updates = [], []

    async def gateway(request):
        payload = json.loads(request.content)
        requests.append(payload)
        call = manager.store.get(session["id"])["inference_calls"][-1]
        assert call["purpose"] == "compaction" and call["status"] == "running"
        assert payload["stream"] is False and "tools" not in payload
        # Compaction handoffs are on by default: the request asks for a detailed handoff.
        assert payload["max_tokens"] == handoff_output_tokens(DEFAULT_CONTEXT_WINDOW)
        assert payload["messages"][0]["content"].startswith("Write a detailed handoff document")
        assert "Preserve migration decisions" in payload["messages"][1]["content"]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "Migration completed and must remain compatible."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 16},
        })

    def forbidden(*args, **kwargs):
        pytest.fail("Manual compaction must not execute tools or start MCP servers")

    async def broadcast(sid, data):
        updates.append(copy.deepcopy(data))

    manager.execute_tool = forbidden
    manager.extensions.turn = forbidden
    manager.broadcast = broadcast
    mock_gateway(monkeypatch, gateway)
    manager.start_compaction(session["id"], "Preserve migration decisions")
    assert manager.statuses[session["id"]] == "compacting"
    with pytest.raises(ValueError, match="already running"):
        manager.start(session["id"], "New request")
    with pytest.raises(ValueError, match="Stop the current response"):
        manager.start_compaction(session["id"])
    await manager.tasks[session["id"]]
    saved = manager.store.get(session["id"])
    assert len(requests) == 1
    assert saved["wire"] == original["wire"]
    assert saved["events"][:1] == original["events"]
    assert saved["context_state"]["through"] == 2
    assert saved["context_state"]["compactions"] == 3
    assert saved["context_info"]["prepared_for_next_turn"] is True
    handoffs = list((project / "handoffs" / "auto").glob("*-compaction-3.md"))
    assert len(handoffs) == 1 and "Migration completed and must remain compatible." in handoffs[0].read_text()
    assert saved["context_state"]["handoff_files"] == [
        {"path": f"handoffs/auto/{handoffs[0].name}", "compaction": 3}]
    assert any("Saved a detailed handoff" in event.get("text", "") for event in saved["events"])
    assert [(call["purpose"], call["status"], call["usage"])
            for call in saved["inference_calls"]] == [
                ("compaction", "completed", {"input_tokens": 120, "output_tokens": 16})]
    assert saved["context_info"]["instruction_sources"][0]["scope"] == "."
    assert (bool(saved["context_info"]["warnings"])) is not saved_definitions
    assert any(update.get("type") == "context" for update in updates)
    assert manager.statuses[session["id"]] == "idle"
    assert "context_state" not in public_session(saved)
    restarted = AgentManager(manager.store, manager.settings)
    assert restarted.get(session["id"])["context_state"] == saved["context_state"]


@pytest.mark.parametrize("failure", ["http", "empty", "length", "cancel"])
async def test_manual_failure_or_stop_keeps_previous_summary_and_archive(runtime, monkeypatch, failure):
    manager, session, _ = runtime
    # A cut-off summary fails compaction; a cut-off handoff is kept (see test_compaction_handoffs).
    manager.settings.values["compaction_handoffs"] = failure != "length"
    original = copy.deepcopy(session)
    entered = asyncio.Event()

    async def gateway(request):
        entered.set()
        if failure == "cancel":
            await asyncio.Event().wait()
        if failure == "http":
            return httpx.Response(500)
        return httpx.Response(200, json={"choices": [{"message": {"content": "" if failure == "empty" else "Partial"},
            "finish_reason": "length" if failure == "length" else "stop"}]})

    mock_gateway(monkeypatch, gateway)
    manager.start_compaction(session["id"])
    await asyncio.wait_for(entered.wait(), 2)
    if failure == "cancel":
        await manager.stop(session["id"])
    else:
        await manager.tasks[session["id"]]
    saved = manager.store.get(session["id"])
    for key in ("wire", "context_state", "context_info"):
        assert saved[key] == original[key]
    assert saved["events"][-1]["type"] == ("notice" if failure == "cancel" else "error")
    if failure == "http":
        assert "HTTP 500" in saved["events"][-1]["text"]
    elif failure == "length":
        assert "finish reason: length" in saved["events"][-1]["text"]
    assert manager.statuses[session["id"]] == "idle"


async def test_manual_compaction_retries_pre_admission_rate_limit_without_losing_state(runtime, monkeypatch):
    manager, session, _ = runtime
    requests, delays = [], []

    async def gateway(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={
                "error_code": "REQUEST_LIMIT_EXCEEDED",
                "message": "Exceeded workspace input tokens per minute rate limit",
            })
        return httpx.Response(200, json={"choices": [{
            "message": {"content": "Earlier work summarized."}, "finish_reason": "stop",
        }]})

    async def sleep(delay):
        delays.append(delay)

    mock_gateway(monkeypatch, gateway)
    monkeypatch.setattr("local_agent.agents.asyncio.sleep", sleep)
    manager.start_compaction(session["id"])
    await manager.tasks[session["id"]]

    saved = manager.store.get(session["id"])
    assert len(requests) == 2 and delays == [0]
    assert saved["context_state"]["compactions"] == 3
    assert any(event["type"] == "notice" and "rate limited context compaction" in event["text"]
               for event in saved["events"])


async def test_manual_compaction_can_be_cancelled_before_it_starts(runtime):
    manager, session, _ = runtime
    original = copy.deepcopy(session)
    manager.start_compaction(session["id"])
    await manager.stop(session["id"])
    saved = manager.store.get(session["id"])
    for key in ("wire", "events", "context_state", "context_info"):
        assert saved[key] == original[key]
    assert manager.statuses[session["id"]] == "idle"


async def test_manual_compaction_save_failure_does_not_publish_or_later_persist_new_state(runtime, monkeypatch):
    manager, session, _ = runtime
    original = copy.deepcopy(session)
    published = []

    async def gateway(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "New summary"}, "finish_reason": "stop"}]})

    save = manager.store.save
    failed = False

    def fail_first_compaction_save(candidate):
        nonlocal failed
        if not failed and candidate.get("context_info", {}).get("prepared_for_next_turn"):
            failed = True
            raise OSError("Cannot save compacted context")
        save(candidate)

    async def broadcast(sid, data):
        published.append(data)

    manager.store.save = fail_first_compaction_save
    manager.broadcast = broadcast
    mock_gateway(monkeypatch, gateway)
    manager.start_compaction(session["id"])
    await manager.tasks[session["id"]]
    saved = manager.store.get(session["id"])
    assert failed
    for key in ("wire", "context_state", "context_info"):
        assert saved[key] == original[key]
    assert not any(update.get("type") == "context" for update in published)
    assert saved["events"][-1]["type"] == "error"


def test_manual_compaction_rejects_no_earlier_turn_and_busy_sessions_before_inference(runtime):
    manager, session, _ = runtime
    session["context_state"]["through"] = 6
    manager.store.save(session)
    with pytest.raises(ValueError, match="No earlier turns"):
        manager.start_compaction(session["id"])
    manager.statuses[session["id"]] = "awaiting_approval"
    with pytest.raises(ValueError, match="Stop the current response"):
        manager.start_compaction(session["id"])
    assert session["id"] not in manager.tasks


async def test_preservation_note_is_counted_in_each_bounded_summary_request():
    note = "Preserve decisions. " * 50
    wire = [{"role": "user", "content": "界" * 7000}, {"role": "assistant", "content": "Done"},
            {"role": "user", "content": "Latest request"}]
    calls = []

    async def summarize(previous, chunk, limit_bytes):
        request = build_summary_messages(previous, chunk, note, limit_bytes)
        assert estimate_tokens(request) <= MIN_CONTEXT_WINDOW - SUMMARY_MAX_TOKENS - 2048
        calls.append(chunk)
        return "Earlier work preserved."

    await prepare_context(wire, {}, {"role": "system", "content": "Rules"}, [],
                          MIN_CONTEXT_WINDOW, summarize, force_compact=True, preservation_note=note)
    assert len(calls) > 1
    assert "".join(calls) == json.dumps(wire[:2], ensure_ascii=False)


def test_instruction_sources_report_scope_and_only_loaded_costs(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Root rules.")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "AGENTS.md").write_text("x" * 17000)
    (tmp_path / "nested" / "CLAUDE.md").write_text("界" * 20)
    guidance = load_project_instructions(WorkspaceTools(str(tmp_path)), ["nested"])
    root, omitted, nested = guidance["sources"]
    assert root["path"] == "AGENTS.md" and root["scope"] == "."
    assert root["estimated_tokens"] > 0
    assert omitted["scope"] == "nested" and omitted["status"] == "omitted"
    assert omitted["estimated_tokens"] == 0 and "Omitted whole file" in omitted["reason"]
    assert nested["path"] == "nested/CLAUDE.md" and nested["scope"] == "nested"
    assert nested["estimated_tokens"] > 0
    assert sum(source["estimated_tokens"] for source in guidance["sources"]) < estimate_tokens([
        {"role": "system", "content": guidance["text"]}])


async def test_compaction_endpoint_auth_validation_and_busy_guard(runtime, monkeypatch):
    manager, _, _ = runtime
    app = create_app(manager.settings)
    session = app.state.manager.store.create(manager.settings.values)
    started = []
    monkeypatch.setattr(app.state.manager, "start_compaction", lambda sid, note: started.append((sid, note)))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            path = f"/api/sessions/{session['id']}/compact"
            assert (await client.post(path, json={})).status_code == 403
            token = (await client.get("/api/bootstrap")).json()["token"]
            headers = {"X-Local-Token": token}
            app.state.manager.statuses[session["id"]] = "running"
            assert (await client.post(path, headers=headers, json={})).status_code == 409
            app.state.manager.statuses[session["id"]] = "idle"
            for note in (123, "x" * 1001):
                assert (await client.post(path, headers=headers, json={"preservation_note": note})).status_code == 422
            assert (await client.post(path, headers=headers, json={"preservation_note": "Keep decisions"})).status_code == 202
            assert started == [(session["id"], "Keep decisions")]
    finally:
        app.state.manager.store.db.close()
