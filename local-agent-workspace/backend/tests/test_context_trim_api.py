"""Trimming one item of the live context by hand (Story 4, decision 4d): POST /api/sessions/{id}/context/trim and
GET /api/sessions/{id}/context/trim-preview. The mark is the one an automatic pass writes, with by "user"."""
import json
import math

import httpx
import pytest

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.context import context_messages, estimate_scale, estimate_text_tokens
from test_context_trim import WINDOW, gateway, load, long_history


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", compaction_handoffs=False, context_window=WINDOW,
                           max_output_tokens=4_096, context_trim=False)
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    app = create_app(settings)
    manager = app.state.manager
    session = manager.store.create(settings.values)
    session.update(title="Trim by hand", title_generated=True, permission_mode="bypassPermissions")
    manager.store.save(session)
    yield app, manager, session
    manager.store.db.close()


async def client_for(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        token = (await client.get("/api/bootstrap")).json()["token"]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                             headers={"X-Local-Token": token})


@pytest.fixture
async def chat(runtime, monkeypatch):
    """(client, manager, session id, requests): a chat of eight turns, one request made (trim off), idle."""
    app, manager, session = runtime
    client = await client_for(app)  # Before the fake gateway replaces httpx.AsyncClient.
    requests = []
    gateway(monkeypatch, requests)
    await manager.run(load(session, long_history(8)), "Turn 9: next.")
    yield client, manager, session["id"], requests
    await client.aclose()


def stored(manager, session_id):
    return manager.store.get(session_id)


def old_big_command(manager, session_id):
    """The event of the first turn's command result: old, and large."""
    return next(event for event in stored(manager, session_id)["events"]
                if event.get("name") == "run_command" and "t1" in event["output"])


def trim(client, session_id, event_id, mode="shortened"):
    return client.post(f"/api/sessions/{session_id}/context/trim", json={"event_id": event_id, "mode": mode})


def preview(client, session_id, event_id, mode="shortened"):
    return client.get(f"/api/sessions/{session_id}/context/trim-preview", params={"event_id": event_id, "mode": mode})


async def test_a_trim_is_recorded_and_returns_the_new_context_info_and_the_cache_cost(chat):
    client, manager, session_id, requests = chat
    event = old_big_command(manager, session_id)
    before = stored(manager, session_id)["context_info"]
    reply = await trim(client, session_id, event["id"])
    assert reply.status_code == 200, reply.text
    body = reply.json()
    info = body["context_info"]
    assert type(body["cache_cost_tokens"]) is int and body["cache_cost_tokens"] == 0  # No prompt caching on this endpoint.
    assert info["trimmed"]["events"] == 1 and info["trimmed"]["tokens"] > 0
    assert info["used_tokens"] < before["used_tokens"] and info["is_estimate"] is True
    assert info["percent_used"] == round(100 * info["used_tokens"] / info["limit_tokens"])
    assert all(item["tokens"] < 2_000 for item in info["largest_items"] if item["event_id"] == event["id"])
    saved = stored(manager, session_id)
    mark = next(e for e in saved["events"] if e["id"] == event["id"])["context_trim"]
    assert set(mark) == {"mode", "by", "at", "freed_tokens", "content"} and (mark["mode"], mark["by"]) == ("shortened", "user")
    # L4: the meter says it is a trim, not a compaction preview.
    assert info["after_trim"] is True and not info.get("prepared_for_next_turn")
    assert info["is_estimate"] is True and info["last_request_cached"] == 0
    assert saved["context_info"]["trimmed"] == info["trimmed"]  # Saved at once.
    assert next(e for e in saved["events"] if e["id"] == event["id"])["output"] == event["output"]


async def test_the_next_request_sends_the_notice_and_the_same_bytes_after_it(chat, monkeypatch):
    client, manager, session_id, requests = chat
    event = old_big_command(manager, session_id)
    assert (await trim(client, session_id, event["id"], "dropped")).status_code == 200
    session = stored(manager, session_id)
    await manager.run(session, "Turn 10: again.")
    await manager.run(session, "Turn 11: and again.")
    index = next(i for i, m in enumerate(session["wire"]) if m.get("tool_call_id") == event["call_id"]) + 1
    assert "Cleared by the app to save context" in requests[1][index]["content"]
    assert "get_job_output" in requests[1][index]["content"]
    assert json.dumps(requests[1][index]) == json.dumps(requests[2][index])
    assert requests[0][index]["content"] == event["output"]  # The request before the trim carried it whole.


async def test_the_preview_changes_nothing_and_reports_what_the_trim_would_free(chat):
    client, manager, session_id, _ = chat
    event = old_big_command(manager, session_id)
    before = json.dumps(stored(manager, session_id), sort_keys=True, default=str)
    freed = {}
    for mode in ("shortened", "dropped"):
        reply = await preview(client, session_id, event["id"], mode)
        assert reply.status_code == 200 and set(reply.json()) == {"freed_tokens", "cache_cost_tokens"}
        freed[mode] = reply.json()["freed_tokens"]
    assert 0 < freed["shortened"] < freed["dropped"]
    assert json.dumps(stored(manager, session_id), sort_keys=True, default=str) == before
    # What the trim then frees is what the preview said (the meter's estimate drops by it).
    used = stored(manager, session_id)["context_info"]["next_estimate"]["tokens"]  # The next request's estimate.
    reply = await trim(client, session_id, event["id"], "shortened")
    assert reply.json()["context_info"]["estimated_tokens"] == used - freed["shortened"]
    assert reply.json()["context_info"]["next_estimate"]["tokens"] == used - freed["shortened"]


async def test_the_cache_cost_is_the_tokens_after_the_trimmed_message_and_zero_without_caching(chat, monkeypatch):
    client, manager, session_id, _ = chat
    event = old_big_command(manager, session_id)
    session = stored(manager, session_id)
    position = next(i for i, m in enumerate(session["wire"]) if m.get("tool_call_id") == event["call_id"])
    scale = float(estimate_scale(session.get("inference_calls"), session["model"]))
    # From the trimmed result on: its own new text and everything after it (a lower bound: the provider's cached
    # prefix may break earlier).
    text = manager.trim_choice(session, event["id"], "shortened")[2]
    after = context_messages(session["wire"], session.get("context_state"), {position: text})[position:]
    expected = math.ceil(sum(estimate_text_tokens(json.dumps(message, ensure_ascii=False)) for message in after) * scale)
    assert expected > 10_000 and "Shortened by the app" in after[0]["content"]
    for level, cost in (("full", expected), ("messages", expected), ("off", 0), (None, 0)):
        monkeypatch.setattr(manager, "cache_level", lambda chat_session, level=level: level)
        reply = await preview(client, session_id, event["id"])
        assert reply.json()["cache_cost_tokens"] == cost, level
    monkeypatch.setattr(manager, "cache_level", lambda chat_session: "full")
    assert (await trim(client, session_id, event["id"])).json()["cache_cost_tokens"] == expected
    # A result late in the chat leaves less to re-read than an early one.
    later = next(e for e in stored(manager, session_id)["events"] if e.get("name") == "run_command" and "t4" in e["output"])
    assert 0 < (await preview(client, session_id, later["id"])).json()["cache_cost_tokens"] < expected


async def test_a_drop_after_a_shorten_takes_effect_and_nothing_trims_it_again(chat):
    client, manager, session_id, requests = chat
    event = old_big_command(manager, session_id)
    first = await trim(client, session_id, event["id"], "shortened")
    mid = stored(manager, session_id)
    wire_after_shorten = next(e for e in mid["events"] if e["id"] == event["id"])["context_trim"]["content"]
    assert (await trim(client, session_id, event["id"], "shortened")).status_code == 422  # Already shortened.
    second = await trim(client, session_id, event["id"], "dropped")
    assert first.status_code == second.status_code == 200
    saved = stored(manager, session_id)
    mark = next(e for e in saved["events"] if e["id"] == event["id"])["context_trim"]
    assert mark["mode"] == "dropped" and mark["by"] == "user" and mark["freed_tokens"] >= first.json()["context_info"]["trimmed"]["tokens"]
    dropped = mark["content"]
    assert dropped != wire_after_shorten and len(dropped) < len(wire_after_shorten) and "Cleared by the app" in dropped
    assert second.json()["context_info"]["trimmed"]["events"] == 1  # One event, counted once.
    again = await trim(client, session_id, event["id"], "dropped")
    assert again.status_code == 422 and "already dropped" in again.json()["detail"]
    assert all(item["can_trim"] is False for item in second.json()["context_info"]["largest_items"]
               if item["event_id"] == event["id"])  # Dropped: nothing more to take.


async def test_the_mark_survives_a_reload_of_the_store(chat):
    client, manager, session_id, _ = chat
    event = old_big_command(manager, session_id)
    assert (await trim(client, session_id, event["id"], "dropped")).status_code == 200
    manager.live.pop(session_id, None)
    again = stored(manager, session_id)
    assert next(e for e in again["events"] if e["id"] == event["id"])["context_trim"]["mode"] == "dropped"
    message = next(m for m in again["wire"] if m.get("tool_call_id") == event["call_id"])
    assert message["content"] == event["output"] and "context_trim" not in message


async def test_a_busy_chat_answers_409_and_nothing_changes(chat):
    client, manager, session_id, _ = chat
    event = old_big_command(manager, session_id)
    manager.statuses[session_id] = "running"
    try:
        reply = await trim(client, session_id, event["id"])
    finally:
        manager.statuses[session_id] = "idle"
    assert reply.status_code == 409 and "trimming context" in reply.json()["detail"]["message"]
    assert "context_trim" not in next(e for e in stored(manager, session_id)["events"] if e["id"] == event["id"])


async def test_422_for_a_result_that_cannot_be_trimmed_and_for_a_bad_mode(chat):
    client, manager, session_id, _ = chat
    saved = stored(manager, session_id)
    recent = [e for e in saved["events"] if e.get("name") == "run_command"][-1]
    user = next(e for e in saved["events"] if e["type"] == "user")
    reply = await trim(client, session_id, recent["id"])
    assert reply.status_code == 422 and "latest three turns" in reply.json()["detail"]
    assert (await trim(client, session_id, user["id"])).status_code == 422  # Not a tool result.
    assert (await trim(client, session_id, old_big_command(manager, session_id)["id"], "erase")).status_code == 422
    assert (await preview(client, session_id, recent["id"])).status_code == 422
    assert not [e for e in stored(manager, session_id)["events"] if "context_trim" in e]


async def test_404_for_an_unknown_session_or_event(chat):
    client, manager, session_id, _ = chat
    event = old_big_command(manager, session_id)
    assert (await trim(client, session_id, "no-such-event")).status_code == 404
    assert (await preview(client, session_id, "no-such-event")).status_code == 404
    assert (await trim(client, "no-such-session", event["id"])).status_code == 404
    assert (await preview(client, "no-such-session", event["id"])).status_code == 404


# --- H2 and L2 through the route -------------------------------------------------------------------------------------------

from test_context_trim import special_results  # noqa: E402


@pytest.fixture
async def special_chat(runtime, monkeypatch):
    """(client, manager, session id, ids): old results of the tools the app never trims, one request made."""
    app, manager, session = runtime
    client = await client_for(app)
    ids = {}
    gateway(monkeypatch, [])
    await manager.run(load(session, long_history(5, first=lambda h: ids.update(special_results(h)))), "Turn 6: next.")
    yield client, manager, session["id"], ids
    await client.aclose()


async def test_the_route_refuses_what_the_app_never_trims_and_the_page_shows_no_button_for_it(special_chat):
    client, manager, session_id, ids = special_chat
    assert {"ask_user", "submit_plan", "submit_report", "remember", "create_task", "update_task", "list_tasks", "get_task",
            "use_skill", "list_skills", "access_directory", "delegate_task", "declined", "edited"} <= set(ids)
    before = json.dumps(stored(manager, session_id)["events"], sort_keys=True, default=str)
    for name, event_id in ids.items():
        for mode in ("shortened", "dropped"):
            reply = await trim(client, session_id, event_id, mode)
            assert reply.status_code == 422 and "never trims" in reply.json()["detail"], (name, mode, reply.text)
            assert (await preview(client, session_id, event_id, mode)).status_code == 422
    assert json.dumps(stored(manager, session_id)["events"], sort_keys=True, default=str) == before
    items = stored(manager, session_id)["context_info"]["largest_items"]
    assert items and all(item["can_trim"] is False for item in items if item["event_id"] in ids.values())


async def test_a_background_subagents_report_can_be_shortened_not_dropped_through_the_route(runtime, monkeypatch):
    app, manager, session = runtime
    client = await client_for(app)
    report = json.dumps({"subagent_id": "a1b2c3d4-0000-4000-8000-000000000001", "status": "completed",
                         "result": {"summary": "Findings. " * 300}})
    ids = {}
    gateway(monkeypatch, [])
    history = long_history(7, first=lambda h: ids.update(r=h.tool("get_subagent_result", {"subagent_id": "a1b2c3d4"}, report)))
    await manager.run(load(session, history), "Turn 8: next.")
    try:
        dropped = await trim(client, session["id"], ids["r"], "dropped")
        assert dropped.status_code == 422 and "shortened, not dropped" in dropped.json()["detail"]
        shortened = await trim(client, session["id"], ids["r"], "shortened")
        assert shortened.status_code == 200
        mark = next(e for e in stored(manager, session["id"])["events"] if e["id"] == ids["r"])["context_trim"]
        assert mark["mode"] == "shortened" and "a1b2c3d4-0000-4000-8000-000000000001" in mark["content"]
        assert "get_subagent_result" in mark["content"]
    finally:
        await client.aclose()


async def test_shortening_a_small_result_records_dropped_and_a_later_drop_is_refused_as_already_dropped(chat):
    client, manager, session_id, _ = chat
    small = next(e for e in stored(manager, session_id)["events"] if e.get("name") == "read_file" and "small_1" in e["input"]["path"])
    reply = await trim(client, session_id, small["id"], "shortened")
    assert reply.status_code == 200
    mark = next(e for e in stored(manager, session_id)["events"] if e["id"] == small["id"])["context_trim"]
    assert mark["mode"] == "dropped" and mark["content"].startswith("[Cleared by the app") and mark["freed_tokens"] > 0
    again = await trim(client, session_id, small["id"], "dropped")
    assert again.status_code == 422 and "already dropped" in again.json()["detail"]


async def test_the_marks_a_compaction_summarized_away_are_not_counted(chat):
    client, manager, session_id, _ = chat
    first, second = [e for e in stored(manager, session_id)["events"] if e.get("name") == "run_command"][:2]
    for event in (first, second):
        assert (await trim(client, session_id, event["id"], "dropped")).status_code == 200
    session = stored(manager, session_id)
    history = [dict(m) for m in session["wire"]]
    position = next(i for i, m in enumerate(history) if m.get("tool_call_id") == second["call_id"])
    both = manager.trim_info(session, history, session["context_state"])["trimmed"]
    summarized = manager.trim_info(session, history, {**session["context_state"], "through": position})["trimmed"]
    assert both["events"] == 2 and summarized["events"] == 1 and 0 < summarized["tokens"] < both["tokens"]
