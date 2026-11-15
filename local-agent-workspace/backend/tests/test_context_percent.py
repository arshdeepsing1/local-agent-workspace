"""One context percentage (Story 4): context_info carries percent_used, the limit it is measured against, whether
it is an estimate, where compaction starts on the same scale, and the last request's cached tokens. The page shows
these fields and computes nothing."""
import copy
import json
import math

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.context import context_reading, estimate_tokens, last_request_report, prepare_context, with_context_reading
from local_agent.prompt_cache import unmarked
from local_agent.store import Store

REAL_CLIENT = httpx.AsyncClient
SYSTEM = {"role": "system", "content": "Rules."}
TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file.", "parameters": {}}}]
FIELDS = {"percent_used": int, "used_tokens": int, "limit_tokens": int, "limit_name": str, "is_estimate": bool,
          "compact_at_percent": int, "last_request_cached": int, "warn": str}

# context_info of a fixed request as the code built it before these fields existed (32,768 window, 4,096 reserve).
OLD_INFO = {
    "estimated_tokens": 1102, "input_budget": 26624, "context_window": 32768, "reply_reserve": 4096, "compactions": 0,
    "summarized_messages": 0, "estimate_method": "weighted_utf8",
    "breakdown": {"system_instructions": 15, "tool_definitions": 42, "messages_and_results": 722, "summary": 0,
                  "request_overhead": 323},
    "tool_groups": {"Built-in tools": 42}, "auto_compact_threshold": 23961, "auto_compact_percent": 90,
    "compactable": True, "estimate_scale": 1.2}
WIRE = [{"role": "user", "content": "Hello " * 200}, {"role": "assistant", "content": "Hi there."},
        {"role": "user", "content": "Next " * 100}]


async def forbidden(*args):
    raise AssertionError("no summary expected")


def info_with(**changes):
    return {**copy.deepcopy(OLD_INFO), **changes}


def report_for(info, input_tokens, model="m", **changes):
    """A last_request report of a request that sent input_tokens, made as the app makes it."""
    call = {"id": "c1", "model": model, "status": "completed", "finish_reason": "stop",
            "usage": {"input_tokens": input_tokens, "output_tokens": 5}}
    report = last_request_report(call, info, 131_072)
    return {**report, **changes}


def check_types(fields):
    for name, kind in FIELDS.items():
        assert type(fields[name]) is kind, name


# The pure function

def test_every_field_is_present_and_typed_for_a_chat_with_no_request_yet():
    fields = context_reading(info_with())
    assert set(FIELDS) <= set(fields)
    check_types(fields)
    assert fields["is_estimate"] is True and fields["used_tokens"] == 1102 and fields["last_request_cached"] == 0
    assert fields["limit_name"] == "context budget" and fields["limit_tokens"] == 32768


def test_percent_used_is_the_rounded_share_of_the_context_budget():
    for used in (1, 1102, 16_384, 26_000, 32_768, 40_000):
        fields = context_reading(info_with(estimated_tokens=used))
        assert fields["used_tokens"] == used and fields["limit_tokens"] == 32768
        assert fields["percent_used"] == round(100 * used / 32768)


def test_the_compaction_mark_is_the_threshold_on_the_same_scale_and_below_100_by_default():
    fields = context_reading(info_with())
    assert fields["compact_at_percent"] == round(100 * 23961 / 32768) == 73
    assert fields["compact_at_percent"] < 100
    defaults = info_with(context_window=200_000, input_budget=181_568, auto_compact_threshold=163_411)
    assert context_reading(defaults)["compact_at_percent"] == round(100 * 163_411 / 200_000) < 100


def test_a_current_provider_report_gives_its_count_and_is_not_an_estimate():
    info = info_with()
    info["last_request"] = report_for(info, 9_000)
    fields = context_reading(info, "m")
    assert fields["is_estimate"] is False and fields["used_tokens"] == 9_000
    assert fields["percent_used"] == round(100 * 9_000 / 32768)


@pytest.mark.parametrize("kind", ["no_report", "model_changed", "budget_changed", "compacted", "preview", "bad_shape"])
def test_no_report_or_a_stale_or_unrecognised_one_is_an_estimate(kind):
    info = info_with()
    if kind != "no_report":
        info["last_request"] = report_for(info, 9_000)
    model = "m"
    if kind == "model_changed":
        info["estimated_tokens"] = 1200  # A request to the new model was prepared since.
        model = "other"
    elif kind == "budget_changed":
        info["input_budget"] = 20_000
    elif kind == "compacted":
        info["compactions"] = 1
    elif kind == "preview":
        info["prepared_for_next_turn"] = True
    elif kind == "bad_shape":
        # Cache counts with no prompt_tokens (telemetry.reported_usage keeps no input_tokens), so no report is made.
        usage = {"cache_read_input_tokens": 500, "cache_creation_input_tokens": 100, "completion_tokens": 5}
        assert last_request_report({"usage": usage_of(usage)}, info) is None
        info.pop("last_request")
    fields = context_reading(info, model)
    assert fields["is_estimate"] is True and fields["used_tokens"] == info["estimated_tokens"]
    check_types(fields)


def usage_of(raw):
    from local_agent.telemetry import reported_usage
    return reported_usage(raw)


def test_a_setup_change_after_the_last_request_uses_the_next_request_estimate():
    info = info_with()
    info["last_request"] = report_for(info, 9_000)
    info["input_budget"], info["auto_compact_threshold"] = 20_000, 18_000
    info["next_estimate"] = {"tokens": 8_000, "scale": 1.2, "percent_of_input_budget": 40.0}
    fields = context_reading(info, "m")
    assert fields["is_estimate"] is True and fields["used_tokens"] == 8_000
    assert fields["percent_used"] == round(100 * 8_000 / 32768)
    assert fields["compact_at_percent"] == round(100 * 18_000 / 32768)


def test_inconsistent_report_numbers_fall_back_to_the_estimate():
    info = info_with()
    good = report_for(info, 9_000)
    for broken in ({"input_tokens": "9000"}, {"input_tokens": 0}, {"input_tokens": -4},
                   {"uncached_input_tokens": 100, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
                   {"percent_of_input_budget": None}, {"usage_recognised": False}):
        fields = context_reading({**info, "last_request": {**good, **broken}}, "m")
        assert fields["is_estimate"] is True and fields["used_tokens"] == 1102, broken


def test_a_report_with_cache_counts_but_no_total_is_not_trusted():
    info = info_with()
    raw = {"prompt_tokens": 20_000, "completion_tokens": 5, "cache_read_input_tokens": 12_000,
           "cache_creation_input_tokens": 3_000}  # total_tokens missing: prompt_tokens may or may not include the cache.
    call = {"id": "c1", "model": "m", "status": "completed", "finish_reason": "stop", "usage": usage_of(raw),
            "usage_keys": sorted(raw)}
    report = last_request_report(call, info, 131_072)
    assert report is not None and report["usage_recognised"] is False
    fields = context_reading({**info, "last_request": report}, "m")
    assert fields["is_estimate"] is True and fields["used_tokens"] == 1102
    # The report is not the request the meter shows, so its cache count is not shown either (L6).
    assert fields["last_request_cached"] == 0 and fields["is_estimate"] is True


def test_the_inclusive_cache_shape_of_the_real_endpoint_reads_about_85_percent_and_never_over_100():
    info = info_with(estimated_tokens=27_000)
    raw = {"prompt_tokens": 27_850, "completion_tokens": 40, "total_tokens": 27_890, "cache_read_input_tokens": 22_000,
           "cache_creation_input_tokens": 4_000}
    call = {"id": "c1", "model": "m", "status": "completed", "finish_reason": "stop", "usage": usage_of(raw),
            "usage_keys": sorted(raw)}
    report = last_request_report(call, info, 131_072)
    assert report["input_tokens"] == 27_850 and "usage_recognised" not in report
    fields = context_reading({**info, "last_request": report}, "m")
    assert fields["is_estimate"] is False and fields["used_tokens"] == 27_850
    assert fields["percent_used"] == round(100 * 27_850 / 32768) == 85 <= 100
    assert fields["last_request_cached"] == 22_000 and fields["is_estimate"] is False


def test_a_request_that_truly_does_not_fit_reads_over_100_unclamped():
    info = info_with(estimated_tokens=40_000)
    fields = context_reading(info)
    assert fields["percent_used"] == round(100 * 40_000 / 32768) == 122
    info["last_request"] = report_for(info, 40_000)
    assert context_reading(info, "m")["percent_used"] == 122


def test_malformed_input_never_raises():
    assert context_reading({}) == {} and context_reading(None) == {}
    assert context_reading({"estimated_tokens": 5, "context_window": 0}) == {}
    fields = context_reading({"estimated_tokens": 5, "context_window": 100, "last_request": "x", "next_estimate": []})
    assert fields["percent_used"] == 5 and fields["last_request_cached"] == 0


def test_warn_is_none_well_below_near_at_85_percent_of_the_threshold_and_at_threshold_when_over_it():
    # The threshold is 23,961 tokens; 85% of it is 20,366.85.
    assert context_reading(info_with(estimated_tokens=1102))["warn"] == "none"
    assert context_reading(info_with(estimated_tokens=20366))["warn"] == "none"
    assert context_reading(info_with(estimated_tokens=20367))["warn"] == "near"
    assert context_reading(info_with(estimated_tokens=23961))["warn"] == "near"
    assert context_reading(info_with(estimated_tokens=23962))["warn"] == "at_threshold"


def test_with_context_reading_adds_fields_and_keeps_every_other_key():
    info = info_with()
    merged = with_context_reading(info)
    assert {key: merged[key] for key in info} == info and set(FIELDS) <= set(merged) and merged is not info


# prepare_context

async def test_prepare_context_keeps_every_old_key_and_adds_the_fields():
    _, _, info = await prepare_context(WIRE, None, SYSTEM, TOOLS, 32768, forbidden, reply_reserve=4096, scale=1.2,
                                       auto_compact_percent=90)
    assert {key: info[key] for key in OLD_INFO} == OLD_INFO
    assert set(info) - set(OLD_INFO) == set(FIELDS)
    check_types(info)
    assert info["percent_used"] == round(100 * 1102 / 32768) and info["is_estimate"] is True


async def test_a_just_compacted_chat_has_the_fields():
    calls = []

    async def summarize(previous, chunk, limit):
        calls.append(chunk)
        return "## Goal and current status\nWork."
    wire = [{"role": "user", "content": "old " * 6000}, {"role": "assistant", "content": "Done."},
            {"role": "user", "content": "more " * 6000}, {"role": "assistant", "content": "Done too."},
            {"role": "user", "content": "Latest"}]
    _, _, info = await prepare_context(wire, None, SYSTEM, TOOLS, 16_384, summarize, reply_reserve=2_048, scale=1.0,
                                       auto_compact_percent=50)
    assert calls and info["compactions"] == 1
    check_types(info)
    assert info["used_tokens"] == info["estimated_tokens"] and info["percent_used"] == round(
        100 * info["estimated_tokens"] / 16_384)


# Through the real manager (fake gateway)

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", compaction_handoffs=False, context_window=32_768,
                           max_output_tokens=4_096)
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Percent", title_generated=True, permission_mode="bypassPermissions")
    store.save(session)
    yield AgentManager(store, settings), session
    store.db.close()


def gateway(monkeypatch, usage):
    def handle(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}]})
        sent = unmarked(body)
        chunk = {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}],
                 "usage": usage(estimate_tokens(sent["messages"], sent["tools"]))}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(handle)))


def inclusive(raw):
    """The real endpoint's shape: prompt_tokens includes the cache counts, total_tokens is prompt plus completion."""
    prompt = raw * 85 // 100
    return {"prompt_tokens": prompt, "completion_tokens": 7, "total_tokens": prompt + 7,
            "cache_read_input_tokens": prompt * 6 // 10, "cache_creation_input_tokens": prompt // 10}


async def test_a_turn_through_the_manager_reads_the_reported_count_and_its_cache(runtime, monkeypatch):
    manager, session = runtime
    gateway(monkeypatch, inclusive)
    await manager.run(session, "Hello " * 200)
    info = session["context_info"]
    check_types(info)
    call = [call for call in session["inference_calls"] if call["purpose"] == "agent"][-1]
    fields = context_reading(info, session["model"])
    assert fields["is_estimate"] is False
    # The stored reading follows the report as soon as it arrives (note_last_request), not at the next prepare.
    assert {name: info[name] for name in FIELDS} == fields
    # The reported input is the whole prompt (cache included), counted once.
    assert fields["used_tokens"] == call["usage"]["input_tokens"] + call["usage"]["cache_read_input_tokens"] \
        + call["usage"]["cache_creation_input_tokens"] == info["last_request"]["input_tokens"]
    assert fields["percent_used"] == round(100 * fields["used_tokens"] / fields["limit_tokens"]) <= 100
    assert fields["last_request_cached"] == call["usage"]["cache_read_input_tokens"] > 0
    # A model change moves the budgets (refresh_meter_limits): the report is history, the next request's estimate shows.
    moved = {**info, "input_budget": info["input_budget"] - 1_000}
    changed = context_reading(moved, session["model"])
    assert changed["is_estimate"] is True and changed["used_tokens"] == info["next_estimate"]["tokens"]


async def test_a_subagent_chat_has_the_fields(runtime, monkeypatch):
    manager, session = runtime
    session["is_subagent"] = True
    gateway(monkeypatch, lambda raw: {"prompt_tokens": raw, "completion_tokens": 5})
    await manager.run(session, "Check the logs")
    info = session["context_info"]
    check_types(info)
    assert info["limit_tokens"] == 32_768 and info["percent_used"] == round(100 * info["used_tokens"] / 32_768)
    assert math.isfinite(info["compact_at_percent"]) and info["compact_at_percent"] < 100


async def test_a_real_turn_ends_with_a_reading_from_the_providers_count(runtime, monkeypatch):
    manager, session = runtime
    gateway(monkeypatch, lambda raw: {"prompt_tokens": raw * 2, "completion_tokens": 5})
    await manager.run(session, "Hello " * 200)
    info = session["context_info"]
    call = [call for call in session["inference_calls"] if call["purpose"] == "agent"][-1]
    assert info["is_estimate"] is False
    assert info["used_tokens"] == info["last_request"]["input_tokens"] == call["usage"]["input_tokens"]
    assert info["percent_used"] == round(100 * call["usage"]["input_tokens"] / 32_768)
    assert info["used_tokens"] != info["estimated_tokens"]  # The provider's count, not the app's estimate.


async def test_a_context_window_change_makes_the_reading_an_estimate_until_the_next_request(runtime, monkeypatch):
    manager, session = runtime
    gateway(monkeypatch, lambda raw: {"prompt_tokens": raw, "completion_tokens": 5})
    await manager.run(session, "Hello " * 200)
    assert session["context_info"]["is_estimate"] is False
    manager.settings.values.update(context_window=65_536)
    await manager.refresh_meter_limits(session)
    info = session["context_info"]
    assert info["limit_tokens"] == 65_536 and info["is_estimate"] is True
    assert info["used_tokens"] == info["next_estimate"]["tokens"]
    assert info["percent_used"] == round(100 * info["used_tokens"] / 65_536)
    assert info["compact_at_percent"] == round(100 * info["auto_compact_threshold"] / 65_536)
    await manager.run(session, "Again " * 100)
    assert session["context_info"]["is_estimate"] is False and session["context_info"]["limit_tokens"] == 65_536


# A report whose cache counts cannot be placed, or that looks counted twice, never reaches the meter (H1)

BIG = "Hello " * 6_500


def prompt_free(raw):
    """Claude-style names with no total_tokens: input_tokens may or may not include the cache read."""
    return {"input_tokens": raw, "cache_read_input_tokens": raw * 9 // 10, "output_tokens": 5}


def prompt_free_with_write(raw):
    return {"input_tokens": raw, "cache_read_input_tokens": raw * 9 // 10, "cache_creation_input_tokens": raw // 10,
            "output_tokens": 5}


@pytest.mark.parametrize("shape", [prompt_free, prompt_free_with_write])
async def test_a_report_with_cache_counts_and_no_total_reads_the_estimate_whatever_the_key_is_named(
        runtime, monkeypatch, shape):
    manager, session = runtime
    gateway(monkeypatch, shape)
    await manager.run(session, BIG)
    info = session["context_info"]
    call = [call for call in session["inference_calls"] if call["purpose"] == "agent"][-1]
    assert info["last_request"]["usage_recognised"] is False
    assert info["is_estimate"] is True and info["used_tokens"] == info["estimated_tokens"]
    assert info["percent_used"] == round(100 * info["estimated_tokens"] / 32_768) <= 100
    assert 60 <= info["percent_used"] <= 100 and call["usage"]["cache_read_input_tokens"] > 0


async def test_a_report_counted_twice_is_not_trusted_even_when_its_total_looks_consistent(runtime, monkeypatch):
    manager, session = runtime

    def counted_twice(raw):  # total_tokens adds the cache on top of an input_tokens that already holds it.
        read = raw * 9 // 10
        return {"input_tokens": raw, "cache_read_input_tokens": read, "output_tokens": 5, "total_tokens": raw + read + 5}
    gateway(monkeypatch, counted_twice)
    await manager.run(session, BIG)
    info = session["context_info"]
    assert info["is_estimate"] is True and info["used_tokens"] == info["estimated_tokens"]
    assert info["percent_used"] <= 100


def test_a_current_report_over_the_limit_against_an_estimate_far_under_it_reads_the_estimate():
    info = info_with(estimated_tokens=10_000)
    info["last_request"] = report_for(info, 50_000)  # 153% of 32,768 against an estimate of 31%.
    fields = context_reading(info, "m")
    assert fields["is_estimate"] is True and fields["used_tokens"] == 10_000 and fields["percent_used"] == 31
    close = info_with(estimated_tokens=30_000)
    close["last_request"] = report_for(close, 40_000)  # Within the estimate's calibration range: it stands.
    assert context_reading(close, "m")["is_estimate"] is False


async def test_the_normal_inclusive_prompt_tokens_shape_still_reads_from_the_provider(runtime, monkeypatch):
    manager, session = runtime
    gateway(monkeypatch, inclusive)
    await manager.run(session, BIG)
    info = session["context_info"]
    assert info["is_estimate"] is False and "usage_recognised" not in info["last_request"]
    assert info["used_tokens"] == info["last_request"]["input_tokens"] != info["estimated_tokens"]


def test_the_cached_count_is_shown_for_the_current_report_even_at_zero_and_never_for_a_stale_one():
    info = info_with()
    report = {**report_for(info, 9_000), "cache_read_input_tokens": 0, "uncached_input_tokens": 9_000}
    current = context_reading({**info, "last_request": report}, "m")
    assert current["is_estimate"] is False and current["last_request_cached"] == 0  # 0 is a count: shown.
    stale = {**report, "cache_read_input_tokens": 4_000, "uncached_input_tokens": 5_000}
    after_trim = context_reading({**info, "last_request": stale, "after_trim": True}, "m")
    assert after_trim["is_estimate"] is True and after_trim["last_request_cached"] == 0  # Not shown: not the current request.
