import asyncio
import copy
import json
import re

import pytest

from local_agent.context import (
    DEFAULT_CONTEXT_WINDOW, MAX_CONTEXT_WINDOW, MIN_CONTEXT_WINDOW, OUTPUT_LIMIT_CONTINUATION, REPLY_RESERVE,
    SAFETY_MARGIN, SUMMARY_MAX_BYTES, SUMMARY_MAX_TOKENS, SUMMARY_PREFIX, build_summary_messages,
    context_boundary, context_breakdown, context_messages, estimate_tokens, prepare_context, retained_request,
)


SYSTEM = {"role": "system", "content": "Follow the user's current request."}
TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
COMPACTION_CONTEXT_WINDOW = 32768


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


def long_history():
    return [user("old " * 22500), assistant("Old work done."),
            user("Previous request"), assistant("Previous answer"), user("Latest request")]


async def forbidden_summary(previous, chunk, limit_bytes):
    pytest.fail("Summarization should not run")


def test_estimate_counts_unicode_escaping_tool_definitions_and_overhead():
    messages = [SYSTEM, user('Unicode: 汉字🙂 and escapes: \\"\n')]
    assert estimate_tokens(messages, TOOLS) > estimate_tokens(messages)
    assert estimate_tokens(messages, TOOLS) > estimate_tokens([SYSTEM, user("Unicode")], TOOLS)
    assert estimate_tokens([user("🙂")]) > estimate_tokens([user("x")])


def test_estimate_uses_token_scale_for_ascii_and_conservative_unicode_weighting():
    ascii_estimate = estimate_tokens([SYSTEM, user("File contents. " * 7000)], TOOLS)
    assert 30000 < ascii_estimate < 40000
    assert estimate_tokens([user("🙂" * 1000)]) > estimate_tokens([user("x" * 1000)]) * 4


@pytest.mark.parametrize("text", ["", "plain text", '汉字🙂\\"\n' * 50])
@pytest.mark.parametrize("has_summary", [False, True])
def test_context_breakdown_accounts_for_same_estimate(text, has_summary):
    messages = [SYSTEM, *([user(SUMMARY_PREFIX + text)] if has_summary else []), user(text),
                {"role": "tool", "tool_call_id": "read-1", "content": text}]
    original = copy.deepcopy(messages)
    breakdown = context_breakdown(messages, TOOLS, has_summary)
    assert sum(breakdown.values()) == estimate_tokens(messages, TOOLS)
    assert all(type(value) is int and value >= 0 for value in breakdown.values())
    assert breakdown["system_instructions"] > 0
    assert breakdown["tool_definitions"] > 0
    assert breakdown["messages_and_results"] > 0
    assert breakdown["request_overhead"] >= 256
    assert (breakdown["summary"] > 0) == has_summary
    assert messages == original


def test_empty_context_categories_are_zero_not_phantom_tokens():
    breakdown = context_breakdown([SYSTEM], [])
    assert breakdown["tool_definitions"] == breakdown["summary"] == breakdown["messages_and_results"] == 0
    assert sum(breakdown.values()) == estimate_tokens([SYSTEM], [])


async def test_large_ascii_request_fits_default_context_without_premature_compaction():
    wire = [user("Read these files: " + "path/to/file.py\n" * 7000)]
    messages, state, info = await prepare_context(
        wire, None, SYSTEM, TOOLS, DEFAULT_CONTEXT_WINDOW, forbidden_summary)
    assert messages == [SYSTEM, *wire]
    assert state["compactions"] == 0
    assert info["estimated_tokens"] < info["input_budget"] * 0.4


def test_context_messages_adds_historical_summary_without_changing_archive():
    wire = [user("Archived"), assistant("Done"), user("Current")]
    state = {"summary": "Earlier work", "through": 2, "compactions": 1}
    original = copy.deepcopy((wire, state))
    assert context_messages(wire, state) == [user(SUMMARY_PREFIX + "Earlier work"), user("Current")]
    assert context_messages(wire, {}) == wire
    assert (wire, state) == original


async def test_small_context_requires_no_compaction_and_reports_estimated_budget():
    wire = [user("Hello"), assistant("Hi")]
    messages, state, info = await prepare_context(wire, None, SYSTEM, TOOLS, DEFAULT_CONTEXT_WINDOW, forbidden_summary)
    assert messages == [SYSTEM, *wire]
    assert state == {"summary": "", "through": 0, "compactions": 0}
    assert info == {"estimated_tokens": estimate_tokens(messages, TOOLS),
                    "input_budget": DEFAULT_CONTEXT_WINDOW - REPLY_RESERVE - SAFETY_MARGIN,
                    "context_window": DEFAULT_CONTEXT_WINDOW, "reply_reserve": REPLY_RESERVE,
                    "compactions": 0, "summarized_messages": 0, "estimate_method": "weighted_utf8",
                    "breakdown": context_breakdown(messages, TOOLS)}


async def test_dynamic_reply_reserve_is_budgeted_without_mutating_wire():
    wire = [user("Hello")]
    original = copy.deepcopy(wire)
    messages, state, info = await prepare_context(
        wire, None, SYSTEM, TOOLS, DEFAULT_CONTEXT_WINDOW, forbidden_summary,
        reply_reserve=16384)
    assert messages == [SYSTEM, *wire]
    assert wire == original
    assert state["through"] == 0
    assert info["reply_reserve"] == 16384
    assert info["input_budget"] == DEFAULT_CONTEXT_WINDOW - 16384 - SAFETY_MARGIN
    assert info["estimated_tokens"] == estimate_tokens(messages, TOOLS)


async def test_summary_chunks_use_dedicated_budget_when_main_reply_reserve_is_large():
    wire = [user("old " * 30000), assistant("Old work done."), user("Latest request")]
    reply_reserve = 25000
    main_input_budget = COMPACTION_CONTEXT_WINDOW - reply_reserve - SAFETY_MARGIN
    summary_input_budget = COMPACTION_CONTEXT_WINDOW - SUMMARY_MAX_TOKENS - SAFETY_MARGIN
    request_sizes = []

    async def summarize(previous, chunk, limit_bytes):
        request_sizes.append(estimate_tokens(build_summary_messages(previous, chunk, limit_bytes=limit_bytes)))
        return "Earlier work summarized."

    _, state, info = await prepare_context(
        wire, {}, SYSTEM, TOOLS, COMPACTION_CONTEXT_WINDOW, summarize,
        reply_reserve=reply_reserve)

    assert state["compactions"] == 1
    assert info["input_budget"] == main_input_budget
    assert request_sizes and max(request_sizes) <= summary_input_budget
    assert max(request_sizes) > main_input_budget
    assert len(request_sizes) < 10


async def test_summary_failure_preserves_provider_detail():
    async def summarize(previous, chunk, limit_bytes):
        raise ValueError("Context compaction failed (HTTP 429): input token rate limit")

    with pytest.raises(ValueError, match=r"HTTP 429.*input token rate limit"):
        await prepare_context(long_history(), {}, SYSTEM, TOOLS,
                              COMPACTION_CONTEXT_WINDOW, summarize)


async def test_compaction_keeps_latest_two_turns_and_complete_tool_exchanges():
    wire = long_history()
    wire[3:4] = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "file contents"}, assistant("Previous answer"),
    ]
    wire.extend([
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call-2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-2", "content": "more contents"},
    ])
    state = {"summary": "", "through": 0, "compactions": 0}
    original = copy.deepcopy((wire, state))
    chunks = []

    async def summarize(previous, chunk, limit_bytes):
        chunks.append(chunk)
        return "The earlier work is complete."

    messages, updated, info = await prepare_context(wire, state, SYSTEM, TOOLS, COMPACTION_CONTEXT_WINDOW, summarize)
    assert updated["through"] == 2
    assert updated["compactions"] == 1
    assert messages[2:] == wire[2:]
    assert "".join(chunks) == json.dumps(wire[:2], ensure_ascii=False)
    assert info["summarized_messages"] == 2
    assert info["estimated_tokens"] <= info["input_budget"]
    assert info["breakdown"] == context_breakdown(messages, TOOLS, has_summary=True)
    assert info["breakdown"]["summary"] > 0
    assert sum(info["breakdown"].values()) == info["estimated_tokens"]
    assert (wire, state) == original


async def test_compaction_falls_back_to_latest_turn_when_two_do_not_fit():
    wire = long_history()
    wire[2] = user("p" * 70000)

    async def summarize(previous, chunk, limit_bytes):
        return "Earlier requests summarized."

    messages, state, _ = await prepare_context(wire, {}, SYSTEM, TOOLS, COMPACTION_CONTEXT_WINDOW, summarize)
    assert state["through"] == 4
    assert messages[2:] == wire[4:]


@pytest.mark.parametrize("large_system", [False, True])
async def test_no_summary_call_when_latest_turn_or_system_cannot_fit(large_system):
    wire = [user("old"), assistant("answer"), user("x" * (10 if large_system else 90000))]
    system = {"role": "system", "content": "s" * (90000 if large_system else 10)}
    state = {"summary": "", "through": 0, "compactions": 0}
    original = copy.deepcopy((wire, state))
    with pytest.raises(ValueError, match="Increase the context budget in Settings"):
        await prepare_context(wire, state, system, TOOLS, COMPACTION_CONTEXT_WINDOW, forbidden_summary)
    assert (wire, state) == original


async def test_large_output_reserve_error_shows_budget_arithmetic_and_lower_output_advice():
    # A saved 131,000-token context with a 121,000-token output reserve leaves
    # only 7,952 input tokens; raising the context by 72 tokens cannot fix that.
    wire = [user("Read the notes."), assistant("Reading."), user("Now the pipeline files: " + "x" * 24000)]
    with pytest.raises(ValueError) as error:
        await prepare_context(wire, {}, SYSTEM, TOOLS, 131000, forbidden_summary, reply_reserve=121000)
    message = str(error.value)
    assert re.search(r"need about [\d,]+ estimated input tokens", message)
    assert ("The input budget is 7,952 tokens: 131,000 context budget minus 121,000 reserved for output "
            "minus the 2,048-token safety margin.") in message
    assert "Lower Max output tokens in Settings (currently 121,000; default 8,192)" in message


async def test_default_output_reserve_error_does_not_blame_the_output_setting():
    wire = [user("old"), assistant("answer"), user("x" * 90000)]
    with pytest.raises(ValueError) as error:
        await prepare_context(wire, {}, SYSTEM, TOOLS, COMPACTION_CONTEXT_WINDOW, forbidden_summary)
    message = str(error.value)
    assert "The input budget is 22,528 tokens: 32,768 context budget minus 8,192 reserved" in message
    assert "Increase the context budget in Settings" in message
    assert "Max output tokens" not in message


@pytest.mark.parametrize("failure", [RuntimeError("provider failed"), asyncio.CancelledError()])
async def test_failure_after_first_chunk_never_commits_partial_summary(failure):
    wire = [user("already archived"), assistant("done"), user("x" * 180000), assistant("done"),
            user("recent"), assistant("answer"), user("latest")]
    state = {"summary": "Prior summary", "through": 2, "compactions": 3}
    original = copy.deepcopy((wire, state))
    calls = 0

    async def summarize(previous, chunk, limit_bytes):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure
        return "Intermediate summary"

    expected = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else ValueError
    with pytest.raises(expected):
        await prepare_context(wire, state, SYSTEM, TOOLS, COMPACTION_CONTEXT_WINDOW, summarize)
    assert calls == 2
    assert (wire, state) == original


@pytest.mark.parametrize("summary", ["", "  ", None])
async def test_summary_must_be_nonempty_text(summary):
    wire, state = long_history(), {}
    original = copy.deepcopy((wire, state))

    async def summarize(previous, chunk, limit_bytes):
        return summary

    with pytest.raises(ValueError, match="summary"):
        await prepare_context(wire, state, SYSTEM, TOOLS, COMPACTION_CONTEXT_WINDOW, summarize)
    assert (wire, state) == original


async def test_unicode_and_escaped_chunks_fit_each_summary_request():
    wire = [user('汉字🙂\\"\n' * 2500), assistant("done"), user("recent"), assistant("answer"), user("latest")]
    state = {"summary": "Earlier summary", "through": 0, "compactions": 2}
    chunks = []
    main_input_budget = MIN_CONTEXT_WINDOW - REPLY_RESERVE - SAFETY_MARGIN
    summary_input_budget = MIN_CONTEXT_WINDOW - SUMMARY_MAX_TOKENS - SAFETY_MARGIN

    async def summarize(previous, chunk, limit_bytes):
        assert estimate_tokens(build_summary_messages(previous, chunk, limit_bytes=limit_bytes)) <= summary_input_budget
        chunk.encode("utf-8").decode("utf-8")
        chunks.append(chunk)
        return '事实🙂\\"\n' * 250

    messages, updated, info = await prepare_context(wire, state, SYSTEM, TOOLS, MIN_CONTEXT_WINDOW, summarize)
    assert len(chunks) > 1
    assert "".join(chunks) == json.dumps(wire[:2], ensure_ascii=False)
    assert updated["compactions"] == 3
    assert estimate_tokens(messages, TOOLS) == info["estimated_tokens"] <= main_input_budget


async def test_repeated_compaction_only_summarizes_newly_archived_messages():
    wire = long_history()
    requests = []

    async def summarize(previous, chunk, limit_bytes):
        requests.append((previous, chunk))
        return "All earlier work summarized."

    _, first, _ = await prepare_context(wire, None, SYSTEM, TOOLS, COMPACTION_CONTEXT_WINDOW, summarize)
    requests.clear()
    wire.extend([assistant("last answer"), user("new " * 21000), assistant("done"), user("Final request")])
    original = copy.deepcopy((wire, first))
    messages, second, info = await prepare_context(wire, first, SYSTEM, TOOLS, COMPACTION_CONTEXT_WINDOW, summarize)
    assert first["through"] == 2
    assert second["through"] == 8
    assert second["compactions"] == 2
    assert requests[0][0] == first["summary"]
    assert "".join(chunk for _, chunk in requests) == json.dumps(wire[2:8], ensure_ascii=False)
    assert messages[2:] == wire[8:]
    assert info["summarized_messages"] == 8
    assert (wire, first) == original


async def test_final_serialized_request_is_checked_before_committing_summary():
    wire = long_history()
    # The retained turns fit beside a plain placeholder summary of the maximum
    # size, but JSON escaping doubles a backslash summary's serialized size.
    wire[2] = user("x" * 54000)
    state = {}
    original = copy.deepcopy((wire, state))

    async def summarize(previous, chunk, limit_bytes):
        return "\\" * limit_bytes

    with pytest.raises(ValueError, match="still exceed the context budget"):
        await prepare_context(wire, state, SYSTEM, TOOLS, COMPACTION_CONTEXT_WINDOW, summarize)
    assert (wire, state) == original


@pytest.mark.parametrize("context_window", [MIN_CONTEXT_WINDOW - 1, MAX_CONTEXT_WINDOW + 1])
async def test_invalid_context_window_is_rejected(context_window):
    with pytest.raises(ValueError, match="Choose a context window"):
        await prepare_context([user("Hello")], {}, SYSTEM, TOOLS, context_window, forbidden_summary)


@pytest.mark.parametrize("reply_reserve", [
    True, 1023, 131073, DEFAULT_CONTEXT_WINDOW - SAFETY_MARGIN,
])
async def test_invalid_reply_reserve_is_rejected(reply_reserve):
    with pytest.raises(ValueError, match="output-token limit"):
        await prepare_context([user("Hello")], {}, SYSTEM, TOOLS, DEFAULT_CONTEXT_WINDOW,
                              forbidden_summary, reply_reserve=reply_reserve)


# --- Compaction inside one long turn ----------------------------------------------

def read_exchange(number, size=6000):
    call_id = f"read-{number}"
    return [{"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": f"notes/file-{number}.md"})}}]},
            {"role": "tool", "tool_call_id": call_id, "content": f"file {number}: " + "fact " * (size // 5)}]


def reading_turn(request="Read every notes file and write a detailed handoff.", files=15):
    return [user(request), *[message for number in range(files) for message in read_exchange(number)]]


async def test_one_turn_that_outgrows_the_budget_is_compacted_inside_the_turn():
    wire = reading_turn()
    original = copy.deepcopy(wire)
    chunks = []
    input_budget = COMPACTION_CONTEXT_WINDOW - REPLY_RESERVE - SAFETY_MARGIN
    assert estimate_tokens([SYSTEM, *wire], TOOLS) > input_budget

    async def summarize(previous, chunk, limit_bytes):
        assert estimate_tokens(build_summary_messages(previous, chunk, limit_bytes=limit_bytes)) <= (
            COMPACTION_CONTEXT_WINDOW - SUMMARY_MAX_TOKENS - SAFETY_MARGIN)
        chunks.append(chunk)
        return "Files 0-8 read: their facts are recorded here."

    messages, state, info = await prepare_context(wire, {}, SYSTEM, TOOLS, COMPACTION_CONTEXT_WINDOW, summarize)
    through = state["through"]
    assert wire[through]["role"] == "assistant" and 0 < through < len(wire) - 1
    assert "".join(chunks) == json.dumps(wire[:through], ensure_ascii=False)
    # The request stays verbatim after the summary, then the latest exchanges.
    assert messages[1] == user(SUMMARY_PREFIX + "Files 0-8 read: their facts are recorded here.")
    assert messages[2] == wire[0] and messages[3:] == wire[through:]
    assert messages[-1] == wire[-1]
    assert info["compacted_in_turn"] is True and state["compactions"] == 1
    assert info["estimated_tokens"] <= input_budget
    # Only the exchanges that fit in half the budget are kept, leaving room to continue.
    retained = estimate_tokens([SYSTEM, user(SUMMARY_PREFIX + "x" * 12000), *messages[2:]], TOOLS)
    assert retained <= input_budget // 2
    assert estimate_tokens([SYSTEM, user(SUMMARY_PREFIX + "x" * 12000), wire[0], *wire[through - 2:]], TOOLS) > input_budget // 2
    assert wire == original


async def test_repeated_compaction_in_one_turn_gives_the_summarizer_the_request_again():
    wire = reading_turn()
    chunks = []

    async def summarize(previous, chunk, limit_bytes):
        chunks.append((previous, chunk))
        return f"Summary {len(chunks)}"

    _, first, _ = await prepare_context(wire, {}, SYSTEM, TOOLS, COMPACTION_CONTEXT_WINDOW, summarize)
    wire.extend(message for number in range(15, 27) for message in read_exchange(number))
    chunks.clear()
    messages, second, info = await prepare_context(wire, first, SYSTEM, TOOLS, COMPACTION_CONTEXT_WINDOW, summarize)
    assert second["through"] > first["through"] and wire[second["through"]]["role"] == "assistant"
    assert chunks[0][0] == first["summary"]
    assert "".join(chunk for _, chunk in chunks) == json.dumps(
        [wire[0], *wire[first["through"]:second["through"]]], ensure_ascii=False)
    assert messages[2] == wire[0] and messages[3:] == wire[second["through"]:]
    assert info["compacted_in_turn"] is True and second["compactions"] == 2


async def test_request_before_an_output_limit_continuation_is_the_one_kept():
    wire = reading_turn()
    wire[1:1] = [assistant("[The previous response reached the configured output-token limit.]"),
                 user(OUTPUT_LIMIT_CONTINUATION)]

    async def summarize(previous, chunk, limit_bytes):
        return "Earlier reads summarized."

    messages, state, _ = await prepare_context(wire, {}, SYSTEM, TOOLS, COMPACTION_CONTEXT_WINDOW, summarize)
    assert state["through"] > 3 and messages[2] == wire[0]
    assert OUTPUT_LIMIT_CONTINUATION not in json.dumps(messages)
    assert context_messages(wire, {"summary": "s", "through": 2})[1] == wire[0]


async def test_turn_whose_latest_exchange_alone_is_too_large_reports_the_budget():
    wire = [user("old"), assistant("answer"), user("Read the log."), *read_exchange(0, size=90000)]
    with pytest.raises(ValueError) as error:
        await prepare_context(wire, {}, SYSTEM, TOOLS, COMPACTION_CONTEXT_WINDOW, forbidden_summary)
    message = str(error.value)
    assert re.search(r"The latest request, its most recent tool results, and project instructions need about "
                     r"[\d,]+ estimated input tokens", message)
    assert "The input budget is 22,528 tokens" in message


async def test_manual_compaction_still_keeps_the_latest_turn_intact():
    with pytest.raises(ValueError, match="No earlier turns to compact"):
        await prepare_context(reading_turn(files=2), {}, SYSTEM, TOOLS, DEFAULT_CONTEXT_WINDOW,
                              forbidden_summary, force_compact=True)


def test_summary_boundaries_are_turn_starts_or_responses_inside_a_turn():
    wire = reading_turn(files=2)
    assert [index for index in range(len(wire) + 1) if context_boundary(wire, index)] == [0, 1, 3, 5]
    assert retained_request(wire, 3) == [wire[0]] and retained_request(wire, 0) == []
    assert context_boundary([assistant("orphan"), user("next")], 0) and not context_boundary(
        [assistant("orphan"), assistant("again")], 1)
