"""Compaction that keeps what matters: the summarizer's view of history, the app record,
section-aware trimming, the auto-compact threshold and the reported-input report (pure context)."""
import copy
import json
import random

import pytest

from local_agent import context
from local_agent.composer_api import STEER_PREFIX
from local_agent.context import (
    APP_PROMPT_LABELS, CARRY_DIVISOR, ELIDED_ARGUMENT, HANDOFF_EXTRA_SECTIONS, INCOMPLETE_RESPONSE_CONTINUATION,
    INVALID_ARGUMENTS_CONTINUATION, MAX_CONTEXT_WINDOW, OUTLINE_HEADING, OUTPUT_LIMIT_CONTINUATION,
    REQUEST_CLIP_MARKER, REQUIRED_SUMMARY_SECTIONS, SAFETY_MARGIN, SECTION_TRIM_MARKER, SUMMARY_PREFIX,
    SUMMARY_SECTIONS, VERIFY_CONTINUATION_PREFIX, VERIFY_LABEL, VERIFY_NOTE_LABEL, app_record_limit,
    build_condense_messages, build_handoff_messages, build_summary_messages, compaction_output_allowance,
    context_messages, elision_limit, estimate_scale, estimate_tokens, fit_summary, handoff_output_tokens,
    handoff_pointer, handoff_with_contents, last_request_report, merge_user_requests, model_context_window,
    prepare_context, render_app_record, request_text, summary_byte_limit, summary_output_tokens, summary_sections,
    summary_transcript, trim_summary,
)
from local_agent.mentions import HEADER
from local_agent.tools import WorkspaceTools, read_page_bytes


SYSTEM = {"role": "system", "content": "Follow the user's current request."}
TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


def calls(*items):
    """An assistant message with tool calls: items are (id, name, arguments)."""
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        for call_id, name, arguments in items]}


def result(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def page(path, size=2000):
    return json.dumps({"path": path, "start_line": 1, "content": "\n".join(
        f"{n}: line {n} " + "x" * 40 for n in range(1, size // 48 + 2))}, indent=2)


def sections(bodies):
    return "\n\n".join(f"## {name}\n{body}" for name, body in bodies.items())


async def never(*_):
    raise AssertionError("no summary request expected")


# --- 1-7: the summarizer's view (summary_transcript) -------------------------------

def test_a_large_tool_result_keeps_its_head_and_tail():
    output = "HEAD-OF-OUTPUT " + "a" * 30_000 + " MIDDLE-TOKEN " + "b" * 30_000 + " END-OF-OUTPUT"
    archived = [user("Run the tests"), calls(("c1", "run_command", {"command": "pytest"})), result("c1", output)]
    text, stats = summary_transcript(archived, [], 8000)
    assert "HEAD-OF-OUTPUT" in text and "END-OF-OUTPUT" in text and "MIDDLE-TOKEN" not in text
    assert "The app shortened this tool result before summarizing" in text
    assert stats["shortened"] == 1 and stats["elided_bytes"] > 50_000
    assert len(text) < 10_000


@pytest.mark.parametrize("later, completed, superseded", [
    ([calls(("e1", "edit_file", {"path": "./src/a.py", "old_text": "a", "new_text": "b"}))], {"r1", "e1"}, True),
    ([calls(("e1", "edit_file", {"path": "src/a.py", "old_text": "a", "new_text": "b"}))], {"r1"}, False),
    ([calls(("r2", "read_file", {"path": "src/a.py", "max_lines": 20}))], {"r1", "r2"}, False),
    ([calls(("r2", "read_file", {"path": "src//a.py"}))], {"r1", "r2"}, True),
    ([calls(("m1", "move_path", {"path": "src/b.py", "destination": "src/a.py"}))], {"m1"}, True),
    ([calls(("r2", "read_file", {"path": "src/a.py"}))], None, False),
    ([calls(("d1", "delete_path", {"path": "src/other.py"}))], {"d1"}, False),
])
def test_a_read_is_superseded_only_by_a_later_completed_change_or_identical_read(later, completed, superseded):
    archived = [user("Fix a.py"), calls(("r1", "read_file", {"path": "src/a.py"})), result("r1", page("src/a.py"))]
    for where in ("archived", "later"):
        history, after = (archived + later, []) if where == "archived" else (archived, later)
        text, stats = summary_transcript(history, after, 64_000, completed)
        assert ("The app omitted this read_file result for src/a.py" in text) is superseded
        assert stats["superseded"] == int(superseded)


def test_a_read_page_keeps_the_definition_lines_of_its_middle(tmp_path):
    lines = [f"    value_{n} = compute({n})  # filler text for the page" for n in range(1, 401)]
    lines[199] = "class BillingRepository:"
    (tmp_path / "db.py").write_text("\n".join(lines) + "\n")
    tools = WorkspaceTools(str(tmp_path))
    output = json.dumps(tools.read_file_range("db.py", max_lines=2000, page_bytes=read_page_bytes(65_536)), indent=2)
    assert 15_000 < len(output) <= 16_384
    text, _ = summary_transcript([user("Read db.py"), calls(("r1", "read_file", {"path": "db.py"})),
                                  result("r1", output)], [], 8000)
    clipped = json.loads(text)[2]["content"]
    assert OUTLINE_HEADING in clipped and "\n200: class BillingRepository:\n" in clipped
    assert "100: " not in clipped.split(OUTLINE_HEADING)[1]


def test_file_contents_in_tool_arguments_are_shortened_but_keep_paths():
    content = "start of file\n" + "y" * 10_000 + "UNIQUE-MIDDLE-TOKEN" + "z" * 10_000
    archived = [user("Write it"), calls(
        ("w1", "write_file", {"path": "src/new.py", "content": content}),
        ("e1", "edit_file", {"path": "src/api.py", "edits": [{"old_text": "a", "new_text": content}]}),
        ("n1", "notebook_edit", {"path": "nb.ipynb", "source": "short"}))]
    archived[1]["tool_calls"].append({"id": "x1", "type": "function",
                                      "function": {"name": "write_file", "arguments": "{" + "q" * 5000}})
    text, stats = summary_transcript(archived, [], 64_000)
    assert "UNIQUE-MIDDLE-TOKEN" not in text
    tool_calls = json.loads(text)[1]["tool_calls"]
    write, edit, notebook, broken = (json.loads(call["function"]["arguments"]) if index < 3
                                     else call["function"]["arguments"] for index, call in enumerate(tool_calls))
    assert write["path"] == "src/new.py" and write["content"].startswith("start of file")
    assert ELIDED_ARGUMENT.format(total=len(content), field="content") in write["content"]
    assert edit["path"] == "src/api.py" and edit["edits"][0]["old_text"] == "a"
    assert ELIDED_ARGUMENT.format(total=len(content), field="edits[0].new_text") in edit["edits"][0]["new_text"]
    assert notebook == {"path": "nb.ipynb", "source": "short"}
    assert broken.endswith(" [truncated by the app]") and len(broken.encode()) == 2000 + len(" [truncated by the app]")
    assert stats["shortened"] == 3


def test_app_prompts_are_labelled_and_a_declined_verify_note_stays_the_users():
    failed = f"{VERIFY_CONTINUATION_PREFIX} the project's verify command `pytest` failed (failed, exit 1); fix round 1."
    declined = (f"{VERIFY_CONTINUATION_PREFIX} the user declined the project's verify command `pytest`, so it did not "
                "run. Their note: use tabs\nAnswer the note. Do not run the verify command yourself unless the user asks.")
    archived = [user("Fix it"), assistant("a"), user(OUTPUT_LIMIT_CONTINUATION), assistant("b"),
                user(INVALID_ARGUMENTS_CONTINUATION), assistant("c"), user(INCOMPLETE_RESPONSE_CONTINUATION),
                assistant("d"), user(failed), assistant("e"), user(declined), assistant("f")]
    messages = json.loads(summary_transcript(archived, [], 64_000)[0])
    assert messages[0] == user("Fix it")
    assert [m for m in messages[2:9:2]] == [
        {"role": "app", "content": APP_PROMPT_LABELS[OUTPUT_LIMIT_CONTINUATION]},
        {"role": "app", "content": APP_PROMPT_LABELS[INVALID_ARGUMENTS_CONTINUATION]},
        {"role": "app", "content": APP_PROMPT_LABELS[INCOMPLETE_RESPONSE_CONTINUATION]},
        {"role": "app", "content": VERIFY_LABEL + failed}]
    assert messages[10] == {"role": "user", "content": VERIFY_NOTE_LABEL + "use tabs"}


def test_a_history_that_needs_no_change_serializes_exactly_like_json_dumps():
    archived = [user("Héllo ✓"), calls(("r1", "read_file", {"path": "a.py"})), result("r1", "small"),
                assistant("Done."), {"role": "assistant", "content": "Partial [Stopped by the user.]"}]
    text, stats = summary_transcript(archived, [], 8000, {"r1"})
    assert text == json.dumps(archived, ensure_ascii=False)
    assert stats == {"elided_bytes": 0, "superseded": 0, "shortened": 0}


def test_a_user_messages_attachments_are_clipped_but_its_words_are_not():
    words = "Please keep this exact wording " * 400
    attachment = "\n\n---\n" + HEADER.format(nonce="0123456789ab") + "\n<<<ATTACHMENT 0123456789ab file @a.py lines 1–9>>>\n" + "k" * 30_000
    text, _ = summary_transcript([user(words + attachment)], [], 8000)
    content = json.loads(text)[0]["content"]
    assert content.startswith(words) and "The app shortened this tool result" in content and len(content) < 22_000
    assert json.loads(summary_transcript([user(words * 3)], [], 8000)[0])[0]["content"] == words * 3


async def test_in_turn_compaction_doubles_the_result_limit():
    medium = "M" * 6000 + "IN-TURN-MIDDLE" + "M" * 6000  # Between the limit (8,000) and twice it.
    wire = [user("Read everything"), calls(("c1", "read_file", {"path": "a"})), result("c1", medium)]
    for n in range(2, 6):
        wire += [calls((f"c{n}", "read_file", {"path": f"f{n}"})), result(f"c{n}", f"{n}" * 45_000)]
    chunks = []

    async def summarize(previous, chunk, limit):
        chunks.append(chunk)
        return "Summary."

    _, _, info = await prepare_context(wire, {}, SYSTEM, TOOLS, 65_536, summarize)
    assert info["compacted_in_turn"] and elision_limit(65_536 - 4096 - SAFETY_MARGIN) == 8000
    assert "IN-TURN-MIDDLE" in "".join(chunks)


# --- 8-9: the app record ---------------------------------------------------------------

def test_request_text_strips_steering_and_attachments_and_clips_by_bytes():
    attached = "Fix @a.py\n\n---\n" + HEADER.format(nonce="0123456789ab") + "\n<<<ATTACHMENT 0123456789ab file @a.py lines 1–2>>>\nx\n<<<END 0123456789ab>>>"
    assert request_text(user(STEER_PREFIX + "Use tabs")) == "Use tabs"
    assert request_text(user(attached)) == "Fix @a.py [attachments omitted]"
    assert request_text(assistant("no")) is None and request_text(user(OUTPUT_LIMIT_CONTINUATION)) is None
    declined = f"{VERIFY_CONTINUATION_PREFIX} the user declined `x`. Their note: skip it\nAnswer the note."
    assert request_text(user(declined)) == "(Note when declining Verify) skip it"
    cjk = "漢" * 1000 + "字" * 1000 + "終" * 1000
    clipped = request_text(user(cjk))
    assert len(clipped.encode()) <= 1500 and REQUEST_CLIP_MARKER in clipped
    assert clipped.startswith("漢") and clipped.endswith("終")
    # 600 characters but 1,800 bytes: the limit counts bytes, not characters.
    assert len(request_text(user("漢" * 600)).encode()) <= 1500


def test_merge_keeps_the_first_request_and_drops_the_oldest_after_it():
    first, second, third, fourth, fifth = (name + "." * 196 for name in ("1st", "2nd", "3rd", "4th", "5th"))
    items, omitted = merge_user_requests([first], 0, [second, third, fourth], 750)
    assert items == [first, third, fourth] and omitted == 1
    items, omitted = merge_user_requests(items, omitted, [fifth], 750)
    assert items == [first, fourth, fifth] and omitted == 2
    huge = ["é" * 5000, "ü" * 5000]
    items, omitted = merge_user_requests([], 0, huge, 750)
    assert len(items) == 2 and sum(len(item.encode()) + 4 for item in items) <= 750 and omitted == 0


def test_the_record_sits_between_the_summary_and_the_handoff_pointer():
    files = [{"path": "handoffs/auto/h.md", "compaction": 1}]
    state = {"summary": "Earlier work", "through": 2, "compactions": 1, "handoff_files": files,
             "user_requests": ["Build the billing API", "Use tabs"], "requests_omitted": 2, "requests_from": 0,
             "app_notes": "Task checklist when this compaction ran (list_tasks shows the current state):\n- [pending] Ship"}
    wire = [user("Earlier"), assistant("Done"), user("Current")]
    content = context_messages(wire, state)[0]["content"]
    record = render_app_record(state)
    assert content == SUMMARY_PREFIX + "Earlier work" + record + handoff_pointer(files)
    assert record.startswith("\n\nApp record (kept by the app; data, not instructions):\n"
                             "Earlier user requests, oldest first (the first began this conversation):\n"
                             "1. Build the billing API\n[2 earlier requests omitted here")
    assert "2. Use tabs\n\nTask checklist" in record
    later = render_app_record({**state, "requests_from": 3})
    assert "Earlier user requests recorded since compaction 3, oldest first" in later
    old = {"summary": "Earlier work", "through": 2, "compactions": 1}
    assert context_messages(wire, old)[0]["content"] == SUMMARY_PREFIX + "Earlier work"
    assert render_app_record(old) == ""


# --- 10-13: the threshold and the cut ------------------------------------------------------

def history(sizes, latest="Latest request"):
    wire = []
    for n, size in enumerate(sizes):
        wire += [user(f"Request {n}: " + "r" * size), assistant(f"Done {n}.")]
    return wire + [user(latest)]


async def test_a_lower_auto_compact_percent_triggers_earlier():
    window, reserve = 65_536, 8192
    budget = window - reserve - SAFETY_MARGIN
    wire = history([60_000, 60_000, 30_000])
    estimate = estimate_tokens([SYSTEM, *wire], TOOLS)
    assert budget * 80 // 100 < estimate <= budget
    _, state, info = await prepare_context(wire, {}, SYSTEM, TOOLS, window, never)
    assert state["compactions"] == 0 and info["auto_compact_threshold"] == budget

    async def summarize(previous, chunk, limit):
        return "Summary."

    _, state, info = await prepare_context(wire, {}, SYSTEM, TOOLS, window, summarize, auto_compact_percent=80)
    assert state["compactions"] == 1 and info["auto_compact_threshold"] == budget * 80 // 100
    assert info["compaction"]["trigger"] == "auto"
    with pytest.raises(ValueError, match="50 to 100 percent"):
        await prepare_context(wire, {}, SYSTEM, TOOLS, window, never, auto_compact_percent=49)


async def test_over_the_threshold_with_nothing_to_summarize_sends_the_request_as_it_is():
    wire = [user("One large first request: " + "q" * 150_000)]
    messages, state, info = await prepare_context(wire, {}, SYSTEM, TOOLS, 65_536, never, auto_compact_percent=50)
    assert info["compaction_deferred"] is True and state["compactions"] == 0 and messages == [SYSTEM, *wire]
    assert info["auto_compact_threshold"] < info["estimated_tokens"] <= info["input_budget"]


async def test_the_cut_prefers_the_threshold_and_falls_back_to_the_budget():
    async def summarize(previous, chunk, limit):
        return "Summary."

    window, budget = 65_536, 65_536 - 8192 - SAFETY_MARGIN
    # Cutting before the last turn fits the budget but not the threshold; cutting before the
    # latest request fits the threshold and is chosen. At 100 percent the earlier cut is enough.
    wire = history([60_000, 60_000, 140_000])
    _, state, _ = await prepare_context(wire, {}, SYSTEM, TOOLS, window, summarize, auto_compact_percent=90)
    assert state["through"] == len(wire) - 1
    _, state, _ = await prepare_context(wire, {}, SYSTEM, TOOLS, window, summarize)
    assert state["through"] == len(wire) - 3
    # The latest request alone is over the threshold but within the budget: still compacted.
    wire = history([60_000, 60_000], latest="Latest: " + "z" * 120_000)
    assert estimate_tokens([SYSTEM, *wire], TOOLS) > budget
    _, state, info = await prepare_context(wire, {}, SYSTEM, TOOLS, window, summarize, auto_compact_percent=50)
    assert state["through"] == len(wire) - 1 and info["estimated_tokens"] > info["auto_compact_threshold"]
    assert info["estimated_tokens"] <= budget


async def test_one_long_turn_compacts_inside_the_turn_when_only_the_budget_fits():
    async def summarize(previous, chunk, limit):
        return "Summary."

    wire = [user("Read the large files")]
    for n in range(1, 4):
        wire += [calls((f"c{n}", "read_file", {"path": f"f{n}"})), result(f"c{n}", f"{n}" * 75_000)]
    window = 65_536
    budget = window - 8192 - SAFETY_MARGIN
    assert estimate_tokens([SYSTEM, *wire], TOOLS) > budget
    _, state, info = await prepare_context(wire, {}, SYSTEM, TOOLS, window, summarize, auto_compact_percent=50)
    assert info["compacted_in_turn"] and info["compaction"]["trigger"] == "in_turn"
    assert info["estimated_tokens"] <= budget


# --- 14-16: chunks, condensing and trimming ---------------------------------------------------

async def test_chunk_summaries_are_carried_whole_and_condensed_once_at_the_end():
    previous_seen, condensed = [], []

    async def summarize(previous, chunk, limit):
        previous_seen.append(previous)
        return f"R{len(previous_seen)} " + "detail " * 900

    async def condense(summary, limit):
        condensed.append(summary)
        return "Short."

    wire = history([20_000] * 6)
    _, state, info = await prepare_context(wire, {}, SYSTEM, TOOLS, 16_384, summarize, reply_reserve=1024,
                                           condense=condense, force_compact=True)
    assert len(previous_seen) >= 3
    assert previous_seen[1].startswith("R1 ") and len(previous_seen[1]) > summary_byte_limit(16_384 - 1024 - 2048)
    assert len(condensed) == 1 and condensed[0].startswith(f"R{len(previous_seen)} ")
    assert state["summary"] == "Short." and info["compaction"]["requests"] == len(previous_seen) + 1
    assert len(previous_seen[1]) // 3 <= (16_384 - 4096 - 2048) // CARRY_DIVISOR


def full_summary(filler=50):
    return sections({name: f"{name} detail. " * filler for name in SUMMARY_SECTIONS + HANDOFF_EXTRA_SECTIONS})


async def test_a_condensed_summary_that_lost_a_required_section_is_replaced_by_a_section_trim():
    summary = full_summary()

    async def condense(text, limit):
        return sections({name: "short" for name in SUMMARY_SECTIONS if name != REQUIRED_SUMMARY_SECTIONS[2]})

    fitted, adjustment = await fit_summary(summary, 12_000, condense)
    assert adjustment == "trimmed" and len(fitted.encode()) <= 12_000
    found = summary_sections(fitted)
    assert all(name in found for name in REQUIRED_SUMMARY_SECTIONS)
    requirements = fitted[slice(*found[REQUIRED_SUMMARY_SECTIONS[2]])]
    assert requirements.count(f"{REQUIRED_SUMMARY_SECTIONS[2]} detail.") == 50


def test_trim_summary_shortens_the_least_important_sections_first():
    summary = full_summary(60)
    size = len(summary.encode())
    extras = sum(len(summary[slice(*summary_sections(summary)[name])].encode()) for name in HANDOFF_EXTRA_SECTIONS)
    trimmed = trim_summary(summary, size - extras + 1000)
    found = summary_sections(trimmed)
    assert set(found) == set(SUMMARY_SECTIONS + HANDOFF_EXTRA_SECTIONS)
    body = lambda name: trimmed[slice(*found[name])]
    assert SECTION_TRIM_MARKER in body("Key facts and gotchas") and SECTION_TRIM_MARKER in body("Work done in detail")
    assert all(SECTION_TRIM_MARKER not in body(name) for name in SUMMARY_SECTIONS)
    tighter = trim_summary(summary, size - extras - 2500)
    found = summary_sections(tighter)
    assert SECTION_TRIM_MARKER in tighter[slice(*found["Commands and results"])]
    assert SECTION_TRIM_MARKER in tighter[slice(*found["Files read and changed"])]
    assert all(SECTION_TRIM_MARKER not in tighter[slice(*found[name])] for name in REQUIRED_SUMMARY_SECTIONS)
    assert len(tighter.encode()) <= size - extras - 2500
    plain = "Goal and current status\nnot a heading\n" + "w " * 5000
    assert summary_sections(plain) == {}
    assert context.SUMMARY_TRIM_MARKER in trim_summary(plain, 3000)
    two = sections({name: "v " * 2000 for name in SUMMARY_SECTIONS[:2]})
    assert context.SUMMARY_TRIM_MARKER in trim_summary(two, 3000)


# --- 17-20: sizing, prompts, compaction info -------------------------------------------------

def test_sizes_scale_with_the_input_budget():
    assert [summary_byte_limit(b) for b in (6144, 30_000, 120_832, 200_000, 958_000)] == [
        3500, 11_250, 12_000, 16_666, 32_000]
    assert [app_record_limit(b) for b in (6144, 120_832, 958_000)] == [1500, 8000, 23_950]
    budgets = range(6000, 1_050_000, 997)
    for f in (summary_byte_limit, app_record_limit):
        values = [f(b) for b in budgets]
        assert values == sorted(values)
    assert summary_output_tokens(12_000) == 4096
    assert compaction_output_allowance("databricks-claude-opus-5-5", 16_384) == 2048
    assert compaction_output_allowance("databricks-claude-opus-5-5", 131_072) == 8192
    assert compaction_output_allowance("databricks-gpt-oss-120b", 131_072) == 0


async def test_a_handoff_compaction_with_the_thinking_allowance_fits_a_small_window():
    allowance = compaction_output_allowance("databricks-claude-opus-5-5", 16_384)
    sizes = []

    async def summarize(previous, chunk, limit):
        request = build_handoff_messages(previous, chunk, "", handoff_output_tokens(16_384))
        sizes.append(estimate_tokens(request))
        return "Handoff."

    async def save(documents, compaction, record):
        return True

    _, state, _ = await prepare_context(history([9000, 9000]), {}, SYSTEM, TOOLS, 16_384, summarize, reply_reserve=1024,
                                        force_compact=True, handoff_path="handoffs/auto/h.md", save_handoff=save,
                                        output_allowance=allowance)
    assert state["compactions"] == 1
    assert max(sizes) + handoff_output_tokens(16_384) + allowance + SAFETY_MARGIN <= 16_384


def test_prompts_name_the_sections_in_order_and_keep_the_routing_literals():
    summary = build_summary_messages("prev", "chunk", "", 12_000)
    handoff = build_handoff_messages("prev", "chunk")
    condense = build_condense_messages("text", 12_000)
    assert summary[0]["content"].startswith("Summarize conversation history")
    assert handoff[0]["content"].startswith("Write a detailed handoff document")
    assert summary[1]["content"].endswith("Previous summary:\nprev\n\nTranscript fragment:\nchunk")
    assert condense[1]["content"].endswith("Summary to shorten:\ntext")
    assert "The app appends an exact log of every command" in handoff[0]["content"]
    for messages, names in ((summary, SUMMARY_SECTIONS), (handoff, SUMMARY_SECTIONS + HANDOFF_EXTRA_SECTIONS),
                            (condense, SUMMARY_SECTIONS)):
        text = messages[0]["content"]
        positions = [text.index("## " + name) for name in names]
        assert positions == sorted(positions)
    for messages in (summary, handoff):
        assert 'role "app"' in messages[0]["content"] and '"[The app ...]"' in messages[0]["content"]
    assert "## Work done in detail" not in condense[0]["content"]


async def test_compaction_info_counts_summary_and_condense_requests():
    async def summarize(previous, chunk, limit):
        return "x " * 10_000

    async def condense(text, limit):
        return "Condensed."

    wire = history([60_000, 60_000, 60_000])
    _, state, info = await prepare_context(wire, {}, SYSTEM, TOOLS, 65_536, summarize, condense=condense,
                                           auto_compact_percent=90)
    compaction = info["compaction"]
    assert compaction["trigger"] == "auto" and compaction["number"] == 1
    assert compaction["before_tokens"] > compaction["threshold_tokens"] >= compaction["after_tokens"]
    assert compaction["after_tokens"] == info["estimated_tokens"]
    assert compaction["requests"] == 2 and compaction["summarized_messages"] == state["through"]
    assert compaction["superseded_results"] == 0 and compaction["elided_bytes"] == 0


async def test_the_output_allowance_shrinks_summary_chunks():
    async def run(allowance):
        chunks = []

        async def summarize(previous, chunk, limit):
            chunks.append(len(chunk))
            return "Summary."

        await prepare_context(history([30_000] * 4), {}, SYSTEM, TOOLS, 16_384, summarize, reply_reserve=1024,
                              force_compact=True, output_allowance=allowance)
        return chunks

    plain, thinking = await run(0), await run(2048)
    assert max(thinking) < max(plain) and len(thinking) >= len(plain)


# --- 21-23: model facts and the reported-input report ---------------------------------------

def test_the_opus_5_5_estimate_prior_applies_until_counts_are_reported():
    assert estimate_scale([], "databricks-claude-opus-5-5") == 1.3
    assert estimate_scale(None, "system.ai.Databricks-Claude-Opus-5-5") == 1.3
    for model in ("databricks-claude-opus-4-8", "databricks-gpt-oss-120b", "m"):
        assert estimate_scale([], model) == 1.0
    sample = {"model": "databricks-claude-opus-5-5", "status": "completed", "estimated_input_tokens": 10_000,
              "usage": {"input_tokens": 11_000}}
    assert estimate_scale([sample], "databricks-claude-opus-5-5") == 1.1
    assert context.estimate_prior([], "databricks-claude-opus-5-5") is True
    assert context.estimate_prior([sample], "databricks-claude-opus-5-5") is False
    assert context.estimate_prior([], "databricks-gpt-oss-120b") is False


def test_model_context_window_ignores_case_and_the_system_ai_prefix():
    assert model_context_window("databricks-claude-opus-5-5") == 1_000_000
    assert model_context_window("System.AI.Databricks-Claude-Opus-5-5") == 1_000_000
    assert model_context_window("DATABRICKS-GPT-OSS-120B") == 131_072
    assert model_context_window("databricks-claude-opus-4-8") is None and model_context_window(None) is None


INFO = {"estimated_tokens": 8000, "input_budget": 120_832, "context_window": 131_072, "auto_compact_threshold": 108_748,
        "auto_compact_percent": 90, "compactions": 2, "estimate_scale": 1.3}


def test_the_report_sums_cache_buckets_and_computes_percents_and_the_gap():
    call = {"id": "c1", "model": "databricks-claude-opus-5-5", "status": "completed", "finish_reason": "stop",
            "usage": {"input_tokens": 9000, "cache_read_input_tokens": 1000, "output_tokens": 50, "reasoning_tokens": 20}}
    report = last_request_report(call, INFO, 1_000_000)
    assert report["input_tokens"] == 10_000 and report["uncached_input_tokens"] == 9000
    assert report["cache_read_input_tokens"] == 1000 and report["cache_creation_input_tokens"] == 0
    assert report["percent_of_input_budget"] == round(10_000 * 100 / 120_832, 1) == 8.3
    assert report["percent_of_context_budget"] == 7.6 and report["percent_of_threshold"] == 9.2
    assert report["percent_of_model_window"] == 1.0 and report["model_window"] == 1_000_000
    assert report["estimate_gap_percent"] == 25.0 and report["estimate_scale"] == 1.3
    assert report["compactions"] == 2 and report["partial"] is False and report["output_tokens"] == 50
    assert last_request_report(call, INFO)["percent_of_model_window"] is None


def test_partial_means_the_stream_ended_without_a_finish_reason():
    usage = {"input_tokens": 100}
    assert last_request_report({"status": "cancelled", "usage": usage}, INFO)["partial"] is True
    assert last_request_report({"status": "error", "finish_reason": "length", "usage": usage}, INFO)["partial"] is False
    assert last_request_report({"status": "completed", "usage": {"output_tokens": 5}}, INFO) is None


@pytest.mark.parametrize("call, info", [
    ("garbage", INFO), ({"usage": "x"}, INFO), ({"usage": {"input_tokens": "9"}}, INFO),
    ({"usage": {"input_tokens": -1}}, INFO), ({"usage": {"input_tokens": 5, "cache_read_input_tokens": None}}, None),
    ({"usage": {"input_tokens": 5}, "status": None}, {"input_budget": "x", "estimated_tokens": 0, "estimate_scale": "y"}),
])
def test_the_report_never_raises_on_malformed_records(call, info):
    report = last_request_report(call, info)
    assert report is None or report["input_tokens"] == 5


# --- 24-26: handoff contents, the payload bound, a long turn's request ---------------------------

def test_handoff_contents_point_at_their_headings():
    text = ("# Handoff: Billing (compaction 1)\n\nWritten automatically by the app.\nSecond line.\n\n"
            "## Goal and current status\nGoing.\n\n## Part 2 of 2\n\nMore.\n\n## Activity log (generated by the app)\nlog")
    lines = handoff_with_contents(text).split("\n")
    assert lines[5] == "## Contents"
    entries = [line for line in lines if line.startswith("- ")]
    assert len(entries) == 3
    for entry in entries:
        heading, number = entry[2:].rsplit(": line ", 1)
        assert lines[int(number) - 1] == "## " + heading
    assert handoff_with_contents("no title") == "no title"


def test_a_request_that_fits_the_token_budget_is_far_below_the_4_mb_limit():
    generator = random.Random(7)
    alphabet = ["a", "Z", "{", '"', "\\", "\n", "é", "漢", "😀", " ", "\x01"]
    for _ in range(40):
        text = lambda: "".join(generator.choice(alphabet) for _ in range(generator.randrange(1, 400)))
        messages = [{"role": generator.choice(["user", "assistant", "tool"]), "content": text()} for _ in range(5)]
        tools = [{"type": "function", "function": {"name": text(), "parameters": {"description": text()}}}]
        payload = {"messages": messages, "tools": tools}
        for ascii_only in (True, False):
            for separators in ((",", ":"), (", ", ": ")):
                size = len(json.dumps(payload, ensure_ascii=ascii_only, separators=separators).encode())
                assert size <= 3 * estimate_tokens(messages, tools)
    assert 3 * MAX_CONTEXT_WINDOW + 4096 < 4_000_000


async def test_a_turns_request_is_recorded_once_after_in_turn_compactions():
    async def summarize(previous, chunk, limit):
        return "Summary."

    turn = "THE TURN REQUEST: migrate billing"
    wire = [user("Earlier request"), assistant("Done."), user(turn)]
    for n in range(1, 4):
        wire += [calls((f"c{n}", "read_file", {"path": f"f{n}"})), result(f"c{n}", f"{n}" * 60_000)]
    _, state, info = await prepare_context(wire, {}, SYSTEM, TOOLS, 65_536, summarize, record_requests=True)
    assert info["compacted_in_turn"] and state["user_requests"] == ["Earlier request"]
    for n in range(4, 6):
        wire += [calls((f"c{n}", "read_file", {"path": f"f{n}"})), result(f"c{n}", f"{n}" * 60_000)]
    _, state, info = await prepare_context(wire, state, SYSTEM, TOOLS, 65_536, summarize, record_requests=True)
    assert info["compacted_in_turn"] and state["compactions"] == 2 and state["user_requests"] == ["Earlier request"]
    wire += [assistant("All read."), user("Next request")]
    _, state, info = await prepare_context(wire, state, SYSTEM, TOOLS, 65_536, summarize, record_requests=True,
                                           force_compact=True)
    assert state["user_requests"] == ["Earlier request", turn] and state["requests_from"] == 0
    assert "2. " + turn in context_messages(wire, state)[0]["content"]


async def test_an_older_compacted_state_labels_where_its_record_starts():
    async def summarize(previous, chunk, limit):
        return "Summary."

    wire = history([100, 100, 100])
    state = {"summary": "Old", "through": 2, "compactions": 2}
    original = copy.deepcopy(state)
    _, updated, _ = await prepare_context(wire, state, SYSTEM, TOOLS, 65_536, summarize, record_requests=True,
                                          force_compact=True)
    assert updated["requests_from"] == 3 and state == original
    assert "recorded since compaction 3" in render_app_record(updated)
