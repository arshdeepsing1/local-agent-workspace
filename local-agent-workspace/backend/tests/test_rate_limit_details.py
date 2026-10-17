"""Databricks 429 bodies: the limit they name, the learned OTPM refusal and a wait shared by
every chat on the same endpoint (u-large-inputs item 2). asyncio.sleep in local_agent.agents is
replaced in every test that runs a turn."""
import json
import math
from types import SimpleNamespace

import httpx
import pytest

from local_agent.agents import AgentManager, rate_limit_details, retry_notice
from local_agent.config import Settings
from local_agent.portability import export_bundle
from local_agent.rate_limits import (ITPM, LEARNED_TTL, OTPM, RateLimitGate, RateLimitInfo, describe,
                                     limit_type_from_words, parse_rate_limit)
from local_agent.store import Store
from local_agent.telemetry import InferenceError, session_metrics

OTPM_BODY = {"error": {"message": "Too many tokens", "limit_type": "output_tokens_per_minute", "limit": 100000,
                       "current": 100500, "retry_after": 3}}


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
    manager = AgentManager(store, settings)
    now = [1000.0]
    manager.rate_gate = RateLimitGate(clock=lambda: now[0])
    manager.rate_rng = SimpleNamespace(uniform=lambda low, high: 0.5)

    def new_session(model=None):
        session = store.create(settings.values)
        session.update(title="Limits", title_generated=True, **({"model": model} if model else {}))
        return session
    yield manager, new_session, now
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
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(gateway)))


def notices(session):
    return [event["text"] for event in session["events"] if event["type"] == "notice"]


# --- parse_rate_limit and describe ----------------------------------------------------------

def test_the_documented_nested_body_is_parsed():
    info = parse_rate_limit({"error": {"message": "Rate limit exceeded: ITPM limit of 1,000,000 tokens reached",
                                       "type": "rate_limit_exceeded", "code": 429,
                                       "limit_type": "input_tokens_per_minute", "limit": 1000000,
                                       "current": 1003000, "retry_after": 12}})
    assert info == RateLimitInfo("input_tokens_per_minute", "input tokens per minute (ITPM)", 1000000, 1003000, 12.0,
                                 "rate_limit_exceeded", "Rate limit exceeded: ITPM limit of 1,000,000 tokens reached")
    assert describe(info) == "input tokens per minute (ITPM): limit 1,000,000, current 1,003,000"


def test_root_fields_are_read_and_nested_values_win():
    info = parse_rate_limit({"limit_type": "OTPM", "limit": 5, "current": 1.9,
                             "error": {"limit": 100000}})
    assert info.limit_type == OTPM and info.limit == 100000 and info.current == 1


@pytest.mark.parametrize("value, expected", [
    ("output-tokens-per-minute", ("output_tokens_per_minute", "output tokens per minute (OTPM)")),
    ("QPH", ("queries_per_hour", "queries per hour (QPH)")),
    ("queries_per_second", ("queries_per_second", "queries per second (QPS)")),
    ("queries-per-minute", ("queries_per_minute", "queries per minute (QPM)")),
    ("QPM", ("queries_per_minute", "queries per minute (QPM)")),
    ("Concurrent Requests!", ("concurrentrequests", "concurrentrequests")),
    ("tokens per day", ("tokensperday", "tokensperday")),
    ("x" * 65, (None, None)),
    ("", (None, None)),
    (7, (None, None)),
])
def test_limit_types_are_normalized(value, expected):
    info = parse_rate_limit({"limit_type": value})
    assert (info.limit_type, info.label) == expected


@pytest.mark.parametrize("bad", [True, -1, float("nan"), float("inf"), "100", None, [1]])
def test_invalid_numbers_are_ignored(bad):
    info = parse_rate_limit({"error": {"limit_type": "otpm", "limit": bad, "current": bad, "retry_after": bad}})
    assert info.limit is None and info.current is None and info.retry_after is None
    assert describe(info) == "output tokens per minute (OTPM)"


def test_describe_is_empty_without_a_known_limit():
    assert describe(parse_rate_limit({"message": "slow down"})) == "" and describe(None) == ""


def test_the_existing_rate_limit_notice_is_unchanged_without_details():
    error = InferenceError("", "rate_limit", 429)
    error.retry_reason = "rate_limit"
    assert retry_notice(error, 5.0, 1, 3) == "Databricks rate limited the model request. Retrying in 5 seconds (1 of 3)."
    error.rate_limit = parse_rate_limit({"error_code": "REQUEST_LIMIT_EXCEEDED"})
    assert retry_notice(error, 5.0, 1, 3) == "Databricks rate limited the model request. Retrying in 5 seconds (1 of 3)."


# --- The limit type in a 429's words (a body with only error_code and message) -------------------

ITPM_WORDS = {"error_code": "REQUEST_LIMIT_EXCEEDED",
              "message": "Exceeded workspace input tokens per minute rate limit for test-endpoint."}


@pytest.mark.parametrize("text, expected", [
    ("Exceeded workspace input tokens per minute rate limit for test-endpoint.", ITPM),
    ("Exceeded workspace output tokens per minute rate limit.", OTPM),
    ("Rate limit exceeded: ITPM limit of 1,000,000 tokens reached.", ITPM),
    ("OUTPUT_TOKENS_PER_MINUTE limit reached", OTPM),
    ("The endpoint is limited to 0 queries per minute.", "queries_per_minute"),
    ("QPH limit reached", "queries_per_hour"),
    ("Too many queries per second", "queries_per_second"),
    ("Exceeded tokens per minute limit", "tokens_per_minute"),
    ("Exceeded the tokens per minute and the input tokens per minute limits", ITPM),  # The specific one wins.
    ("input tokens per minute and output tokens per minute both exceeded", None),  # Ambiguous.
    ("Exceeded workspace rate limit", None),
    ("REQUEST_LIMIT_EXCEEDED", None),
    ("Too Many Requests", None),
    ("an itpmx value", None),
    ("", None),
])
def test_the_limit_type_is_read_from_the_words(text, expected):
    assert limit_type_from_words(text) == expected


def test_a_body_with_only_a_code_and_a_message_names_its_limit_but_no_number():
    info = parse_rate_limit(ITPM_WORDS)
    assert (info.limit_type, info.label, info.code) == (ITPM, "input tokens per minute (ITPM)", "REQUEST_LIMIT_EXCEEDED")
    assert describe(info) == "input tokens per minute (ITPM)"
    # Numbers in words are never read: no limit is learned from them.
    info = parse_rate_limit({"message": "ITPM limit of 1,000,000 tokens reached; current 1,200,000"})
    assert info.limit_type == ITPM and info.limit is None and info.current is None
    # A field wins over the words; nested values win over the root's.
    assert parse_rate_limit({"limit_type": "qph", "message": "output tokens per minute"}).limit_type == "queries_per_hour"
    nested = parse_rate_limit({"message": "root", "error": {"message": "QPM reached", "error_code": "LIMITED"}})
    assert (nested.limit_type, nested.label, nested.code, nested.message) == (
        "queries_per_minute", "queries per minute (QPM)", "LIMITED", "QPM reached")


def test_the_kept_message_is_redacted_collapsed_and_clipped():
    redact = lambda text: text.replace("private-token", "[REDACTED]")  # noqa: E731
    info = parse_rate_limit({"error_code": "bad code!", "message": "Rate\n  limited  private-token " + "y" * 400},
                            redact=redact)
    assert info.message.startswith("Rate limited [REDACTED] yyy") and len(info.message) == 200
    assert info.message.endswith("…") and info.code is None
    # A body that is not JSON: its text is the message (and its words name the limit).
    plain = parse_rate_limit(None, "Too many output tokens per minute", redact)
    assert (plain.message, plain.limit_type) == ("Too many output tokens per minute", OTPM)
    assert parse_rate_limit(None, "").message is None and parse_rate_limit({}, "{}").message is None


def test_a_smaller_output_reservation_passes_an_otpm_block():
    now = [0.0]
    gate = RateLimitGate(clock=lambda: now[0])
    gate.note("m", "a", 30, parse_rate_limit(OTPM_BODY), max_tokens=8192)
    assert gate.remaining("m", "b", 4096) == 0 and gate.remaining("m", "b", 8192) == 30
    assert gate.remaining("m", "b") == 30 and gate.remaining("m", "a", 8192) == 0
    gate.note("m", "a", 500)  # Capped at RATE_LIMIT_WAIT_CAP (120 s).
    assert gate.remaining("m", "b") == 120
    now[0] = 121
    assert gate.remaining("m", "b") == 0


# --- Retries with the limit's details ---------------------------------------------------------

async def test_a_429_that_names_a_limit_is_retried_with_its_details(runtime, monkeypatch, delays):
    manager, new_session, _ = runtime
    session = new_session()
    requests = []

    async def gateway(request):
        requests.append(request)
        return httpx.Response(429, json=OTPM_BODY) if len(requests) == 1 else httpx.Response(200, text=sse())
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 2 and delays == [3.0]
    assert notices(session) == ["Databricks rate limited the model request (output tokens per minute (OTPM): limit "
                                "100,000, current 100,500). Retrying in 3 seconds (1 of 3)."]
    assert session["terminal_reason"] == "completed"


async def test_a_final_429_keeps_its_details_in_the_request_and_the_ledger(runtime, monkeypatch, delays):
    manager, new_session, _ = runtime
    manager.settings.values["rate_limit_auto_continue"] = False
    session = new_session()
    body = {"error": {"message": "Rate limit exceeded", "limit_type": "input_tokens_per_minute", "limit": 1000000,
                      "current": 1200000, "retry_after": 2}}

    async def gateway(request):
        return httpx.Response(429, json=body)
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    reply = next(event for event in session["events"] if event["type"] == "assistant")
    assert reply["request_info"]["rate_limit"] == {"limit_type": "input_tokens_per_minute", "limit": 1000000,
                                                   "current": 1200000, "retry_after": 2.0,
                                                   "message": "Rate limit exceeded"}
    calls = [call for call in session["inference_calls"] if call["purpose"] == "agent"]
    assert len(calls) == 4 and all(call["limit_type"] == "input_tokens_per_minute" for call in calls)
    assert all(call["limit_type"] == "input_tokens_per_minute" for call in session_metrics(session)["calls"])
    error = session["events"][-1]
    estimate = session["context_info"]["estimated_tokens"]
    assert error["type"] == "error" and error["error_kind"] == "rate_limit"
    assert (f"Each agent step resends the whole conversation: at about {estimate:,} input tokens per request, the "
            f"per-minute input limit of 1,000,000 allows about {1000000 // estimate} requests per minute.") in error["text"]
    assert error["text"].endswith("For about five steps a minute, keep Settings → Context budget near 200,000 tokens.")


def test_the_itpm_advice_names_the_requests_per_minute():
    info = parse_rate_limit({"limit_type": "input_tokens_per_minute", "limit": 1000000})
    advice = AgentManager.itpm_advice({"context_info": {"estimated_tokens": 400000}}, info)
    assert "at about 400,000 input tokens per request, the per-minute input limit of 1,000,000 allows about 2 " \
           "requests per minute." in advice
    assert AgentManager.itpm_advice({}, info) == (" For about five steps a minute, keep Settings → Context budget "
                                                  "near 200,000 tokens.")
    assert AgentManager.itpm_advice({}, parse_rate_limit(OTPM_BODY)) == ""


async def test_a_429_whose_message_names_the_limit_is_labelled_kept_and_detailed(runtime, monkeypatch, delays):
    manager, new_session, _ = runtime
    session = new_session()
    body = {**ITPM_WORDS, "message": ITPM_WORDS["message"] + " Token private-token. " + "z" * 300}
    requests = []

    async def gateway(request):
        requests.append(request)
        return httpx.Response(429, json=body) if len(requests) == 1 else httpx.Response(200, text=sse())
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 2 and delays == [5.0]
    notice = next(event for event in session["events"] if event["type"] == "notice")
    assert notice["text"] == ("Databricks rate limited the model request (input tokens per minute (ITPM)). "
                              "Retrying in 5 seconds (1 of 3).")
    assert notice["details"].startswith("HTTP 429 REQUEST_LIMIT_EXCEEDED: Exceeded workspace input tokens per minute "
                                        "rate limit for test-endpoint. Token [REDACTED]. zzz")
    assert notice["details"].endswith("… · Retry-After: none")
    first = [call for call in session["inference_calls"] if call["purpose"] == "agent"][0]
    assert first["limit_type"] == ITPM and first["retry_after_header"] is False and "retry_after" not in first
    assert len(first["rate_limit_message"]) == 200 and "[REDACTED]" in first["rate_limit_message"]
    public = session_metrics(session)["calls"][0]
    assert public["rate_limit_message"] == first["rate_limit_message"] and public["retry_after_header"] is False
    assert "private-token" not in json.dumps(manager.store.get(session["id"]))


async def test_a_final_429_records_the_message_and_the_retry_after_header(runtime, monkeypatch, delays):
    manager, new_session, _ = runtime
    manager.settings.values["rate_limit_auto_continue"] = False
    session = new_session()

    async def gateway(request):
        return httpx.Response(429, headers={"Retry-After": "7"}, json={"error_code": "REQUEST_LIMIT_EXCEEDED",
                                                                       "message": "Exceeded workspace rate limit"})
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert delays == [7.0, 7.0, 7.0]
    reply = next(event for event in session["events"] if event["type"] == "assistant")
    assert reply["request_info"]["rate_limit"] == {"code": "REQUEST_LIMIT_EXCEEDED",
                                                   "message": "Exceeded workspace rate limit", "retry_after": 7.0,
                                                   "retry_after_header": "7"}
    assert all(text.startswith("Databricks rate limited the model request. Retrying in 7 seconds")
               for text in notices(session))
    assert {event["details"] for event in session["events"] if event["type"] == "notice"} == {
        "HTTP 429 REQUEST_LIMIT_EXCEEDED: Exceeded workspace rate limit · Retry-After: 7 s"}
    calls = [call for call in session["inference_calls"] if call["purpose"] == "agent"]
    assert all(call["retry_after"] == 7.0 and call["retry_after_header"] is True and "limit_type" not in call
               for call in calls)


async def test_a_retry_after_header_the_app_cannot_read_is_not_named_as_the_wait(runtime, monkeypatch, delays):
    """A date in the past: the body's retry_after is the wait, and nothing says it came in the header."""
    manager, new_session, _ = runtime
    manager.settings.values["rate_limit_auto_continue"] = False
    session = new_session()

    async def gateway(request):
        return httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
                              json={"error_code": "REQUEST_LIMIT_EXCEEDED", "message": "Slow down", "retry_after": 4})
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert delays == [4.0, 4.0, 4.0]
    reply = next(event for event in session["events"] if event["type"] == "assistant")
    assert reply["request_info"]["rate_limit"] == {"code": "REQUEST_LIMIT_EXCEEDED", "message": "Slow down",
                                                   "retry_after": 4.0}
    assert {event["details"] for event in session["events"] if event["type"] == "notice"} == {
        "HTTP 429 REQUEST_LIMIT_EXCEEDED: Slow down · retry_after: 4 s"}
    calls = [call for call in session["inference_calls"] if call["purpose"] == "agent"]
    assert all(call["retry_after"] == 4.0 and call["retry_after_header"] is False for call in calls)


async def test_an_export_keeps_a_429_without_a_limit_type_and_its_details(runtime, monkeypatch, delays):
    manager, new_session, _ = runtime
    manager.settings.values["rate_limit_auto_continue"] = False
    session = new_session()

    async def gateway(request):
        return httpx.Response(429, text="Too Many Requests")
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    bundle = export_bundle([session], session["id"])
    events = bundle["sessions"][0]["events"]
    reply = next(event for event in events if event["type"] == "assistant")
    assert reply["request_info"]["rate_limit"] == {"message": "Too Many Requests"}
    assert [event["details"] for event in events if event["type"] == "notice"] == [
        "HTTP 429: Too Many Requests · Retry-After: none"] * 3


def test_the_details_name_a_retry_after_in_the_body_or_none():
    error = InferenceError("", "rate_limit", 429)
    error.rate_limit = parse_rate_limit({"error": {"message": "Slow down", "type": "rate_limit_exceeded"}})
    error.retry_headers, error.retry_payload = {"retry-after": None}, {"error": {"retry_after": 2.5}}
    assert rate_limit_details(error) == "HTTP 429 rate_limit_exceeded: Slow down · retry_after: 2.5 s"
    error.rate_limit, error.retry_payload = parse_rate_limit(None, ""), None
    assert rate_limit_details(error) == "HTTP 429 · Retry-After: none"
    error.rate_limit = parse_rate_limit({"error_code": "LIMITED", "message": "LIMITED: try later"})
    error.retry_headers = {"retry-after": "soon"}
    assert rate_limit_details(error) == "HTTP 429: LIMITED: try later · Retry-After: soon"
    error.retry_payload = {"retry_after": 3}  # The body's wait is the one used, not the unreadable header.
    assert rate_limit_details(error) == "HTTP 429: LIMITED: try later · retry_after: 3 s"
    error.retry_headers, error.retry_payload = {"retry-after": " "}, None
    assert rate_limit_details(error) == "HTTP 429: LIMITED: try later · Retry-After: none"


async def test_an_otpm_type_from_words_without_a_number_refuses_nothing(runtime, monkeypatch, delays):
    manager, new_session, _ = runtime
    manager.settings.values.update(context_window=262144, max_output_tokens=120000)
    session = new_session()
    requests = []

    async def gateway(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"error_code": "REQUEST_LIMIT_EXCEEDED",
                                             "message": "Exceeded workspace output tokens per minute rate limit."})
        return httpx.Response(200, text=sse())
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 2 and session["terminal_reason"] == "completed"
    assert notices(session)[0].startswith("Databricks rate limited the model request (output tokens per minute (OTPM)).")
    assert manager.rate_gate.learned_limit(session["model"], OTPM) is None
    assert manager.otpm_preflight(session, 120000) is None
    await manager.run(session, "Again")
    assert len(requests) == 3 and session["terminal_reason"] == "completed"


# --- The learned OTPM limit ----------------------------------------------------------------------

def big_wire(session):
    session["wire"] = [message for number in range(6) for message in (
        {"role": "user", "content": f"Question {number} " + "x" * 120_000},
        {"role": "assistant", "content": f"Answer {number}."})]


async def test_max_tokens_above_the_reported_otpm_limit_fails_fast_and_is_remembered(runtime, monkeypatch, delays):
    manager, new_session, now = runtime
    manager.settings.values.update(context_window=262144, max_output_tokens=120000)
    session = new_session()
    requests, summaries = [], []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return httpx.Response(429, json=OTPM_BODY) if len(requests) == 1 else httpx.Response(200, text=sse())
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 1 and delays == []
    error = session["events"][-1]
    assert error["error_kind"] == "output_tokens_over_limit" and error["http_status"] == 429
    assert error["text"].startswith("Max output tokens (120,000) is above the output-tokens-per-minute limit "
                                    "Databricks reported for test-endpoint (100,000), so Databricks rejects every "
                                    "request before it starts. Lower Settings → Max output tokens to at most 100,000;")
    assert error["text"].endswith("With 4 requests at once (a chat and its subagents), at most 25,000 each.")
    reply = next(event for event in session["events"] if event["type"] == "assistant")
    assert reply["request_info"]["error_kind"] == "output_tokens_over_limit"

    async def summarize(*args, **kwargs):
        summaries.append(args)
        return "Summary."
    monkeypatch.setattr(manager, "summarize_context", summarize)
    big_wire(session)
    await manager.run(session, "Again")
    assert len(requests) == 1 and summaries == []
    error = session["events"][-1]
    assert error["error_kind"] == "output_tokens_over_limit" and "http_status" not in error

    now[0] += LEARNED_TTL + 1
    session["wire"] = []
    await manager.run(session, "Later")
    assert len(requests) == 2 and session["terminal_reason"] == "completed"


async def test_a_side_question_learns_and_checks_the_otpm_limit(runtime, monkeypatch):
    manager, new_session, _ = runtime
    manager.settings.values.update(context_window=262144, max_output_tokens=120000)
    session = new_session()
    manager.rate_gate.note(session["model"], "other", 0, parse_rate_limit(OTPM_BODY))
    requests = []

    async def gateway(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Yes."}, "finish_reason": "stop"}]})
    mock_gateway(monkeypatch, gateway)
    # The side question's own reserve (4,096 here) fits the learned limit.
    assert (await manager.side_question(session["id"], "Fine?"))["answer"] == "Yes."
    manager.rate_gate.learned[(session["model"], OTPM)] = (1000, manager.rate_gate.clock())
    with pytest.raises(ValueError, match="above the output-tokens-per-minute limit"):
        await manager.side_question(session["id"], "Now?")
    assert len(requests) == 1


# --- The shared, jittered wait ---------------------------------------------------------------

async def test_another_chat_on_the_same_endpoint_waits_with_jitter(runtime, monkeypatch, delays):
    manager, new_session, _ = runtime
    first, second, other = new_session(), new_session(), new_session("other-endpoint")
    requests = []

    async def gateway(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"error": {"message": "Rate limit exceeded", "retry_after": 20}})
        return httpx.Response(200, text=sse())
    mock_gateway(monkeypatch, gateway)
    await manager.run(first, "Hello")
    assert delays == [20.0]  # The chat's own retry; it never waits for its own block twice.
    await manager.run(second, "Hello")
    assert delays == [20.0, 20.5]
    assert notices(second) == ["Waiting 20 seconds before the model request: Databricks rate-limited another chat's "
                               "request to test-endpoint."]
    await manager.run(other, "Hello")
    assert delays == [20.0, 20.5] and not notices(other)
    await manager.run(first, "Again")
    assert delays == [20.0, 20.5]


async def test_a_waiting_chat_retries_one_to_three_seconds_after_the_owner(runtime, monkeypatch, delays):
    manager, new_session, _ = runtime
    drawn = []

    def uniform(low, high):
        drawn.append((low, high))
        return 2.0
    manager.rate_rng = SimpleNamespace(uniform=uniform)
    first, second = new_session(), new_session()
    requests = []

    async def gateway(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json=ITPM_WORDS)
        return httpx.Response(200, text=sse())
    mock_gateway(monkeypatch, gateway)
    await manager.run(first, "Hello")
    await manager.run(second, "Hello")
    # A floor, not a fraction of the remaining wait: even a short remainder is followed by at least 1 s.
    assert drawn == [(1.0, 3.0)] and delays == [5.0, 7.0]
    waiting = next(event for event in second["events"] if event["type"] == "notice")
    assert waiting["text"].startswith("Waiting 5 seconds before the model request")
    # The other chat's 429, under Details.
    assert waiting["details"] == ("HTTP 429 REQUEST_LIMIT_EXCEEDED: Exceeded workspace input tokens per minute rate "
                                  "limit for test-endpoint. · Retry-After: none")


async def test_a_compaction_429_blocks_the_endpoint_for_other_chats(runtime, delays):
    manager, new_session, _ = runtime
    session = new_session()
    responses = [httpx.Response(429, json={"error": {"message": "Rate limit exceeded", "limit_type": "qph",
                                                     "limit": 10, "retry_after": 5}}),
                 httpx.Response(200, json={"choices": [{"message": {"content": "Summary."}, "finish_reason": "stop"}]})]

    class Client:
        async def post(self, url, **kwargs):
            response = responses.pop(0)
            response.request = httpx.Request("POST", url)
            return response
    result = await manager.summarize_context(session, Client(), "https://gateway.example/x", {},
                                             [{"role": "user", "content": "history"}])
    assert result == "Summary." and delays == [5.0]
    assert manager.rate_gate.remaining(session["model"], "another chat") == 5
    assert notices(session) == ["Databricks rate limited context compaction (queries per hour (QPH): limit 10). "
                                "Retrying in 5 seconds (1 of 3)."]
    assert session["events"][-1]["details"] == "HTTP 429: Rate limit exceeded · retry_after: 5 s"
    compaction = [call for call in session["inference_calls"] if call["purpose"] == "compaction"]
    assert compaction[0]["limit_type"] == "queries_per_hour" and "limit_type" not in compaction[1]


async def test_a_side_question_during_another_chats_block_asks_to_wait(runtime, monkeypatch):
    manager, new_session, _ = runtime
    session = new_session()
    manager.rate_gate.note(session["model"], "another chat", 42.2)
    requests = []

    async def gateway(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Yes."}, "finish_reason": "stop"}]})
    mock_gateway(monkeypatch, gateway)
    with pytest.raises(ValueError) as caught:
        await manager.side_question(session["id"], "Still there?")
    assert str(caught.value) == f"Databricks is rate limiting test-endpoint; ask again in {math.ceil(42.2)} seconds."
    assert requests == []


async def test_compaction_waits_for_another_chats_block(runtime, delays):
    manager, new_session, _ = runtime
    session = new_session()
    manager.rate_gate.note(session["model"], "another chat", 30)
    sent = []

    class Client:
        async def post(self, url, **kwargs):
            sent.append(len(delays))
            response = httpx.Response(200, json={"choices": [{"message": {"content": "Summary."},
                                                              "finish_reason": "stop"}]})
            response.request = httpx.Request("POST", url)
            return response
    result = await manager.summarize_context(session, Client(), "https://gateway.example/x", {},
                                             [{"role": "user", "content": "history"}])
    assert result == "Summary." and delays == [30.5] and sent == [1]  # Waited before sending.
    assert notices(session) == ["Waiting 30 seconds before context compaction: Databricks rate-limited another "
                                "chat's request to test-endpoint."]


async def test_the_automatic_continue_notice_names_the_limit(runtime, monkeypatch, delays):
    manager, new_session, _ = runtime
    manager.settings.values["rate_limit_auto_continue"] = True
    session = new_session()
    body = {"error": {"message": "Rate limit exceeded", "limit_type": "input_tokens_per_minute", "limit": 1000000,
                      "current": 1003000, "retry_after": 2}}
    requests = []

    async def gateway(request):
        requests.append(request)
        return httpx.Response(429, json=body) if len(requests) <= 4 else httpx.Response(200, text=sse())
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert len(requests) == 5 and session["terminal_reason"] == "completed"
    waiting = [text for text in notices(session) if text.startswith("Databricks is still rate limiting this chat")]
    assert len(waiting) == 1 and waiting[0].startswith(
        "Databricks is still rate limiting this chat (input tokens per minute (ITPM): limit 1,000,000, current "
        "1,003,000). Continued automatically at ")
    assert waiting[0].endswith("(1 of 2).") and "Continued automatically at " in waiting[0]
    wait = next(event for event in session["events"] if event.get("text") == waiting[0])
    assert wait["details"] == "HTTP 429: Rate limit exceeded · retry_after: 2 s"


def test_the_gate_keeps_at_most_64_endpoints():
    now = [0.0]
    gate = RateLimitGate(clock=lambda: now[0])
    info = RateLimitInfo("output_tokens_per_minute", "output tokens per minute (OTPM)", 1000, None, None)
    for index in range(65):
        gate.note(f"endpoint-{index}", "chat", 60, info)
    assert len(gate.blocked) == 64 and len(gate.learned) == 64
    assert gate.remaining("endpoint-0", "other") == 0 and gate.learned_limit("endpoint-0", OTPM) is None
    assert gate.remaining("endpoint-64", "other") == 60 and gate.learned_limit("endpoint-64", OTPM) == 1000
    assert gate.remaining("endpoint-1", "other") == 60


# --- Compaction under a reported OTPM limit (integration review B, findings 1 and 6) -------------

OPUS_OTPM = 20_000
OPUS_OTPM_BODY = {"error": {"message": "Too many tokens", "limit_type": "output_tokens_per_minute",
                            "limit": OPUS_OTPM, "current": 0, "retry_after": 3}}
HANDOFF_TEXT = "\n\n".join(f"## {section}\nok" for section in (
    "Goal and current status", "Open tasks and next steps", "User requirements, preferences and constraints",
    "Decisions and reasons", "Errors, causes and fixes", "Files read and changed", "Commands and results",
    "Work done in detail", "Key facts and gotchas"))


@pytest.fixture
def opus_otpm(runtime, monkeypatch, delays):
    """The default Opus 5.5 chat (200,000 / 16,384, handoffs on) in a workspace whose OTPM limit is
    20,000: like Databricks, the gateway answers any max_tokens above it with an OTPM 429."""
    manager, new_session, now = runtime
    manager.settings.values.update(model="databricks-claude-opus-5-5", context_window=200000, max_output_tokens=16384)
    sent = []

    async def gateway(request):
        body = json.loads(request.content)
        sent.append((bool(body.get("stream")), body.get("max_tokens")))
        if body.get("max_tokens", 0) > OPUS_OTPM:
            return httpx.Response(429, json=OPUS_OTPM_BODY)
        if body.get("stream"):
            return httpx.Response(200, text=sse())
        return httpx.Response(200, json={"choices": [{"message": {"content": HANDOFF_TEXT},
                                                      "finish_reason": "stop"}]})
    mock_gateway(monkeypatch, gateway)

    def chat():
        session = new_session()
        assert session["model"] == "databricks-claude-opus-5-5" and manager.request_limits(session) == (200000, 16384)
        session["wire"] = [{"role": "user", "content": "first " * 50}, {"role": "assistant", "content": "ok " * 50},
                           {"role": "user", "content": "second"}, {"role": "assistant", "content": "ok"}]
        return session
    return manager, chat, sent


def errors(session):
    return [event["text"] for event in session["events"] if event["type"] == "error"]


async def test_a_compaction_429_on_otpm_is_resent_at_the_reported_limit(opus_otpm):
    manager, chat, sent = opus_otpm
    session = chat()
    await manager.manual_compact(session)
    # 16,000 handoff + 8,192 thinking room is rejected (not billed); the handoff shrinks to fit.
    assert sent == [(False, 24192), (False, OPUS_OTPM)] and errors(session) == []
    sent.clear()
    await manager.manual_compact(chat())
    assert sent == [(False, OPUS_OTPM)]  # Learned: no second rejection.


async def test_a_learned_otpm_limit_lets_an_automatic_compaction_turn_complete(opus_otpm):
    manager, chat, sent = opus_otpm
    session = chat()
    # A retryable OTPM 429 on an agent step teaches the same limit.
    manager.rate_gate.note(session["model"], "another chat", 0, parse_rate_limit(OPUS_OTPM_BODY))
    session["wire"] = [message for number in range(6) for message in (
        {"role": "user", "content": f"Q{number} " + "x" * 120_000}, {"role": "assistant", "content": f"A{number}."})]
    await manager.run(session, "next")
    assert session["terminal_reason"] == "completed" and errors(session) == []
    assert (False, OPUS_OTPM) in sent and all(max_tokens <= OPUS_OTPM for _, max_tokens in sent)


@pytest.mark.parametrize("handoffs", [True, False])
async def test_a_compaction_that_cannot_fit_names_the_context_budget_that_makes_it_fit(opus_otpm, handoffs):
    manager, chat, sent = opus_otpm
    manager.settings.values["compaction_handoffs"] = handoffs  # Both shrink to the same smallest request.
    session = chat()
    manager.rate_gate.note(session["model"], "another chat", 0,
                           parse_rate_limit({"limit_type": "output_tokens_per_minute", "limit": 10_000}))
    await manager.manual_compact(session)
    assert sent == []
    error = errors(session)[-1]
    assert "needs max_tokens 12,288" in error and "(10,000)" in error
    # 10,000 - 4,096 leaves 5,904 tokens of thinking room: an eighth of a budget of at most 47,239.
    assert "lower the context budget to at most 47,239 tokens" in error and "workspace admin" in error
    assert "handoff" not in error and "Max output tokens" not in error
    # The budget it names makes the same compaction fit.
    manager.settings.values["context_window"] = 47_239
    await manager.manual_compact(session)
    assert len(errors(session)) == 1
    assert sent and all(max_tokens <= 10_000 for _, max_tokens in sent)


async def test_a_compaction_no_setting_can_fit_asks_for_a_higher_limit(opus_otpm):
    manager, chat, sent = opus_otpm
    session = chat()
    manager.rate_gate.note(session["model"], "another chat", 0,
                           parse_rate_limit({"limit_type": "output_tokens_per_minute", "limit": 4_000}))
    await manager.manual_compact(session)
    assert sent == []
    error = errors(session)[-1]
    assert "(4,000)" in error and "No setting makes it fit" in error and "workspace admin" in error
    assert "Context budget" not in error
