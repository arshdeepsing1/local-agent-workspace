import asyncio
import copy
import json
import math

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.context import (
    MAX_ESTIMATE_SCALE, MIN_CONTEXT_WINDOW, REPLY_RESERVE, SAFETY_MARGIN, SUMMARY_MAX_BYTES,
    SUMMARY_MAX_TOKENS, SUMMARY_MIN_BYTES, SUMMARY_TRIM_MARKER, build_condense_messages,
    build_summary_messages, context_breakdown, estimate_scale, estimate_tokens, fit_summary,
    prepare_context, summary_byte_limit, trim_summary,
)
from local_agent.portability import export_bundle
from local_agent.store import Store


SYSTEM = {"role": "system", "content": "Follow the user's current request."}
TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
WINDOW = 32768
LIMIT = summary_byte_limit(WINDOW - REPLY_RESERVE - SAFETY_MARGIN)


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


def long_history():
    return [user("old " * 22500), assistant("Old work done."),
            user("Previous request"), assistant("Previous answer"), user("Latest request")]


def call(model="m", estimate=10000, status="completed", **usage):
    return {"model": model, "status": status, "estimated_input_tokens": estimate,
            "usage": usage or {"input_tokens": estimate}}


# --- Summary size limit -----------------------------------------------------

def test_summary_limit_scales_with_input_budget_within_bounds():
    assert summary_byte_limit(MIN_CONTEXT_WINDOW - REPLY_RESERVE - SAFETY_MARGIN) == SUMMARY_MIN_BYTES
    assert summary_byte_limit(WINDOW - REPLY_RESERVE - SAFETY_MARGIN) == LIMIT == 8448
    assert summary_byte_limit(131000 - 20000 - SAFETY_MARGIN) == SUMMARY_MAX_BYTES
    # The largest accepted summary stays below what one summary reply can produce.
    assert SUMMARY_MAX_BYTES < SUMMARY_MAX_TOKENS * 3


def test_summary_and_condense_prompts_state_the_active_limit():
    summary_prompt = build_summary_messages("prev", "chunk", "", 8448)[0]["content"]
    condense = build_condense_messages("long summary", 8448, "Keep the migration plan")
    assert "8,448 UTF-8 bytes" in summary_prompt and "1,056 words" in summary_prompt
    assert "8,448 UTF-8 bytes" in condense[0]["content"]
    assert "Keep the migration plan" in condense[1]["content"]
    assert condense[1]["content"].endswith("Summary to shorten:\nlong summary")


async def test_summary_within_the_raised_limit_is_accepted_unchanged():
    # The reported failure: a ~7.5 KB summary used to be rejected at 3,500 bytes.
    reply = "Decision recorded. " * 400
    assert SUMMARY_MIN_BYTES < len(reply.encode()) <= LIMIT

    async def summarize(previous, chunk, limit_bytes):
        assert limit_bytes == LIMIT
        return reply

    async def condense(summary, limit_bytes):
        pytest.fail("A summary within the limit must not be condensed")

    _, state, info = await prepare_context(long_history(), {}, SYSTEM, TOOLS, WINDOW, summarize, condense=condense)
    assert state["summary"] == reply.strip()
    assert "summary_adjustment" not in info


# --- Condense and trim instead of discarding ----------------------------------

async def test_oversized_summary_is_condensed_by_a_follow_up_request():
    oversized = "Detail. " * (LIMIT // 8 + 200)
    condensed = []

    async def summarize(previous, chunk, limit_bytes):
        return oversized

    async def condense(summary, limit_bytes):
        condensed.append((summary, limit_bytes))
        return "  Short, complete summary.  "

    wire = long_history()
    original = copy.deepcopy(wire)
    messages, state, info = await prepare_context(wire, {}, SYSTEM, TOOLS, WINDOW, summarize, condense=condense)
    # Each summarized chunk gets its own fitting pass.
    assert condensed and all(item == (oversized.strip(), LIMIT) for item in condensed)
    assert state["summary"] == "Short, complete summary."
    assert state["compactions"] == 1
    assert info["summary_adjustment"] == "condensed"
    assert messages[2:] == wire[2:] and wire == original


@pytest.mark.parametrize("outcome", ["none", "error", "empty", "still_long"])
async def test_oversized_summary_is_trimmed_when_condensing_is_unavailable_or_fails(outcome):
    oversized = "HEAD " + "middle " * 3000 + " TAIL"

    async def summarize(previous, chunk, limit_bytes):
        return oversized

    async def condense(summary, limit_bytes):
        if outcome == "error":
            raise RuntimeError("Context compaction failed (HTTP 500)")
        return "" if outcome == "empty" else "HEAD " + "shorter " * 1500 + " TAIL"

    _, state, info = await prepare_context(long_history(), {}, SYSTEM, TOOLS, WINDOW, summarize,
                                           condense=None if outcome == "none" else condense)
    summary = state["summary"]
    assert len(summary.encode()) <= LIMIT
    assert summary.startswith("HEAD") and summary.endswith("TAIL")
    assert SUMMARY_TRIM_MARKER in summary
    # A condensed reply that is shorter but still too long is trimmed in preference.
    assert ("shorter" in summary) is (outcome == "still_long")
    assert info["summary_adjustment"] == "trimmed"


@pytest.mark.parametrize("raised", ["condense", "summarize"])
async def test_cancellation_during_summary_fitting_commits_nothing(raised):
    wire, state = long_history(), {"summary": "", "through": 0, "compactions": 0}
    original = copy.deepcopy((wire, state))

    async def summarize(previous, chunk, limit_bytes):
        if raised == "summarize":
            raise asyncio.CancelledError()
        return "x" * (limit_bytes + 1)

    async def condense(summary, limit_bytes):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await prepare_context(wire, state, SYSTEM, TOOLS, WINDOW, summarize, condense=condense)
    assert (wire, state) == original


@pytest.mark.parametrize("text", ["🙂" * 5000, "界a" * 4000, "x" * 20000])
def test_trim_keeps_valid_utf8_start_and_end_within_limit(text):
    trimmed = trim_summary(text, 3500)
    assert len(trimmed.encode("utf-8")) <= 3500
    assert trimmed.encode("utf-8").decode("utf-8") == trimmed
    assert trimmed.startswith(text[:2]) and trimmed.endswith(text[-2:])
    assert trim_summary("short", 3500) == "short"


async def test_fit_summary_prefers_the_original_when_condensing_grows_it():
    async def condense(summary, limit_bytes):
        return summary + " and more"

    fitted, adjustment = await fit_summary("y" * 5000, 3500, condense)
    assert adjustment == "trimmed" and "more" not in fitted


# --- Estimate calibration -----------------------------------------------------

def test_estimate_scale_uses_recent_same_model_completed_samples():
    assert estimate_scale([], "m") == 1.0
    assert estimate_scale(None, "m") == 1.0
    assert estimate_scale([call(input_tokens=12650)], "m") == 1.265
    ignored = [
        call(model="other", input_tokens=30000),
        call(status="error", input_tokens=30000),
        call(estimate=500, input_tokens=1500),
        {"model": "m", "status": "completed", "usage": {"input_tokens": 30000}},
        {"model": "m", "status": "completed", "estimated_input_tokens": 10000},
        call(input_tokens=True),
        "not a call",
    ]
    assert estimate_scale([call(input_tokens=11000), *ignored], "m") == 1.1


def test_estimate_scale_adds_cache_buckets_weights_by_size_and_clamps():
    cached = call(input_tokens=2000, cache_read_input_tokens=9000, cache_creation_input_tokens=1000)
    assert estimate_scale([cached], "m") == 1.2
    # Large requests dominate: (13000 + 1500) / (10000 + 1000).
    assert estimate_scale([call(input_tokens=13000), call(estimate=1000, input_tokens=1500)], "m") == 1.318
    assert estimate_scale([call(input_tokens=8000)], "m") == 1.0
    assert estimate_scale([call(input_tokens=90000)], "m") == MAX_ESTIMATE_SCALE


def test_estimate_scale_only_uses_the_latest_five_samples():
    old = [call(input_tokens=19000)] * 3
    recent = [call(input_tokens=11000)] * 5
    assert estimate_scale([*old, *recent], "m") == 1.1


async def test_scaled_estimate_triggers_compaction_the_raw_estimate_would_skip():
    wire = [user("x" * 45000), assistant("done"), user("Latest request")]
    raw = estimate_tokens([SYSTEM, *wire], TOOLS)
    budget = WINDOW - REPLY_RESERVE - SAFETY_MARGIN
    assert raw <= budget < math.ceil(raw * 1.5)
    summarized = []

    async def summarize(previous, chunk, limit_bytes):
        request = build_summary_messages(previous, chunk, "", limit_bytes)
        assert math.ceil(estimate_tokens(request) * 1.5) <= WINDOW - SUMMARY_MAX_TOKENS - SAFETY_MARGIN
        summarized.append(chunk)
        return "Earlier file review summarized."

    _, unscaled_state, _ = await prepare_context(
        wire, {}, SYSTEM, TOOLS, WINDOW, summarize)
    assert unscaled_state["compactions"] == 0 and not summarized
    messages, state, info = await prepare_context(wire, {}, SYSTEM, TOOLS, WINDOW, summarize, scale=1.5)
    assert state["compactions"] == 1 and summarized
    assert info["estimate_scale"] == 1.5
    assert info["estimated_tokens"] == math.ceil(estimate_tokens(messages, TOOLS) * 1.5) <= budget
    assert info["breakdown"] == context_breakdown(messages, TOOLS, True, 1.5)
    assert sum(info["breakdown"].values()) == info["estimated_tokens"]
    assert all(value >= 0 for value in info["breakdown"].values())


@pytest.mark.parametrize("scale", [0.9, MAX_ESTIMATE_SCALE + 0.1, True, "1.2", float("nan")])
async def test_invalid_estimate_scale_is_rejected(scale):
    async def summarize(previous, chunk, limit_bytes):
        pytest.fail("Summarization should not run")

    with pytest.raises(ValueError, match="estimate scale"):
        await prepare_context([user("Hello")], {}, SYSTEM, TOOLS, WINDOW, summarize, scale=scale)


# --- Agent integration ----------------------------------------------------------

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    # These tests cover summary fitting; test_compaction_handoffs covers handoff mode.
    settings.values.update(workspace=str(project), env_file="", context_window=WINDOW, compaction_handoffs=False)
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    yield manager, session
    store.db.close()


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(gateway)))


def stream_reply(text="Done.", usage=None):
    chunk = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}
    if usage:
        chunk["usage"] = usage
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


def summary_reply(text, prompt_tokens=20000):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 2000}})


def with_long_history(session):
    session["wire"] = [message for i in range(4) for message in (
        user(f"Earlier request {i}: " + "x" * 21000), assistant(f"Completed step {i}."))]


def notices(session):
    return [event["text"] for event in session["events"] if event["type"] == "notice"]


@pytest.mark.parametrize("condense_status", [200, 500])
async def test_agent_keeps_an_oversized_summary_by_condensing_or_trimming(runtime, monkeypatch, condense_status):
    manager, session = runtime
    with_long_history(session)
    archive = copy.deepcopy(session["wire"])
    sent = []

    async def gateway(request):
        payload = json.loads(request.content)
        if payload["stream"]:
            sent.append(payload)
            return stream_reply()
        if "Summary to shorten" in payload["messages"][1]["content"]:
            if condense_status != 200:
                return httpx.Response(condense_status, json={"error_code": "INTERNAL_ERROR"})
            return summary_reply("Condensed: steps 0-3 completed.", prompt_tokens=3000)
        return summary_reply("Step detail. " * 1000)

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Continue with the remaining work.")

    summary = session["context_state"]["summary"]
    assert session["wire"][:len(archive)] == archive
    assert len(summary.encode()) <= LIMIT
    assert session["context_info"]["summary_adjustment"] == ("condensed" if condense_status == 200 else "trimmed")
    assert (summary == "Condensed: steps 0-3 completed.") is (condense_status == 200)
    assert len(sent) == 1 and summary in sent[0]["messages"][1]["content"]
    compactions = [item for item in session["inference_calls"] if item["purpose"] == "compaction"]
    assert [item["status"] for item in compactions][-1] == ("completed" if condense_status == 200 else "error")
    assert all(type(item["estimated_input_tokens"]) is int for item in compactions)
    expected = "condensed it" if condense_status == 200 else "could not be condensed"
    assert any(expected in text for text in notices(session))
    assert not [event for event in session["events"] if event["type"] == "error"]


async def test_agent_calibrates_budget_from_reported_input_tokens(runtime, monkeypatch):
    manager, session = runtime
    session["inference_calls"] = [{"id": "earlier", "purpose": "agent", "model": session["model"],
                                   "status": "completed", "estimated_input_tokens": 10000,
                                   "usage": {"input_tokens": 12500, "output_tokens": 10}}]
    manager.store.save(session)
    sent = []

    async def gateway(request):
        payload = json.loads(request.content)
        sent.append(payload)
        return stream_reply(usage={"prompt_tokens": 7000, "completion_tokens": 5})

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    raw = estimate_tokens(sent[0]["messages"], sent[0]["tools"])
    info = session["context_info"]
    assert info["estimate_scale"] == 1.25
    assert info["estimated_tokens"] == math.ceil(raw * 1.25)
    agent_calls = [item for item in session["inference_calls"] if item["purpose"] == "agent"]
    assert agent_calls[-1]["estimated_input_tokens"] == raw
    assert "estimated_input_tokens" not in session["inference_calls"][-1]  # title request
    # The unscaled estimate is recorded, so calibration never compounds itself.
    restarted = AgentManager(manager.store, manager.settings).get(session["id"])
    assert [item for item in restarted["inference_calls"] if item["purpose"] == "agent"][-1][
        "estimated_input_tokens"] == raw


async def test_manual_compaction_condenses_and_reports_it(runtime, monkeypatch):
    manager, session = runtime
    with_long_history(session)
    session["wire"].append(user("What is next?"))
    manager.store.save(session)
    prompts = []

    async def gateway(request):
        payload = json.loads(request.content)
        assert payload["stream"] is False and "tools" not in payload
        prompts.append(payload["messages"][1]["content"])
        if "Summary to shorten" in prompts[-1]:
            return summary_reply("Condensed with the preserved plan.", prompt_tokens=3000)
        return summary_reply("Step detail. " * 1000)

    mock_gateway(monkeypatch, gateway)
    manager.start_compaction(session["id"], "Preserve the plan")
    await manager.tasks[session["id"]]
    saved = manager.store.get(session["id"])
    assert saved["context_state"]["summary"] == "Condensed with the preserved plan."
    assert saved["context_info"]["summary_adjustment"] == "condensed"
    assert all("Preserve the plan" in prompt for prompt in prompts)
    assert any("condensed it" in event["text"] for event in saved["events"] if event["type"] == "notice")


# --- Export and import --------------------------------------------------------

def test_export_accepts_the_new_summary_limit_and_context_fields(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    manager = create_app(settings).state.manager
    try:
        source = manager.store.create(settings.values)
        source["wire"] = [user("Earlier"), assistant("Done."), user("Latest")]
        source["context_state"] = {"summary": "é" * (SUMMARY_MAX_BYTES // 2), "through": 2, "compactions": 1}
        source["context_info"] = {
            "estimated_tokens": 5000, "input_budget": 20000, "context_window": WINDOW, "reply_reserve": 8192,
            "compactions": 1, "summarized_messages": 2, "estimate_method": "weighted_utf8",
            "estimate_scale": 1.265, "summary_adjustment": "condensed"}
        bundle = export_bundle([source], source["id"])
        exported = bundle["sessions"][0]
        assert exported["context_info"]["estimate_scale"] == 1.265
        assert exported["context_info"]["summary_adjustment"] == "condensed"
        assert len(exported["context_state"]["summary"].encode()) == SUMMARY_MAX_BYTES
        source["context_state"]["summary"] += "é"
        with pytest.raises(ValueError):
            export_bundle([source], source["id"])
    finally:
        manager.store.db.close()
