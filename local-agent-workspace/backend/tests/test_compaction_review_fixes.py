"""Compaction review fixes: the app record keeps long requests whole while it has room, trimming a
summary never cuts through the user's requirements first, and a billed summary is shortened instead
of discarded when the request overshoots (pure context)."""

import random

from local_agent import context
from local_agent.agents import AgentManager
from local_agent.context import (
    REQUEST_CLIP_MARKER, SAFETY_MARGIN, SECTION_TRIM_MARKER, SUMMARY_TRIM_MARKER, app_record_limit,
    merge_user_requests, prepare_context, summary_byte_limit, summary_sections, trim_summary,
)

SYSTEM = {"role": "system", "content": "Follow the user's current request."}
TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
OPUS_WINDOW, OPUS_OUTPUT = 200_000, 16_384
OPUS_BUDGET = OPUS_WINDOW - OPUS_OUTPUT - SAFETY_MARGIN  # 181,568
RULE = "RULE: never write to the prod database."


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


def spec_with_rule(size=2_500):
    """A long first request (about 400 words) with a rule in the middle."""
    words = "Describe the billing export, its columns, the retry policy and the report layout in detail. "
    head = (words * 40)[:1_200]
    middle = f" {RULE} "
    return head + middle + (words * 40)[:size - len(head) - len(middle)]


# --- The app record keeps requests whole while it has room ------------------------

def test_requests_stay_whole_while_the_record_has_room():
    area = app_record_limit(OPUS_BUDGET) // 2
    assert area == 4_000
    spec = spec_with_rule()
    assert len(spec.encode()) == 2_500
    assert merge_user_requests([], 0, [spec], area) == ([spec], 0)
    # Full: only the long requests are shortened (start and end kept), never below the per-request floor.
    long_one = "Start of the second spec. " + "y" * 3_000 + " END"
    items, omitted = merge_user_requests([spec], 0, ["Use tabs.", long_one], area)
    assert omitted == 0 and items[1] == "Use tabs."
    assert sum(len(item.encode()) + 4 for item in items) <= area
    assert REQUEST_CLIP_MARKER in items[0] and REQUEST_CLIP_MARKER in items[2]
    assert items[0].startswith(spec[:100]) and items[2].endswith(" END")
    assert len(items[0].encode()) >= 1_500 and len(items[2].encode()) >= 1_500


async def test_a_long_first_request_is_kept_whole_in_the_record():
    spec = spec_with_rule()
    wire = [user(spec), assistant("Done."), user("Add the CSV export."), assistant("Added."), user("Now the tests.")]

    async def summarize(previous, chunk, limit):
        return "Summary that does not repeat the rule."

    messages, state, _ = await prepare_context(wire, {}, SYSTEM, TOOLS, OPUS_WINDOW, summarize,
                                               reply_reserve=OPUS_OUTPUT, force_compact=True, record_requests=True)
    assert state["user_requests"][0] == spec
    assert RULE in messages[1]["content"]


# --- Trimming a summary cuts the lower sections to headings before the requirements -----

def rule_line(n):
    return f'- RULE-{n}: "never push to main unless the user says so; rule {n} of the project"'


def rule_summary(rules, lower_bytes=2_000, goal_lines=12):
    """A nine-section handoff whose requirements quote many rules."""
    bodies = {
        "Goal and current status": "\n".join(f"Goal line {i}: the billing export is in progress."
                                             for i in range(goal_lines)),
        "Open tasks and next steps": "\n".join(f"- Task {i}: finish part {i} of the export." for i in range(25)),
        "User requirements, preferences and constraints": "\n".join(rule_line(n) for n in range(1, rules + 1)),
    }
    for name in (*context.SUMMARY_SECTIONS[3:], *context.HANDOFF_EXTRA_SECTIONS):
        bodies[name] = "\n".join(f"{name} detail {i} " + "z" * 60 for i in range(lower_bytes // 90))
    return "\n\n".join(f"## {name}\n{body}" for name, body in bodies.items()), bodies


def test_lower_sections_go_down_to_their_headings_before_a_requirement_is_cut():
    limit = summary_byte_limit(OPUS_BUDGET)
    assert limit == 15_130
    summary, bodies = rule_summary(150)
    required = sum(len(bodies[name].encode()) for name in context.REQUIRED_SUMMARY_SECTIONS)
    assert 13_000 < required < limit < len(summary.encode())
    trimmed = trim_summary(summary, limit)
    assert len(trimmed.encode()) <= limit and SUMMARY_TRIM_MARKER not in trimmed
    assert all(rule_line(n) in trimmed.split("\n") for n in range(1, 151))
    assert set(summary_sections(trimmed)) == set(bodies)


def test_required_sections_over_the_limit_lose_whole_middle_lines_only():
    limit = summary_byte_limit(OPUS_BUDGET)
    summary, bodies = rule_summary(300)
    trimmed = trim_summary(summary, limit)
    assert len(trimmed.encode()) <= limit
    lines = trimmed.split("\n")
    for name in context.REQUIRED_SUMMARY_SECTIONS:
        first, *_, last = bodies[name].split("\n")
        assert first in lines and last in lines, name
    kept = [line for line in lines if "RULE-" in line]
    originals = {rule_line(n) for n in range(1, 301)}
    assert kept and all(line in originals for line in kept)  # No rule is cut inside its line.
    assert SECTION_TRIM_MARKER.strip() in trimmed and SUMMARY_TRIM_MARKER not in trimmed


def test_a_short_goal_passes_its_share_of_the_cut_to_the_other_sections():
    # A goal of one to three lines has no middle lines to give; its share used to fall
    # through to the byte-offset cut, which split a quoted rule and dropped the next one.
    limit = summary_byte_limit(OPUS_BUDGET)
    rng = random.Random(17)
    cases = [(3, 25, 200)] + [(rng.randint(1, 3), rng.randint(3, 60), rng.randint(150, 400)) for _ in range(150)]
    for goal_lines, tasks, rules in cases:
        summary, bodies = rule_summary(rules, lower_bytes=1_980, goal_lines=goal_lines)
        body = "\n".join(f"- Task {i}: " + "t" * rng.randint(10, 120) for i in range(tasks))
        summary = summary.replace(bodies["Open tasks and next steps"], body)
        trimmed = trim_summary(summary, limit)
        case = (goal_lines, tasks, rules)
        assert len(trimmed.encode()) <= limit and SUMMARY_TRIM_MARKER not in trimmed, case
        lines = trimmed.split("\n")
        assert all(line in lines for line in bodies["Goal and current status"].split("\n")), case
        originals = {rule_line(n) for n in range(1, rules + 1)}
        kept = [line for line in lines if "RULE-" in line]
        assert all(line in originals for line in kept), case  # No rule is cut inside its line.
        assert rule_line(1) in kept and rule_line(rules) in kept, case


# --- A billed summary is shortened, not discarded, when the request overshoots -----------

def placeholder_needs(window, reserve, size):
    """What needed_at reserves: an ASCII summary and record of their full byte limits beside the kept turn."""
    budget = window - reserve - SAFETY_MARGIN
    room = "x" * (summary_byte_limit(budget) + app_record_limit(budget))
    return context.estimate_tokens([SYSTEM, user(context.SUMMARY_PREFIX + room), user("Prev request"),
                                    assistant("ok"), user("L" * size)], TOOLS)


def history_at(window, reserve, tokens):
    """Two large earlier turns, then a kept turn sized so the placeholder request needs about tokens."""
    low, high = 0, 2_000_000
    while low < high:
        middle = (low + high + 1) // 2
        low, high = (middle, high) if placeholder_needs(window, reserve, middle) <= tokens else (low, middle - 1)
    return [user("First: " + "a" * 200_000), assistant("Done."), user("Second: " + "b" * 200_000),
            assistant("Done."), user("Prev request"), assistant("ok"), user("L" * (low - 50))]


async def notice_for(info):
    """The compaction notice AgentManager posts for this info."""
    texts = []

    class Manager:
        async def event(self, session, kind, **fields):
            texts.append(fields["text"])

    await AgentManager.summary_adjustment_notice(Manager(), {"id": "c"}, info)
    return texts[-1]


def japanese(limit):
    text = "\n\n".join(f"## {name}\n" + "ユーザーは日本語で作業しています。\n" * 60 for name in context.SUMMARY_SECTIONS)
    return text.encode()[:limit - 10].decode("utf-8", errors="ignore")


def english(limit):
    lines = [f'- `src/app/module_{n}.py`: error "KeyError: \'id\'" fixed — use `{{"id": 1}}` → ok' for n in range(400)]
    text = "## Goal and current status\nShip it.\n\n## Open tasks and next steps\n- Tests\n\n" \
           "## User requirements, preferences and constraints\n- Use tabs.\n\n## Errors, causes and fixes\n" + "\n".join(lines)
    return text.encode()[:limit - 10].decode("utf-8", errors="ignore")


async def test_a_billed_summary_is_shortened_until_the_request_fits():
    cases = [("japanese", 100, 1.0), ("japanese", 90, 1.0), ("japanese", 100, 1.3), ("english", 90, 1.0)]
    for kind, percent, scale in cases:
        # Kept turns sized against the whole budget: at 90% no cut reaches the threshold (the fallback cut).
        wire = history_at(OPUS_WINDOW, OPUS_OUTPUT, int(OPUS_BUDGET / scale))
        calls = []

        async def summarize(previous, chunk, limit):
            calls.append(limit)
            return (japanese if kind == "japanese" else english)(limit)

        notes = "\n".join(f"- src/pkg/module_{n}.py" for n in range(400))
        _, state, info = await prepare_context(
            wire, {}, SYSTEM, TOOLS, OPUS_WINDOW, summarize, reply_reserve=OPUS_OUTPUT, scale=scale,
            auto_compact_percent=percent, record_requests=True, app_record=lambda wire, cut, limit: notes)
        assert len(calls) == 1, (kind, percent, scale)
        assert info["estimated_tokens"] <= OPUS_BUDGET and info["compaction"]["after_tokens"] <= OPUS_BUDGET
        # The summary was within its own limit: the shortening is reported as fitting the request.
        assert "summary_adjustment" not in info and info["summary_fitted_to_budget"] == "trimmed"
        assert len(state["summary"].encode()) < calls[0]
        text = await notice_for(info)
        assert "over the input budget" in text and "part of the summary was omitted to fit" in text
        assert "longer than" not in text and "could not be condensed" not in text


async def test_a_large_overshoot_is_condensed_once_before_trimming():
    wire = history_at(OPUS_WINDOW, OPUS_OUTPUT, OPUS_BUDGET)
    condensed = []

    async def summarize(previous, chunk, limit):
        return japanese(limit)

    async def condense(text, limit):
        condensed.append(limit)
        return japanese(limit)

    _, state, info = await prepare_context(wire, {}, SYSTEM, TOOLS, OPUS_WINDOW, summarize, reply_reserve=OPUS_OUTPUT,
                                           condense=condense, auto_compact_percent=100, record_requests=True)
    assert len(condensed) == 1 and info["summary_fitted_to_budget"] == "condensed"
    assert "summary_adjustment" not in info
    text = await notice_for(info)
    assert "over the input budget" in text and "a short extra request condensed the summary to fit" in text
    assert "longer than" not in text
    assert info["estimated_tokens"] <= OPUS_BUDGET and info["compaction"]["requests"] == 2


async def test_the_notice_says_a_condensed_summary_was_shortened_and_counts_the_requests_apart():
    compaction = {"number": 3, "trigger": "auto", "before_tokens": 170_000, "after_tokens": 60_000,
                  "threshold_tokens": 163_411, "summarized_messages": 12, "requests": 2, "summarize_requests": 1,
                  "condense_requests": 1, "elided_bytes": 0, "superseded_results": 0}
    info = {"input_budget": OPUS_BUDGET, "auto_compact_percent": 90, "compaction": compaction,
            "summary_adjustment": "condensed_trimmed", "handoff_path": "handoffs/auto/chat-compaction-3.md"}
    text = await notice_for(info)
    assert "12 earlier messages were summarized in 1 request, plus 1 condensing request; the request now needs" in text
    assert ("The condensed summary was still longer than 15,130 bytes, so the app shortened its least important "
            "sections; the saved handoff has them in full.") in text
    assert "could not be condensed" not in text and "in 2 requests" not in text
    text = await notice_for({**info, "handoff_path": None})
    assert text.endswith("so the app shortened its least important sections.") and "handoff" not in text
    # A compaction recorded before the requests were counted apart.
    old = {key: value for key, value in compaction.items() if key not in ("summarize_requests", "condense_requests")}
    assert "were summarized in 2 requests;" in await notice_for({**info, "compaction": old})
    two = {**compaction, "requests": 4, "summarize_requests": 2, "condense_requests": 2}
    assert "were summarized in 2 requests, plus 2 condensing requests;" in await notice_for({**info, "compaction": two})


async def test_a_failed_condense_is_counted_without_claiming_it_condensed():
    # The condensing request is sent and fails: it is still a request, and the notice says the summary
    # could not be condensed rather than that it was condensed.
    wire = [{"role": "user", "content": "Old: " + "o" * 60_000}, {"role": "assistant", "content": "Done."},
            {"role": "user", "content": "Latest request"}]

    async def summarize(previous, chunk, limit_bytes):
        return "## Goal and current status\n" + "s" * (limit_bytes * 2)

    async def condense(text, limit_bytes):
        raise RuntimeError("HTTP 503")

    _, _, info = await prepare_context(wire, {}, SYSTEM, [], 32_768, summarize, reply_reserve=4096,
                                       condense=condense, auto_compact_percent=50)
    assert info["summary_adjustment"] == "trimmed" and info["compaction"]["condense_requests"] == 1
    text = await notice_for(info)
    assert "summarized in 1 request, plus 1 condensing request;" in text and "could not be condensed" in text
    assert "condensed in" not in text


async def test_a_summary_that_cannot_fit_above_the_floor_still_fails_without_committing():
    window, reserve = 16_384, 8_192
    wire = history_at(window, reserve, window - reserve - SAFETY_MARGIN)
    state = {}

    async def summarize(previous, chunk, limit):
        return "漢" * (limit // 3)

    try:
        await prepare_context(wire, state, SYSTEM, TOOLS, window, summarize, reply_reserve=reserve,
                              auto_compact_percent=100)
    except ValueError as exc:
        assert "still exceed the context budget" in str(exc)
    else:
        raise AssertionError("expected the budget error")
    assert state == {}
