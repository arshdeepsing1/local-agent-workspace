"""The 4 MB Databricks request limit, request timeouts, the execution-limit notice and a
cache-stable system message (u-large-inputs item 1, addendum items 6 and 7)."""
import json
import random
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from local_agent import agents, model_profiles, request_limits
from local_agent.agents import AgentManager, current_time_text
from local_agent.config import Settings
from local_agent.context import estimate_tokens, prepare_context
from local_agent.request_limits import (PayloadTooLarge, check_payload, encode_payload, request_bytes_over)
from local_agent.store import Store


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    settings.redact = lambda text: text.replace("private-token", "[REDACTED]")
    store = Store(tmp_path / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Limits", title_generated=True)
    yield AgentManager(store, settings), session
    store.db.close()


def sse(text="Done.", finish="stop"):
    return ("data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": finish}]}) + "\n\n"
            + "data: [DONE]\n\n")


def mock_gateway(monkeypatch, gateway, clients=None, streams=None):
    client = httpx.AsyncClient

    def build(**kwargs):
        if clients is not None:
            clients.append(kwargs)
        built = client(**kwargs, transport=httpx.MockTransport(gateway))
        if streams is not None:
            stream = built.stream

            def record(method, url, **options):
                streams.append(options)
                return stream(method, url, **options)
            built.stream = record
        return built
    monkeypatch.setattr(httpx, "AsyncClient", build)


def notices(session):
    return [event["text"] for event in session["events"] if event["type"] == "notice"]


SAMPLES = ["plain ascii", "café é", "中文字符", "emoji 😀🚀", "control \x01\x02\x1f", 'quotes " and \\ backslash',
           "line\nbreak\ttab"]


@pytest.mark.parametrize("text", SAMPLES)
def test_encode_payload_is_compact_utf8_json(text):
    payload = {"messages": [{"role": "user", "content": text}], "stream": True, "max_tokens": 10}
    assert encode_payload(payload) == json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


@pytest.mark.parametrize("text", SAMPLES)
def test_encode_payload_matches_httpx_json_encoding(text):
    if tuple(int(part) for part in httpx.__version__.split(".")[:2]) < (0, 28):
        pytest.skip("httpx before 0.28 encodes json= with ASCII escapes and spaces")
    payload = {"messages": [{"role": "user", "content": text}], "tools": [], "stream": False}
    assert encode_payload(payload) == httpx.Request("POST", "https://x", json=payload).content


def test_the_compact_body_never_exceeds_three_bytes_per_estimated_token():
    rng = random.Random(7)
    alphabet = ["a", "Z", " ", "é", "中", "😀", "\x01", '"', "\\", "\n", "{", "0"]
    tools = [{"type": "function", "function": {"name": "t", "description": "d " * 50, "parameters": {}}}]
    for _ in range(200):
        messages = [{"role": rng.choice(["user", "assistant", "tool"]),
                     "content": "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 400)))}
                    for _ in range(rng.randint(1, 6))]
        body = encode_payload({"messages": messages, "tools": tools, "stream": True, "max_tokens": 131072})
        assert len(body) <= 3 * estimate_tokens(messages, tools)


def test_request_bytes_over_short_circuits_without_encoding(monkeypatch):
    def boom(payload):
        raise AssertionError("encoded")
    monkeypatch.setattr(request_limits, "encode_payload", boom)
    assert request_bytes_over([{"role": "user", "content": "x" * 900}], [], 1000, 3000) is False


def test_request_bytes_over_measures_above_the_bound():
    messages = [{"role": "user", "content": "x" * 5000}]
    assert request_bytes_over(messages, [], 2000, 4000) is True
    assert request_bytes_over(messages, [], 2000, 6000) is False


def test_the_limits_mirror_the_endpoint_facts():
    assert request_limits.PAYLOAD_LIMIT_BYTES == model_profiles.MAX_REQUEST_BYTES
    assert request_limits.EXECUTION_LIMIT_SECONDS == model_profiles.MAX_EXECUTION_SECONDS


def test_check_payload_refuses_over_the_limit(monkeypatch):
    monkeypatch.setattr(request_limits, "PAYLOAD_LIMIT_BYTES", 100)
    with pytest.raises(PayloadTooLarge) as caught:
        check_payload({"messages": [{"role": "user", "content": "x" * 200}]})
    assert caught.value.kind == "payload_too_large" and "Databricks accepts at most 4 MB" in str(caught.value)
    assert check_payload({"messages": []}) == b'{"messages":[]}'


async def test_compaction_also_runs_when_the_request_passes_the_byte_target(monkeypatch):
    monkeypatch.setattr(request_limits, "PAYLOAD_TARGET_BYTES", 60_000)
    wire = []
    for number in range(4):
        wire += [{"role": "user", "content": f"Read file {number}"},
                 {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{number}", "type": "function",
                  "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{number}.txt"})}}]},
                 {"role": "tool", "tool_call_id": f"c{number}", "content": "y" * 30_000},
                 {"role": "assistant", "content": f"File {number} read."}]
    wire.append({"role": "user", "content": "Now summarize them"})
    calls = []

    async def summarize(previous, chunk, limit):
        calls.append(len(chunk))
        return "## Goal and current status\nRead four files."
    system = {"role": "system", "content": "s"}
    before = estimate_tokens([system, *wire])
    messages, state, info = await prepare_context(wire, {}, system, [], 131072, summarize)
    assert before < info["auto_compact_threshold"]  # The token estimate alone would not compact.
    assert calls and info["compactions"] == 1 and state["through"] > 0
    assert len(encode_payload({"messages": messages, "tools": []})) <= 60_000


async def test_a_byte_triggered_compaction_that_cannot_reach_the_target_still_shrinks_the_request(monkeypatch):
    """Within the token budget and over the byte target, with no cut that reaches the target: the
    latest cut that fits is taken when it makes the request smaller (addendum B)."""
    monkeypatch.setattr(request_limits, "PAYLOAD_TARGET_BYTES", 100_000)
    wire = [{"role": "user", "content": "old " + "o" * 90_000}, {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "new " + "n" * 101_000}]
    calls = []

    async def summarize(previous, chunk, limit):
        calls.append(len(chunk))
        return "## Goal and current status\nRead the old text."
    system = {"role": "system", "content": "s"}
    before = len(encode_payload({"messages": [system, *wire], "tools": []}))
    messages, state, info = await prepare_context(wire, {}, system, [], 1_048_576, summarize, reply_reserve=8192)
    after = len(encode_payload({"messages": messages, "tools": []}))
    assert info["estimated_tokens"] < info["auto_compact_threshold"]  # Not a token-triggered compaction.
    assert calls and info["compactions"] == 1 and state["through"] == 2
    # Still over the target (the latest message alone is), but under the 4 MB limit: it is sent.
    assert request_limits.PAYLOAD_TARGET_BYTES < after < before
    assert messages[-1] == wire[-1]


async def test_the_byte_checks_add_no_serialization_of_their_own(monkeypatch):
    """fits() uses the scaled estimate as the bound and the trigger the raw estimate it scales: every other
    estimate_tokens call in a compaction comes from a scaled_estimate."""
    from local_agent import context
    counts = {"estimate": 0, "scaled": 0}
    estimate, scaled = context.estimate_tokens, context.scaled_estimate

    def counted_estimate(*args, **options):
        counts["estimate"] += 1
        return estimate(*args, **options)

    def counted_scaled(*args, **options):
        counts["scaled"] += 1
        return scaled(*args, **options)
    monkeypatch.setattr(context, "estimate_tokens", counted_estimate)
    monkeypatch.setattr(context, "scaled_estimate", counted_scaled)
    wire = [{"role": "user", "content": "Read everything"}]
    for number in range(12):
        wire += [{"role": "assistant", "content": None, "tool_calls": [{"id": f"c{number}", "type": "function",
                  "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{number}.txt"})}}]},
                 {"role": "tool", "tool_call_id": f"c{number}", "content": "y" * 12_000}]

    async def summarize(previous, chunk, limit):
        return "## Goal and current status\nReading files."
    messages, state, info = await prepare_context(wire, {}, {"role": "system", "content": "s"}, [], 32768, summarize,
                                                  reply_reserve=4096)
    assert info["compactions"] == 1 and info.get("compacted_in_turn")
    assert counts["scaled"] > 3 and counts["estimate"] == counts["scaled"] + 1  # The first estimate, taken raw.


async def test_the_measured_body_is_sent_and_shown_before_the_request(runtime, monkeypatch):
    manager, session = runtime
    seen, order = [], []
    broadcast = manager.broadcast

    async def capture(session_id, data):
        if data.get("type") == "context":
            order.append(("context", data["context_info"].get("payload_bytes")))
        return await broadcast(session_id, data)
    monkeypatch.setattr(manager, "broadcast", capture)

    async def gateway(request):
        seen.append(request)
        order.append(("request", len(request.content)))
        return httpx.Response(200, text=sse())
    clients, streams = [], []
    mock_gateway(monkeypatch, gateway, clients, streams)
    await manager.run(session, "Hello é 中")
    request = seen[0]
    assert "json" not in streams[0] and streams[0]["content"] == request.content
    assert request.headers["content-type"] == "application/json"
    info = session["context_info"]
    assert len(request.content) == info["payload_bytes"] and info["payload_limit_bytes"] == 4_000_000
    assert order[0] == ("context", len(request.content)) and order[1] == ("request", len(request.content))
    timeout = clients[0]["timeout"]
    assert timeout.read == 300 and timeout.connect == 20 and timeout.write == 120 and timeout.pool == 20


async def test_a_request_over_the_limit_is_never_sent(runtime, monkeypatch):
    manager, session = runtime
    monkeypatch.setattr(request_limits, "PAYLOAD_LIMIT_BYTES", 1_000)
    seen = []

    async def gateway(request):
        seen.append(request)
        return httpx.Response(200, text=sse())
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert seen == []
    error = session["events"][-1]
    assert error["type"] == "error" and error["error_kind"] == "payload_too_large" and "http_status" not in error
    assert "Databricks accepts at most 4 MB per request, so it was not sent" in error["text"]
    assert session["terminal_reason"] == "inference_error"
    assert not session.get("inference_calls")
    assert session["context_info"]["payload_bytes"] > 1_000


class Client:
    """A stand-in for the compaction client: records each post."""
    def __init__(self):
        self.posts = []

    async def post(self, url, **kwargs):
        self.posts.append(kwargs)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Summary."}, "finish_reason": "stop"}]},
                              request=httpx.Request("POST", url))


async def test_a_compaction_request_is_sent_as_bytes_with_the_long_timeout(runtime):
    manager, session = runtime
    client = Client()
    messages = [{"role": "system", "content": "Summarize"}, {"role": "user", "content": "é history"}]
    assert await manager.summarize_context(session, client, "https://gateway.example/x", {}, messages) == "Summary."
    post = client.posts[0]
    assert "json" not in post and post["content"] == encode_payload(
        {"messages": messages, "stream": False, "max_tokens": 4096})
    assert post["headers"]["Content-Type"] == "application/json"
    assert post["timeout"].read == 600 and post["timeout"].connect == 20


async def test_a_compaction_request_over_the_limit_is_refused_before_it_is_recorded(runtime, monkeypatch):
    manager, session = runtime
    monkeypatch.setattr(request_limits, "PAYLOAD_LIMIT_BYTES", 100)
    client = Client()
    with pytest.raises(ValueError, match="above the Databricks 4 MB request limit, so it was not sent. History is"):
        await manager.summarize_context(session, client, "https://gateway.example/x", {},
                                        [{"role": "user", "content": "x" * 500}])
    assert client.posts == [] and not session.get("inference_calls")


async def test_a_side_question_is_sent_as_bytes_with_the_long_timeout_and_refused_over_the_limit(runtime, monkeypatch):
    manager, session = runtime
    session["wire"] = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello."}]
    manager.store.save(session)
    seen, clients = [], []

    async def gateway(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Yes."}, "finish_reason": "stop"}]})
    mock_gateway(monkeypatch, gateway, clients)
    result = await manager.side_question(session["id"], "Still there?")
    assert result["answer"] == "Yes."
    assert seen[0].headers["content-type"] == "application/json" and json.loads(seen[0].content)["stream"] is False
    assert clients[0]["timeout"].read == 600
    monkeypatch.setattr(request_limits, "PAYLOAD_LIMIT_BYTES", 1_000)
    with pytest.raises(ValueError, match="Databricks accepts at most 4 MB per request"):
        await manager.side_question(session["id"], "And now?")
    assert len(seen) == 1


async def test_a_stream_cut_near_the_execution_limit_gives_one_notice_per_turn(runtime, monkeypatch):
    manager, session = runtime
    ticks = iter([0, 590, 1000, 1590, 2000, 2590, 3000, 3590])
    monkeypatch.setattr(agents, "request_clock", lambda: next(ticks))
    requests = []

    async def gateway(request):
        requests.append(request)
        # No finish reason: the response ended before it was complete.
        return httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": {"content": "Part"}}]})
                              + "\n\ndata: [DONE]\n\n")
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Write the report")
    assert len(requests) == 3  # The request and two hidden retries.
    texts = [text for text in notices(session) if text.startswith("Databricks stops a request after 597 seconds")]
    assert texts == ["Databricks stops a request after 597 seconds, and this one ran 590 seconds. If this repeats, "
                     "lower Settings → Max output tokens or Settings → Context budget."]


async def test_no_execution_notice_for_a_quick_incomplete_response(runtime, monkeypatch):
    manager, session = runtime
    monkeypatch.setattr(agents, "request_clock", lambda: 5.0)

    async def gateway(request):
        return httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": {"content": "Part"}}]})
                              + "\n\ndata: [DONE]\n\n")
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Write the report")
    assert not any("597 seconds" in text for text in notices(session))


class Clock(datetime):
    moment = None

    @classmethod
    def now(cls, tz=None):
        return cls.moment if tz is None else cls.moment.astimezone(tz)


async def test_the_system_message_changes_once_a_day_not_once_a_minute(runtime, monkeypatch):
    manager, session = runtime
    zone = timezone(timedelta(hours=2))
    monkeypatch.setattr(agents, "datetime", Clock)
    systems = []

    async def gateway(request):
        systems.append(json.loads(request.content)["messages"][0]["content"])
        return httpx.Response(200, text=sse())
    mock_gateway(monkeypatch, gateway)
    for moment in (datetime(2026, 10, 13, 10, 0, tzinfo=zone), datetime(2026, 10, 13, 10, 59, tzinfo=zone),
                   datetime(2026, 10, 14, 10, 0, tzinfo=zone)):
        Clock.moment = moment
        await manager.run(session, "Hello")
    assert systems[0] == systems[1]
    first, third = systems[0].split("\n"), systems[2].split("\n")
    changed = [(old, new) for old, new in zip(first, third) if old != new]
    expected = [current_time_text(datetime(2026, 10, day, 10, 0, tzinfo=zone).astimezone()) for day in (13, 14)]
    assert len(first) == len(third) and changed == [tuple(expected)]


async def test_a_stream_read_error_near_the_execution_limit_gives_the_notice(runtime, monkeypatch):
    manager, session = runtime
    ticks = iter([0, 590, 1000, 1590])
    monkeypatch.setattr(agents, "request_clock", lambda: next(ticks))

    class CutStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ("data: " + json.dumps({"choices": [{"delta": {"content": "Part"}}]}) + "\n\n").encode()
            raise httpx.ReadError("The connection was closed.")
    requests = []

    async def gateway(request):
        requests.append(request)
        return httpx.Response(200, stream=CutStream())
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Write the report")
    assert len(requests) == 1  # A failure after a status is not repeated.
    texts = [text for text in notices(session) if text.startswith("Databricks stops a request after 597 seconds")]
    assert texts == ["Databricks stops a request after 597 seconds, and this one ran 590 seconds. If this repeats, "
                     "lower Settings → Max output tokens or Settings → Context budget."]
