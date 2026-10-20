"""Endpoint checks (endpoint_probes.py, endpoint_checks_api.py): the cost plan the user approves, approval and plan
checks before anything is sent, reading the replies, sanitized fixtures, the results file and the hooks that read
it, and whole runs against a ReplayClient and the offline simulator. No request leaves the machine."""
import asyncio
import importlib.util
import json
import stat
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from local_agent import endpoint_checks_api, endpoint_probes
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.endpoint_probes import CheckStore, capabilities, interpret, plan, prompt_counting, read_stream, sanitize
from local_agent.serving import ReplayClient, ServingClient, sse_chunks
from local_agent.telemetry import MODEL_DBU_PER_MILLION, PRICING_EFFECTIVE_AT, REQUEST_TAGS_HEADER

OPUS, OPUS_48, GPT = "databricks-claude-opus-5-5", "databricks-claude-opus-4-8", "databricks-gpt-oss-120b"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "simulate_databricks.py"
FORBIDDEN_KEYS = ("temperature", "top_p", "top_k", "tool_choice")


# The plan: what a run sends and what it may cost

def test_the_plan_prices_each_request_at_list_prices_and_names_the_worst_case():
    value = plan(OPUS)
    rates = MODEL_DBU_PER_MILLION[OPUS]
    requests = {request["id"]: request for probe in value["probes"] for request in probe["requests"]}
    assert [probe["id"] for probe in value["probes"]] == list(endpoint_probes.PROBE_IDS)
    for request_id, request in requests.items():
        rate = rates["cache_creation_input_tokens"] if request_id.startswith("cache_") else rates["input_tokens"]
        expected = (request["estimated_input_tokens"] * rate + request["max_tokens"] * rates["output_tokens"]) / 1_000_000
        assert request["estimated_dbu"] == pytest.approx(expected, abs=1e-6)
        assert request["max_tokens"] == endpoint_probes.THINKING_MAX_TOKENS  # Opus 5.5 always thinks.
    # The documented-form cache request replaces a rejected (unbilled) first one: it adds nothing to the worst case.
    assert requests["cache_write_documented"]["alternative_to"] == "cache_write"
    worst = sum(request["estimated_dbu"] for request_id, request in requests.items() if request_id != "cache_write_documented")
    assert value["total_dbu"] == pytest.approx(round(worst, 3)) and value["priced"] is True
    assert value["total_dbu"] < 7.5  # The whole suite, at most; Test model alone:
    test = plan(OPUS, ["basic"])
    assert test["total_dbu"] <= 0.65 and [request["id"] for request in test["probes"][0]["requests"]] == ["basic"]
    assert value["pricing_effective_at"] == PRICING_EFFECTIVE_AT == "2026-10-16"


def test_an_endpoint_without_prices_is_unpriced_and_checks_that_do_not_apply_are_left_out():
    value = plan("team-gpt-oss-endpoint")  # Not in the list prices.
    assert value["priced"] is False and value["total_dbu"] is None
    assert all(request["estimated_dbu"] is None for probe in value["probes"] for request in probe["requests"])
    assert "cache" not in [probe["id"] for probe in value["probes"]]  # cache_control is for Claude endpoints only.
    assert value["max_output_tokens"] > 0
    # GPT OSS 120B has list prices (input and output only): priced, without the Claude cache check.
    gpt = plan(GPT)
    assert gpt["priced"] is True and gpt["total_dbu"] > 0 and "cache" not in [probe["id"] for probe in gpt["probes"]]
    assert [probe["id"] for probe in plan("plain-endpoint")["probes"]] == ["basic", "tools", "response_format", "image"]
    with pytest.raises(ValueError, match="None of the chosen checks"):
        plan("plain-endpoint", ["reasoning"])
    with pytest.raises(ValueError, match="Unknown endpoint check"):
        plan(OPUS, ["everything"])


def test_the_plan_id_names_the_endpoint_the_checks_and_the_bodies():
    assert plan(OPUS)["plan_id"] == plan(OPUS)["plan_id"]  # Deterministic synthetic bodies.
    ids = {plan(OPUS)["plan_id"], plan(OPUS, ["basic"])["plan_id"], plan(OPUS_48)["plan_id"], plan(" " + OPUS)["plan_id"]}
    assert len(ids) == 3  # Surrounding spaces name the same endpoint.


def test_the_follow_ups_plan_room_for_the_reply_they_send_back_and_a_larger_one_is_not_sent():
    value = plan(OPUS, ["tools"])
    planned = {request["id"]: request["estimated_input_tokens"] for request in value["probes"][0]["requests"]}
    assert planned["replay_signed"] > planned["replay_plain"] > planned["tool_call"]
    # A tool_call reply that used its whole max_tokens: visible text, a thinking summary with a signature, one call.
    call = {"id": "toolu_bdrk_01ABCDEF", "type": "function",
            "function": {"name": "get_weekday", "arguments": '{"date": "2026-01-05"}'}}
    items = [{"type": "reasoning", "summary": [{"type": "summary_text", "signature": "S" * 1_200,
                                                "text": "I should call get_weekday for 2026-01-05 and then answer. " * 40}]}]
    reply = {"http_status": 200, "parsed": {"tool_calls": [call], "items": items,
                                            "text": "Let me check that date carefully. " * 170}}
    for request_id in ("replay_plain", "replay_signed"):
        sent = endpoint_probes.body(request_id, OPUS, {"tool_call": reply})
        assert endpoint_probes.input_tokens(OPUS, sent) <= planned[request_id]
        assert not endpoint_probes.exceeds_plan(value, request_id, sent)
    # Signatures far above the room the plan made: the run does not send that request.
    huge = {**reply, "parsed": {**reply["parsed"], "items": [{"type": "thinking", "thinking": "Plan.", "signature": "S" * 40_000}]}}
    assert endpoint_probes.exceeds_plan(value, "replay_signed", endpoint_probes.body("replay_signed", OPUS, {"tool_call": huge}))


@pytest.mark.parametrize("model", [OPUS, OPUS_48, GPT, "plain-endpoint"])
def test_probe_bodies_never_carry_sampling_fields_or_an_assistant_prefill(model):
    for probe in endpoint_probes.chosen_probes(model):
        for request_id, _ in probe["requests"]:
            body = endpoint_probes.body(request_id, model)
            assert not set(FORBIDDEN_KEYS) & set(body), request_id
            assert body["messages"][-1]["role"] in ("user", "tool") and body["stream"] is True
    image = endpoint_probes.body("image", OPUS)["messages"][0]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/png;base64,iVBORw0KGgo")  # A PNG made by the app, no file read.


# Reading the replies

def outcome(chunks=None, status=200, error=None):
    value = {"http_status": status, "error": error, "ttfb": 0.4, "elapsed": 1.2}
    if chunks is not None:
        value.update(chunks=chunks, parsed=read_stream(chunks))
    return value


def thinking(text="Plan.", signature="sig-123"):
    return [{"choices": [{"delta": {"content": [{"type": "reasoning", "summary": [
        {"type": "summary_text", "text": text, "signature": ""}]}]}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": [{"type": "reasoning", "summary": [
            {"type": "summary_text", "text": "", "signature": signature}]}]}, "finish_reason": None}]}]


def test_a_smoke_reply_reports_its_answer_finish_reason_timing_and_usage_keys():
    chunks = sse_chunks("ready", usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15})
    result = interpret("basic", {"basic": outcome(chunks)})
    assert result["status"] == "accepted" and result["answer"] == "ready" and result["finish_reason"] == "stop"
    assert result["usage_keys"] == ["completion_tokens", "prompt_tokens", "total_tokens"]
    assert "in 1.2 s (first byte after 0.4 s)" in result["summary"]
    rejected = interpret("basic", {"basic": outcome(status=400, error="Bad request")})
    assert rejected["status"] == "rejected" and rejected["summary"] == "HTTP 400: Bad request"
    assert interpret("basic", {}) is None  # Not sent: nothing to save.


def test_the_reasoning_check_reads_the_shape_and_signature_and_sees_missing_thinking():
    result = interpret("reasoning", {"reasoning": outcome([*thinking(), *sse_chunks("391")])})
    assert (result["status"], result["reasoning_shape"], result["signature"]) == ("accepted", "content.reasoning", True)
    unsigned = interpret("reasoning", {"reasoning": outcome([*thinking(signature=""), *sse_chunks("391")])})
    assert unsigned["signature"] is False and "without a signature" in unsigned["summary"]
    silent = interpret("reasoning", {"reasoning": outcome(sse_chunks("391"))})
    assert silent["status"] == "not_seen"


def cache_usage(prompt, read, written):
    return {"prompt_tokens": prompt, "completion_tokens": 2, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": written}


def test_the_cache_check_finds_the_level_the_read_and_how_prompt_tokens_count():
    write = outcome(sse_chunks("cached", usage=cache_usage(8, 0, 6_400)))
    read = outcome(sse_chunks("cached", usage=cache_usage(8, 6_400, 0)))
    result = interpret("cache", {"cache_write": write, "cache_read": read})
    assert (result["status"], result["level"], result["cache_read_tokens"], result["prompt_counting"]) == (
        "accepted", "full", 6_400, "separate")
    # prompt_tokens that still counts the cache read: Databricks would count it twice in a sum.
    doubled = outcome(sse_chunks("cached", usage=cache_usage(6_408, 6_400, 0)))
    assert interpret("cache", {"cache_write": write, "cache_read": doubled})["prompt_counting"] == "included"
    assert prompt_counting({"input_tokens": 6_408}, {"input_tokens": 6_408, "cache_read_input_tokens": 6_400}) == "included"
    assert prompt_counting({"input_tokens": 8}, {"input_tokens": 8}) is None
    # OpenAI-style usage: prompt_tokens counts the cached tokens (prompt_tokens_details.cached_tokens), although
    # reported_usage keeps input_tokens as the uncached part.
    nested = outcome(sse_chunks("cached", usage={"prompt_tokens": 6_408, "completion_tokens": 2,
                                                 "prompt_tokens_details": {"cached_tokens": 6_400}}))
    plain = outcome(sse_chunks("cached", usage={"prompt_tokens": 6_408, "completion_tokens": 2}))
    assert nested["parsed"]["usage"]["input_tokens"] == 8 and nested["parsed"]["raw_input_tokens"] == 6_408
    result = interpret("cache", {"cache_write": plain, "cache_read": nested})
    assert result["prompt_counting"] == "included" and "prompt_tokens also counts the cached tokens" in result["summary"]
    # The system-message marker rejected, the documented form accepted, nothing read back.
    rejected = outcome(status=400, error="cache_control is not supported for role system")
    nothing = outcome(sse_chunks("cached", usage=cache_usage(6_408, 0, 0)))
    result = interpret("cache", {"cache_write": rejected, "cache_write_documented": write, "cache_read": nothing})
    assert (result["status"], result["level"]) == ("not_seen", "messages")
    both = interpret("cache", {"cache_write": rejected, "cache_write_documented": rejected})
    assert (both["status"], both["level"]) == ("rejected", "off")


def tool_reply(signed=True, finish="tool_calls"):
    calls = [{"id": "toolu_bdrk_01ABC", "name": "get_weekday", "arguments": {"date": "2026-01-05"}}]
    return [*(thinking() if signed else []), *sse_chunks("", finish=finish, tool_calls=calls)]


def test_the_tool_check_reads_the_finish_reason_and_both_replays():
    call = outcome(tool_reply())
    body = endpoint_probes.body("replay_signed", OPUS, {"tool_call": call})
    assistant = body["messages"][1]
    assert assistant["content"] == [{"type": "reasoning", "summary": [
        {"type": "summary_text", "text": "Plan.", "signature": "sig-123"}]}]
    assert assistant["tool_calls"][0]["id"] == "toolu_bdrk_01ABC" and body["messages"][2]["role"] == "tool"
    plain = endpoint_probes.body("replay_plain", OPUS, {"tool_call": call})["messages"][1]
    assert plain["content"] is None and plain["tool_calls"] == assistant["tool_calls"]
    result = interpret("tools", {"tool_call": call, "replay_plain": outcome(sse_chunks("Monday.")),
                                 "replay_signed": outcome(status=400, error="invalid signature")})
    assert (result["status"], result["finish_reason"], result["replay_plain"], result["replay_signed"]) == (
        "accepted", "tool_calls", "accepted", "rejected")
    # No signature came: the signed replay is not sent.
    unsigned = outcome(tool_reply(signed=False, finish="stop"))
    assert endpoint_probes.body("replay_signed", OPUS, {"tool_call": unsigned}) is None
    result = interpret("tools", {"tool_call": unsigned, "replay_plain": outcome(sse_chunks("Monday."))})
    assert result["replay_signed"] == "skipped" and result["finish_stop_with_calls"] is True
    assert "finish reason stop although a complete tool call came" in result["summary"]


def test_structured_output_and_image_checks():
    assert interpret("response_format", {"response_format": outcome(sse_chunks('{"answer": "5"}'))})["status"] == "accepted"
    assert interpret("response_format", {"response_format": outcome(sse_chunks("5"))})["status"] == "not_seen"
    assert interpret("image", {"image": outcome(status=400, error="image_url not supported")})["status"] == "rejected"
    assert interpret("image", {"image": outcome(sse_chunks("Red."))})["answer"] == "Red."


# Fixtures and the results file

def test_fixtures_keep_no_signature_id_host_or_token():
    chunks = [{"id": "msg_bdrk_01Secret", **chunk} for chunk in tool_reply()]
    chunks.append({"choices": [{"delta": {"content": [{"type": "redacted_thinking", "data": "opaque-bytes"}]}}]})
    sent = {"messages": [{"role": "user", "content": "token private-token in text"}]}
    record = endpoint_probes.fixture(OPUS, "tool_call", sent, {**outcome(chunks), "headers": {"content-type": "text/event-stream"}},
                                     lambda text: text.replace("private-token", "[REDACTED]"))
    text = json.dumps(record)
    assert "sig-123" not in text and "<signature: 7 chars>" in text and "opaque-bytes" not in text
    assert "msg_bdrk_01Secret" not in text and "msg_bdrk_\\u2026" in text and "toolu_bdrk_\\u2026" in text
    assert "private-token" not in text and "[REDACTED]" in text
    assert record["headers"] == {"content-type": "text/event-stream"} and "serving" not in text
    assert sanitize({"type": "summary_encrypted_text", "text": "abc"}) == {"type": "summary_encrypted_text", "text": "<text: 3 chars>"}
    assert endpoint_probes.id_prefix("chatcmpl-9xY") == "chatcmpl-…" and endpoint_probes.id_prefix("42") == "…"
    # A URL a Databricks error names (its workspace host) is not kept either.
    missing = endpoint_probes.fixture(OPUS, "basic", sent, outcome(status=404, error=(
        "Endpoint x not found at https://workspace.example.com/serving-endpoints/x.")))
    assert missing["text"] == "Endpoint x not found at <url>" and "workspace.example" not in json.dumps(missing)
    assert endpoint_probes.without_host("No route to workspace.example.com (https://workspace.example.com/api)",
                                        "https://workspace.example.com") == "No route to <host> (<host>/api)"


def test_the_results_file_is_private_merges_probes_and_survives_damage(tmp_path):
    store = CheckStore(tmp_path)
    store.save(OPUS, "a" * 64, {"basic": {"status": "accepted", "checked": "2026-10-16T00:00:00Z"}}, [])
    store.save(OPUS, "b" * 64, {"image": {"status": "rejected", "checked": "2026-10-16T00:00:00Z"}}, [])
    assert set(store.get(OPUS)["results"]) == {"basic", "image"} and store.get(OPUS.upper())["plan_id"] == "b" * 64
    assert stat.S_IMODE((tmp_path / "endpoint_checks.json").stat().st_mode) == 0o600
    path = store.write_fixture(OPUS, "basic", {"model": OPUS})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.parent == tmp_path / "endpoint_fixtures" / OPUS
    assert store.write_fixture("../escape", "basic", {}) is None and store.write_fixture(OPUS, "../x", {}) is None
    assert store.refused(OPUS, "vision") is True and store.refused(OPUS, "structured_output") is False
    (tmp_path / "endpoint_checks.json").write_text("{not json")
    assert store.get(OPUS) is None and store.problem and store.cache_level(OPUS) is None


def test_the_capability_table_names_probed_documented_and_unknown_sources(tmp_path):
    rows = {row["id"]: row for row in capabilities(OPUS, None)}
    assert rows["streaming"]["source"] == "unknown" and rows["vision"]["source"] == "documented"
    assert rows["cache_control"]["result"] == "Documented (Claude)" and rows["request_limit"]["result"] == "597 s"
    assert {row["id"]: row for row in capabilities(GPT, None)}["vision"]["result"] == "Not documented"
    saved = {"results": {"cache": {"status": "accepted", "level": "full", "cache_read_tokens": 6400,
                                   "prompt_counting": "separate", "checked": "2026-10-16T01:02:03Z"}}}
    rows = {row["id"]: row for row in capabilities(OPUS, saved)}
    assert rows["cache_read"] == {"id": "cache_read", "capability": "Cache read on a repeat", "result": "6,400 tokens read",
                                  "source": "probed 2026-10-16", "status": "accepted"}
    assert rows["cache_counting"]["result"] == "Leaves cached tokens out"


# The routes: approval first, nothing sent without it

@pytest.fixture
async def api(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_MODEL", OPUS)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            yield client, app.state.manager, settings


def replay(manager, *fixtures):
    manager.serving = ReplayClient(manager.settings, fixtures)
    return manager.serving.transport


async def finished(manager):
    await manager.endpoint_runner.task
    return manager.endpoint_runner.public()


async def test_a_run_without_approval_or_for_another_plan_sends_nothing(api):
    client, manager, _ = api
    transport = replay(manager)  # Any request would be recorded (and fail).
    fresh = (await client.post("/api/endpoints/plan", json={"model": OPUS})).json()
    assert fresh["total_dbu"] > 0 and transport.requests == []
    for path, extra in (("/api/endpoints/run", {}), ("/api/endpoints/test", {})):
        body = {"model": OPUS, "plan_id": (fresh if path.endswith("run") else plan(OPUS, ["basic"]))["plan_id"], **extra}
        response = await client.post(path, json=body)
        assert response.status_code == 400 and "no approval" in response.json()["detail"]
    other = plan(OPUS, ["basic", "image"])["plan_id"]
    response = await client.post("/api/endpoints/run", json={"model": OPUS, "plan_id": other, "approved_dbu": 100})
    assert response.status_code == 409 and "nothing was sent" in response.json()["detail"]
    response = await client.post("/api/endpoints/run", json={"model": OPUS, "plan_id": fresh["plan_id"],
                                                             "approved_dbu": fresh["total_dbu"] / 2})
    assert response.status_code == 400 and "below the estimate" in response.json()["detail"]
    assert (await client.post("/api/endpoints/run", json={"model": "../x", "plan_id": "0" * 64,
                                                          "approved_dbu": 1})).status_code == 400
    assert transport.requests == [] and manager.endpoint_runner.task is None
    checks = (await client.get(f"/api/endpoints/{OPUS}/checks")).json()
    assert checks["saved"] is None and checks["run"] is None and transport.requests == []


async def test_test_model_sends_one_small_tagged_request_and_saves_its_result(api):
    client, manager, settings = api
    transport = replay(manager, {"model": OPUS, "chunks": [{"id": "msg_bdrk_1", **chunk} for chunk in sse_chunks(
        "ready", usage={"prompt_tokens": 20, "completion_tokens": 30})]})
    approval = plan(OPUS, ["basic"])
    response = await client.post("/api/endpoints/test", json={"model": OPUS, "plan_id": approval["plan_id"],
                                                              "approved_dbu": approval["total_dbu"]})
    assert response.status_code == 202 and response.json()["kind"] == "test"
    run = await finished(manager)
    assert run["status"] == "completed" and run["requests"][0]["state"] == "done"
    (request,) = transport.requests
    assert request["body"]["max_tokens"] == 2_048 and request["body"]["messages"][0]["content"] == endpoint_probes.BASIC_PROMPT
    assert json.loads(request["headers"][REQUEST_TAGS_HEADER.lower()])["local_agent_purpose"] == "probe"
    checks = (await client.get(f"/api/endpoints/{OPUS}/checks")).json()
    assert checks["saved"]["results"]["basic"]["status"] == "accepted"
    assert checks["saved"]["calls"][0]["estimated_dbu"] == pytest.approx((20 * 57.143 + 30 * 285.714) / 1e6)
    assert checks["capabilities"][0]["source"].startswith("probed 20")
    fixture = json.loads((settings.state_dir / "endpoint_fixtures" / OPUS / "basic.json").read_text())
    assert fixture["status"] == 200 and fixture["chunks"][0]["id"] == "msg_bdrk_…"
    assert "serving.example" not in json.dumps(fixture) and "replay-token" not in json.dumps(fixture)


async def test_a_rate_limit_stops_the_run_and_nothing_is_retried(api):
    client, manager, _ = api
    transport = replay(manager, {"model": OPUS, "chunks": sse_chunks("ready")},
                       {"model": OPUS, "status": 429, "headers": {"Retry-After": "5"}, "text": "Too many requests"})
    approval = plan(OPUS, ["basic", "reasoning", "image"])
    await client.post("/api/endpoints/run", json={"model": OPUS, "probes": ["basic", "reasoning", "image"],
                                                  "plan_id": approval["plan_id"], "approved_dbu": approval["total_dbu"]})
    run = await finished(manager)
    assert run["status"] == "failed" and "Nothing is retried" in run["error"]
    assert [entry["state"] for entry in run["requests"]] == ["done", "done", "not_sent"]
    assert len(transport.requests) == 2
    saved = manager.endpoint_checks.get(OPUS)
    assert saved["results"]["reasoning"]["status"] == "error" and "image" not in saved["results"]


async def test_a_reply_that_breaks_off_stops_the_run_and_error_text_leaves_out_the_host(api):
    client, manager, _ = api
    transport = replay(manager, {"model": OPUS, "chunks": sse_chunks("re")[:1], "break": "read"},
                       {"model": OPUS, "chunks": sse_chunks("391")})
    approval = plan(OPUS, ["basic", "reasoning", "image"])
    await client.post("/api/endpoints/run", json={"model": OPUS, "probes": ["basic", "reasoning", "image"],
                                                  "plan_id": approval["plan_id"], "approved_dbu": approval["total_dbu"]})
    run = await finished(manager)
    assert run["status"] == "failed" and "The connection failed" in run["error"] and "Nothing is retried" in run["error"]
    assert [entry["state"] for entry in run["requests"]] == ["done", "not_sent", "not_sent"]
    assert len(transport.requests) == 1  # Nothing more is sent after a connection failure.
    # An error that names the workspace host keeps it out of the summary and the results file.
    replay(manager, {"model": OPUS, "status": 404, "text": f"Endpoint not found at {ReplayClient.HOST}/serving-endpoints/x"})
    approval = plan(OPUS, ["basic"])
    await client.post("/api/endpoints/test", json={"model": OPUS, "plan_id": approval["plan_id"],
                                                   "approved_dbu": approval["total_dbu"]})
    run = await finished(manager)
    assert run["requests"][0]["summary"] == "HTTP 404: Endpoint not found at <host>/serving-endpoints/x"
    assert "serving.example" not in json.dumps(manager.endpoint_checks.get(OPUS))


async def test_a_follow_up_larger_than_its_estimate_is_not_sent(api):
    client, manager, _ = api
    signed = [{"choices": [{"delta": {"content": [{"type": "reasoning", "summary": [
        {"type": "summary_text", "text": "Plan.", "signature": "S" * 40_000}]}]}, "finish_reason": None}]}]
    transport = replay(manager, {"model": OPUS, "chunks": [*signed, *sse_chunks("", finish="tool_calls", tool_calls=[
        {"id": "toolu_bdrk_01ABC", "name": "get_weekday", "arguments": {"date": "2026-01-05"}}])]},
        {"model": OPUS, "chunks": sse_chunks("Monday.")})
    approval = plan(OPUS, ["tools"])
    await client.post("/api/endpoints/run", json={"model": OPUS, "probes": ["tools"], "plan_id": approval["plan_id"],
                                                  "approved_dbu": approval["total_dbu"]})
    run = await finished(manager)
    assert run["status"] == "completed" and [entry["state"] for entry in run["requests"]] == ["done", "done", "skipped"]
    assert "larger than the estimate you approved" in run["requests"][2]["summary"] and len(transport.requests) == 2
    saved = manager.endpoint_checks.get(OPUS)
    assert saved["results"]["tools"]["replay_signed"] == "too_large" and "not sent (larger than its estimate)" in (
        saved["results"]["tools"]["summary"])
    assert [call["request"] for call in saved["calls"]] == ["tool_call", "replay_plain"]


async def test_stop_cancels_the_request_in_flight_and_keeps_what_came_back(api, monkeypatch):
    client, manager, _ = api
    release = asyncio.Event()
    replay(manager, {"model": OPUS, "chunks": sse_chunks("ready")})
    original = endpoint_checks_api.EndpointCheckRunner.send

    async def send(self, client, host, token, model, body):
        if body["messages"][0]["content"] == endpoint_probes.REASONING_PROMPT:
            await release.wait()  # The second request never answers until stopped.
        return await original(self, client, host, token, model, body)
    monkeypatch.setattr(endpoint_checks_api.EndpointCheckRunner, "send", send)
    approval = plan(OPUS, ["basic", "reasoning"])
    await client.post("/api/endpoints/run", json={"model": OPUS, "probes": ["basic", "reasoning"],
                                                  "plan_id": approval["plan_id"], "approved_dbu": approval["total_dbu"]})
    while manager.endpoint_runner.state["requests"][1]["state"] != "running":
        await asyncio.sleep(0.01)
    busy = await client.post("/api/endpoints/test", json={"model": OPUS, "plan_id": plan(OPUS, ["basic"])["plan_id"],
                                                          "approved_dbu": 1})
    assert busy.status_code == 409  # One run at a time.
    run = (await client.post("/api/endpoints/run/stop")).json()["run"]
    assert run["status"] == "stopped" and [entry["state"] for entry in run["requests"]] == ["done", "stopped"]
    assert set(manager.endpoint_checks.get(OPUS)["results"]) == {"basic"}


async def test_the_endpoint_state_is_read_without_a_model_request(api):
    client, manager, _ = api
    transport = replay(manager, {"path": f"/api/2.0/serving-endpoints/{OPUS}",
                                 "json": {"name": OPUS, "task": "llm/v1/chat", "state": {"ready": "READY"}}})
    state = (await client.get(f"/api/endpoints/{OPUS}/state")).json()
    assert (state["ready"], state["task"], state["error"]) == ("READY", "llm/v1/chat", None)
    assert transport.bodies() == []  # No invocations request.


# Whole runs against the offline simulator, and the hooks that read their results

@pytest.fixture(scope="module")
def simulator():
    spec = importlib.util.spec_from_file_location("simulate_databricks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gateway(simulator):
    gateway = simulator.Gateway
    gateway.speed, gateway.claude_fields, gateway.cache_mode = 0.0, "both", "accept"
    gateway.replay_mode = gateway.vision_mode = gateway.response_format_mode = "accept"
    gateway.prompt_cache = simulator.PromptCache()
    server = ThreadingHTTPServer(("127.0.0.1", 0), gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield gateway, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


class GatewayClient(ServingClient):
    def __init__(self, settings, host):
        super().__init__(settings)
        self.host = host

    def credentials(self):
        return self.host, "simulated-token"


async def run_all(client, manager, model=OPUS):
    approval = (await client.post("/api/endpoints/plan", json={"model": model})).json()
    response = await client.post("/api/endpoints/run", json={"model": model, "plan_id": approval["plan_id"],
                                                             "approved_dbu": approval["total_dbu"] or 0})
    assert response.status_code == 202
    return await finished(manager)


async def test_a_whole_check_runs_against_the_simulator(api, gateway):
    client, manager, _ = api
    manager.serving = GatewayClient(manager.settings, gateway[1])
    run = await run_all(client, manager)
    assert run["status"] == "completed", run
    states = {entry["id"]: entry["state"] for entry in run["requests"]}
    assert states["cache_write_documented"] == "skipped" and states["replay_signed"] == "done"
    results = manager.endpoint_checks.get(OPUS)["results"]
    assert {probe: result["status"] for probe, result in results.items()} == {
        "basic": "accepted", "reasoning": "accepted", "cache": "accepted", "tools": "accepted",
        "response_format": "accepted", "image": "accepted"}
    assert results["reasoning"]["reasoning_shape"] == "content.reasoning" and results["reasoning"]["signature"] is True
    assert results["cache"]["level"] == "full" and results["cache"]["prompt_counting"] == "included"
    assert results["cache"]["cache_read_tokens"] > 5_000
    assert (results["tools"]["replay_plain"], results["tools"]["replay_signed"]) == ("accepted", "accepted")
    assert results["basic"]["answer"] == "ready" and results["image"]["answer"] == "Red."
    assert run["reported_dbu"] > 0
    rows = {row["id"]: row for row in (await client.get(f"/api/endpoints/{OPUS}/checks")).json()["capabilities"]}
    assert rows["replay"]["result"] == "Accepted" and rows["cache_control"]["result"] == "All markers accepted"
    # Nothing the checks found changes agent requests here: every marker and the image part were accepted.
    session = manager.store.create(manager.settings.values)
    assert manager.cache_level(session) == "full" and manager.supports(OPUS, "vision") is True


async def test_refusals_found_by_a_check_reach_only_their_hooks(api, gateway):
    client, manager, settings = api
    simulated, host = gateway
    simulated.replay_mode, simulated.vision_mode, simulated.cache_mode = "reject", "reject", "reject"
    simulated.response_format_mode = "reject"
    manager.serving = GatewayClient(settings, host)
    run = await run_all(client, manager)
    assert run["status"] == "completed"
    results = manager.endpoint_checks.get(OPUS)["results"]
    assert results["cache"]["level"] == "off" and results["tools"]["replay_signed"] == "rejected"
    assert results["image"]["status"] == "rejected" and results["response_format"]["status"] == "rejected"
    session = manager.store.create(settings.values)
    assert manager.cache_level(session) == "off"  # The cache ladder starts where the check ended.
    assert manager.supports(OPUS, "vision") is False
    settings.values["thinking_replay"] = True
    assert manager.replay_model(session) is None  # Send earlier thinking back skips this endpoint.
    limits = (await client.post("/api/settings/limits", json={"model": OPUS, "context_window": 200_000,
                                                              "max_output_tokens": 16_384})).json()
    assert limits["profile"]["vision"] is False


async def test_a_gpt_oss_check_is_priced_at_its_list_rates_and_has_no_signed_replay(api, gateway):
    client, manager, _ = api
    manager.serving = GatewayClient(manager.settings, gateway[1])
    run = await run_all(client, manager, GPT)
    assert run["status"] == "completed" and run["approved_dbu"] == plan(GPT)["total_dbu"]
    results = manager.endpoint_checks.get(GPT)["results"]
    assert results["tools"]["replay_signed"] == "skipped" and results["image"]["status"] == "rejected"
    assert 0 < run["reported_dbu"] <= run["approved_dbu"]


def test_the_readme_cost_figures_match_the_plan():
    readme = " ".join((Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8").split())
    total, test = plan(OPUS)["total_dbu"], plan(OPUS, ["basic"])["total_dbu"]
    assert f"at most about {total:.1f} DBU for all checks on Claude Opus 5.5 and {test:.1f} DBU for Test model" in readme
