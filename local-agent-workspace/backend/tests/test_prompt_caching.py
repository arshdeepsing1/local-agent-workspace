"""Prompt caching: the cache markers agent requests to Databricks-hosted Claude endpoints carry (prompt_cache.py),
where they go and where not, the fallback when Databricks rejects them, Settings → Prompt caching, and the
response-shape fields the usage ledger keeps (usage_keys, reasoning_shape, system_tail_changed)."""
import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from local_agent import model_profiles, prompt_cache
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.context import estimate_tokens
from local_agent.prompt_cache import CACHE_CONTROL, marked, markers, unmarked
from local_agent.store import Store
from local_agent.telemetry import MODEL_DBU_PER_MILLION, finish_inference_call, session_metrics, usage_keys

OPUS = "databricks-claude-opus-5-5"
OPUS_48 = "databricks-claude-opus-4-8"
GPT = "databricks-gpt-oss-120b"
README = Path(__file__).resolve().parents[2] / "README.md"
REAL_CLIENT = httpx.AsyncClient
MARK = {"cache_control": CACHE_CONTROL}


# marked(): the levels

def request(*messages):
    return {"messages": [{"role": "system", "content": "Stable rules.\nAdditional allowed folders: []"}, *messages],
            "tools": [{"type": "function", "function": {"name": "read_file"}}], "stream": True, "max_tokens": 1024}


TOOL_TURN = ({"role": "user", "content": "Read it"},
             {"role": "assistant", "content": None, "tool_calls": [
                 {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
                 {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
             {"role": "tool", "tool_call_id": "c1", "content": "one"},
             {"role": "tool", "tool_call_id": "c2", "content": "two"})


def test_full_splits_the_system_message_and_marks_the_last_user_message():
    payload = request({"role": "user", "content": "Hello"})
    before = copy.deepcopy(payload)
    sent = marked(payload, len("Stable rules."), "full")
    assert payload == before  # Never changed in place.
    assert sent["messages"][0]["content"] == [{"type": "text", "text": "Stable rules.", **MARK},
                                              {"type": "text", "text": "\nAdditional allowed folders: []"}]
    assert sent["messages"][-1] == {"role": "user", "content": [{"type": "text", "text": "Hello", **MARK}]}
    assert sent["tools"] is payload["tools"] and markers(sent) == 2
    assert unmarked(sent) == payload
    # Each marker is its own dict: changing one changes nothing else.
    sent["messages"][0]["content"][0]["cache_control"]["type"] = "changed"
    assert CACHE_CONTROL == {"type": "ephemeral"} and sent["messages"][-1]["content"][0]["cache_control"] == CACHE_CONTROL


def test_full_marks_the_last_tool_result_and_omits_an_empty_tail():
    payload = request(*TOOL_TURN)
    whole = len(payload["messages"][0]["content"])
    sent = marked(payload, whole + 10, "full")
    assert sent["messages"][0]["content"] == [{"type": "text", "text": payload["messages"][0]["content"], **MARK}]
    assert sent["messages"][-1]["content"] == [{"type": "text", "text": "two", **MARK}]
    assert sent["messages"][-2] is payload["messages"][-2]  # Only the messages that get a marker are copied.
    assert markers(sent) == 2 and unmarked(sent) == payload


def test_messages_uses_only_the_documented_forms():
    user = request({"role": "user", "content": "Hello"})
    sent = marked(user, 6, "messages")
    assert sent["messages"][0] == user["messages"][0]  # No system split.
    assert sent["messages"][-1]["content"] == [{"type": "text", "text": "Hello", **MARK}] and markers(sent) == 1
    tools = request(*TOOL_TURN)
    before = copy.deepcopy(tools)
    sent = marked(tools, 6, "messages")
    assert tools == before
    calls = sent["messages"][2]["tool_calls"]
    assert "cache_control" not in calls[0] and calls[1]["cache_control"] == CACHE_CONTROL
    assert [message["content"] for message in sent["messages"][-2:]] == ["one", "two"]  # Tool results stay text.
    assert markers(sent) == 1 and unmarked(sent) == tools


def test_nothing_is_marked_after_an_assistant_message_or_at_off():
    payload = request({"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi"})
    assert markers(marked(payload, 6, "full")) == 1  # The system part only.
    assert marked(payload, 6, "messages") == payload and markers(marked(payload, 6, "messages")) == 0
    off = marked(request({"role": "user", "content": "Hello"}), 6, "off")
    assert off == request({"role": "user", "content": "Hello"}) and markers(off) == 0
    # A lone system message, list or empty content, or no stable length: nothing that Databricks could reject.
    assert markers(marked(request(), 6, "full")) == 1
    assert markers(marked(request({"role": "user", "content": [{"type": "text", "text": "x"}]}), 0, "full")) == 0
    assert markers(marked(request({"role": "tool", "tool_call_id": "c", "content": ""}), None, "full")) == 0


@pytest.mark.parametrize("level", prompt_cache.LEVELS)
@pytest.mark.parametrize("messages", [({"role": "user", "content": "Hello"},), TOOL_TURN,
                                      (*TOOL_TURN, {"role": "assistant", "content": "Done."},
                                       {"role": "user", "content": "Again"})])
def test_at_most_two_markers_and_the_markers_come_off_again(level, messages):
    payload = request(*messages)
    sent = marked(payload, 6, level)
    assert markers(sent) <= 2 and unmarked(sent) == payload


def test_rejections_and_levels():
    for text in ('{"message": "cache_control is not supported"}', "Unknown field: Cache Control",
                 "prompt caching is not enabled", "PROMPT CACHE unavailable", "type 'ephemeral' is not allowed"):
        assert prompt_cache.rejected(text), text
    for text in ("The prompt is too long", "", None, "thinking: not supported"):
        assert not prompt_cache.rejected(text), text
    assert [prompt_cache.next_level(level) for level in ("full", "messages", "off", "other")] == [
        "messages", "off", "off", "off"]


def test_which_endpoints_get_markers():
    assert [model_profiles.prompt_caching(model) for model in (OPUS, OPUS_48, " Databricks-Claude-Opus-5-5 ", GPT,
            "databricks-gpt-oss-20b", "test-endpoint", "databricks-claude-sonnet-4-5", None)] == [
        True, True, True, False, False, False, False, False]
    assert model_profiles.public_profile(OPUS)["prompt_caching"] is True
    assert model_profiles.public_profile(GPT)["prompt_caching"] is False


# Agent requests (fake Databricks gateway)

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Caching", title_generated=True, model=OPUS, permission_mode="bypassPermissions")
    store.save(session)
    yield AgentManager(store, settings), session, project
    store.db.close()


def sse(delta, finish="stop", usage=None):
    chunks = [{"choices": [{"delta": delta, "finish_reason": finish}]}]
    if usage:
        chunks.append({"choices": [], "usage": usage})
    return httpx.Response(200, text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n")


def reply(text="Done.", usage=None):
    return sse({"content": text}, usage=usage)


def call_reply(name, arguments, call_id="c1"):
    return sse({"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)}}]}, "tool_calls")


def mock_gateway(monkeypatch, respond):
    """Agent requests (streamed) go to respond(body); the others get a short JSON answer. Returns every body."""
    bodies = []

    async def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "A short answer."}, "finish_reason": "stop"}]})
        return respond(body)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(handle)))
    return bodies


def agent_bodies(bodies):
    return [body for body in bodies if body.get("stream") is True]


def agent_calls(session):
    return [call for call in session["inference_calls"] if call["purpose"] == "agent"]


def notices(session):
    return [event["text"] for event in session["events"] if event["type"] == "notice"]


def last_reply(session):
    return next(event for event in reversed(session["events"]) if event["type"] == "assistant")


async def test_agent_requests_carry_markers_and_keep_a_stable_prefix_across_steps(runtime, monkeypatch):
    manager, session, project = runtime
    (project / "note.txt").write_text("first\n")
    script = [lambda: call_reply("read_file", {"path": "note.txt"}), lambda: call_reply("list_tasks", {}, "c2"),
              reply, reply, lambda: call_reply("list_tasks", {}, "c3"), reply]
    bodies = mock_gateway(monkeypatch, lambda body: script.pop(0)())
    await manager.run(session, "Read the note")
    (project / "note.txt").write_text("changed by the user\n")  # A changed-files notice joins the system tail.
    await manager.run(session, "Continue")
    await manager.run(session, "Once more")
    sent = agent_bodies(bodies)
    assert len(sent) == 6
    for body in sent:
        system = body["messages"][0]["content"]
        assert system[0]["cache_control"] == CACHE_CONTROL and "cache_control" not in system[1]
        assert system[1]["text"].startswith("\nAdditional allowed folders: ")
        assert body["messages"][-1]["role"] in ("user", "tool")
        assert body["messages"][-1]["content"][0]["cache_control"] == CACHE_CONTROL and markers(body) == 2
    assert "Files changed since" in sent[3]["messages"][0]["content"][1]["text"]
    # The stable part never changed, and each request's messages (markers off) start with the previous one's.
    assert all(body["messages"][0]["content"][0] == sent[0]["messages"][0]["content"][0] for body in sent)
    for before, after in zip(sent, sent[1:]):
        previous, current = unmarked(before)["messages"][1:], unmarked(after)["messages"][1:]
        assert current[:len(previous)] == previous
    # Estimates and the ledger read the request without markers; the meter's size is the bytes sent.
    calls = agent_calls(session)
    assert [call["prompt_cache"] for call in calls] == ["full"] * 6
    assert [call["estimated_input_tokens"] for call in calls] == [
        estimate_tokens(unmarked(body)["messages"], body["tools"]) for body in sent]
    assert session["context_info"]["payload_bytes"] == len(json.dumps(sent[-1], ensure_ascii=False,
                                                                       separators=(",", ":")).encode())
    assert session_metrics(session)["calls"][-1]["prompt_cache"] == "full"
    # The system tail changed once (the notice); it stays until the file is read again.
    assert [call.get("system_tail_changed") for call in calls] == [None, None, None, True, None, None]
    assert session_metrics(session)["calls"][3]["system_tail_changed"] is True
    assert "system_tail" not in json.dumps(session["context_info"]) and "Files changed" not in json.dumps(calls)


@pytest.mark.parametrize("model,setting", [(OPUS, "off"), ("test-endpoint", "auto"), (GPT, "auto")])
async def test_off_and_endpoints_without_caching_send_no_markers(runtime, monkeypatch, model, setting):
    manager, session, _ = runtime
    session["model"] = model
    manager.settings.values["prompt_caching"] = setting
    bodies = mock_gateway(monkeypatch, lambda body: reply())
    await manager.run(session, "Hello")
    assert "cache_control" not in json.dumps(bodies)
    assert agent_calls(session)[0].get("prompt_cache") == ("off" if model == OPUS else None)


async def test_compaction_title_and_side_questions_carry_no_markers(runtime, monkeypatch):
    manager, session, _ = runtime
    session.update(title="New conversation", title_generated=False)
    manager.settings.values.update(model=OPUS, context_window=200_000, max_output_tokens=16_384)
    manager.store.save(session)
    bodies = mock_gateway(monkeypatch, lambda body: reply())
    await manager.run(session, "Hello")  # A title request follows the first reply.
    await manager.run(session, "And then?")
    await manager.side_question(session["id"], "Which file?")
    manager.settings.values["compaction_handoffs"] = False
    await manager.manual_compact(manager.get(session["id"]))
    others = [body for body in bodies if body.get("stream") is not True]
    kinds = {"title" if "Create a short title" in json.dumps(body) else "side" if "tools" in body else "summary"
             for body in others}
    assert kinds == {"title", "side", "summary"}, kinds
    assert all(markers(body) == 0 and "cache_control" not in json.dumps(body) for body in others)
    assert [markers(body) for body in agent_bodies(bodies)] == [2, 2]


async def test_a_subagent_on_opus_is_marked(runtime, monkeypatch):
    manager, session, _ = runtime
    parent = []

    def respond(body):
        first = next(message["content"] for message in unmarked(body)["messages"] if message["role"] == "user")
        if first.startswith("Delegated task:\n"):
            return reply("Report: nothing found.")
        parent.append(body)
        return call_reply("delegate_task", {"task": "Map it", "agent_type": "explore"}) if len(parent) == 1 else reply()
    bodies = mock_gateway(monkeypatch, respond)
    manager.start(session["id"], "Delegate")
    await asyncio.wait_for(manager.tasks[session["id"]], 10)
    children = [body for body in agent_bodies(bodies) if body not in parent]
    assert children and all(markers(body) == 2 for body in children)
    assert all(markers(body) == 2 for body in parent)
    assert unmarked(children[0])["messages"][-1]["content"].startswith("Delegated task:\n")


def rejects(predicate, message="Unrecognized request argument supplied: cache_control"):
    """A gateway that answers HTTP 400 with message to agent requests for which predicate(body) holds."""
    def respond(body):
        if predicate(body):
            return httpx.Response(400, json={"error_code": "BAD_REQUEST", "message": message})
        return reply()
    return respond


def system_list(body):
    return isinstance(body["messages"][0]["content"], list)


async def test_a_rejection_lowers_the_level_once_and_later_requests_keep_it(runtime, monkeypatch):
    manager, session, _ = runtime
    # Like a route that takes only the documented forms: list content for role system is refused.
    bodies = mock_gateway(monkeypatch, rejects(system_list, "messages[0].content: Input should be a valid string"))
    await manager.run(session, "Hello")
    sent = agent_bodies(bodies)
    assert [markers(body) for body in sent] == [2, 1]
    assert sent[1]["messages"][-1]["content"][0]["cache_control"] == CACHE_CONTROL  # The last user message.
    assert unmarked(sent[0]) == unmarked(sent[1])
    assert notices(session) == [
        f"Databricks rejected the cache markers for {OPUS} (HTTP 400), so the app sent the request again with fewer "
        "cache markers. Requests to this endpoint use that until the app restarts or Settings are saved. To stop "
        "sending cache markers, set Settings → Model and context → Prompt caching to Off."]
    # The notice comes before the reply it explains.
    kinds = [event["type"] for event in session["events"]]
    assert kinds.index("notice") < kinds.index("assistant")
    replies = [event for event in session["events"] if event["type"] == "assistant"]
    assert len(replies) == 1 and replies[0]["text"] == "Done." and replies[0]["request_info"]["status"] == "completed"
    assert replies[0]["request_info"]["http_status"] == 200
    # The rejected attempt's error category stays on its own ledger call, not on the reply that succeeded.
    assert "error_kind" not in replies[0]["request_info"]
    assert "error_kind" not in last_reply(manager.store.get(session["id"]))["request_info"]
    calls = agent_calls(session)
    assert [(call["attempt"], call["status"], call.get("http_status"), call.get("retry_reason"), call["prompt_cache"])
            for call in calls] == [(1, "error", 400, None, "full"), (2, "completed", 200, "cache_rejected", "messages")]
    assert calls[0]["error_kind"] == "invalid_request" and "error_kind" not in calls[1]
    assert session_metrics(session)["calls"][1].get("error_kind") is None
    assert session_metrics(session)["calls"][1]["retry_reason"] == "cache_rejected"
    assert manager.cache_levels == {OPUS: "messages"}
    # The next turn starts at the remembered level: no rejection and no second notice.
    await manager.run(session, "Again")
    assert [markers(body) for body in agent_bodies(bodies)] == [2, 1, 1]
    assert agent_calls(session)[-1]["prompt_cache"] == "messages" and len(notices(session)) == 1
    # A route that refuses every marker: messages, then none, named in the notice when the 400 names caching.
    bodies = mock_gateway(monkeypatch, rejects(lambda body: markers(body) > 0))
    await manager.run(session, "Third")
    assert [markers(body) for body in agent_bodies(bodies)] == [1, 0]
    assert notices(session)[-1].startswith(
        f"Databricks rejected prompt caching (cache_control) for {OPUS} (HTTP 400), so the app sent the request again "
        "without them. Requests to this endpoint use that until")
    assert manager.cache_levels == {OPUS: "off"}
    await manager.run(session, "Fourth")
    assert [markers(body) for body in agent_bodies(bodies)] == [1, 0, 0]
    assert agent_calls(session)[-1]["prompt_cache"] == "off" and len(notices(session)) == 2


async def test_when_the_unmarked_request_fails_too_its_error_is_shown_and_nothing_is_remembered(runtime, monkeypatch):
    manager, session, _ = runtime
    bodies = mock_gateway(monkeypatch, rejects(lambda body: True, "The prompt is too long for this model"))
    await manager.run(session, "Hello")
    assert [markers(body) for body in agent_bodies(bodies)] == [2, 1, 0]
    assert manager.cache_levels == {} and notices(session) == []
    assert session["events"][-1]["type"] == "error" and "The prompt is too long" in session["events"][-1]["text"]
    assert [(call.get("retry_reason"), call["prompt_cache"]) for call in agent_calls(session)] == [
        (None, "full"), ("cache_rejected", "messages"), ("cache_rejected", "off")]
    # The next turn tries the full level again.
    await manager.run(session, "Again")
    assert [markers(body) for body in agent_bodies(bodies)][3] == 2


async def test_a_lowered_request_that_is_rate_limited_keeps_its_own_retries(runtime, monkeypatch):
    manager, session, _ = runtime
    monkeypatch.setattr("local_agent.agents.transient_retry_delay", lambda error, attempt: 0)
    answers = [httpx.Response(400, json={"message": "cache_control: unsupported"}),
               httpx.Response(429, json={"error_code": "REQUEST_LIMIT_EXCEEDED", "message": "Slow down."}), reply()]
    bodies = mock_gateway(monkeypatch, lambda body: answers.pop(0))
    await manager.run(session, "Hello")
    assert [markers(body) for body in agent_bodies(bodies)] == [2, 1, 1]
    assert [call.get("retry_reason") for call in agent_calls(session)] == [None, "cache_rejected", "rate_limit"]
    assert manager.cache_levels == {OPUS: "messages"}
    assert sum("prompt caching (cache_control)" in text for text in notices(session)) == 1


async def test_a_rate_limit_before_a_rejection_leaves_no_error_on_the_answered_attempt(runtime, monkeypatch):
    manager, session, _ = runtime
    monkeypatch.setattr("local_agent.agents.transient_retry_delay", lambda error, attempt: 0)
    answers = [httpx.Response(429, json={"error_code": "REQUEST_LIMIT_EXCEEDED", "message": "Slow down."}),
               httpx.Response(400, json={"message": "cache_control: unsupported"}), reply()]
    mock_gateway(monkeypatch, lambda body: answers.pop(0))
    await manager.run(session, "Hello")
    calls = agent_calls(session)
    assert [(call["status"], call.get("http_status"), call.get("error_kind"), call.get("retry_reason"))
            for call in calls] == [("error", 429, "rate_limit", None), ("error", 400, "invalid_request", "rate_limit"),
                                   ("completed", 200, None, "cache_rejected")]
    info = last_reply(session)["request_info"]
    assert info["status"] == "completed" and info["http_status"] == 200
    assert "error_kind" not in info and "rate_limit" not in info


def network_after_rejection(*kinds, message="messages[0].content: Input should be a valid string"):
    """Agent requests answered in turn by kinds: "400" (a rejection), "connect" (no connection), "timeout" (no data
    before the headers) or "ok"."""
    answers = list(kinds)

    def respond(body):
        kind = answers.pop(0)
        if kind == "400":
            return httpx.Response(400, json={"message": message})
        if kind == "connect":
            raise httpx.ConnectError("connection refused")
        if kind == "timeout":
            raise httpx.ReadTimeout("timed out")
        return reply()
    return respond


async def test_a_connection_failure_on_the_resend_is_retried_at_the_same_level(runtime, monkeypatch):
    manager, session, _ = runtime
    monkeypatch.setattr("local_agent.agents.transient_retry_delay", lambda error, attempt: 0)
    bodies = mock_gateway(monkeypatch, network_after_rejection("400", "connect", "ok"))
    await manager.run(session, "Hello")
    # A failed connection is no second rejection: the same level is sent again, and only it is remembered.
    assert [markers(body) for body in agent_bodies(bodies)] == [2, 1, 1]
    assert [(call["status"], call.get("http_status"), call.get("error_kind"), call.get("retry_reason"),
             call["prompt_cache"]) for call in agent_calls(session)] == [
        ("error", 400, "invalid_request", None, "full"), ("error", None, "network", "cache_rejected", "messages"),
        ("completed", 200, None, "network", "messages")]
    assert manager.cache_levels == {OPUS: "messages"}
    texts = notices(session)
    assert len(texts) == 2 and texts[0].startswith("The connection to Databricks failed before the reply started.")
    assert texts[1].startswith(f"Databricks rejected the cache markers for {OPUS} (HTTP 400), so the app sent the "
                               "request again with fewer cache markers.")
    assert last_reply(session)["text"] == "Done." and "error_kind" not in last_reply(session)["request_info"]


async def test_the_reply_shows_running_again_while_the_resend_is_sent(runtime, monkeypatch):
    manager, session, _ = runtime
    shown, seen = {}, []  # The reply's request details the page was last sent; those at each request.
    respond = network_after_rejection("400", "ok")
    broadcast = manager.broadcast

    async def page(session_id, data):
        if data.get("type") == "event" and data["event"]["type"] == "assistant":
            shown.update(info=dict(data["event"]["request_info"]))
        await broadcast(session_id, data)

    def watch(body):
        seen.append((dict(last_reply(manager.store.get(session["id"]))["request_info"]), shown.get("info", {})))
        return respond(body)
    monkeypatch.setattr(manager, "broadcast", page)
    mock_gateway(monkeypatch, watch)
    await manager.run(session, "Hello")
    # The resend's details (stored and on the page) are not the rejected attempt's status and error.
    assert [[{key: info.get(key) for key in ("status", "http_status", "error_kind")} for info in pair]
            for pair in seen] == [[{"status": "running", "http_status": None, "error_kind": None}] * 2] * 2


async def test_a_connection_failure_on_the_unmarked_resend_keeps_the_network_retries(runtime, monkeypatch):
    manager, session, _ = runtime
    monkeypatch.setattr("local_agent.agents.transient_retry_delay", lambda error, attempt: 0)
    bodies = mock_gateway(monkeypatch, network_after_rejection(
        "400", "400", "connect", "ok", message="Unrecognized request argument supplied: cache_control"))
    await manager.run(session, "Hello")
    assert [markers(body) for body in agent_bodies(bodies)] == [2, 1, 0, 0]
    assert [call.get("http_status") for call in agent_calls(session)] == [400, 400, None, 200]
    assert manager.cache_levels == {OPUS: "off"} and session["events"][-1]["type"] != "error"


async def test_a_read_timeout_on_the_resend_is_not_sent_again(runtime, monkeypatch):
    manager, session, _ = runtime
    bodies = mock_gateway(monkeypatch, network_after_rejection("400", "timeout", "ok"))
    await manager.run(session, "Hello")
    # Databricks may still be generating (and billing) a request that timed out: never resent, nothing remembered.
    assert [markers(body) for body in agent_bodies(bodies)] == [2, 1]
    assert [(call.get("http_status"), call.get("error_kind")) for call in agent_calls(session)] == [
        (400, "invalid_request"), (None, "network")]
    assert manager.cache_levels == {} and notices(session) == []
    assert session["events"][-1]["type"] == "error" and "sent no data" in session["events"][-1]["text"]


async def test_a_reasoning_field_rejection_is_handled_first(runtime, monkeypatch):
    manager, session, _ = runtime
    session["reasoning_effort"] = "high"

    def respond(body):
        if "output_config" in body:
            return httpx.Response(400, json={"message": "output_config: Extra inputs are not permitted"})
        if system_list(body):
            return httpx.Response(400, json={"message": "cache_control is not supported for role system"})
        return reply()
    bodies = mock_gateway(monkeypatch, respond)
    await manager.run(session, "Hello")
    sent = agent_bodies(bodies)
    assert [("output_config" in body, markers(body)) for body in sent] == [(True, 2), (False, 2), (False, 1)]
    assert [call.get("retry_reason") for call in agent_calls(session)] == [None, "reasoning_rejected", "cache_rejected"]
    assert manager.reasoning_rejections == {(OPUS, "anthropic"): "output_config.effort"}
    assert manager.cache_levels == {OPUS: "messages"}
    texts = notices(session)
    assert len(texts) == 2 and texts[0].startswith("Databricks rejected the reasoning field output_config.effort")
    assert texts[1].startswith(f"Databricks rejected prompt caching (cache_control) for {OPUS} (HTTP 400)")


async def test_a_rejection_that_names_caching_goes_down_the_cache_levels_before_the_reasoning_fields(runtime, monkeypatch):
    manager, session, _ = runtime
    session["reasoning_effort"] = "high"
    # The 400 names cache_control and also a reasoning field: the markers are dropped, the reasoning fields kept.
    bodies = mock_gateway(monkeypatch, rejects(lambda body: markers(body) > 0,
                                               "cache_control is not supported together with thinking"))
    await manager.run(session, "Hello")
    sent = agent_bodies(bodies)
    assert [(markers(body), "thinking" in body, "output_config" in body) for body in sent] == [
        (2, True, True), (1, True, True), (0, True, True)]
    assert [call.get("retry_reason") for call in agent_calls(session)] == [None, "cache_rejected", "cache_rejected"]
    assert manager.reasoning_rejections == {} and manager.cache_levels == {OPUS: "off"}
    texts = notices(session)
    assert len(texts) == 1 and texts[0].startswith(f"Databricks rejected prompt caching (cache_control) for {OPUS}")
    # Once no markers go out, a 400 that names a reasoning field takes the reasoning branch as before.
    bodies = mock_gateway(monkeypatch, rejects(lambda body: "thinking" in body, "thinking: cache_control unsupported"))
    await manager.run(session, "Again")
    assert [("thinking" in body, markers(body)) for body in agent_bodies(bodies)] == [(True, 0), (False, 0)]
    assert manager.reasoning_rejections == {(OPUS, "anthropic"): "thinking"}


async def test_documented_forms_only_sends_no_system_or_tool_result_marker(runtime, monkeypatch):
    manager, session, project = runtime
    manager.settings.values["prompt_caching"] = "messages"
    (project / "note.txt").write_text("first\n")
    script = [lambda: call_reply("read_file", {"path": "note.txt"}), reply]
    bodies = mock_gateway(monkeypatch, lambda body: script.pop(0)())
    await manager.run(session, "Read the note")
    first, second = agent_bodies(bodies)
    for body in (first, second):
        assert markers(body) == 1 and isinstance(body["messages"][0]["content"], str)
        assert all(isinstance(message["content"], str) for message in body["messages"] if message["role"] == "tool")
    assert first["messages"][-1]["content"] == [{"type": "text", "text": "Read the note", **MARK}]
    assert second["messages"][-1]["role"] == "tool"
    calls = next(message for message in reversed(second["messages"]) if message.get("tool_calls"))["tool_calls"]
    assert calls[-1]["cache_control"] == CACHE_CONTROL
    assert [call["prompt_cache"] for call in agent_calls(session)] == ["messages", "messages"]
    # A rejection from there goes to no markers, as from Automatic.
    bodies = mock_gateway(monkeypatch, rejects(lambda body: markers(body) > 0))
    await manager.run(session, "Again")
    assert [markers(body) for body in agent_bodies(bodies)] == [1, 0]
    assert manager.cache_levels == {OPUS: "off"} and manager.cache_level(session) == "off"


async def test_the_tail_change_is_flagged_on_the_attempt_that_is_answered(runtime, monkeypatch):
    manager, session, _ = runtime
    monkeypatch.setattr("local_agent.agents.transient_retry_delay", lambda error, attempt: 0)
    answers = [reply(), httpx.Response(429, json={"error_code": "REQUEST_LIMIT_EXCEEDED", "message": "Slow down."}),
               reply(), reply()]
    mock_gateway(monkeypatch, lambda body: answers.pop(0))
    await manager.run(session, "Hello")
    session["allowed_directories"] = ["/srv/other"]  # The system message's changing part differs from here on.
    await manager.run(session, "Again")
    await manager.run(session, "Once more")
    calls = agent_calls(session)
    # Every attempt of the step after the change carries the flag (the 429 wrote nothing to the cache); the
    # step after the answered one compares with it.
    assert [(call.get("http_status"), call.get("retry_reason"), call.get("system_tail_changed")) for call in calls] == [
        (200, None, None), (429, None, True), (200, "rate_limit", True), (200, None, None)]


# Settings → Prompt caching

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


async def test_the_setting_defaults_to_automatic_is_validated_and_a_save_restores_the_full_level(api):
    client, manager, settings = api
    assert (await client.get("/api/settings")).json()["prompt_caching"] == "auto"
    base = {key: settings.values[key] for key in ("workspace", "model", "env_file")}
    manager.cache_levels[OPUS] = "off"
    saved = await client.put("/api/settings", json={**base, "prompt_caching": "off"})
    assert saved.status_code == 200 and saved.json()["prompt_caching"] == "off"
    assert json.loads(settings.path.read_text())["prompt_caching"] == "off"
    assert Settings(settings.state_dir).values["prompt_caching"] == "off"
    assert manager.cache_levels == {}  # Rejected markers are tried again after any Settings save.
    assert manager.cache_level({"model": OPUS}) == "off" and manager.cache_level({"model": GPT}) is None
    for bad in ("on", "full", True, 1):
        assert (await client.put("/api/settings", json={**base, "prompt_caching": bad})).status_code == 422, bad
    with pytest.raises(ValueError, match="Prompt caching must be one of: auto, messages, off"):
        settings.update({"prompt_caching": "full"})
    # Documented forms only: the "messages" level, or none once a rejection lowered the endpoint to none.
    saved = await client.put("/api/settings", json={**base, "prompt_caching": "messages"})
    assert saved.status_code == 200 and saved.json()["prompt_caching"] == "messages"
    assert manager.cache_level({"model": OPUS}) == "messages" and manager.cache_level({"model": GPT}) is None
    manager.cache_levels[OPUS] = "messages"
    assert manager.cache_level({"model": OPUS}) == "messages"
    manager.cache_levels[OPUS] = "off"
    assert manager.cache_level({"model": OPUS}) == "off"
    assert (await client.put("/api/settings", json={**base, "prompt_caching": "auto"})).status_code == 200
    manager.cache_levels[OPUS] = "messages"
    assert manager.cache_level({"model": OPUS}) == "messages"
    assert (await client.put("/api/settings", json=base)).status_code == 200
    assert manager.cache_level({"model": OPUS}) == "full"
    limits = (await client.post("/api/settings/limits", json={"model": OPUS, "context_window": 200_000,
                                                               "max_output_tokens": 16_384})).json()
    assert limits["profile"]["prompt_caching"] is True


# The response's shape in the ledger (R4)

def test_usage_keys_are_bounded_names_only():
    value = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cache_read_input_tokens": None,
             "prompt_tokens_details": {"cached_tokens": 0, "Odd Key": 1}, "Bad-Name": 3, "x" * 65: 1}
    assert usage_keys(value) == ["cache_read_input_tokens", "completion_tokens", "prompt_tokens", "prompt_tokens_details",
                                 "prompt_tokens_details.cached_tokens", "total_tokens"]
    many = {f"key_{index:02d}": index for index in range(17)}
    assert usage_keys(many) == [f"key_{index:02d}" for index in range(16)]
    assert usage_keys({"b": 1}, ["a", "BAD", 3]) == ["a", "b"] and usage_keys(None) == [] and usage_keys([1]) == []
    call = finish_inference_call({}, {"status": "completed", "usage_keys": ["a", "b c", "z"], "reasoning_shape": "other",
                                      "reasoning_signature": "yes"})
    assert call["usage_keys"] == ["a", "z"] and "reasoning_shape" not in call and "reasoning_signature" not in call
    public = session_metrics({"inference_ledger_version": 1, "inference_calls": [
        {"id": "1", "purpose": "agent", "model": OPUS, "created": "2026-10-16T00:00:00Z", "status": "completed",
         "usage_keys": ["prompt_tokens"], "reasoning_shape": "reasoning_content", "reasoning_signature": True,
         "prompt_cache": "messages", "system_tail_changed": True},
        {"id": "2", "purpose": "agent", "model": OPUS, "created": "2026-10-16T00:00:01Z", "status": "completed",
         "usage_keys": ["Bad"], "reasoning_shape": 3, "reasoning_signature": 1, "prompt_cache": "half",
         "system_tail_changed": "yes"}]})["calls"]
    assert {key: public[0][key] for key in ("usage_keys", "reasoning_shape", "reasoning_signature", "prompt_cache",
                                            "system_tail_changed")} == {
        "usage_keys": ["prompt_tokens"], "reasoning_shape": "reasoning_content", "reasoning_signature": True,
        "prompt_cache": "messages", "system_tail_changed": True}
    assert not {"usage_keys", "reasoning_shape", "reasoning_signature", "prompt_cache", "system_tail_changed"} & set(public[1])


async def test_the_ledger_keeps_the_usage_keys_and_reasoning_shape_but_no_signature(runtime, monkeypatch):
    manager, session, _ = runtime
    signature = "secret-signature-value"
    chunks = [{"choices": [{"delta": {"content": [{"type": "reasoning", "summary": [
                  {"type": "summary_text", "text": "Plan.", "signature": signature}]}]}, "finish_reason": None}]},
              {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 100, "completion_tokens": 5}},
              {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 9, "total_tokens": 109,
                                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}]
    text = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    mock_gateway(monkeypatch, lambda body: httpx.Response(200, text=text))
    await manager.run(session, "Hello")
    call = agent_calls(session)[0]
    assert call["usage_keys"] == ["cache_creation_input_tokens", "cache_read_input_tokens", "completion_tokens",
                                  "prompt_tokens", "total_tokens"]
    assert call["reasoning_shape"] == "content.reasoning" and call["reasoning_signature"] is True
    assert signature not in json.dumps(manager.store.get(session["id"]))
    shown = session_metrics(session)["calls"][0]
    assert shown["usage_keys"] == call["usage_keys"] and shown["reasoning_shape"] == "content.reasoning"


def test_the_readme_cache_prices_match_the_rate_table():
    text = " ".join(README.read_text(encoding="utf-8").split())
    rates = MODEL_DBU_PER_MILLION[OPUS]
    assert (f"cache reads cost {rates['cache_read_input_tokens']} DBU per million tokens on Claude Opus 5.5 instead of "
            f"{rates['input_tokens']} for uncached input, and cache writes {rates['cache_creation_input_tokens']}") in text
