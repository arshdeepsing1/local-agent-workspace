"""A chat's spending limit (spending.py): a soft cap on the estimated DBU of a conversation and its
subagents, checked before each agent model request. The Settings default, the chat's own value, the
warning at 80 %, the stop, the route that changes it and the usage panel's status."""
import asyncio
import json

import httpx
import pytest

from local_agent import spending
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.store import Store

OPUS = "databricks-claude-opus-5-5"
REAL_CLIENT = httpx.AsyncClient
# 10,000 input and 1,000 output tokens on Opus 5.5: 0.57143 + 0.285714 = 0.857144 DBU per response.
USAGE = {"prompt_tokens": 10_000, "completion_tokens": 1_000}
PER_CALL = 0.857144


def test_limits_are_numbers_of_dbu_from_zero_to_the_maximum():
    assert spending.validate_limit(0) == 0.0
    assert spending.validate_limit(2) == 2.0
    assert spending.validate_limit(1.23456) == 1.235
    for value in (-0.1, spending.MAX_LIMIT_DBU + 1, True, "3", None, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="from 0 \\(off\\)"):
            spending.validate_limit(value)
    with pytest.raises(ValueError, match="at least 0.001"):
        spending.validate_limit(0.0004)  # Rounded to 0 it would mean no limit.
    assert spending.validate_limit(0.001) == 0.001


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(tmp_path / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Spend", title_generated=True, model=OPUS, permission_mode="bypassPermissions")
    yield AgentManager(store, settings), session
    store.db.close()


def sse(delta, finish, usage=USAGE):
    chunks = [{"choices": [{"delta": delta, "finish_reason": finish}]}, {"choices": [], "usage": usage}]
    return "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def tool_reply(index):
    return sse({"tool_calls": [{"index": 0, "id": f"call-{index}", "type": "function",
                                "function": {"name": "list_tasks", "arguments": "{}"}}]}, "tool_calls")


def endless_tools(monkeypatch, requests):
    """Every response asks for one more tool call, so only a limit ends the turn."""
    async def gateway(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text=tool_reply(len(requests)))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))


def notices(session):
    return [event for event in session["events"] if event["type"] == "notice"]


async def test_the_chat_warns_once_at_80_percent_and_stops_before_the_request_over_its_limit(runtime, monkeypatch):
    manager, session = runtime
    session["spending_limit_dbu"] = 2.0
    requests = []
    endless_tools(monkeypatch, requests)
    await manager.run(session, "Work until stopped")
    # 0.857 after one response, 1.714 (over 80 % of 2) after two, 2.571 after three: no fourth request.
    assert len(requests) == 3
    texts = [event["text"] for event in notices(session)]
    warned = [text for text in texts if "spending limit" in text and "did not send" not in text]
    assert warned == ["This conversation has used about 1.714 of its 2.000 DBU spending limit (an estimate from list prices)."]
    stop = notices(session)[-1]
    assert stop["terminal_reason"] == "spending_limit" and "about 2.571 of its 2.000 DBU" in stop["text"]
    assert "did not send the next model request" in stop["text"] and "Agent tools → Usage" in stop["text"]
    assert session["terminal_reason"] == "spending_limit" and session["spending_warned"] == 2.0
    status = spending.status(manager, session)
    assert status == {"limit_dbu": 2.0, "source": "chat", "spent_dbu": round(3 * PER_CALL, 6),
                      "own_dbu": round(3 * PER_CALL, 6), "subagents_dbu": 0.0, "unpriced_calls": 0, "percent": 128.6}
    # Continuing at the same limit stops again before any request; nothing is billed.
    await manager.run(session, "Continue")
    assert len(requests) == 3 and session["terminal_reason"] == "spending_limit"


async def test_the_settings_default_applies_until_the_chat_sets_its_own(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["spending_limit_dbu"] = 1.0
    requests = []
    endless_tools(monkeypatch, requests)
    await manager.run(session, "Go")
    assert len(requests) == 2 and session["terminal_reason"] == "spending_limit"  # 0.857 < 1, then 1.714.
    assert spending.limit_for(manager, session) == (1.0, "settings")
    # The chat's own 0 means no limit even with a Settings default: the step limit ends it.
    session["spending_limit_dbu"] = 0
    manager.settings.values["max_agent_steps"] = 3
    await manager.run(session, "Go on")
    assert len(requests) == 5 and session["terminal_reason"] == "step_limit"
    assert spending.status(manager, session)["percent"] is None


async def test_no_limit_by_default_and_unpriced_endpoints_count_as_unpriced(runtime, monkeypatch):
    manager, session = runtime
    session["model"] = "test-endpoint"
    session["spending_limit_dbu"] = 0.001
    requests = []
    endless_tools(monkeypatch, requests)
    manager.settings.values["max_agent_steps"] = 2
    await manager.run(session, "Go")
    assert manager.settings.values.get("spending_limit_dbu") == 0
    assert len(requests) == 2 and session["terminal_reason"] == "step_limit"  # No price data: never reached.
    assert spending.chat_spend(manager, session) == {"spent_dbu": 0.0, "own_dbu": 0.0, "subagents_dbu": 0.0,
                                                     "unpriced_calls": 2}


class Delegates:
    def __init__(self, active):
        self.active = active


class FakeManager:
    def __init__(self, sessions, live=(), active=None, default=0):
        self.sessions = sessions
        self.live = {item["id"]: item for item in live}
        self.delegates = Delegates(active or {})
        self.settings = type("S", (), {"values": {"spending_limit_dbu": default}})()

    def get(self, session_id):
        return self.sessions.get(session_id)


def calls(*amounts):
    return [{"purpose": "agent", "dbu": amount} for amount in amounts]


def test_subagents_count_for_their_parent_whose_limit_applies_to_them(monkeypatch):
    monkeypatch.setattr(spending, "call_estimated_dbu", lambda call: (call["dbu"], True))
    parent = {"id": "p", "spending_limit_dbu": 5, "inference_calls": calls(1.0),
              "subagents": [{"id": "ended", "estimated_dbu": 1.5, "unpriced_calls": 1},
                            {"id": "old"},  # Ended before the index kept estimates: not counted.
                            {"id": "running"}]}
    running = {"id": "running", "is_subagent": True, "parent_session_id": "p", "inference_calls": calls(0.25, 0.25)}
    queued = {"id": "queued", "is_subagent": True, "parent_session_id": "p", "inference_calls": calls(0.5)}
    manager = FakeManager({"p": parent}, live=[running, queued],
                          active={"p": [{"child_session_id": "running"}, {"child_session_id": "queued"}, {}]})
    spend = spending.chat_spend(manager, running)
    assert spend == {"spent_dbu": 3.5, "own_dbu": 1.0, "subagents_dbu": 2.5, "unpriced_calls": 1}
    assert spending.limit_for(manager, running) == (5.0, "chat")
    parent["spending_limit_dbu"] = 3.5
    verdict, text = spending.check(manager, running)
    assert verdict == "stop" and text.startswith("Its parent conversation has used about 3.500")
    # A subagent never warns; its parent does, from its own next request.
    parent["spending_limit_dbu"] = 4
    assert spending.check(manager, running) == (None, None)
    verdict, _ = spending.check(manager, parent)
    assert verdict == "warn" and spending.check(manager, parent) == (None, None)  # Once per limit value.
    # A subagent chat the user continued after it ended counts its live calls, not its stale entry.
    ended = {"id": "ended", "is_subagent": True, "parent_session_id": "p", "inference_calls": calls(2.0)}
    assert spending.chat_spend(manager, ended)["subagents_dbu"] == 3.0
    # A parent that cannot be read: the subagent's own limit fields are not used, the Settings default is.
    orphan = {"id": "x", "is_subagent": True, "parent_session_id": "gone", "inference_calls": calls(1.0)}
    assert spending.limit_for(FakeManager({}, default=0.5), orphan) == (0.5, "settings")


# Settings and the route

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


def settings_body(settings, **values):
    return {key: settings.values[key] for key in ("workspace", "model", "env_file")} | values


async def test_settings_keep_a_default_limit(api):
    client, _, settings = api
    assert (await client.get("/api/settings")).json()["spending_limit_dbu"] == 0
    saved = await client.put("/api/settings", json=settings_body(settings, spending_limit_dbu=12.5))
    assert saved.status_code == 200 and saved.json()["spending_limit_dbu"] == 12.5
    assert json.loads(settings.path.read_text())["spending_limit_dbu"] == 12.5
    for value in (-1, spending.MAX_LIMIT_DBU + 1, True, "3"):
        refused = await client.put("/api/settings", json=settings_body(settings, spending_limit_dbu=value))
        assert refused.status_code in (400, 422), value
    assert settings.values["spending_limit_dbu"] == 12.5
    # A hand-edited settings file is checked when the next save validates it.
    settings.values["spending_limit_dbu"] = -3
    refused = await client.put("/api/settings", json=settings_body(settings))
    assert refused.status_code == 400 and "Default spending limit" in refused.json()["detail"]


async def test_the_route_sets_clears_and_reports_the_chat_limit(api):
    client, manager, settings = api
    settings.values["spending_limit_dbu"] = 4
    session = (await client.post("/api/sessions", json={})).json()
    url = f"/api/sessions/{session['id']}/spending-limit"
    metrics = (await client.get(f"/api/sessions/{session['id']}/metrics")).json()
    assert metrics["spending"] == {"limit_dbu": 4.0, "source": "settings", "spent_dbu": 0.0, "own_dbu": 0.0,
                                   "subagents_dbu": 0.0, "unpriced_calls": 0, "percent": 0.0}
    live = manager.get(session["id"])
    live["spending_warned"] = 4.0
    changed = await client.put(url, json={"spending_limit_dbu": 1.5})
    assert changed.status_code == 200 and changed.json()["spending_limit_dbu"] == 1.5
    assert "spending_warned" not in manager.get(session["id"])  # A new limit warns again.
    assert manager.store.get(session["id"])["spending_limit_dbu"] == 1.5
    metrics = (await client.get(f"/api/sessions/{session['id']}/metrics")).json()
    assert (metrics["spending"]["limit_dbu"], metrics["spending"]["source"]) == (1.5, "chat")
    off = await client.put(url, json={"spending_limit_dbu": 0})
    assert off.json()["spending_limit_dbu"] == 0
    assert (await client.get(f"/api/sessions/{session['id']}/metrics")).json()["spending"]["limit_dbu"] is None
    cleared = await client.put(url, json={"spending_limit_dbu": None})
    assert "spending_limit_dbu" not in cleared.json()
    assert (await client.get(f"/api/sessions/{session['id']}/metrics")).json()["spending"]["source"] == "settings"
    for body in ({"spending_limit_dbu": -1}, {"spending_limit_dbu": "2"}, {"spending_limit_dbu": True}, {}, {"limit": 2}):
        assert (await client.put(url, json=body)).status_code == 422, body
    child = manager.store.create(settings.values)
    child.update(is_subagent=True, parent_session_id=session["id"])
    manager.store.save(child)
    refused = await client.put(f"/api/sessions/{child['id']}/spending-limit", json={"spending_limit_dbu": 1})
    assert refused.status_code == 400 and "parent conversation" in refused.json()["detail"]


async def test_a_change_while_a_turn_runs_applies_to_its_next_request(runtime, monkeypatch):
    manager, session = runtime
    requests, first = [], asyncio.Event()
    release = asyncio.Event()

    async def gateway(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            first.set()
            await release.wait()
        return httpx.Response(200, text=tool_reply(len(requests)))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    manager.settings.values["max_agent_steps"] = 5
    task = asyncio.create_task(manager.run(session, "Go"))
    await asyncio.wait_for(first.wait(), 5)
    await manager.change_spending_limit(session, 0.5)
    release.set()
    await asyncio.wait_for(task, 10)
    assert len(requests) == 1 and session["terminal_reason"] == "spending_limit"


async def test_an_ended_subagent_keeps_its_estimate_in_the_parent_index(runtime, monkeypatch):
    manager, parent = runtime
    manager.store.save(parent)
    parent_requests = []

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "A title"}, "finish_reason": "stop"}]})
        first = next((m["content"] for m in body["messages"] if m["role"] == "user"), "")
        if isinstance(first, str) and first.startswith("Delegated task:\n"):
            return httpx.Response(200, text=sse({"content": "Mapped."}, "stop"))
        parent_requests.append(body)
        if len(parent_requests) == 1:
            return httpx.Response(200, text=sse({"tool_calls": [{"index": 0, "id": "d1", "type": "function", "function": {
                "name": "delegate_task", "arguments": json.dumps({"task": "Map it", "agent_type": "explore"})}}]},
                "tool_calls"))
        return httpx.Response(200, text=sse({"content": "Done."}, "stop"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    manager.start(parent["id"], "Delegate")
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    entry = saved["subagents"][0]
    assert (entry["estimated_dbu"], entry["unpriced_calls"]) == (PER_CALL, 0)
    assert spending.chat_spend(manager, saved) == {"spent_dbu": round(3 * PER_CALL, 6), "own_dbu": round(2 * PER_CALL, 6),
                                                   "subagents_dbu": PER_CALL, "unpriced_calls": 0}


async def test_forks_exports_and_imports_keep_the_limit_but_not_the_spend(api, tmp_path):
    client, manager, settings = api
    source = manager.store.create(settings.values)
    source.update(title="Capped", title_generated=True, terminal_reason="completed", spending_limit_dbu=3.25,
                  spending_warned=3.25, events=[{"id": "u", "type": "user", "text": "Hi", "in_wire": True},
                                                {"id": "a", "type": "assistant", "text": "Hello",
                                                 "request_info": {"model": OPUS, "status": "completed"}}],
                  wire=[{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}])
    manager.store.save(source)
    fork = await client.post(f"/api/sessions/{source['id']}/fork")
    assert fork.status_code == 201, fork.text
    copied = manager.store.get(fork.json()["id"])
    assert copied["spending_limit_dbu"] == 3.25 and "spending_warned" not in copied
    assert spending.chat_spend(manager, copied)["spent_dbu"] == 0
    bundle = (await client.get(f"/api/sessions/{source['id']}/export")).json()
    assert bundle["sessions"][0]["spending_limit_dbu"] == 3.25
    imported = await client.post("/api/sessions/import", params={"workspace": settings.values["workspace"]}, json=bundle)
    assert imported.status_code == 201, imported.text
    assert manager.store.get(imported.json()["session"]["id"])["spending_limit_dbu"] == 3.25
    bundle["sessions"][0]["spending_limit_dbu"] = -1
    refused = await client.post("/api/sessions/import", params={"workspace": settings.values["workspace"]}, json=bundle)
    assert refused.status_code == 400
