"""Automatic retries of model requests that got no usable response, and auto-continue after rate limits.

One rule: a request is repeated unchanged only when no response status arrived
(a connection failure), or the provider rejected it before streaming with a 429
or a 502/503/504. Every test here replaces asyncio.sleep in local_agent.agents.
"""
import asyncio
import json
from datetime import datetime

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.store import Store

RATE_LIMITED = {"error_code": "REQUEST_LIMIT_EXCEEDED", "message": "Exceeded workspace rate limit"}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    settings.redact = lambda text: text.replace("private-token", "[REDACTED]")
    store = Store(tmp_path / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Retries", title_generated=True)  # No title request after the reply.
    yield AgentManager(store, settings), session
    store.db.close()


@pytest.fixture
def delays(monkeypatch):
    waited = []

    async def sleep(delay):
        waited.append(delay)
    monkeypatch.setattr("local_agent.agents.asyncio.sleep", sleep)
    return waited


def sse(text="Done.", finish="stop"):
    return ("data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": finish}]}) + "\n\n"
            + "data: [DONE]\n\n")


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(gateway)))


def notices(session):
    return [event["text"] for event in session["events"] if event["type"] == "notice"]


def agent_calls(session):
    return [call for call in session["inference_calls"] if call["purpose"] == "agent"]


class FailingStream(httpx.AsyncByteStream):
    """A 200 stream that sends its first bytes and then fails."""
    def __init__(self, first, error):
        self.first, self.error = first, error

    async def __aiter__(self):
        yield self.first
        raise self.error


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_gateway_errors_before_streaming_are_retried_once_then_succeed(runtime, monkeypatch, delays, status):
    manager, session = runtime
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return httpx.Response(status, text="Upstream unavailable") if len(requests) == 1 else httpx.Response(200, text=sse())

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 2 and requests[0] == requests[1] and delays == [2.0]
    replies = [event for event in session["events"] if event["type"] == "assistant"]
    assert len(replies) == 1 and replies[0]["text"] == "Done." and replies[0]["request_info"]["status"] == "completed"
    assert notices(session) == [f"Databricks returned HTTP {status} before the reply started. Retrying in 2 seconds (1 of 3)."]
    calls = agent_calls(session)
    assert [(call["attempt"], call["status"], call.get("http_status"), call.get("retry_reason")) for call in calls] == [
        (1, "error", status, None), (2, "completed", 200, "server_error")]
    assert session["terminal_reason"] == "completed"


async def test_server_retry_after_header_is_used_and_capped(runtime, monkeypatch, delays):
    manager, session = runtime
    requests = []

    async def gateway(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, headers={"Retry-After": "7"}, text="busy")
        if len(requests) == 2:
            return httpx.Response(503, headers={"Retry-After": "600"}, text="busy")
        return httpx.Response(200, text=sse())

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert delays == [7.0, 60.0] and len(requests) == 3


async def test_http_500_is_not_retried(runtime, monkeypatch, delays):
    manager, session = runtime
    requests = []

    async def gateway(request):
        requests.append(request)
        return httpx.Response(500, text="Internal error")

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 1 and delays == [] and not notices(session)
    assert session["events"][-1]["type"] == "error" and session["events"][-1]["http_status"] == 500


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                                   httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError])
async def test_connection_failures_before_a_status_are_retried(runtime, monkeypatch, delays, error):
    manager, session = runtime
    requests = []

    async def gateway(request):
        requests.append(request)
        if len(requests) == 1:
            raise error("connection failed", request=request)
        return httpx.Response(200, text=sse())

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 2 and delays == [2.0]
    assert notices(session) == ["The connection to Databricks failed before the reply started. Retrying in 2 seconds (1 of 3)."]
    calls = agent_calls(session)
    assert [(call["status"], call.get("error_kind"), call.get("retry_reason")) for call in calls] == [
        ("error", "network", None), ("completed", None, "network")]


async def test_read_timeout_is_not_retried(runtime, monkeypatch, delays):
    """A ReadTimeout may mean a long, billed generation is still running."""
    manager, session = runtime
    requests = []

    async def gateway(request):
        requests.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 1 and delays == [] and not notices(session)
    assert session["events"][-1]["error_kind"] == "network"


@pytest.mark.parametrize("case", ["read_error_after_200", "sse_error_503", "event_cut_mid_way"])
async def test_nothing_is_repeated_once_a_status_arrived(runtime, monkeypatch, delays, case):
    manager, session = runtime
    requests = []
    partial = ("data: " + json.dumps({"choices": [{"delta": {"content": "Part"}, "finish_reason": None}]}) + "\n\n").encode()

    async def gateway(request):
        requests.append(request)
        if case == "read_error_after_200":
            return httpx.Response(200, stream=FailingStream(partial, httpx.ReadError("connection reset")))
        if case == "sse_error_503":
            return httpx.Response(200, text="data: " + json.dumps({"error": {"status": 503, "message": "overloaded"}}) + "\n\n")
        return httpx.Response(200, text='data: {"choices":[')

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 1 and delays == [] and not notices(session)
    assert len(agent_calls(session)) == 1
    assert session["wire"][-1]["role"] == "user"  # No continuation was added.
    expected = {"read_error_after_200": "network", "sse_error_503": "server", "event_cut_mid_way": "invalid_response"}[case]
    assert session["events"][-1]["type"] == "error" and session["events"][-1]["error_kind"] == expected
    assert session["terminal_reason"] == "inference_error"


async def test_four_503s_make_four_attempts_then_fail(runtime, monkeypatch, delays):
    manager, session = runtime
    requests = []

    async def gateway(request):
        requests.append(request)
        return httpx.Response(503, text="Upstream unavailable")

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 4 and delays == [2.0, 5.0, 15.0]
    assert [text[-9:] for text in notices(session)] == ["(1 of 3).", "(2 of 3).", "(3 of 3)."]
    error = session["events"][-1]
    assert error["type"] == "error" and error["error_kind"] == "server" and error["http_status"] == 503
    assert [call.get("retry_reason") for call in agent_calls(session)] == [None, "server_error", "server_error", "server_error"]


async def test_rate_limits_and_gateway_errors_share_one_budget(runtime, monkeypatch, delays):
    manager, session = runtime
    requests = []

    async def gateway(request):
        requests.append(request)
        if len(requests) % 2:
            return httpx.Response(429, headers={"Retry-After": "0"}, json=RATE_LIMITED)
        return httpx.Response(503, text="Upstream unavailable")

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 4  # 1 + 3, not 1 + 3 per error type; a 503 last gets no rate-limit wait.
    assert session["events"][-1]["error_kind"] == "server"
    assert [call.get("retry_reason") for call in agent_calls(session)] == [None, "rate_limit", "server_error", "rate_limit"]


# Compaction is not streamed and has a long timeout: only failures to send are repeated.

def summary_response():
    return httpx.Response(200, json={"choices": [{"message": {"content": "Short summary."}, "finish_reason": "stop"}]})


async def summarize(manager, session, gateway):
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        return await manager.summarize_context(session, client, "https://gateway.example/invocations", {},
                                               [{"role": "user", "content": "Summarize."}])


@pytest.mark.parametrize("failure", ["connect", "503"])
async def test_compaction_retries_connection_failures_and_gateway_errors(runtime, delays, failure):
    manager, session = runtime
    requests = []

    async def gateway(request):
        requests.append(request)
        if len(requests) == 1:
            if failure == "connect":
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(503, text="busy")
        return summary_response()

    assert await summarize(manager, session, gateway) == "Short summary."
    assert len(requests) == 2 and delays == [2.0]
    calls = session["inference_calls"]
    assert [(call["purpose"], call["status"], call.get("retry_reason")) for call in calls] == [
        ("compaction", "error", None), ("compaction", "completed", "network" if failure == "connect" else "server_error")]


async def test_a_summary_cut_off_after_a_retried_connection_keeps_its_own_note(runtime, delays):
    manager, session = runtime
    requests = []

    async def gateway(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Partial summary"},
                                                      "finish_reason": "length"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        text = await manager.summarize_context(session, client, "https://gateway.example/invocations", {},
                                               [{"role": "user", "content": "Summarize."}], keep_truncated=True,
                                               document="summary")
    assert text == "Partial summary\n\n[This summary was cut off at the output limit.]" and len(requests) == 2


@pytest.mark.parametrize("error", [httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout])
async def test_compaction_does_not_retry_failures_that_may_follow_a_billed_request(runtime, delays, error):
    manager, session = runtime
    requests = []

    async def gateway(request):
        requests.append(request)
        raise error("reset", request=request)

    with pytest.raises(error):
        await summarize(manager, session, gateway)
    assert len(requests) == 1 and delays == []
    assert session["inference_calls"][-1]["error_kind"] == "network"


# Auto-continue after the retries, for rate limits only.

NOW = 1_800_000_030.0  # 30 seconds past a minute boundary


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr("local_agent.agents.time.time", lambda: NOW)
    monkeypatch.setattr("local_agent.agents.random.uniform", lambda low, high: 2.5)


def rate_limited_gateway(requests, failures, headers=None):
    async def gateway(request):
        requests.append(request)
        if len(requests) <= failures:
            return httpx.Response(429, headers=headers or {}, json=RATE_LIMITED)
        return httpx.Response(200, text=sse("Finally."))
    return gateway


async def test_auto_continue_waits_for_the_next_minute_twice_then_succeeds(runtime, monkeypatch, delays, clock):
    manager, session = runtime
    requests = []
    mock_gateway(monkeypatch, rate_limited_gateway(requests, 5))
    await manager.run(session, "Hello")
    assert len(requests) == 6
    wait = 60 - NOW % 60 + 2.5
    assert delays == [5.0, 15.0, 40.0, wait, wait]
    at = datetime.fromtimestamp(NOW + wait).strftime("%H:%M:%S")
    # A finished wait no longer asks for Stop.
    assert notices(session)[3:] == [
        f"Databricks is still rate limiting this chat. Continued automatically at {at} ({number} of 2)."
        for number in (1, 2)]
    replies = [event for event in session["events"] if event["type"] == "assistant"]
    assert [reply["text"] for reply in replies] == ["Finally."] and session["terminal_reason"] == "completed"
    # The reply comes after the notices of the waits before it, in the stored (and exported) order.
    assert [event["type"] for event in session["events"]] == ["user"] + ["notice"] * 5 + ["assistant"]
    created = [event["created"] for event in session["events"]]
    assert created == sorted(created)
    assert [call.get("retry_reason") for call in agent_calls(session)] == [
        None, "rate_limit", "rate_limit", "rate_limit", "rate_limit_wait", "rate_limit_wait"]


async def test_auto_continue_gives_up_after_six_attempts(runtime, monkeypatch, delays, clock):
    manager, session = runtime
    requests = []
    mock_gateway(monkeypatch, rate_limited_gateway(requests, 100, {"Retry-After": "900"}))
    await manager.run(session, "Hello")
    assert len(requests) == 6
    assert delays == [60.0, 60.0, 60.0, 120.0, 120.0]  # Retry-After, capped at 60 s and then at 120 s.
    assert session["events"][-1]["type"] == "error" and session["events"][-1]["error_kind"] == "rate_limit"
    assert session["terminal_reason"] == "inference_error"


async def test_auto_continue_waits_are_counted_per_turn_not_per_request(runtime, monkeypatch, delays, clock):
    manager, session = runtime
    requests = []
    tool_call = ("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "list", "function": {
        "name": "list_files", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}) + "\n\ndata: [DONE]\n\n")

    async def gateway(request):
        requests.append(request)
        if len(requests) == 6:  # Step 1 succeeds after both waits; step 2 is rate limited again.
            return httpx.Response(200, text=tool_call)
        return httpx.Response(429, json=RATE_LIMITED)

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "List the files")
    assert len(requests) == 6 + 4  # Step 2: the first attempt and three retries, then no more waits.
    assert sum("Continued automatically" in text for text in notices(session)) == 2
    assert [event["state"] for event in session["events"] if event["type"] == "tool"] == ["completed"]
    assert session["events"][-1]["error_kind"] == "rate_limit" and session["terminal_reason"] == "inference_error"
    assert [call.get("retry_reason") for call in agent_calls(session)][6:] == [
        None, "rate_limit", "rate_limit", "rate_limit"]


async def test_auto_continue_can_be_turned_off_in_settings(runtime, monkeypatch, delays, clock):
    manager, session = runtime
    manager.settings.values["rate_limit_auto_continue"] = False
    requests = []
    mock_gateway(monkeypatch, rate_limited_gateway(requests, 100))
    await manager.run(session, "Hello")
    assert len(requests) == 4 and not any("Continuing automatically" in text for text in notices(session))


async def test_stop_cancels_an_auto_continue_wait(runtime, monkeypatch, clock):
    manager, session = runtime
    requests, waiting = [], asyncio.Event()

    async def sleep(delay):
        if delay > 100:  # The auto-continue wait.
            waiting.set()
            await asyncio.Event().wait()

    monkeypatch.setattr("local_agent.agents.asyncio.sleep", sleep)
    mock_gateway(monkeypatch, rate_limited_gateway(requests, 100, {"Retry-After": "300"}))
    manager.start(session["id"], "Hello")
    await asyncio.wait_for(waiting.wait(), 2)
    assert manager.statuses[session["id"]] == "running"
    await manager.stop(session["id"])
    saved = manager.store.get(session["id"])
    assert saved["terminal_reason"] == "stopped" and len(requests) == 4
    waits = [text for text in notices(saved) if text.startswith("Databricks is still rate limiting")]
    assert len(waits) == 1 and "Stopped while waiting to continue at" in waits[0] and "Press Stop" not in waits[0]


async def test_a_wait_notice_asks_for_stop_only_while_it_waits(runtime, monkeypatch, clock):
    manager, session = runtime
    requests, seen = [], []

    async def sleep(delay):
        seen.append((delay, [event["text"] for event in session["events"] if event["type"] == "notice"][-1],
                     session["events"][-1]["type"]))
    monkeypatch.setattr("local_agent.agents.asyncio.sleep", sleep)
    mock_gateway(monkeypatch, rate_limited_gateway(requests, 4, {"Retry-After": "1"}))
    await manager.run(session, "Hello")
    # While it waits, the newest notice is the one it is waiting on and the reply placeholder sits below it.
    assert [item[0] for item in seen] == [1.0, 1.0, 1.0, 1.0] and {item[2] for item in seen} == {"assistant"}
    assert seen[0][1].endswith("Retrying in 1 second (1 of 3).")
    assert seen[3][1].endswith("(1 of 2). Press Stop to cancel.")
    assert notices(session)[3].endswith("Continued automatically at "
                                        + datetime.fromtimestamp(NOW + 1).strftime("%H:%M:%S") + " (1 of 2).")
    assert session["events"][-1]["type"] == "assistant" and session["events"][-1]["text"] == "Finally."


async def test_a_stalled_stream_names_the_read_timeout(runtime, monkeypatch):
    """No data for STREAM_READ_TIMEOUT seconds ends the turn with advice, not a bare connection failure.
    No delays fixture: it replaces asyncio.sleep everywhere, and the stall needs real time."""
    manager, session = runtime
    monkeypatch.setattr("local_agent.agents.STREAM_READ_TIMEOUT", 0.2)
    requests = []
    partial = ("data: " + json.dumps({"choices": [{"delta": {"content": "Part"}, "finish_reason": None}]}) + "\n\n").encode()

    class Stalled(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield partial
            await asyncio.Event().wait()

    async def gateway(request):
        requests.append(request)
        return httpx.Response(200, stream=Stalled())

    class Timing(httpx.AsyncBaseTransport):
        """MockTransport ignores timeouts; this one applies the read timeout between chunks."""
        async def handle_async_request(self, request):
            response = await gateway(request)
            read = request.extensions["timeout"]["read"]

            class Timed(httpx.AsyncByteStream):
                async def __aiter__(inner):
                    chunks = response.stream.__aiter__()
                    while True:
                        try:
                            chunk = await asyncio.wait_for(chunks.__anext__(), read)
                        except StopAsyncIteration:
                            return
                        except asyncio.TimeoutError:
                            raise httpx.ReadTimeout("", request=request) from None
                        yield chunk
            return httpx.Response(response.status_code, headers=response.headers, stream=Timed())

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=Timing()))
    await manager.run(session, "Hello")
    error = session["events"][-1]
    assert len(requests) == 1 and not notices(session)
    assert error["error_kind"] == "network" and error["text"].startswith("Databricks sent no data for 0.2 seconds")
    assert session["terminal_reason"] == "inference_error"
