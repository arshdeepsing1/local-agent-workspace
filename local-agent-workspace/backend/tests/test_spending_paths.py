"""Spending limit paths found in the 2026-10-15 review: a subagent's spend survives Stop, a restart,
deletion and continuing it; a stop leaves the wire answerable and keeps queued messages queued;
stop hooks do not run into a refused request; a hook-blocked message is not a finished turn;
parentless subagent chats have their own limit; an idle parent is not reloaded every step."""
import asyncio
import json
import shlex

import httpx
import pytest

from local_agent import spending
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.notify import attention_message
from local_agent.store import Store

OPUS = "databricks-claude-opus-5-5"
REAL_CLIENT = httpx.AsyncClient
USAGE = {"prompt_tokens": 10_000, "completion_tokens": 1_000}
PER_CALL = 0.857144


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(tmp_path / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Spend", title_generated=True, model=OPUS, permission_mode="bypassPermissions")
    store.save(session)
    yield AgentManager(store, settings), session
    store.db.close()


def sse(delta, finish, usage=USAGE):
    chunks = [{"choices": [{"delta": delta, "finish_reason": finish}]}, {"choices": [], "usage": usage}]
    return "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def is_child(body):
    first = next((m["content"] for m in body["messages"] if m["role"] == "user"), "")
    return isinstance(first, str) and first.startswith("Delegated task:\n")


def delegate_reply():
    return sse({"tool_calls": [{"index": 0, "id": "d1", "type": "function", "function": {
        "name": "delegate_task", "arguments": json.dumps({"task": "Map it", "agent_type": "explore"})}}]}, "tool_calls")


def tool_reply(index):
    return sse({"tool_calls": [{"index": 0, "id": f"c{index}", "type": "function",
                                "function": {"name": "list_tasks", "arguments": "{}"}}]}, "tool_calls")


async def test_a_parent_stop_keeps_the_running_subagents_spend(runtime, monkeypatch):
    manager, parent = runtime
    manager.store.save(parent)
    child_requests, blocked = [], asyncio.Event()

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "T"}, "finish_reason": "stop"}]})
        if is_child(body):
            child_requests.append(body)
            if len(child_requests) == 1:
                return httpx.Response(200, text=tool_reply(1))
            blocked.set()
            await asyncio.sleep(3600)
        return httpx.Response(200, text=delegate_reply())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    manager.start(parent["id"], "Delegate")
    await asyncio.wait_for(blocked.wait(), 10)
    live_parent = manager.live[parent["id"]]
    during = spending.chat_spend(manager, live_parent)
    assert during["spent_dbu"] == pytest.approx(2 * PER_CALL)  # parent 1 + child 1 (live)
    await manager.stop(parent["id"])
    saved = manager.store.get(parent["id"])
    after = spending.chat_spend(manager, saved)
    # Expected 2 * PER_CALL: the child's completed call was billed.
    assert after["spent_dbu"] == pytest.approx(2 * PER_CALL), after


async def test_a_continued_subagents_new_spend_reaches_the_parent(runtime, monkeypatch):
    manager, parent = runtime
    manager.store.save(parent)
    parent_requests = []

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "T"}, "finish_reason": "stop"}]})
        if is_child(body):
            return httpx.Response(200, text=sse({"content": "Mapped."}, "stop"))
        parent_requests.append(body)
        if len(parent_requests) == 1:
            return httpx.Response(200, text=delegate_reply())
        return httpx.Response(200, text=sse({"content": "Done."}, "stop"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    manager.start(parent["id"], "Delegate")
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    child_id = saved["subagents"][0]["id"]
    assert spending.chat_spend(manager, saved)["spent_dbu"] == pytest.approx(3 * PER_CALL)
    # The user continues the subagent chat: three more billed responses.
    for text in ("More", "Even more", "Again"):
        manager.start(child_id, text)
        await asyncio.wait_for(manager.tasks[child_id], 10)
    child = manager.store.get(child_id)
    assert spending.own_spend(child)[0] == pytest.approx(4 * PER_CALL)
    saved = manager.store.get(parent["id"])
    total = spending.chat_spend(manager, saved)
    # Expected: parent 2 + child 4 = 6 calls.
    assert total["spent_dbu"] == pytest.approx(6 * PER_CALL), total


async def test_recovery_after_a_restart_records_the_estimate(runtime):
    manager, parent = runtime
    child = manager.store.create({"workspace": parent["workspace"], "model": OPUS})
    child.update(parent_session_id=parent["id"], is_subagent=True, parent_event_id="e1",
                 delegation={"status": "running", "completed_tools": 0, "agent_type": "explore", "started": 1.0},
                 inference_calls=[{"id": "x", "purpose": "agent", "model": OPUS, "status": "completed",
                                   "usage": {"input_tokens": 10_000, "output_tokens": 1_000}}])
    manager.store.save(child)
    parent["subagents"] = [{"id": child["id"], "task": "Map", "agent_type": "explore", "status": "running"}]
    manager.store.save(parent)
    manager.delegates.recover()
    saved = manager.store.get(parent["id"])
    total = spending.chat_spend(manager, saved)
    assert total["subagents_dbu"] == pytest.approx(PER_CALL), total


async def test_a_subagent_deleted_while_it_runs_keeps_its_spend(runtime, monkeypatch):
    manager, parent = runtime
    manager.store.save(parent)
    child_requests, blocked = [], asyncio.Event()

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "T"}, "finish_reason": "stop"}]})
        if is_child(body):
            child_requests.append(body)
            if len(child_requests) == 1:
                return httpx.Response(200, text=tool_reply(1))
            blocked.set()
            await asyncio.sleep(3600)
        if any(m["role"] == "tool" for m in body["messages"]):
            return httpx.Response(200, text=sse({"content": "Done."}, "stop"))
        return httpx.Response(200, text=delegate_reply())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    real_broadcast = manager.broadcast

    async def page_broadcast(*args, **kwargs):  # An open page: each send yields.
        await asyncio.sleep(0)
        return await real_broadcast(*args, **kwargs)
    monkeypatch.setattr(manager, "broadcast", page_broadcast)
    manager.start(parent["id"], "Delegate")
    await asyncio.wait_for(blocked.wait(), 10)
    child_id = manager.live[parent["id"]]["subagents"][0]["id"]
    # What DELETE /api/sessions/{child} does for one member.
    await manager.stop(child_id)
    manager.delegates.forget(child_id, keep=[child_id])
    await manager.jobs.remove_session(child_id)
    manager.store.delete(child_id)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    total = spending.chat_spend(manager, saved)
    # parent: 2 responses; child: 1 billed response.
    assert total["spent_dbu"] == pytest.approx(3 * PER_CALL), total


def roles(messages):
    return [m["role"] for m in messages if m["role"] != "system"]


async def test_a_steered_message_stays_queued_when_the_limit_stops_the_turn(runtime, monkeypatch):
    manager, session = runtime
    session["spending_limit_dbu"] = 0.8  # The first response (0.857) reaches it.
    manager.store.save(session)
    requests, first, release = [], asyncio.Event(), asyncio.Event()

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "T"}, "finish_reason": "stop"}]})
        requests.append(body)
        if len(requests) == 1:
            first.set()
            await release.wait()
            return httpx.Response(200, text=tool_reply(1))
        return httpx.Response(200, text=sse({"content": "Answer."}, "stop"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    manager.start(session["id"], "Work")
    await asyncio.wait_for(first.wait(), 5)
    await manager.enqueue(session["id"], "Also check the README")
    release.set()
    await asyncio.wait_for(manager.tasks[session["id"]], 10)
    # Raise the limit and send the next message.
    await manager.change_spending_limit(manager.get(session["id"]), 0)
    manager.start(session["id"], "Next")
    await asyncio.wait_for(manager.tasks[session["id"]], 10)
    sent = roles(requests[-1]["messages"])
    assert not any(a == b == "user" for a, b in zip(sent, sent[1:])), sent


async def test_continue_while_over_the_limit_leaves_answered_prompts(runtime, monkeypatch):
    manager, session = runtime
    session["spending_limit_dbu"] = 0.8
    manager.store.save(session)
    requests = []

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "T"}, "finish_reason": "stop"}]})
        requests.append(body)
        return httpx.Response(200, text=tool_reply(len(requests)) if len(requests) == 1 else sse({"content": "Ok."}, "stop"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    for text in ("Work", "Continue from where you stopped.", "Continue from where you stopped."):
        manager.start(session["id"], text)
        await asyncio.wait_for(manager.tasks[session["id"]], 10)
        assert manager.store.get(session["id"])["terminal_reason"] == "spending_limit"
    await manager.change_spending_limit(manager.get(session["id"]), 10)
    manager.start(session["id"], "Continue from where you stopped.")
    await asyncio.wait_for(manager.tasks[session["id"]], 10)
    sent = roles(requests[-1]["messages"])
    assert not any(a == b == "user" for a, b in zip(sent, sent[1:])), sent


async def test_sibling_checks_count_a_child_that_just_ended(runtime, monkeypatch):
    manager, parent = runtime
    parent_requests = []

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "T"}, "finish_reason": "stop"}]})
        first = next((m["content"] for m in body["messages"] if m["role"] == "user"), "")
        if first.startswith("Delegated task:\nquick"):
            return httpx.Response(200, text=sse({"content": "Quick done."}, "stop"))
        if first.startswith("Delegated task:\nslow"):
            n = sum(m["role"] == "assistant" for m in body["messages"])
            return httpx.Response(200, text=sse({"tool_calls": [{"index": 0, "id": f"s{n}", "type": "function",
                                                  "function": {"name": "list_tasks", "arguments": "{}"}}]}, "tool_calls"))
        parent_requests.append(body)
        if len(parent_requests) == 1:
            calls = [{"index": i, "id": f"d{i}", "type": "function", "function": {
                "name": "delegate_task", "arguments": json.dumps({"task": task, "agent_type": "explore", "max_steps": 8})}}
                for i, task in enumerate(("quick", "slow"))]
            return httpx.Response(200, text=sse({"tool_calls": calls}, "tool_calls"))
        return httpx.Response(200, text=sse({"content": "Done."}, "stop"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    seen = []
    real_check = spending.check

    def recording(m, s):
        if s.get("is_subagent"):
            seen.append(round(spending.chat_spend(m, s)["subagents_dbu"] - spending.own_spend(s)[0], 6))
        return real_check(m, s)
    monkeypatch.setattr(spending, "check", recording)
    # Slow every await a little, as an open page's broadcasts would.
    real_broadcast = manager.broadcast

    async def slow_broadcast(session_id, message, *args, **kwargs):
        # The ended quick child's idle status (an open page's websocket send can take this long).
        item = manager.get(session_id) if session_id not in manager.live else manager.live[session_id]
        if message.get("type") == "status" and message.get("status") == "idle" and item and \
                str((item.get("events") or [{}])[0].get("text", "")).startswith("Delegated task:\nquick"):
            await asyncio.sleep(0.2)
        return await real_broadcast(session_id, message, *args, **kwargs)
    monkeypatch.setattr(manager, "broadcast", slow_broadcast)
    manager.start(parent["id"], "Delegate twice")
    await asyncio.wait_for(manager.tasks[parent["id"]], 20)
    assert all(b >= a - 1e-9 for a, b in zip(seen, seen[1:])), seen  # Spend never goes down.


@pytest.mark.parametrize("limit", [0, 50])
async def test_a_continued_subagent_does_not_reload_its_idle_parent_every_step(runtime, monkeypatch, limit):
    manager, parent = runtime
    parent["spending_limit_dbu"] = limit
    manager.store.save(parent)
    child = manager.store.create({"workspace": parent["workspace"], "model": OPUS})
    child.update(parent_session_id=parent["id"], is_subagent=True, max_steps=4, title="Child", title_generated=True,
                 permission_mode="bypassPermissions",
                 delegation={"status": "completed", "terminal_reason": "completed", "completed_tools": 0})
    manager.store.save(child)
    requests = []

    async def gateway(request):
        requests.append(1)
        return httpx.Response(200, text=tool_reply(len(requests)))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    loads = []
    original = manager.store.get

    def counting(session_id):
        if session_id == parent["id"]:
            loads.append(1)
        return original(session_id)
    monkeypatch.setattr(manager.store, "get", counting)
    checks = []
    real_check = spending.check

    def counted_check(m, s):
        checks.append(1)
        return real_check(m, s)
    monkeypatch.setattr(spending, "check", counted_check)
    manager.start(child["id"], "Keep going")
    await asyncio.wait_for(manager.tasks[child["id"]], 10)
    assert len(loads) <= 2  # Once for its own spend, once to save its index entry; not every step.
    assert len(checks) >= 2


@pytest.fixture
async def rt(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-token")
    store = Store(settings.state_dir / "hooks.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Hooks", title_generated=True, permission_mode="bypassPermissions", model=OPUS)
    store.save(session)
    manager.live[session["id"]] = session
    yield manager, session, tmp_path
    for session_id in list(manager.tasks):
        await manager.stop(session_id)
    await manager.jobs.shutdown()
    store.db.close()


def hook_sse(delta, finish, usage=USAGE):
    chunks = [{"choices": [{"delta": delta, "finish_reason": finish}]}, {"choices": [], "usage": usage}]
    return "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def final_replies(monkeypatch, requests):
    async def handler(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "T"}, "finish_reason": "stop"}]})
        requests.append(body)
        return httpx.Response(200, text=hook_sse({"content": "Done?"}, "stop"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(handler)))


def hook(hook_id, event, command):
    return {"id": hook_id, "event": event, "command": command, "enabled": True, "timeout_seconds": 10}


async def test_stop_hooks_do_not_run_into_a_request_the_limit_refuses(rt, monkeypatch):
    manager, session, tmp = rt
    session["spending_limit_dbu"] = 0.8
    requests = []
    final_replies(monkeypatch, requests)
    record = tmp / "stop.json"
    manager.extensions.update_config({"hooks": [hook("stop", "agent_stop",
        f"cat >> {shlex.quote(str(record))}; printf '%s' '{{\"decision\": \"block\", \"reason\": \"Run the tests.\"}}'")]})
    await manager.run(session, "Fix it")
    texts = [event.get("text") for event in session["events"] if event["type"] == "notice"]
    assert session["terminal_reason"] == "spending_limit"
    assert not any("A hook asked the agent to continue" in text for text in texts), texts
    assert not record.exists()  # The hook never ran: its continuation would be refused.


async def test_a_message_a_hook_blocked_is_not_a_finished_turn(rt, monkeypatch):
    manager, session, tmp = rt
    session["spending_limit_dbu"] = 0.8
    final_replies(monkeypatch, [])
    session.update(terminal_reason="spending_limit")  # The previous turn reached the limit.
    manager.extensions.update_config({"hooks": [hook("guard", "user_prompt",
        "printf '%s' '{\"decision\": \"block\", \"reason\": \"No secrets.\"}'")]})
    await manager.run(session, "my secret")
    assert session["terminal_reason"] == "hook_blocked"
    assert attention_message(session, "running", "idle", turn_ended=True) is None  # Not "Finished".


@pytest.fixture
async def api(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.env = {}
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            yield client, app.state.manager, settings


async def test_parentless_subagent_chats_set_their_own_limit(api):
    client, manager, settings = api
    parent = (await client.post("/api/sessions", json={})).json()
    child = manager.store.create(settings.values)
    child.update(is_subagent=True, parent_session_id=parent["id"], title="Child", title_generated=True,
                 terminal_reason="completed",
                 events=[{"id": "u", "type": "user", "text": "Hi", "in_wire": True},
                         {"id": "a", "type": "assistant", "text": "Hello", "request_info": {"model": OPUS, "status": "completed"}}],
                 wire=[{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}])
    manager.store.save(child)
    fork = await client.post(f"/api/sessions/{child['id']}/fork")
    assert fork.status_code == 201, fork.text
    copied = manager.store.get(fork.json()["id"])
    refused = await client.put(f"/api/sessions/{copied['id']}/spending-limit", json={"spending_limit_dbu": 1})
    deleted = await client.delete(f"/api/sessions/{parent['id']}")
    assert deleted.status_code in (200, 204), deleted.text
    refused2 = await client.put(f"/api/sessions/{child['id']}/spending-limit", json={"spending_limit_dbu": 1})
    assert refused.status_code == 200 and refused2.status_code == 200
