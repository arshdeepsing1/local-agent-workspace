"""/btw side questions (CC-29): one request with the conversation as context; nothing is saved
in the conversation except the usage ledger record."""
import asyncio
import json

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.composer_api import SIDE_QUESTION_PREFIX
from local_agent.config import Settings
from local_agent.context import OUTPUT_LIMIT_CONTINUATION
from local_agent.store import Store
from local_agent.telemetry import session_metrics


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    settings.redact = lambda text: text.replace("private-token", "[REDACTED]")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["wire"] = [{"role": "user", "content": "Which parser do we use?"},
                       {"role": "assistant", "content": "The recursive descent parser in src/parse.py."}]
    session["events"] = [{"id": "u", "type": "user", "text": "Which parser do we use?", "created": 1},
                         {"id": "a", "type": "assistant", "text": "The recursive descent parser.", "created": 2}]
    store.save(session)
    yield manager, session
    store.db.close()


def answer(content="It is in src/parse.py.", finish="stop", **message):
    return httpx.Response(200, json={"id": "resp-1", "choices": [{"message": {"content": content, **message},
                                                                  "finish_reason": finish}],
                                     "usage": {"prompt_tokens": 120, "completion_tokens": 9}})


def gateway(monkeypatch, respond):
    requests = []
    client = httpx.AsyncClient

    async def handle(request):
        requests.append({"body": json.loads(request.content), "headers": request.headers})
        result = respond()
        return await result if asyncio.iscoroutine(result) else result
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**{"transport": httpx.MockTransport(handle), **kwargs}))
    return requests


def file_lines(manager, session_id):
    return manager.store._path(session_id).read_text().splitlines()


async def test_the_request_has_the_history_the_question_and_tools_and_saves_nothing(runtime, monkeypatch):
    manager, session = runtime
    sid = session["id"]
    requests = gateway(monkeypatch, answer)
    wire, events = json.dumps(session["wire"]), json.dumps(session["events"])
    events_in_file = sum('"record":"event"' in line for line in file_lines(manager, sid))
    result = await manager.side_question(sid, "Where is it?")
    assert result == {"answer": "It is in src/parse.py.", "request_info": {
        "status": "completed", "finish_reason": "stop", "usage": {"input_tokens": 120, "output_tokens": 9}}}
    body = requests[0]["body"]
    assert body["stream"] is False and body["max_tokens"] == 4096 and body["tools"]
    assert {tool["function"]["name"] for tool in body["tools"]} >= {"read_file", "run_command"}
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1:] == [*session["wire"], {"role": "user", "content": SIDE_QUESTION_PREFIX + "Where is it?"}]
    tags = json.loads(requests[0]["headers"]["databricks-ai-gateway-request-tags"])
    assert tags["local_agent_purpose"] == "side_question" and tags["local_agent_conversation"] == sid
    stored = manager.get(sid)
    assert json.dumps(stored["wire"]) == wire and json.dumps(stored["events"]) == events
    assert sum('"record":"event"' in line for line in file_lines(manager, sid)) == events_in_file
    calls = [call for call in stored["inference_calls"] if call["purpose"] == "side_question"]
    assert len(calls) == 1 and calls[0]["status"] == "completed" and calls[0]["response_id"] == "resp-1"
    labels = {group["key"]: group["label"] for group in session_metrics(stored)["by_purpose"]}
    assert labels == {"side_question": "Side question"}


async def test_a_turn_waiting_for_approval_is_cut_from_the_copy_and_the_question_merges(runtime, monkeypatch):
    manager, session = runtime
    sid = session["id"]
    calls = [{"id": f"c{n}", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'}}
             for n in range(2)]
    session["wire"] += [{"role": "user", "content": "Read both files"},
                        {"role": "assistant", "content": None, "tool_calls": calls},
                        {"role": "tool", "tool_call_id": "c0", "content": "first"}]
    manager.live[sid] = session  # As while the turn waits for the second call's approval.
    requests = gateway(monkeypatch, answer)
    await manager.side_question(sid, "Why two?")
    messages = requests[0]["body"]["messages"][1:]
    assert messages[:2] == session["wire"][:2]
    assert messages[2:] == [{"role": "user", "content": "Read both files\n\n" + SIDE_QUESTION_PREFIX + "Why two?"}]
    assert len(session["wire"]) == 5 and session["wire"][2]["content"] == "Read both files"
    assert [call["purpose"] for call in session["inference_calls"]] == ["side_question"]


async def test_a_trailing_app_prompt_is_dropped(runtime, monkeypatch):
    manager, session = runtime
    session["wire"] += [{"role": "user", "content": "Write it"}, {"role": "assistant", "content": "[cut off]"},
                        {"role": "user", "content": OUTPUT_LIMIT_CONTINUATION}]
    manager.store.save(session)
    requests = gateway(monkeypatch, answer)
    await manager.side_question(session["id"], "Status?")
    messages = requests[0]["body"]["messages"]
    assert messages[-2] == {"role": "assistant", "content": "[cut off]"}
    assert messages[-1] == {"role": "user", "content": SIDE_QUESTION_PREFIX + "Status?"}


@pytest.mark.parametrize("response, expected", [
    (lambda: answer(None, "tool_calls", tool_calls=[{"id": "x", "type": "function",
                                                     "function": {"name": "read_file", "arguments": "{}"}}]),
     "The model tried to use a tool, which side questions cannot do. Ask in the conversation instead."),
    (lambda: answer("Partial answer private-token", "length"),
     "Partial answer [REDACTED]\n\n[Answer cut off at the output limit.]"),
])
async def test_tool_calls_and_the_output_limit(runtime, monkeypatch, response, expected):
    manager, session = runtime
    gateway(monkeypatch, response)
    assert (await manager.side_question(session["id"], "Q"))["answer"] == expected


async def test_a_failed_request_is_an_error_and_is_recorded(runtime, monkeypatch):
    manager, session = runtime
    gateway(monkeypatch, lambda: httpx.Response(429, json={"error": "slow down private-token"}))
    with pytest.raises(ValueError, match=r"^Databricks returned HTTP 429: .*\[REDACTED\]"):
        await manager.side_question(session["id"], "Q")
    call, = manager.get(session["id"])["inference_calls"]
    assert call["status"] == "error" and call["error_kind"] == "rate_limit" and session["id"] not in manager.side_questions


async def test_a_conversation_too_long_for_a_side_question(runtime, monkeypatch):
    manager, session = runtime
    # The system message and tools alone take about 10,000 tokens: this budget leaves the latest turn
    # room, so only the earlier turn is too long.
    manager.settings.values["context_window"] = 24576
    session["wire"] = [{"role": "user", "content": "x " * 40000}, {"role": "assistant", "content": "ok"},
                       {"role": "user", "content": "next"}, {"role": "assistant", "content": "done"}]
    manager.store.save(session)
    requests = gateway(monkeypatch, answer)
    with pytest.raises(ValueError, match="^This conversation is too long for a side question right now. Compact it first.$"):
        await manager.side_question(session["id"], "Q")
    assert requests == [] and manager.get(session["id"])["inference_calls"] == []


async def test_one_side_question_at_a_time(runtime, monkeypatch):
    manager, _ = runtime
    app = create_app(manager.settings)
    agents = app.state.manager
    session = agents.store.create(manager.settings.values)
    sid = session["id"]
    release = asyncio.Event()

    async def slow():
        await release.wait()
        return answer()
    gateway(monkeypatch, slow)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = {"X-Local-Token": (await client.get("/api/bootstrap")).json()["token"]}
            path = f"/api/sessions/{sid}/side-question"
            first = asyncio.create_task(client.post(path, headers=headers, json={"question": "One?"}))
            for _ in range(200):
                if sid in agents.side_questions:
                    break
                await asyncio.sleep(0.01)
            second = await client.post(path, headers=headers, json={"question": "Two?"})
            assert second.status_code == 409
            assert second.json()["detail"] == "A side question is already being answered in this conversation."
            assert (await client.post(path, headers=headers, json={"question": "x" * 4001})).status_code == 422
            release.set()
            done = await first
            assert done.status_code == 200 and done.json()["answer"] == "It is in src/parse.py."
            assert sid not in agents.side_questions
    finally:
        agents.store.db.close()
