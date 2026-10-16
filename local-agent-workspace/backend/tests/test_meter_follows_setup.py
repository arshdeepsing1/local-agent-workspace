"""The context meter follows a model change or a Settings save before the next request: the budgets,
the auto-compact threshold and the next-request estimate describe the request the chat will send,
while the last request's report stays as it was (so the meter shows it as an earlier request)."""
import asyncio
import json
import math

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.context import estimate_scale

OPUS = "databricks-claude-opus-5-5"
GPT = "databricks-gpt-oss-120b"
USAGE = {"prompt_tokens": 9000, "completion_tokens": 50}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    monkeypatch.delenv("LOCAL_AGENT_MODEL", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")  # Opus 5.5 at 200,000 / 16,384, auto-compact 90%.
    settings.values.update(workspace=str(project), env_file="", compaction_handoffs=False)
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    return settings


@pytest.fixture
async def app_client(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            yield app.state.manager, client


def mock_gateway(monkeypatch):
    client = httpx.AsyncClient

    def gateway(request):
        if not json.loads(request.content)["stream"]:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}]})
        chunk = {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}], "usage": USAGE}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(gateway)))


class Socket:
    def __init__(self):
        self.messages = []

    async def send_json(self, data):
        self.messages.append(data)


async def turn_on(manager, client, monkeypatch, model):
    session_id = (await client.post("/api/sessions")).json()["id"]
    session = manager.get(session_id)
    session["model"] = model
    manager.store.save(session)
    with monkeypatch.context() as patch:  # The API client keeps its own transport.
        mock_gateway(patch)
        await manager.run(session, "Hello")
    return session_id


def limits_of(info):
    return {key: info[key] for key in ("context_window", "reply_reserve", "input_budget", "auto_compact_percent",
                                       "auto_compact_threshold")}


async def test_a_model_change_moves_the_meter_to_the_new_models_budgets(app_client, monkeypatch):
    manager, client = app_client
    session_id = await turn_on(manager, client, monkeypatch, GPT)
    before = manager.get(session_id)["context_info"]
    # GPT OSS 120B's default budget is its whole window, so the input budget keeps 5% of it for estimate error.
    assert limits_of(before) == {"context_window": 131_072, "reply_reserve": 8_192, "input_budget": 115_078,
                                 "auto_compact_percent": 90, "auto_compact_threshold": 103_570}
    tokens, report = before["next_estimate"]["tokens"], before["last_request"]
    # GPT OSS's own reported ratio (estimates never scale below 1).
    old_scale = before["next_estimate"]["scale"]
    assert old_scale == report["next_estimate_scale"] >= 1.0
    socket = Socket()
    manager.listeners.setdefault(session_id, set()).add(socket)
    response = await client.put(f"/api/sessions/{session_id}/model", json={"model": OPUS})
    info = response.json()["context_info"]
    expected = {"context_window": 200_000, "reply_reserve": 16_384, "input_budget": 181_568,
                "auto_compact_percent": 90, "auto_compact_threshold": 163_411}
    assert limits_of(info) == expected
    # The next request goes to Opus 5.5, whose estimates start at its own ratio.
    scale = estimate_scale(manager.get(session_id)["inference_calls"], OPUS)
    assert scale == 1.3
    scaled = math.ceil(tokens * scale / old_scale)
    assert info["next_estimate"] == {"tokens": scaled, "scale": scale,
                                     "percent_of_input_budget": round(scaled * 100 / 181_568, 1),
                                     "percent_of_threshold": round(scaled * 100 / 163_411, 1)}
    # The last request's report and estimate are what was sent.
    assert info["last_request"] == report and info["estimated_tokens"] == before["estimated_tokens"]
    assert limits_of(manager.store.get(session_id)["context_info"]) == expected
    assert [message["type"] for message in socket.messages] == ["model", "context"]
    assert limits_of(socket.messages[-1]["context_info"]) == expected


async def test_a_settings_save_moves_open_idle_chats_and_leaves_running_ones(app_client, monkeypatch, settings):
    manager, client = app_client
    idle = await turn_on(manager, client, monkeypatch, OPUS)
    running = await turn_on(manager, client, monkeypatch, OPUS)
    closed = await turn_on(manager, client, monkeypatch, OPUS)
    report = manager.get(idle)["context_info"]["last_request"]
    sockets = {idle: Socket(), running: Socket()}
    for session_id, socket in sockets.items():
        manager.listeners.setdefault(session_id, set()).add(socket)
    manager.statuses[running] = "running"
    running_before = json.dumps(manager.get(running)["context_info"], sort_keys=True)
    updated_before = manager.store.get(idle)["updated"]
    editable = {"workspace": settings.values["workspace"], "model": OPUS, "env_file": ""}
    response = await client.put("/api/settings", json={**editable, "context_window": 40_000,
                                                       "max_output_tokens": 4_096, "auto_compact_percent": 50})
    assert response.status_code == 200
    info = manager.get(idle)["context_info"]
    assert limits_of(info) == {"context_window": 40_000, "reply_reserve": 4_096, "input_budget": 33_856,
                               "auto_compact_percent": 50, "auto_compact_threshold": 16_928}
    tokens = info["next_estimate"]["tokens"]
    assert info["next_estimate"]["percent_of_input_budget"] == round(tokens * 100 / 33_856, 1)
    assert info["last_request"] == report and report["input_budget"] == 181_568
    assert manager.store.get(idle)["context_info"] == info
    assert manager.store.get(idle)["updated"] == updated_before  # Not moved up in the sidebar.
    assert sockets[idle].messages[-1] == {"type": "context", "context_info": info}
    # A running chat's next step prepares its own info; its in-flight report must keep the old budget.
    assert json.dumps(manager.get(running)["context_info"], sort_keys=True) == running_before
    assert sockets[running].messages == []
    # A chat not open is not rewritten on every save; opening it brings it up to date (below).
    assert manager.store.get(closed)["context_info"]["input_budget"] == 181_568


async def test_a_budget_at_the_opus_window_keeps_a_margin_in_the_meter_and_the_requests(app_client, monkeypatch,
                                                                                          settings):
    # A 1,000,000-token budget on Opus 5.5's 1,000,000-token window: the input budget keeps 5% for estimate
    # error, (1,000,000 - 16,384 - 2,048) / 1.05, in the meter after a save and in the next request.
    manager, client = app_client
    session_id = await turn_on(manager, client, monkeypatch, OPUS)
    manager.listeners.setdefault(session_id, set()).add(Socket())  # Open, so a save refreshes its meter.
    editable = {"workspace": settings.values["workspace"], "model": OPUS, "env_file": ""}
    response = await client.put("/api/settings", json={**editable, "context_window": 1_000_000,
                                                       "max_output_tokens": 16_384})
    assert response.status_code == 200
    expected = {"context_window": 1_000_000, "reply_reserve": 16_384, "input_budget": 934_826,
                "auto_compact_percent": 90, "auto_compact_threshold": 841_343}
    assert limits_of(manager.get(session_id)["context_info"]) == expected
    assert manager.input_budget(manager.get(session_id)) == 934_826
    with monkeypatch.context() as patch:
        mock_gateway(patch)
        await manager.run(manager.get(session_id), "Again")
    info = manager.get(session_id)["context_info"]
    assert limits_of(info) == expected and info["last_request"]["input_budget"] == 934_826
    # GPT OSS 120B at its own full window (capped there; its output tokens follow its default) keeps the same
    # margin against its 131,072-token window.
    gpt = manager.get(await turn_on(manager, client, monkeypatch, GPT))
    assert manager.request_limits(gpt) == (131_072, 8_192)
    assert manager.input_budget(gpt) == gpt["context_info"]["input_budget"] == (131_072 - 8_192 - 2_048) * 100 // 105
    assert gpt["context_info"]["last_request"]["input_budget"] == 115_078


async def test_a_chat_running_during_a_settings_save_follows_when_its_turn_ends(app_client, monkeypatch, settings):
    manager, client = app_client
    session_id = await turn_on(manager, client, monkeypatch, OPUS)
    socket = Socket()
    manager.listeners.setdefault(session_id, set()).add(socket)
    blocked, release = asyncio.Event(), asyncio.Event()
    httpx_client = httpx.AsyncClient

    async def gateway(request):
        if not json.loads(request.content)["stream"]:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}]})
        blocked.set()
        await release.wait()
        chunk = {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}], "usage": USAGE}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    with monkeypatch.context() as patch:
        patch.setattr(httpx, "AsyncClient",
                      lambda **kwargs: httpx_client(**kwargs, transport=httpx.MockTransport(gateway)))
        turn = asyncio.create_task(manager.run(manager.get(session_id), "Again"))
        await asyncio.wait_for(blocked.wait(), 10)
        assert manager.statuses[session_id] == "running"
        editable = {"workspace": settings.values["workspace"], "model": OPUS, "env_file": ""}
        response = await client.put("/api/settings", json={**editable, "context_window": 40_000,
                                                           "max_output_tokens": 4_096, "auto_compact_percent": 50})
        assert response.status_code == 200
        # The in-flight request keeps the budget it was prepared with.
        assert manager.get(session_id)["context_info"]["input_budget"] == 181_568
        release.set()
        await turn
    assert manager.statuses.get(session_id, "idle") == "idle"
    info = manager.get(session_id)["context_info"]
    assert limits_of(info) == {"context_window": 40_000, "reply_reserve": 4_096, "input_budget": 33_856,
                               "auto_compact_percent": 50, "auto_compact_threshold": 16_928}
    tokens = info["next_estimate"]["tokens"]
    assert info["next_estimate"]["percent_of_input_budget"] == round(tokens * 100 / 33_856, 1)
    assert info["last_request"]["input_budget"] == 181_568  # The report stays what was sent.
    assert manager.store.get(session_id)["context_info"] == info
    assert socket.messages[-1] == {"type": "context", "context_info": info}


def test_opening_a_chat_brings_its_meter_up_to_date(settings):
    app = create_app(settings)
    manager = app.state.manager
    with TestClient(app) as client:
        token = client.get("/api/bootstrap").json()["token"]
        session_id = client.post("/api/sessions", headers={"X-Local-Token": token}).json()["id"]
        session = manager.store.get(session_id)
        session["context_info"] = {
            "estimated_tokens": 20_000, "input_budget": 181_568, "context_window": 200_000, "reply_reserve": 16_384,
            "compactions": 0, "auto_compact_threshold": 163_411, "auto_compact_percent": 90, "warnings": [],
            "next_estimate": {"tokens": 20_500, "percent_of_input_budget": 11.3, "percent_of_threshold": 12.5}}
        manager.store.save(session)
        settings.update({"auto_compact_percent": 60})  # Saved while the chat was not open.
        with client.websocket_connect(f"/api/sessions/{session_id}/stream",
                                      subprotocols=["local-workspace", token]) as socket:
            info = socket.receive_json()["session"]["context_info"]
    assert info["auto_compact_percent"] == 60 and info["auto_compact_threshold"] == 181_568 * 60 // 100
    # Without a recorded scale the estimate is kept; only its shares change.
    assert info["next_estimate"] == {"tokens": 20_500, "percent_of_input_budget": 11.3,
                                     "percent_of_threshold": round(20_500 * 100 / 108_940, 1)}
    assert manager.store.get(session_id)["context_info"]["auto_compact_threshold"] == 108_940
