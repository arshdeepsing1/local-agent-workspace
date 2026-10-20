import asyncio
import json

import httpx
import pytest

from local_agent.agents import AgentManager, retry_after_seconds
from local_agent.config import Settings
from local_agent.store import Store
from local_agent.telemetry import reported_usage, stream_error_kind


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    settings.redact = lambda text: text.replace("private-token", "[REDACTED]")
    store = Store(tmp_path / "tests.sqlite3")
    yield AgentManager(store, settings), store.create(settings.values)
    store.db.close()


def chunk(text="Done.", finish="stop", **fields):
    return {"choices": [{"delta": {"content": text}, "finish_reason": finish}], **fields}


def sse(*chunks):
    return "".join("data: " + json.dumps(item) + "\n\n" for item in chunks) + "data: [DONE]\n\n"


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(gateway)))


def request_event(session):
    return next(event for event in reversed(session["events"]) if "request_info" in event)


def test_usage_only_accepts_reported_nonnegative_integer_fields():
    assert reported_usage(None) == {}
    assert reported_usage({"prompt_tokens": False, "completion_tokens": -1, "total_tokens": "10",
                           "reasoning_tokens": 1.5, "secret": "private-token"}) == {}
    assert reported_usage({"input_tokens": 0, "output_tokens": 5,
                           "cache_creation_input_tokens": 0, "prompt_tokens_details": {"cached_tokens": 2},
                           "completion_tokens_details": {"reasoning_tokens": 3}}) == {
        "input_tokens": 0, "output_tokens": 5, "cache_creation_input_tokens": 0,
                           "cache_read_input_tokens": 2, "reasoning_tokens": 3}


def test_openai_style_cached_tokens_are_not_counted_twice():
    # prompt_tokens includes cached tokens: input_tokens keeps the uncached part.
    openai = reported_usage({"prompt_tokens": 1000, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 800}})
    assert openai == {"input_tokens": 200, "output_tokens": 5, "cache_read_input_tokens": 800}
    assert reported_usage(openai) == openai  # Stored records are not split again.
    # Claude-style buckets under input_tokens are already separate.
    assert reported_usage({"input_tokens": 1000, "cache_read_input_tokens": 800}) == {
        "input_tokens": 1000, "cache_read_input_tokens": 800}
    assert reported_usage({"input_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 800}}) == {
        "input_tokens": 1000, "cache_read_input_tokens": 800}
    # A cached count above the prompt count is not a subset of it: nothing is subtracted.
    assert reported_usage({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 800}}) == {
        "input_tokens": 100, "cache_read_input_tokens": 800}


def test_databricks_claude_prompt_tokens_include_the_top_level_cache_counts():
    # Databricks' Claude route: prompt_tokens includes the cache read and write counts, and total_tokens is
    # prompt_tokens plus completion_tokens. The cache counts stay separate buckets; input_tokens keeps the rest.
    usage = reported_usage({"prompt_tokens": 165465, "completion_tokens": 670, "total_tokens": 166135,
                            "cache_read_input_tokens": 158527, "cache_creation_input_tokens": 2196})
    assert usage == {"input_tokens": 4742, "output_tokens": 670, "total_tokens": 166135,
                     "cache_read_input_tokens": 158527, "cache_creation_input_tokens": 2196}
    assert reported_usage(usage) == usage  # Stored records are not split again.
    # A cache miss writes everything: the uncached part is what follows the marker.
    assert reported_usage({"prompt_tokens": 154048, "completion_tokens": 1582, "total_tokens": 155630,
                           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 154046})["input_tokens"] == 2
    # Nothing cached, or cache counts above prompt_tokens (not a subset of it): nothing is subtracted.
    assert reported_usage({"prompt_tokens": 1000, "total_tokens": 1000, "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0})["input_tokens"] == 1000
    assert reported_usage({"prompt_tokens": 100, "total_tokens": 100, "cache_read_input_tokens": 800})["input_tokens"] == 100
    # A total_tokens that counts the cache on top of prompt_tokens, or none, keeps the buckets separate.
    assert reported_usage({"prompt_tokens": 1000, "completion_tokens": 5, "total_tokens": 1805,
                           "cache_read_input_tokens": 800})["input_tokens"] == 1000
    assert reported_usage({"prompt_tokens": 50000, "cache_creation_input_tokens": 10000})["input_tokens"] == 50000
    # OpenAI-style cached tokens and a top-level cache write: each is taken out once.
    mixed = {"prompt_tokens": 1000, "completion_tokens": 5, "total_tokens": 1005,
             "prompt_tokens_details": {"cached_tokens": 300}, "cache_creation_input_tokens": 200}
    assert reported_usage(mixed) == {"input_tokens": 500, "output_tokens": 5, "total_tokens": 1005,
                                     "cache_read_input_tokens": 300, "cache_creation_input_tokens": 200}


def test_ledger_records_that_counted_the_cache_twice_are_repaired_once_on_load(tmp_path):
    keys = ["cache_creation_input_tokens", "cache_read_input_tokens", "completion_tokens", "prompt_tokens", "total_tokens"]
    doubled = {"id": "a", "purpose": "agent", "model": "databricks-claude-opus-5-5", "status": "completed",
               "estimated_input_tokens": 120000, "usage_keys": keys,
               "usage": {"input_tokens": 154048, "output_tokens": 1582, "total_tokens": 155630,
                         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 154046}}
    # Saved by this version (already split), or without the raw keys to tell: kept as they are.
    split = {**doubled, "id": "b", "usage": {**doubled["usage"], "input_tokens": 2}}
    unknown = {**doubled, "id": "c", "usage_keys": ["input_tokens", "cache_read_input_tokens"], "usage": dict(doubled["usage"])}
    store = Store(tmp_path / "tests.sqlite3")
    session = store.create({"workspace": str(tmp_path), "model": "databricks-claude-opus-5-5"})
    session["inference_calls"] = [json.loads(json.dumps(call)) for call in (doubled, split, unknown)]
    session["context_info"] = {"estimated_tokens": 130000, "input_budget": 181568, "context_window": 200000,
                               "last_request": {"call_id": "a", "model": "databricks-claude-opus-5-5", "status": "completed",
                                                "input_tokens": 308094, "context_window": 200000, "input_budget": 181568,
                                                "auto_compact_threshold": 163411, "auto_compact_percent": 90,
                                                "compactions": 1, "estimated_tokens": 132570, "estimate_scale": 1.16,
                                                "model_window": 1000000, "percent_of_input_budget": 169.7}}
    store.save(session)
    loaded = Store(tmp_path / "tests.sqlite3").get(session["id"])
    assert [call["usage"]["input_tokens"] for call in loaded["inference_calls"]] == [2, 2, 154048]
    report = loaded["context_info"]["last_request"]
    assert report["input_tokens"] == 154048 and report["uncached_input_tokens"] == 2
    assert report["percent_of_input_budget"] == 84.8 and report["percent_of_model_window"] == 15.4
    assert report["compactions"] == 1 and report["estimated_tokens"] == 132570 and report["next_estimate_scale"] > 1
    store.save(loaded)  # Saved repaired, then loaded again: not split a second time.
    again = Store(tmp_path / "tests.sqlite3").get(session["id"])
    assert [call["usage"]["input_tokens"] for call in again["inference_calls"]] == [2, 2, 154048]
    assert again["context_info"]["last_request"]["input_tokens"] == 154048


def test_retry_after_accepts_seconds_http_date_and_nested_json_with_a_cap(monkeypatch):
    assert retry_after_seconds({"retry-after": "3"}, 0) == 3
    assert retry_after_seconds({}, 0, {"error": {"retry_after": 7}}) == 7
    monkeypatch.setattr("local_agent.agents.time.time", lambda: 0)
    assert retry_after_seconds({"retry-after": "Thu, 01 Jan 1970 00:02:00 GMT"}, 0) == 60
    assert retry_after_seconds({}, 1) == 15


@pytest.mark.parametrize("partial", [False, True])
async def test_usage_only_chunks_replace_snapshots_without_summing_or_defaults(runtime, monkeypatch, partial):
    manager, session = runtime
    initial = {"prompt_tokens": 20, "completion_tokens": 1, "total_tokens": 21}
    final = {"completion_tokens": 10} if partial else {
        "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 5, "reasoning_tokens": 3}

    async def gateway(request):
        assert "stream_options" not in json.loads(request.content)
        return httpx.Response(200, text=sse(chunk("Almost", None, usage=initial), chunk(usage=initial),
            {"choices": [], "usage": final}, {"choices": [], "usage": final},
            {"choices": [], "usage": None}, {"choices": [], "usage": {"total_tokens": -5}}))

    mock_gateway(monkeypatch, gateway)
    await manager.run_databricks(session, "Hello")
    info = request_event(session)["request_info"]
    assert info == {"model": session["model"], "status": "completed", "http_status": 200,
                    "finish_reason": "stop", "usage": reported_usage(final)}
    assert manager.store.get(session["id"])["events"][-1]["request_info"] == info


async def test_missing_usage_and_model_switch_keep_per_request_identity(runtime, monkeypatch):
    manager, session = runtime
    urls = []

    async def gateway(request):
        urls.append(str(request.url))
        return httpx.Response(200, text=sse(chunk()))

    mock_gateway(monkeypatch, gateway)
    session["model"] = "first-model"
    await manager.run_databricks(session, "First request")
    session["model"] = "second-model"
    await manager.run_databricks(session, "Second request")
    infos = [event["request_info"] for event in session["events"] if "request_info" in event]
    assert [info["model"] for info in infos] == ["first-model", "second-model"]
    assert "first-model" in urls[0] and "second-model" in urls[1]
    assert all("usage" not in info for info in infos)


async def test_request_attempt_is_durable_before_provider_admission(runtime, monkeypatch):
    manager, session = runtime

    async def gateway(request):
        saved = manager.store.get(session["id"])
        call = saved["inference_calls"][-1]
        assert call["purpose"] == "agent"
        assert call["status"] == "running"
        assert "finished" not in call
        return httpx.Response(200, text=sse(chunk()))

    mock_gateway(monkeypatch, gateway)
    await manager.run_databricks(session, "Hello")
    assert manager.store.get(session["id"])["inference_calls"][-1]["status"] == "completed"


@pytest.mark.parametrize("status, kind", [(400, "invalid_request"), (401, "authentication"),
    (403, "permission"), (500, "server")])
async def test_http_failures_are_typed_redacted_and_not_retried(runtime, monkeypatch, status, kind):
    manager, session = runtime
    requests = []

    async def gateway(request):
        requests.append(request)
        return httpx.Response(status, text="Provider error private-token")

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    info = request_event(session)["request_info"]
    assert info == {"model": session["model"], "status": "error", "http_status": status, "error_kind": kind}
    assert len(requests) == 1
    error = session["events"][-1]
    assert error["type"] == "error" and error["error_kind"] == kind and error["http_status"] == status
    assert "[REDACTED]" in error["text"]
    assert "private-token" not in json.dumps(manager.store.get(session["id"]))


async def test_a_plain_text_429_is_retried_typed_and_redacted(runtime, monkeypatch):
    # The status alone means rate limited (RL3); its text is the message kept, redacted.
    manager, session = runtime
    manager.settings.values["rate_limit_auto_continue"] = False
    requests, delays = [], []

    async def gateway(request):
        requests.append(request)
        return httpx.Response(429, text="Provider error private-token")

    async def sleep(delay):
        delays.append(delay)
    mock_gateway(monkeypatch, gateway)
    monkeypatch.setattr("local_agent.agents.asyncio.sleep", sleep)
    await manager.run(session, "Hello")
    assert len(requests) == 4 and delays == [5.0, 15.0, 40.0]
    info = request_event(session)["request_info"]
    assert info == {"model": session["model"], "status": "error", "http_status": 429, "error_kind": "rate_limit",
                    "rate_limit": {"message": "Provider error [REDACTED]"}}
    error = session["events"][-1]
    assert error["type"] == "error" and error["error_kind"] == "rate_limit" and error["http_status"] == 429
    assert "[REDACTED]" in error["text"]
    calls = session["inference_calls"]
    assert all(call["rate_limit_message"] == "Provider error [REDACTED]" and call["retry_after_header"] is False
               and "retry_after" not in call and "limit_type" not in call for call in calls)
    assert "private-token" not in json.dumps(manager.store.get(session["id"]))


def test_an_in_stream_request_limit_error_is_labelled_a_rate_limit():
    # Labelled, but never retried: part of the reply may already have streamed.
    assert stream_error_kind({"error_code": "REQUEST_LIMIT_EXCEEDED", "message": "slow down"}) == "rate_limit"
    assert stream_error_kind({"code": "request_limit_exceeded"}) == "rate_limit"
    assert stream_error_kind({"error_code": "SOMETHING_ELSE"}) == "unknown"


async def test_pre_admission_rate_limit_retries_same_request_without_duplicate_reply(runtime, monkeypatch):
    manager, session = runtime
    requests, delays = [], []

    async def gateway(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(429, json={
                "error": {
                    "error_code": "REQUEST_LIMIT_EXCEEDED",
                    "message": "Exceeded workspace input tokens per minute rate limit",
                    "retry_after": 0,
                },
            })
        return httpx.Response(200, text=sse(chunk()))

    async def sleep(delay):
        delays.append(delay)

    mock_gateway(monkeypatch, gateway)
    monkeypatch.setattr("local_agent.agents.asyncio.sleep", sleep)
    assert await manager.run_databricks(session, "Hello") is True

    replies = [event for event in session["events"] if event["type"] == "assistant"]
    assert len(requests) == 2 and delays == [0]
    assert len(replies) == 1 and replies[0]["request_info"]["status"] == "completed"
    assert not any(event["type"] == "error" for event in session["events"])
    assert any(event["type"] == "notice" and "rate limited the model request" in event["text"]
               for event in session["events"])
    calls = session["inference_calls"]
    assert len(calls) == 2
    assert [(call["purpose"], call["attempt"], call["status"], call.get("http_status"))
            for call in calls] == [
                ("agent", 1, "error", 429), ("agent", 2, "completed", 200)]
    assert calls[0]["error_kind"] == "rate_limit"


async def test_pre_admission_rate_limit_stops_after_bounded_retries(runtime, monkeypatch):
    manager, session = runtime
    requests, delays = [], []

    async def gateway(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "0"}, json={
            "error_code": "REQUEST_LIMIT_EXCEEDED",
            "message": "Exceeded workspace input tokens per minute rate limit",
        })

    async def sleep(delay):
        delays.append(delay)

    mock_gateway(monkeypatch, gateway)
    monkeypatch.setattr("local_agent.agents.asyncio.sleep", sleep)
    manager.settings.values["rate_limit_auto_continue"] = False  # The waits after these are in test_transient_retries.
    await manager.run(session, "Hello")

    assert len(requests) == 4 and delays == [0, 0, 0]
    reply = next(event for event in session["events"] if event["type"] == "assistant")
    assert reply["request_info"]["status"] == "error"
    assert reply["request_info"]["error_kind"] == "rate_limit"
    assert session["events"][-1]["type"] == "error"
    assert session["events"][-1]["error_kind"] == "rate_limit"


@pytest.mark.parametrize("kind, body", [
    ("output_limit", sse(chunk("Partial", "length", usage={"completion_tokens": 8192}))),
    ("incomplete_response", sse(chunk("Partial", None))),
    ("invalid_response", 'data: {"choices":['),
    ("invalid_response", sse({"choices": None})),
    ("invalid_response", sse(chunk("", "stop"))),
    ("invalid_tool_arguments", sse({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "broken", "function": {"name": "write_file", "arguments": '{"path":'}}]},
        "finish_reason": "tool_calls"}]})),
])
async def test_bad_streams_are_typed_and_never_execute_tools(runtime, monkeypatch, kind, body):
    manager, session = runtime
    requests = []

    async def gateway(request):
        requests.append(request)
        return httpx.Response(200, text=body)

    async def execute(*args, **_):
        pytest.fail("A failed response must not execute tools")

    manager.execute_tool = execute
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    info = request_event(session)["request_info"]
    assert info["status"] == "error" and info["error_kind"] == kind and info["http_status"] == 200
    assert session["events"][-1]["error_kind"] == kind
    if kind == "output_limit":
        assert info["finish_reason"] == "length" and info["usage"] == {"output_tokens": 8192}
    # Invalid arguments and a clean end without a finish reason get two hidden retries;
    # an output cutoff gets its two continuations; broken or empty streams none.
    assert len(requests) == (1 if kind == "invalid_response" else 3)
    assert not any(message.get("tool_calls") for message in session["wire"])


@pytest.mark.parametrize("error, kind", [({"type": "rate_limit_error"}, "rate_limit"),
    ({"error_code": "PERMISSION_DENIED"}, "permission"), ({"code": 500}, "server"),
    ({"message": "unrecognized provider failure"}, "unknown")])
async def test_sse_failure_preserves_last_usage_and_redacts_error(runtime, monkeypatch, error, kind):
    manager, session = runtime
    requests = []

    async def gateway(request):
        requests.append(request)
        return httpx.Response(200, text=sse(chunk("Partial", None, usage={"prompt_tokens": 17}),
            {"error": {**error, "message": "private-token"}}))

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    info = request_event(session)["request_info"]
    assert info["status"] == "error" and info["error_kind"] == kind
    assert info["usage"] == {"input_tokens": 17} and info["http_status"] == 200
    assert len(requests) == 1
    assert "private-token" not in json.dumps(manager.store.get(session["id"]))


async def test_network_failure_has_no_invented_status_or_usage(runtime, monkeypatch):
    manager, session = runtime
    requests, delays = [], []

    async def gateway(request):
        requests.append(request)
        raise httpx.ConnectError("Connection failed private-token", request=request)

    async def sleep(delay):
        delays.append(delay)

    mock_gateway(monkeypatch, gateway)
    monkeypatch.setattr("local_agent.agents.asyncio.sleep", sleep)
    await manager.run(session, "Hello")
    # A connection that failed before any response is retried three times.
    assert len(requests) == 4 and delays == [2.0, 5.0, 15.0]
    info = request_event(session)["request_info"]
    assert info == {"model": session["model"], "status": "error", "error_kind": "network"}
    assert "private-token" not in json.dumps(manager.store.get(session["id"]))


async def test_cancelled_stream_persists_partial_usage_and_restart_retains_it(runtime, monkeypatch):
    manager, session = runtime
    received = asyncio.Event()

    class HangingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ("data: " + json.dumps(chunk("Partial", None, usage={"prompt_tokens": 20, "completion_tokens": 2})) + "\n\n").encode()
            received.set()
            await asyncio.Event().wait()

    async def gateway(request):
        return httpx.Response(200, stream=HangingStream())

    mock_gateway(monkeypatch, gateway)
    manager.start(session["id"], "Hello")
    await asyncio.wait_for(received.wait(), 2)
    await manager.stop(session["id"])
    saved = manager.store.get(session["id"])
    info = request_event(saved)["request_info"]
    assert info == {"model": session["model"], "status": "cancelled", "http_status": 200,
                    "usage": {"input_tokens": 20, "output_tokens": 2}}
    AgentManager(manager.store, manager.settings)
    assert request_event(manager.store.get(session["id"]))["request_info"] == info


async def test_cancellation_during_request_event_broadcast_is_finalized(runtime):
    manager, session = runtime
    entered = asyncio.Event()

    async def broadcast(session_id, data):
        if data.get("event", {}).get("request_info", {}).get("status") == "running":
            entered.set()
            await asyncio.Event().wait()

    manager.broadcast = broadcast
    manager.start(session["id"], "Hello")
    await asyncio.wait_for(entered.wait(), 2)
    await manager.stop(session["id"])
    assert request_event(manager.store.get(session["id"]))["request_info"] == {
        "model": session["model"], "status": "cancelled"}


async def test_stopping_tool_execution_does_not_cancel_completed_inference(runtime, monkeypatch):
    manager, session = runtime
    executing = asyncio.Event()

    async def gateway(request):
        return httpx.Response(200, text=sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "read", "function": {"name": "list_files", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 20, "completion_tokens": 5}}))

    async def execute(*args, **_):
        executing.set()
        await asyncio.Event().wait()

    manager.execute_tool = execute
    mock_gateway(monkeypatch, gateway)
    manager.start(session["id"], "List files")
    await asyncio.wait_for(executing.wait(), 2)
    await manager.stop(session["id"])
    info = request_event(manager.store.get(session["id"]))["request_info"]
    assert info["status"] == "completed" and info["finish_reason"] == "tool_calls"
    assert info["usage"] == {"input_tokens": 20, "output_tokens": 5}


async def test_malformed_tail_retains_usage_without_executing_complete_tool_prefix(runtime, monkeypatch):
    manager, session = runtime
    prefix = {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "write", "function": {"name": "write_file", "arguments": '{}'}}]},
        "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 30}}

    async def gateway(request):
        return httpx.Response(200, text="data: " + json.dumps(prefix) + '\n\ndata: {"usage":')

    async def execute(*args, **_):
        pytest.fail("A malformed stream must not execute the complete tool-call prefix")

    manager.execute_tool = execute
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Write file")
    info = request_event(session)["request_info"]
    assert info["status"] == "error" and info["error_kind"] == "invalid_response"
    assert info["finish_reason"] == "tool_calls" and info["usage"] == {"input_tokens": 30}
    assert not any(message.get("tool_calls") for message in session["wire"])


def test_restart_marks_only_inflight_request_metadata_interrupted(runtime):
    manager, session = runtime
    session["events"] = [
        {"type": "assistant", "text": "Old historical response"},
        {"type": "assistant", "text": "Completed", "request_info": {"model": "old", "status": "completed"}},
        {"type": "assistant", "text": "Partial", "request_info": {
            "model": "new", "status": "running", "usage": {"input_tokens": 0}}},
    ]
    manager.store.save(session)
    AgentManager(manager.store, manager.settings)
    events = manager.store.get(session["id"])["events"]
    assert "request_info" not in events[0]
    assert events[1]["request_info"]["status"] == "completed"
    assert events[2]["request_info"] == {"model": "new", "status": "interrupted",
        "error_kind": "incomplete_response", "usage": {"input_tokens": 0}}


def test_a_parents_subagent_index_and_the_cli_report_are_counted_again_once(tmp_path):
    from local_agent.planning import INDEX_USAGE_VERSION
    from local_agent.usage_export import session_from_jsonl, usage_rows
    keys = ["cache_creation_input_tokens", "cache_read_input_tokens", "completion_tokens", "prompt_tokens", "total_tokens"]

    def doubled(call_id, purpose="agent"):
        return {"id": call_id, "purpose": purpose, "model": "databricks-claude-opus-5-5", "status": "completed",
                "created": "2026-10-19T08:00:00Z", "usage_keys": keys,
                "usage": {"input_tokens": 50000, "output_tokens": 100, "total_tokens": 50100,
                          "cache_read_input_tokens": 45000, "cache_creation_input_tokens": 0}}

    store = Store(tmp_path / "tests.sqlite3")
    child = store.create({"workspace": str(tmp_path), "model": "databricks-claude-opus-5-5"})
    # Two calls in the delegated run, then a turn the user continued the subagent's chat in.
    child["inference_calls"] = [doubled("c1"), doubled("c2"), doubled("c3")]
    store.save(child)
    parent = store.create({"workspace": str(tmp_path), "model": "databricks-claude-opus-5-5"})
    stale = {"input_tokens": 150000, "cache_read_input_tokens": 135000}
    parent["subagents"] = [{"id": child["id"], "status": "completed", "estimated_dbu": 13.3, "unpriced_calls": 0,
                            "usage": stale, "turn_spend": {"tokens": 100300, "estimated_dbu": 8.8, "unpriced_calls": 0,
                                                           "finished_calls": 2, "reported_calls": 2}},
                           {"id": "running-one", "status": "running", "usage": dict(stale)}]
    store.save(parent)
    loaded = Store(tmp_path / "tests.sqlite3").get(parent["id"])
    entry, running = loaded["subagents"]
    assert entry["usage_version"] == INDEX_USAGE_VERSION and entry["usage"]["input_tokens"] == 15000
    assert entry["estimated_dbu"] < 13.3 / 2 and entry["turn_spend"]["finished_calls"] == 2
    assert entry["turn_spend"]["tokens"] == 2 * (5000 + 45000 + 100)  # The delegated run's two calls only.
    assert running["usage"] == stale and "usage_version" not in running  # Its live counts are kept until it ends.
    # The CLI usage report repairs the file it reads, like the app.
    path = tmp_path / "conversations" / f"{child['id']}.jsonl"
    assert [row["input_tokens"] for row in usage_rows(session_from_jsonl(path))] == [5000, 5000, 5000]
