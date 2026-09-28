import json

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.model_stream import MAX_ERROR_BODY_BYTES, MAX_SSE_EVENT_BYTES, MAX_TOOL_ARGUMENT_BYTES, MAX_TOOL_CALLS, ToolCallBuffer, sse_data
from local_agent.store import Store
from local_agent.telemetry import InferenceError


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.received = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.received += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("separator", [b"\n", b"\r\n", b"\r"])
async def test_sse_framing_handles_unicode_seams_comments_and_multiline_data(separator):
    encoded = separator.join([b"\xef\xbb\xbf: heartbeat", b"event: message", b'data: {"text":',
        'data: "日😀"}'.encode(), b"", b"data: [DONE]", b"", b""])
    stream = Chunks([encoded[index:index + 1] for index in range(len(encoded))])
    response = httpx.Response(200, stream=stream)
    frames = [value async for value in sse_data(response)]
    assert json.loads(frames[0]) == {"text": "日😀"}
    assert frames[1] == "[DONE]"


@pytest.mark.parametrize("terminated", [False, True])
async def test_oversized_event_is_rejected_before_whole_stream_is_buffered(terminated):
    stream = Chunks([b"data: ", *[b"x" * 4096 for _ in range(MAX_SSE_EVENT_BYTES // 4096 + 20)],
                     b"\n\n" if terminated else b""])
    with pytest.raises(InferenceError, match="SSE event larger") as error:
        _ = [item async for item in sse_data(httpx.Response(200, stream=stream))]
    assert error.value.kind == "invalid_response"
    assert stream.received <= MAX_SSE_EVENT_BYTES // 4096 + 2


async def test_large_complete_transport_chunk_and_multiline_event_are_bounded():
    for raw in (b"data: " + b"x" * MAX_SSE_EVENT_BYTES + b"\n\n",
                (b"data: " + b"x" * 1000 + b"\n") * 1100 + b"\n"):
        with pytest.raises(InferenceError, match="SSE event larger"):
            _ = [item async for item in sse_data(httpx.Response(200, stream=Chunks([raw])))]


@pytest.mark.parametrize("separator", [b"\n", b"\r\n", b"\r"])
async def test_event_byte_boundary_and_unterminated_frame(separator):
    payload_size = MAX_SSE_EVENT_BYTES - 6 - 2 * len(separator)
    accepted = b"data: " + b"x" * payload_size + separator * 2
    result = [item async for item in sse_data(httpx.Response(200, stream=Chunks([accepted])))]
    assert len(result[0]) == payload_size
    for raw in (b"data: " + b"x" * (payload_size + 1) + separator * 2, b'data: {"choices":[]}'):
        with pytest.raises(InferenceError):
            _ = [item async for item in sse_data(httpx.Response(200, stream=Chunks([raw])))]


def test_tool_argument_budget_counts_utf8_across_fragments_and_calls():
    buffer = ToolCallBuffer()
    first = "😀" * (MAX_TOOL_ARGUMENT_BYTES // 8)
    for index in range(2):
        buffer.add({"index": index, "id": f"call-{index}", "function": {"name": "write_file", "arguments": first}})
    assert buffer.argument_bytes == MAX_TOOL_ARGUMENT_BYTES
    with pytest.raises(InferenceError, match="512 KiB") as error:
        buffer.add({"index": 1, "function": {"arguments": "x"}})
    assert error.value.kind == "invalid_tool_arguments"
    assert buffer.argument_bytes == MAX_TOOL_ARGUMENT_BYTES
    assert buffer.finish()[1]["function"]["arguments"] == first


def test_tool_metadata_cannot_bypass_assembly_budget():
    buffer = ToolCallBuffer()
    for index in range(MAX_TOOL_CALLS):
        buffer.add({"index": index})
    with pytest.raises(InferenceError, match="64 tool calls"):
        buffer.add({"index": MAX_TOOL_CALLS})
    for part in ({"index": 0, "id": "x" * 257},
                 {"index": 0, "function": {"name": "x" * 129}},
                 {"index": True}, {"index": -1}, {"index": 0, "function": {"arguments": False}}):
        with pytest.raises(InferenceError):
            ToolCallBuffer().add(part)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(tmp_path / "tests.sqlite3")
    yield AgentManager(store, settings), store.create(settings.values)
    store.db.close()


def event(delta, finish=None, **extra):
    return ("data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}], **extra}) + "\n\n").encode()


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(gateway)))


@pytest.mark.parametrize("overflow", ["frame", "arguments"])
async def test_overflow_preserves_typed_failure_without_executing_valid_prefix_and_can_resume(runtime, monkeypatch, overflow):
    manager, session = runtime
    prefix = event({"tool_calls": [{"index": 0, "id": "valid", "function": {
        "name": "list_files", "arguments": "{}"}}]}, usage={"prompt_tokens": 25})
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        if len(requests) > 1:
            return httpx.Response(200, content=event({"content": "Ready to continue."}, "stop") + b"data: [DONE]\n\n")
        if overflow == "frame":
            tail = [b"data: " + b"x" * MAX_SSE_EVENT_BYTES]
        else:
            tail = [event({"tool_calls": [{"index": 1, "id": "large", "function": {
                "name": "write_file" if number == 0 else "", "arguments": "x" * 65536}}]}) for number in range(9)]
        return httpx.Response(200, stream=Chunks([prefix, *tail]))

    async def execute(*args):
        pytest.fail("No call from an oversized response may execute, including its complete prefix")

    manager.execute_tool = execute
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Inspect the project.")
    assert len(requests) == 1
    info = next(item["request_info"] for item in session["events"] if "request_info" in item)
    assert info["status"] == "error" and info["http_status"] == 200
    assert info["error_kind"] == ("invalid_response" if overflow == "frame" else "invalid_tool_arguments")
    assert info["usage"] == {"input_tokens": 25}
    assert session["events"][-1]["type"] == "error"
    assert not any(message.get("tool_calls") for message in session["wire"])
    saved = manager.store.get(session["id"])
    assert await manager.run_databricks(saved, "Continue in smaller steps.") is True
    assert len(requests) == 2


async def test_maximum_existing_file_size_with_json_escaping_still_executes(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["context_window"] = 1048576
    content = "\x01" * 80_000
    arguments = json.dumps({"path": "large.txt", "content": content})
    calls, requests = [], []

    async def gateway(request):
        requests.append(request)
        if len(requests) == 1:
            chunks = [event({"tool_calls": [{"index": 0, "id": "write", "function": {
                "name": "write_file" if index == 0 else "", "arguments": arguments[index:index + 5000]}}]})
                for index in range(0, len(arguments), 5000)]
            return httpx.Response(200, stream=Chunks([*chunks, event({}, "tool_calls"), b"data: [DONE]\n\n"]))
        return httpx.Response(200, content=event({"content": "Done."}, "stop") + b"data: [DONE]\n\n")

    async def execute(session, tools, name, values, call_id):
        calls.append((name, values, call_id))
        return "File written."

    manager.execute_tool = execute
    mock_gateway(monkeypatch, gateway)
    assert await manager.run_databricks(session, "Write a file.") is True
    assert calls == [("write_file", {"path": "large.txt", "content": content}, "write")]


async def test_large_http_error_stops_reading_closes_stream_and_preserves_redacted_category(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.redact = lambda value: value.replace("private-token", "[REDACTED]")
    stream = Chunks([b"Provider failure private-token " + b"x" * 4096,
                     *[b"x" * 4096 for _ in range(100)]])

    async def gateway(request):
        return httpx.Response(429, stream=stream)

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert stream.received <= MAX_ERROR_BODY_BYTES // 4096
    assert stream.closed
    info = next(item["request_info"] for item in session["events"] if "request_info" in item)
    assert info["http_status"] == 429 and info["error_kind"] == "rate_limit"
    error = session["events"][-1]
    assert "[REDACTED]" in error["text"] and "[Error response truncated.]" in error["text"]
    assert "private-token" not in json.dumps(manager.store.get(session["id"]))
