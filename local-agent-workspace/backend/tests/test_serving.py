"""The model serving seam (serving.py): one place builds every model request's URL, headers and client,
and a ReplayClient answers them offline through the same seam."""
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent import serving
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.serving import ReplayClient, ReplayTransport, ServingClient, UnmatchedRequest, sse_chunks
from local_agent.store import Store
from local_agent.telemetry import REQUEST_TAGS_HEADER

PACKAGE = Path(__file__).resolve().parents[1] / "local_agent"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    store = Store(tmp_path / "tests.sqlite3")
    manager = AgentManager(store, settings)
    yield manager, store.create(settings.values)
    store.db.close()


def test_urls_quote_the_endpoint_name():
    host = "https://serving.example"
    assert serving.invocations_url(host, "databricks-claude-opus-5-5") == (
        "https://serving.example/serving-endpoints/databricks-claude-opus-5-5/invocations")
    # A name is never a path: a slash or a space stays inside its one segment.
    assert serving.invocations_url(host, "a/b c") == "https://serving.example/serving-endpoints/a%2Fb%20c/invocations"
    assert serving.endpoints_url(host) == "https://serving.example/api/2.0/serving-endpoints"
    assert serving.endpoint_url(host, "x/y") == "https://serving.example/api/2.0/serving-endpoints/x%2Fy"


def test_headers_carry_the_token_the_request_tags_and_the_body_type():
    session, call = {"id": "chat-1"}, {"id": "call-1", "purpose": "agent"}
    assert serving.request_headers("t") == {"Authorization": "Bearer t"}
    tagged = serving.request_headers("t", session, call, json_body=True)
    assert tagged["Content-Type"] == "application/json"
    assert json.loads(tagged[REQUEST_TAGS_HEADER]) == {"local_agent_call_id": "call-1",
                                                       "local_agent_conversation": "chat-1",
                                                       "local_agent_purpose": "agent"}
    assert serving.call_headers({"Authorization": "Bearer t"}, session, call) == tagged


def test_the_client_never_follows_redirects_and_passes_a_transport_only_when_set(monkeypatch):
    seen = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: seen.append(kwargs) or kwargs)
    serving.open_client(5)
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    ServingClient(None, transport).client(serving.LONG_TIMEOUT)
    # Test helpers that replace httpx.AsyncClient pass their own transport: a second one would be a TypeError.
    assert seen == [{"timeout": 5, "follow_redirects": False},
                    {"timeout": serving.LONG_TIMEOUT, "follow_redirects": False, "transport": transport}]
    assert serving.stream_timeout(7).read == 7 and serving.stream_timeout().connect == 20


def test_no_model_request_is_built_outside_the_serving_module():
    """Every URL to a serving endpoint and every HTTP client of the app's own requests come from serving.py."""
    offenders = []
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "serving.py":
            continue
        text = path.read_text()
        for pattern in (r"/serving-endpoints", r"\bhttpx\.AsyncClient\(", r"/invocations\b"):
            offenders += [f"{path.name}: {pattern}" for _ in re.finditer(pattern, text)]
    assert offenders == []


async def test_a_replayed_tool_loop_runs_through_the_seam_and_records_every_body(runtime):
    manager, session = runtime
    workspace = Path(session["workspace"])
    (workspace / "notes.txt").write_text("replayed contents\n")
    replay = ReplayClient(manager.settings, [
        {"model": session["model"], "match": lambda body: body["stream"] is True, "chunks": sse_chunks(
            "Reading.", "tool_calls", tool_calls=[{"id": "call_1", "name": "read_file", "arguments": {"path": "notes.txt"}}])},
        {"model": session["model"], "match": lambda body: body["stream"] is True, "chunks": sse_chunks(
            "It says replayed contents.", usage={"prompt_tokens": 900, "completion_tokens": 12})},
        {"model": session["model"], "match": lambda body: body["stream"] is False, "json": {
            "choices": [{"message": {"content": "Read the notes file"}, "finish_reason": "stop"}]}},
    ])
    manager.serving = replay
    await manager.run(session, "What does notes.txt say?")
    transport = replay.transport
    assert transport.unmatched == [] and transport.remaining() == []
    bodies = transport.bodies(session["model"])
    assert [body["stream"] for body in bodies] == [True, True, False]
    tool_message = bodies[1]["messages"][-1]
    assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "call_1"
    assert "replayed contents" in tool_message["content"]
    assert session["events"][-1]["type"] == "assistant" and session["events"][-1]["text"] == "It says replayed contents."
    assert session["title"] == "Read the notes file"
    # Each request carries the synthetic token and the tags of its own ledger record.
    calls = session["inference_calls"]
    assert [call["purpose"] for call in calls] == ["agent", "agent", "title"]
    for request, call in zip(transport.requests, calls):
        assert request["headers"]["authorization"] == "Bearer " + ReplayClient.TOKEN
        assert json.loads(request["headers"][REQUEST_TAGS_HEADER.lower()])["local_agent_call_id"] == call["id"]
        assert request["path"] == f"/serving-endpoints/{session['model']}/invocations"


async def test_an_unmatched_request_fails_and_reaches_no_network(runtime):
    manager, session = runtime
    session.update(title="Named", title_generated=True)
    manager.serving = ReplayClient(manager.settings)
    await manager.run(session, "Hello")
    transport = manager.serving.transport
    assert len(transport.unmatched) == 1 and transport.unmatched[0]["model"] == session["model"]
    assert session["inference_calls"][-1]["status"] == "error"
    assert any(event["type"] == "error" and "No replay fixture" in event["text"] for event in session["events"])


async def test_replayed_failures_and_a_fixture_file(tmp_path):
    fixture = tmp_path / "basic.json"
    fixture.write_text(json.dumps({"model": "m", "status": 200, "chunks": sse_chunks("Hi"),
                                   "body": {"messages": []}, "notes": "recorded request body is kept, not used"}))
    transport = ReplayTransport.from_fixture(fixture).add(
        {"model": "m", "error": "connect"}, {"model": "m", "status": 503, "text": "busy", "times": None},
        {"path": "/api/2.0/serving-endpoints", "json": {"endpoints": [{"name": "m"}]}})
    async with httpx.AsyncClient(transport=transport, base_url="https://serving.example") as client:
        first = await client.post("/serving-endpoints/m/invocations", json={"stream": True})
        assert first.status_code == 200 and b"data: [DONE]" in first.content
        with pytest.raises(httpx.ConnectError):
            await client.post("/serving-endpoints/m/invocations", json={})
        for _ in range(2):  # times None answers every later request.
            assert (await client.post("/serving-endpoints/m/invocations", json={})).status_code == 503
        assert (await client.get("/api/2.0/serving-endpoints")).json() == {"endpoints": [{"name": "m"}]}
        with pytest.raises(UnmatchedRequest):
            await client.post("/serving-endpoints/other/invocations", json={})
    assert [item["model"] for item in transport.requests] == ["m", "m", "m", "m", None, "other"]
    assert transport.bodies("m")[0] == {"stream": True}


async def test_a_replayed_stream_can_break_after_its_first_bytes():
    transport = ReplayTransport([{"model": "m", "chunks": sse_chunks("Part"), "break": "read"}])
    async with httpx.AsyncClient(transport=transport, base_url="https://serving.example") as client:
        async with client.stream("POST", "/serving-endpoints/m/invocations", json={}) as response:
            assert response.status_code == 200
            with pytest.raises(httpx.ReadError):
                async for _ in response.aiter_bytes():
                    pass


def test_the_connection_check_goes_through_the_seam(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    app = create_app(settings)
    replay = ReplayClient(settings, [{"path": "/api/2.0/serving-endpoints", "json": {"endpoints": [
        {"name": "databricks-claude-opus-5-5"}, {"name": "databricks-gte-large-en"}]}}])
    app.state.manager.serving = replay
    with TestClient(app) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        result = client.get("/api/connection", headers=headers).json()
    assert result == {"connected": True, "models": ["databricks-claude-opus-5-5"], "error": None}
    assert replay.transport.requests[0]["headers"]["authorization"] == "Bearer " + ReplayClient.TOKEN
