"""Settings → Fallback endpoint: an agent request that failed before its reply started goes once to another
endpoint, with that endpoint's own limits, reasoning fields and cache markers. Every request is answered by
a ReplayClient (serving.py); asyncio.sleep in local_agent.agents is replaced, so retries do not wait."""
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent import prompt_cache
from local_agent.prompt_cache import unmarked
from local_agent.reasoning import without_reasoning
from local_agent.agents import AgentManager, MAX_TRANSIENT_RETRIES, output_limit_text
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.context import effective_input_budget
from local_agent.model_profiles import thinking_reserve
from local_agent.serving import ReplayClient, sse_chunks
from local_agent.store import Store
from local_agent.telemetry import REQUEST_TAGS_HEADER, estimated_dbu, session_metrics

OPUS, OPUS_48, GPT = "databricks-claude-opus-5-5", "databricks-claude-opus-4-8", "databricks-gpt-oss-120b"
GPT_20B = "databricks-gpt-oss-20b"  # Reasoning like GPT OSS 120B; no limits in model_profiles.
USAGE = {"prompt_tokens": 1_000, "completion_tokens": 20}
RATE_LIMITED = {"error_code": "REQUEST_LIMIT_EXCEEDED", "message": "Exceeded workspace rate limit"}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_MODEL", OPUS)  # conftest sets an endpoint without a profile
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="", fallback_model=GPT)
    store = Store(tmp_path / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Fallback", title_generated=True, reasoning_effort="high")
    yield manager, session
    store.db.close()


@pytest.fixture
def delays(monkeypatch):
    waited = []

    async def sleep(delay):
        waited.append(delay)
    monkeypatch.setattr("local_agent.agents.asyncio.sleep", sleep)
    return waited


def replay(manager, *fixtures):
    manager.serving = ReplayClient(manager.settings, fixtures)
    return manager.serving.transport


def agent(fixture):
    """Only the streamed agent requests (not titles or compaction)."""
    return {**fixture, "match": lambda body: body.get("stream") is True}


def notices(session):
    return [event["text"] for event in session["events"] if event["type"] == "notice"]


def replies(session):
    return [event for event in session["events"] if event["type"] == "assistant"]


FAILURES = {
    "server": ({"status": 503, "text": "Upstream unavailable"}, "HTTP 503"),
    "connection": ({"error": "connect"}, "connection failure"),
}


@pytest.mark.parametrize("fallback", [GPT, OPUS_48])
@pytest.mark.parametrize("failure", sorted(FAILURES))
async def test_a_request_the_endpoint_never_answered_goes_once_to_the_fallback(runtime, delays, fallback, failure):
    manager, session = runtime
    manager.settings.values["fallback_model"] = fallback
    answer, reason = FAILURES[failure]
    transport = replay(manager, agent({"model": OPUS, **answer, "times": 1 + MAX_TRANSIENT_RETRIES}),
                       agent({"model": fallback, "chunks": sse_chunks("From the fallback.", usage=USAGE)}))
    await manager.run(session, "Hello")
    assert transport.unmatched == [] and transport.remaining() == []
    primary, sent = transport.bodies(OPUS), transport.bodies(fallback)
    assert len(primary) == 1 + MAX_TRANSIENT_RETRIES and len(sent) == 1 and delays == [2.0, 5.0, 15.0]
    body = sent[0]
    # The same prepared request, with the fallback's own limits, reasoning fields and cache markers.
    sent_messages, first = unmarked(body)["messages"], unmarked(primary[0])["messages"]
    assert sent_messages[1:] == first[1:] and body["tools"] == primary[0]["tools"]
    # The system message states the fallback's own response limit; the rest of it is the same.
    stated = output_limit_text(16_384, thinking=True)
    own = output_limit_text(body["max_tokens"], thinking=bool(thinking_reserve(fallback)))
    assert sent_messages[0]["content"] == first[0]["content"].replace(stated, own) != first[0]["content"]
    assert "output_config" in primary[0] and prompt_cache.markers(primary[0]) > 0
    if fallback == GPT:
        assert body["max_tokens"] == 8_192 and body["reasoning_effort"] == "high"
        assert "thinking" not in body and "output_config" not in body and prompt_cache.markers(body) == 0
    else:
        assert body["output_config"] == {"effort": "high"} and "reasoning_effort" not in body
        # The stable part's marker still ends where the system message's changing part begins.
        stable, tail = body["messages"][0]["content"]
        primary_stable, primary_tail = primary[0]["messages"][0]["content"]
        assert "cache_control" in stable and tail["text"] == primary_tail["text"]
        assert stable["text"] == primary_stable["text"].replace(stated, own)
    # The chat keeps its endpoint; the reply and the ledger name the one that answered.
    assert session["model"] == OPUS and session["terminal_reason"] == "completed"
    reply = replies(session)[-1]
    assert reply["text"] == "From the fallback."
    info = reply["request_info"]
    assert (info["model"], info["fallback_from"], info["fallback_reason"], info["status"]) == (
        fallback, OPUS, reason, "completed")
    calls = [call for call in session["inference_calls"] if call["purpose"] == "agent"]
    assert [(call["model"], call.get("retry_reason"), call.get("fallback_from")) for call in calls][-2:] == [
        (OPUS, "server_error" if failure == "server" else "network", None), (fallback, "fallback", OPUS)]
    metrics = session_metrics(session)
    last = metrics["calls"][-1]
    assert last["fallback_from"] == OPUS and last["model"] == fallback
    # Priced with the fallback's own rates (GPT OSS has none: unpriced).
    assert last["estimated_dbu"] == estimated_dbu(fallback, last["usage"])
    assert {group["model"] for group in metrics["by_model"]} == {OPUS, fallback}
    assert notices(session)[-1] == (
        f"{OPUS} failed before the reply started ({reason}, after the automatic retries), so the app sent this "
        f"request once to the fallback endpoint {fallback} (Settings → Model and context). The next request "
        f"tries {OPUS} again.")


async def test_the_next_step_tries_the_chats_endpoint_again_and_no_reasoning_is_replayed(runtime, delays):
    manager, session = runtime
    Path(session["workspace"], "notes.txt").write_text("plain notes\n")
    signed = {"choices": [{"delta": {"content": [{"type": "reasoning", "summary": [
        {"type": "summary_text", "text": "Looking.", "signature": "signed-by-opus"}]}]}, "finish_reason": None}]}
    transport = replay(
        manager,
        agent({"model": OPUS, "chunks": [signed, *sse_chunks("Reading.", "tool_calls", tool_calls=[
            {"id": "toolu_1", "name": "read_file", "arguments": {"path": "notes.txt"}}])]}),
        agent({"model": OPUS, "status": 503, "text": "busy", "times": 1 + MAX_TRANSIENT_RETRIES}),
        agent({"model": GPT, "chunks": sse_chunks("Reading again.", "tool_calls", tool_calls=[
            {"id": "call_2", "name": "read_file", "arguments": {"path": "notes.txt"}}])}),
        agent({"model": OPUS, "chunks": sse_chunks("Done on the chat's endpoint.")}))
    await manager.run(session, "Read notes.txt twice")
    assert transport.unmatched == [] and transport.remaining() == []
    assert [item["model"] for item in transport.requests] == [OPUS] * 5 + [GPT, OPUS]
    assert replies(session)[-1]["text"] == "Done on the chat's endpoint."
    assert replies(session)[-1]["request_info"]["model"] == OPUS
    assert "fallback_from" not in replies(session)[-1]["request_info"]
    # History carries no reasoning items, so no endpoint's signed reasoning reaches another.
    for body in transport.bodies():
        assert "signed-by-opus" not in json.dumps(body)
        for message in body["messages"]:
            assert set(message) <= {"role", "content", "tool_calls", "tool_call_id"}
            parts = message["content"] if isinstance(message.get("content"), list) else []
            assert all(part.get("type") == "text" for part in parts)
    # The fallback's own tool call ran and its result went to the chat's endpoint.
    assert transport.bodies(OPUS)[-1]["messages"][-1]["tool_call_id"] == "call_2"


@pytest.mark.parametrize("answer", [
    {"chunks": sse_chunks("Part"), "break": "read"},  # The stream began, then the connection failed.
    {"chunks": [{"error": {"status_code": 503, "message": "overloaded"}}]},  # An error inside the stream.
    {"chunks": sse_chunks("Stopped.", "content_filter")},
    {"status": 400, "text": "Bad request"},
    {"status": 401, "text": "Unauthorized"},
    {"status": 403, "text": "Forbidden"},
    {"status": 500, "text": "Internal error"},
])
async def test_no_fallback_after_the_first_byte_or_for_an_error_a_retry_cannot_fix(runtime, delays, answer):
    manager, session = runtime
    transport = replay(manager, agent({"model": OPUS, **answer, "times": None}))
    await manager.run(session, "Hello")
    assert transport.unmatched == [] and transport.bodies(GPT) == []
    assert not any("fallback" in text for text in notices(session))
    assert all(call["model"] == OPUS for call in session["inference_calls"])


@pytest.mark.parametrize("auto_continue, limited", [(True, 1 + MAX_TRANSIENT_RETRIES + 2), (False, 1 + MAX_TRANSIENT_RETRIES)])
async def test_a_rate_limit_falls_back_only_after_the_retries_and_waits(runtime, delays, auto_continue, limited):
    manager, session = runtime
    manager.settings.values["rate_limit_auto_continue"] = auto_continue
    transport = replay(manager, agent({"model": OPUS, "status": 429, "json": RATE_LIMITED,
                                       "headers": {"Retry-After": "1"}, "times": limited}),
                       agent({"model": GPT, "chunks": sse_chunks("From the fallback.")}))
    await manager.run(session, "Hello")
    assert transport.unmatched == [] and transport.remaining() == []
    assert len(transport.bodies(OPUS)) == limited and len(transport.bodies(GPT)) == 1
    assert replies(session)[-1]["request_info"]["fallback_reason"] == "rate limit, HTTP 429"


async def test_a_failing_fallback_is_tried_once_and_its_error_ends_the_request(runtime, delays):
    manager, session = runtime
    transport = replay(manager, agent({"model": OPUS, "status": 503, "text": "busy", "times": 1 + MAX_TRANSIENT_RETRIES}),
                       agent({"model": GPT, "status": 503, "text": "Fallback busy too"}))
    await manager.run(session, "Hello")
    assert transport.unmatched == [] and len(transport.bodies(GPT)) == 1
    info = replies(session)[-1]["request_info"]
    assert (info["model"], info["status"], info["http_status"], info["fallback_from"]) == (GPT, "error", 503, OPUS)
    assert any(event["type"] == "error" and "Fallback busy too" in event["text"] for event in session["events"])


async def test_a_conversation_too_large_for_the_fallback_is_not_sent_there(runtime, delays):
    manager, session = runtime
    manager.settings.values["context_window"] = 1_000_000  # Customised: GPT OSS still has a 131,072 window.
    transport = replay(manager, agent({"model": OPUS, "status": 503, "text": "busy", "times": 1 + MAX_TRANSIENT_RETRIES}))
    await manager.run(session, "Summarize this: " + "lorem ipsum " * 50_000)
    assert transport.unmatched == [] and transport.bodies(GPT) == []
    assert re.fullmatch(
        rf"{OPUS} failed before the reply started \(HTTP 503\)\. The fallback endpoint {GPT} was not tried: this "
        r"conversation's request \(about [\d,]+ tokens\) does not fit its input budget of 115,078 tokens\.",
        notices(session)[-1])
    assert replies(session)[-1]["request_info"]["model"] == OPUS
    assert any(event["type"] == "error" and "HTTP 503" in event["text"] for event in session["events"])


@pytest.mark.parametrize("settings_model, sent_max_tokens", [(OPUS, 8_192), (GPT_20B, 32_000)])
async def test_a_fallback_without_a_known_window_keeps_to_its_default_budgets(runtime, delays, settings_model,
                                                                              sent_max_tokens):
    manager, session = runtime
    # Budgets customised for the chat's endpoint; the app does not know GPT OSS 20B's window.
    manager.settings.values.update(context_window=1_000_000, max_output_tokens=32_000, fallback_model=GPT_20B)
    failing = agent({"model": OPUS, "status": 503, "text": "busy", "times": 1 + MAX_TRANSIENT_RETRIES})
    if settings_model == OPUS:
        transport = replay(manager, failing)
        await manager.run(session, "Summarize this: " + "lorem ipsum " * 50_000)
        assert transport.unmatched == [] and transport.bodies(GPT_20B) == []
        assert notices(session)[-1].endswith(
            f"does not fit its input budget of {effective_input_budget(131_072, 8_192):,} tokens.")
    # A request that fits goes there with its default output limit, or the Settings one on the Settings model,
    # whose budgets are the ones customised for it.
    manager.settings.values["model"] = settings_model
    second = manager.store.create({**manager.settings.values, "model": OPUS})
    second.update(title="Short", title_generated=True)
    transport = replay(manager, failing, agent({"model": GPT_20B, "chunks": sse_chunks("Short.")}))
    await manager.run(second, "A short question")
    assert transport.unmatched == [] and transport.remaining() == []
    body = transport.bodies(GPT_20B)[0]
    assert body["max_tokens"] == sent_max_tokens and transport.bodies(OPUS)[0]["max_tokens"] == 32_000
    assert output_limit_text(sent_max_tokens, thinking=False) in body["messages"][0]["content"]
    assert replies(second)[-1]["request_info"]["model"] == GPT_20B


@pytest.mark.parametrize("steps", [6, 1])
async def test_an_output_limit_on_the_fallback_names_that_requests_own_limit(runtime, delays, steps):
    manager, session = runtime
    manager.settings.values["max_agent_steps"] = steps
    transport = replay(manager, agent({"model": OPUS, "status": 503, "text": "busy", "times": 1 + MAX_TRANSIENT_RETRIES}),
                       agent({"model": GPT, "chunks": sse_chunks("Cut", "length")}),
                       *([agent({"model": OPUS, "chunks": sse_chunks("Done.")})] if steps > 1 else []))
    await manager.run(session, "Hello")
    assert transport.unmatched == [] and transport.remaining() == []
    assert transport.bodies(GPT)[0]["max_tokens"] == 8_192 and transport.bodies(OPUS)[0]["max_tokens"] == 16_384
    if steps > 1:
        assert notices(session)[-1] == ("The response reached the 8,192-token output limit; no tools from it ran. "
                                        "Automatically continuing in smaller steps (1 of 2).")
        assert replies(session)[-1]["text"] == "Done."
    else:
        errors = [event["text"] for event in session["events"] if event["type"] == "error"]
        assert errors[-1].startswith("The model reached the 8,192-token output limit on the final configured model "
                                     "request (1).")


async def test_a_chat_answered_by_the_fallback_can_be_exported_and_forked(tmp_path, monkeypatch, delays):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_MODEL", OPUS)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", fallback_model=GPT)
    app = create_app(settings)
    manager = app.state.manager
    try:
        session = manager.store.create(settings.values)
        session.update(title="Fallback", title_generated=True)
        transport = replay(manager, agent({"model": OPUS, "status": 503, "text": "busy",
                                           "times": 1 + MAX_TRANSIENT_RETRIES}),
                           agent({"model": GPT, "chunks": sse_chunks("From the fallback.")}),
                           agent({"model": OPUS, "chunks": sse_chunks("From the chat's endpoint.")}))
        await manager.run(session, "Hello")
        await manager.run(session, "Again")
        assert transport.unmatched == [] and transport.remaining() == []

        def answered(events):
            return [(event["text"], event["request_info"]["model"], event["request_info"].get("fallback_from"),
                     event["request_info"].get("fallback_reason")) for event in events if event["type"] == "assistant"]
        expected = [("From the fallback.", GPT, OPUS, "HTTP 503"), ("From the chat's endpoint.", OPUS, None, None)]
        assert answered(session["events"]) == expected
        second_message = [event for event in session["events"] if event["type"] == "user"][1]
        transport_app = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport_app, base_url="http://testserver") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            exported = await client.get(f"/api/sessions/{session['id']}/export")
            assert exported.status_code == 200, exported.text
            assert answered(exported.json()["sessions"][0]["events"]) == expected
            whole = await client.post(f"/api/sessions/{session['id']}/fork")
            cut = await client.post(f"/api/sessions/{session['id']}/fork",
                                    json={"before_event_id": second_message["id"]})
        assert whole.status_code == 201 and cut.status_code == 201, (whole.text, cut.text)
        assert answered(manager.store.get(whole.json()["id"])["events"]) == expected
        assert answered(manager.store.get(cut.json()["id"])["events"]) == expected[:1]
    finally:
        manager.store.db.close()


async def test_a_fallback_nobody_chose_is_not_sent_the_conversation(runtime, delays):
    manager, session = runtime
    manager.settings.values["fallback_model"] = "databricks-unlisted-model"  # A hand-edited settings file.
    failing = agent({"model": OPUS, "status": 503, "text": "busy", "times": 1 + MAX_TRANSIENT_RETRIES})
    transport = replay(manager, failing)
    await manager.run(session, "Hello")
    assert transport.unmatched == [] and notices(session)[-1].endswith(
        "The fallback endpoint databricks-unlisted-model was not tried: it is neither an endpoint the app knows "
        "nor one you use.")
    # Once one of the user's own chats uses it, it may answer.
    manager.store.create({**manager.settings.values, "model": "databricks-unlisted-model"})
    transport = replay(manager, failing, agent({"model": "databricks-unlisted-model", "chunks": sse_chunks("Hi.")}))
    await manager.run(session, "Again")
    assert transport.unmatched == [] and replies(session)[-1]["request_info"]["model"] == "databricks-unlisted-model"


async def test_titles_side_questions_and_compaction_never_fall_back(runtime, delays):
    manager, session = runtime
    session["title_generated"] = False
    session["title"] = "New conversation"
    transport = replay(manager, agent({"model": OPUS, "chunks": sse_chunks("Answer.")}),
                       {"model": OPUS, "status": 503, "text": "busy", "times": None})
    await manager.run(session, "First question")
    assert replies(session)[-1]["text"] == "Answer." and session["title"] == "First question"
    manager.store.save(session)
    with pytest.raises(ValueError, match="HTTP 503"):
        await manager.side_question(session["id"], "What did you say?")
    session["wire"] += [{"role": "user", "content": "Second question"}, {"role": "assistant", "content": "Second answer."}]
    manager.store.save(session)
    await manager.manual_compact(session)
    assert any(event["type"] == "error" and "Context compaction failed (HTTP 503)" in event["text"]
               for event in session["events"])
    purposes = {json.loads(item["headers"][REQUEST_TAGS_HEADER.lower()])["local_agent_purpose"]
                for item in transport.requests}
    assert purposes == {"agent", "title", "side_question", "compaction"}
    assert transport.unmatched == [] and transport.bodies(GPT) == []


def test_a_fallback_request_never_carries_reasoning_items():
    calls = [{"id": "toolu_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
    messages = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "x", "signature": "signed"}]},
        {"type": "thinking", "thinking": "y", "signature": "signed"}, {"type": "redacted_thinking", "data": "z"},
        {"type": "text", "text": "Answer"}], "reasoning_content": "r", "tool_calls": calls},
        {"role": "assistant", "content": "Plain."}]
    assert without_reasoning(messages) == [messages[0], {"role": "assistant", "content": [
        {"type": "text", "text": "Answer"}], "tool_calls": calls}, messages[2]]
    assert messages[1]["reasoning_content"] == "r" and len(messages[1]["content"]) == 4  # The history is not changed.


def test_the_fallback_is_never_the_chats_own_endpoint(runtime):
    manager, session = runtime
    assert manager.fallback_endpoint(session) == GPT
    assert manager.fallback_endpoint({**session, "model": GPT}) is None  # A subagent already on it.
    manager.settings.values["fallback_model"] = ""
    assert manager.fallback_endpoint(session) is None


def test_settings_accept_only_an_endpoint_the_app_knows_or_the_user_uses(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_MODEL", OPUS)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    assert settings.values["fallback_model"] == ""
    base = {"workspace": str(tmp_path), "model": OPUS, "env_file": ""}
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}

        def save(**values):
            return client.put("/api/settings", headers=headers, json={**base, **values})
        # The model endpoint itself is accepted (the new-chat model picker may choose it): chats on it have none.
        assert save(fallback_model=OPUS).json()["fallback_model"] == OPUS
        assert "letters, digits" in save(fallback_model="not a name!").json()["detail"]
        refused = save(fallback_model="databricks-unlisted-model")
        assert refused.status_code == 400 and "neither an endpoint the app knows" in refused.json()["detail"]
        saved = save(fallback_model=" " + GPT + " ")
        assert saved.status_code == 200 and saved.json()["fallback_model"] == GPT
        assert json.loads(settings.path.read_text())["fallback_model"] == GPT
        # An endpoint this save also sets for a subagent type counts as one the user uses.
        team = save(fallback_model="databricks-team-endpoint",
                    subagent_defaults={"explore": {"model": "databricks-team-endpoint", "effort": "low"}})
        assert team.status_code == 200 and team.json()["fallback_model"] == "databricks-team-endpoint"
        assert save(fallback_model="").json()["fallback_model"] == ""
    # A hand-edited value that is not an endpoint name means none.
    settings.path.write_text(json.dumps({**json.loads(settings.path.read_text()), "fallback_model": "../elsewhere"}))
    assert Settings(settings.state_dir).values["fallback_model"] == ""
