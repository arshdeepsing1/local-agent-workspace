"""The anchored estimate (M5g): a request that extends the chat's previous agent request is estimated as what
Databricks reported for that request plus the scaled estimate of what was added since, for the compaction trigger
and the meter; the ledger keeps the raw estimate and the byte guard gets the raw estimate too."""
import json
import math
from pathlib import Path

import httpx
import pytest

from local_agent import context
from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.context import (anchor_candidate, anchored_estimate, context_breakdown, estimate_scale, estimate_tokens,
                                 prepare_context, tool_groups)
from local_agent.prompt_cache import unmarked
from local_agent.store import Store

REAL_CLIENT = httpx.AsyncClient
SYSTEM = {"role": "system", "content": "Rules."}
TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file.", "parameters": {}}},
         {"type": "function", "function": {"name": "mcp__docs__search", "description": "Search the docs " * 20,
                                           "parameters": {}}}]


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


def anchor_on(messages, reported, tools=TOOLS):
    return {**anchor_candidate(messages), "raw": estimate_tokens(messages, tools), "reported": reported, "model": "m"}


def test_an_estimate_extends_the_reported_request_or_falls_back():
    first = [SYSTEM, user("Hello " * 50)]
    anchor = anchor_on(first, 400)
    later = [*first, assistant("Hi " * 30), user("Next " * 40)]
    raw = estimate_tokens(later, TOOLS)
    added = raw - anchor["raw"]
    assert anchored_estimate(later, raw, 1.3, anchor) == (400 + math.ceil(added * 1.3), added)
    # The system message may change (date, folders, a notice): only what follows it must be the same.
    moved = [{"role": "system", "content": "Rules. Today is another day."}, *later[1:]]
    assert anchored_estimate(moved, estimate_tokens(moved, TOOLS), 1.3, anchor)[0] > 400
    # An earlier message edited, rewound or summarized, or nothing added: the scaled estimate applies.
    for messages in ([SYSTEM, user("Hello " * 49), *later[2:]], first, [SYSTEM, user("Summary"), *later[2:]],
                     [SYSTEM]):
        assert anchored_estimate(messages, estimate_tokens(messages, TOOLS), 1.3, anchor) is None
    # A request that shrank (a shorter system message) counts the removed part unscaled: the estimate stays high.
    shorter = [{"role": "system", "content": ""}, *first[1:], assistant("ok")]
    raw = estimate_tokens(shorter, [])
    assert anchored_estimate(shorter, raw, 1.3, anchor) == (400 + raw - anchor["raw"], raw - anchor["raw"])
    # Malformed anchors never anchor.
    for bad in (None, {}, {**anchor, "reported": 0}, {**anchor, "digest": None}, {**anchor, "count": "1"},
                {**anchor, "raw": 1.5}):
        assert anchored_estimate(later, estimate_tokens(later, TOOLS), 1.3, bad) is None


async def forbidden(*args):
    raise AssertionError("no summary expected")


async def test_prepare_context_anchors_the_trigger_and_the_meter_and_keeps_the_raw_byte_guard(monkeypatch):
    seen = []
    guard = context.request_bytes_over

    def watched(messages, tools, raw_estimate, target):
        seen.append(raw_estimate)
        return guard(messages, tools, raw_estimate, target)
    monkeypatch.setattr(context, "request_bytes_over", watched)
    first = [SYSTEM, user("x" * 60_000)]
    wire = [*first[1:], assistant("Read it."), user("y" * 3_000)]
    messages = [SYSTEM, *wire]
    raw = estimate_tokens(messages, TOOLS)
    # A prose-heavy start that Databricks counted at about half the heuristic.
    anchor = anchor_on(first, 10_000)
    _, _, info = await prepare_context(wire, None, SYSTEM, TOOLS, 32_768, forbidden, reply_reserve=4_096,
                                       scale=1.2, auto_compact_percent=90, anchor=anchor)
    expected = 10_000 + math.ceil((raw - anchor["raw"]) * 1.2)
    assert info["estimated_tokens"] == expected < raw  # Under the threshold: no compaction.
    assert info["estimate_anchor"] == {"reported": 10_000, "added": raw - anchor["raw"]}
    assert info["estimate_method"] == "weighted_utf8" and info["estimate_scale"] == 1.2
    assert seen == [raw]  # The byte guard gets the unscaled estimate, never the anchored one.
    assert sum(info["breakdown"].values()) == expected and all(value >= 0 for value in info["breakdown"].values())
    assert info["breakdown"] == context_breakdown(messages, TOOLS, False, 1.2, expected)
    assert sum(info["tool_groups"].values()) == info["breakdown"]["tool_definitions"]
    assert list(info["tool_groups"]) == ["Built-in tools", "MCP docs"]
    # Without the anchor the same request would compact: the scaled estimate is over the threshold.
    compacted = []

    async def summarize(previous, chunk, limit):
        compacted.append(chunk)
        return "## Goal and current status\nReading."
    _, _, scaled = await prepare_context(wire, None, SYSTEM, TOOLS, 32_768, summarize, reply_reserve=4_096,
                                         scale=1.2, auto_compact_percent=90)
    assert math.ceil(raw * 1.2) > info["auto_compact_threshold"] and compacted and "estimate_anchor" not in scaled


def test_tool_groups_split_the_definitions_exactly():
    assert tool_groups([], 0) == {} and tool_groups(None, 10) == {}
    tools = [*TOOLS, {"type": "function", "function": {"name": "mcp__git_hub__log", "parameters": {}}},
             {"type": "function", "function": {"name": "write_file", "parameters": {}}}]
    groups = tool_groups(tools, 1_234)
    assert list(groups) == ["Built-in tools", "MCP docs", "MCP git_hub"] and sum(groups.values()) == 1_234
    assert groups["MCP docs"] > groups["MCP git_hub"]
    assert tool_groups(tools[1:2], 50) == {"MCP docs": 50}
    # A saved server ID containing "__" keeps its own row when the configured IDs are known.
    legacy = [{"type": "function", "function": {"name": f"mcp__team__{part}__search", "parameters": {}}}
              for part in ("a", "b")]
    assert list(tool_groups(legacy, 100)) == ["MCP team"]
    assert list(tool_groups(legacy, 100, ["team", "team__a", "team__b"])) == ["MCP team__a", "MCP team__b"]
    assert list(tool_groups(tools, 100, ["team__a"])) == ["Built-in tools", "MCP docs", "MCP git_hub"]


# Agent requests (fake Databricks gateway)

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
    session.update(title="Anchors", title_generated=True, permission_mode="bypassPermissions")
    store.save(session)
    yield AgentManager(store, settings), session, settings
    store.db.close()


def gateway(monkeypatch, usage=lambda raw: {"prompt_tokens": raw, "completion_tokens": 5}, finish="stop"):
    """Agent requests answered "Done." with usage(raw estimate of the request); returns the bodies."""
    bodies = []

    async def handle(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "## Goal and current status\nWork."},
                                                          "finish_reason": "stop"}]})
        bodies.append(body)
        sent = unmarked(body)
        chunk = {"choices": [{"delta": {"content": "Done."}, "finish_reason": finish}],
                 "usage": usage(estimate_tokens(sent["messages"], sent["tools"]))}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(handle)))
    return bodies


def agent_calls(session):
    return [call for call in session["inference_calls"] if call["purpose"] == "agent"]


async def test_the_next_request_is_anchored_on_the_last_report(runtime, monkeypatch):
    manager, session, _ = runtime
    gateway(monkeypatch, lambda raw: {"prompt_tokens": raw * 7 // 10, "completion_tokens": 5})
    await manager.run(session, "Hello " * 200)
    first = agent_calls(session)[-1]
    assert "estimate_anchor" not in session["context_info"]  # Nothing reported yet when it was prepared.
    # The meter's next-request estimate before your next message is anchored on that report.
    upcoming = session["context_info"]["next_estimate"]
    assert upcoming["anchor"]["reported"] == first["usage"]["input_tokens"] == first["estimated_input_tokens"] * 7 // 10
    assert upcoming["tokens"] == first["usage"]["input_tokens"] + math.ceil(upcoming["anchor"]["added"] * upcoming["scale"])
    await manager.run(session, "More " * 100)
    second, info = agent_calls(session)[-1], session["context_info"]
    scale = info.get("estimate_scale", 1.0)
    assert info["estimate_anchor"]["reported"] == first["usage"]["input_tokens"]
    assert info["estimate_anchor"]["added"] == second["estimated_input_tokens"] - first["estimated_input_tokens"]
    assert info["estimated_tokens"] == first["usage"]["input_tokens"] + math.ceil(info["estimate_anchor"]["added"] * scale)
    # The ledger keeps the raw estimate (calibration and the cost view), and the report compares with the anchored one.
    assert second["estimated_input_tokens"] > info["estimated_tokens"]
    assert info["last_request"]["estimated_tokens"] == info["estimated_tokens"]
    assert info["last_request"]["estimate_gap_percent"] == round(
        (second["usage"]["input_tokens"] / info["estimated_tokens"] - 1) * 100, 1)


async def test_fallbacks_a_model_change_an_edit_and_a_restart(runtime, monkeypatch):
    manager, session, settings = runtime
    gateway(monkeypatch)
    await manager.run(session, "Hello")
    await manager.run(session, "Again")
    assert "estimate_anchor" in session["context_info"]
    # Another endpoint counts with another tokenizer.
    session["model"] = "other-endpoint"
    await manager.run(session, "On another model")
    assert "estimate_anchor" not in session["context_info"]
    await manager.run(session, "Then again")
    assert session["context_info"]["estimate_anchor"]["reported"] == agent_calls(session)[-2]["usage"]["input_tokens"]
    # An earlier message changed (an edit or a rewind): the request no longer extends the reported one.
    session["wire"][0] = {**session["wire"][0], "content": "Hello, edited"}
    await manager.run(session, "After the edit")
    assert "estimate_anchor" not in session["context_info"]
    # After a restart nothing is anchored until the next report.
    restarted = AgentManager(manager.store, settings)
    again = restarted.get(session["id"])
    await restarted.run(again, "After a restart")
    assert "estimate_anchor" not in again["context_info"]
    await restarted.run(again, "And once more")
    assert "estimate_anchor" in again["context_info"]


@pytest.mark.parametrize("kind", ["partial", "double_counted", "no_usage"])
async def test_a_report_that_cannot_anchor_leaves_the_scaled_estimate(runtime, monkeypatch, kind):
    manager, session, _ = runtime
    if kind == "partial":
        gateway(monkeypatch, finish=None)  # The stream ends without a finish reason.
    elif kind == "double_counted":
        # Uncached input (input_tokens: kept as reported) near the estimate and as much again from the cache.
        gateway(monkeypatch, lambda raw: {"input_tokens": raw, "cache_read_input_tokens": raw, "completion_tokens": 5})
    else:
        gateway(monkeypatch, lambda raw: {"completion_tokens": 5})
    await manager.run(session, "Hello " * 100)
    assert manager.estimate_anchors == {}
    await manager.run(session, "Again")
    assert "estimate_anchor" not in session["context_info"]
    assert session["context_info"]["estimated_tokens"] == math.ceil(
        agent_calls(session)[-1]["estimated_input_tokens"] * estimate_scale(session["inference_calls"], session["model"]))


async def test_a_report_that_cannot_anchor_keeps_the_previous_anchor(runtime, monkeypatch):
    manager, session, _ = runtime
    good = lambda raw: {"prompt_tokens": raw * 7 // 10, "completion_tokens": 5}
    doubled = lambda raw: {"input_tokens": raw, "cache_read_input_tokens": raw, "completion_tokens": 5}
    usages = [good, doubled, good]
    gateway(monkeypatch, lambda raw: usages.pop(0)(raw))
    await manager.run(session, "Hello " * 100)
    first = agent_calls(session)[-1]
    await manager.run(session, "Again")  # Its report looks counted twice: the first one still anchors.
    await manager.run(session, "And once more")
    # That prefix is still exactly what Databricks counted for the first request.
    assert session["context_info"]["estimate_anchor"]["reported"] == first["usage"]["input_tokens"]
    assert session["context_info"]["estimate_anchor"]["added"] == (
        agent_calls(session)[-1]["estimated_input_tokens"] - first["estimated_input_tokens"])


async def test_a_prose_heavy_chat_compacts_later_than_with_the_scaled_estimate(runtime, monkeypatch):
    manager = runtime[0]
    # Plain prose: Databricks counts about 70% of the heuristic, and the scale never goes below 1.
    gateway(monkeypatch, lambda raw: {"prompt_tokens": raw * 7 // 10, "completion_tokens": 5})

    async def turns_until_compaction():
        session = manager.store.create(manager.settings.values)
        session.update(title="Prose", title_generated=True, permission_mode="bypassPermissions")
        for turn in range(1, 60):
            await manager.run(session, f"Part {turn}: " + "the quick brown fox jumps over the lazy dog " * 60)
            if session.get("context_state", {}).get("compactions"):
                return turn, session
        raise AssertionError("never compacted")
    anchored, session = await turns_until_compaction()
    # The request that compacted was estimated at most at the threshold until then, and Databricks counted less.
    assert max(call["usage"]["input_tokens"] for call in agent_calls(session)) <= session["context_info"]["input_budget"]
    monkeypatch.setattr(manager, "estimate_anchor", lambda session: None)
    scaled, _ = await turns_until_compaction()
    assert anchored > scaled + 3


async def test_a_step_note_never_breaks_the_anchor(runtime, monkeypatch):
    manager, session, settings = runtime
    session["model"] = "databricks-claude-opus-5-5"  # Prompt caching at the full level: notices end the last message.
    note = Path(settings.values["workspace"]) / "note.txt"
    note.write_text("first\n")
    steps = iter([("read_file", {"path": "note.txt"}), None, ("list_tasks", {}), None])
    bodies = []

    async def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        sent, step = unmarked(body), next(steps)
        usage = {"prompt_tokens": estimate_tokens(sent["messages"], sent["tools"]), "completion_tokens": 5}
        delta = ({"tool_calls": [{"index": 0, "id": f"c{len(bodies)}", "type": "function", "function": {
            "name": step[0], "arguments": json.dumps(step[1])}}]} if step else {"content": "Done."})
        chunk = {"choices": [{"delta": delta, "finish_reason": "tool_calls" if step else "stop"}], "usage": usage}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(respond)))
    await manager.run(session, "Read the note")
    note.write_text("changed by the user\n")
    await manager.run(session, "Continue")  # Two requests, each ending with the changed-files note.
    noted = [body for body in bodies if isinstance(body["messages"][-1]["content"], list)
             and len(body["messages"][-1]["content"]) == 2]
    assert len(noted) == 2 and len(bodies) == 4
    # The second noted request extends the first (its note left out) and is anchored on its report.
    info, calls = session["context_info"], agent_calls(session)
    assert info["estimate_anchor"]["reported"] == calls[-2]["usage"]["input_tokens"]
    assert calls[-1]["estimated_input_tokens"] == estimate_tokens(unmarked(bodies[-1])["messages"], bodies[-1]["tools"])


async def test_a_side_question_fits_while_the_anchored_chat_has_room(runtime, monkeypatch):
    """/btw estimates the conversation as the chat's own requests do: a prose chat, which now compacts later,
    never refuses a side question as too long before it compacts."""
    from local_agent import agents
    from local_agent.agents import SIDE_TOO_LONG
    manager, session, _ = runtime
    gateway(monkeypatch, lambda raw: {"prompt_tokens": raw * 7 // 10, "completion_tokens": 5})
    prepared, original = [], agents.prepare_context

    async def watched(*args, **kwargs):
        result = await original(*args, **kwargs)
        prepared.append(result[2])
        return result
    monkeypatch.setattr(agents, "prepare_context", watched)
    anchored = 0
    for turn in range(1, 60):
        await manager.run(session, f"Part {turn}: " + "the quick brown fox jumps over the lazy dog " * 60)
        if session.get("context_state", {}).get("compactions"):
            break
        prepared.clear()
        try:
            await manager.side_question(session["id"], "What was part one about?")
        except ValueError as exc:
            assert str(exc) != SIDE_TOO_LONG, (turn, session["context_info"]["next_estimate"])
            raise
        anchored += "estimate_anchor" in prepared[-1]
    else:
        raise AssertionError("never compacted")
    # It compacted later than the scaled estimate would have (turn 17), and every question was anchored.
    assert turn > 20 and anchored == turn - 1


async def test_the_meter_groups_tools_by_the_configured_server_ids(runtime, monkeypatch):
    manager, session, _ = runtime
    gateway(monkeypatch)
    # Saved before new IDs had to avoid "__" (enabled=False: nothing starts).
    manager.extensions.config = {"servers": [{"id": server, "name": server, "transport": "stdio", "command": "program",
                                              "enabled": False} for server in ("team", "team__a")], "hooks": []}
    external = [{"type": "function", "function": {"name": f"mcp__{server}__search", "description": "Search.",
                                                  "parameters": {"type": "object", "properties": {}}}}
                for server in ("team", "team__a")]
    # Turned off, the helpers that can join mid-turn keep no place (joinable_helpers): both MCP tools fit.
    session["disabled_tools"] = ["get_tool_output", "report_findings", "get_subagent_result"]
    await manager._run_databricks(session, "Hello", external)
    assert list(session["context_info"]["tool_groups"]) == ["Built-in tools", "MCP team", "MCP team__a"]


async def test_a_databricks_report_whose_prompt_tokens_include_the_cache_anchors_on_prompt_tokens(runtime, monkeypatch):
    # Databricks' Claude route counts the cache inside prompt_tokens: the whole input is prompt_tokens, not the sum
    # (which looked counted twice and never anchored).
    manager, session, _ = runtime
    gateway(monkeypatch, lambda raw: {"prompt_tokens": raw * 13 // 10, "cache_read_input_tokens": raw,
                                      "completion_tokens": 5, "total_tokens": raw * 13 // 10 + 5})
    await manager.run(session, "Hello " * 100)
    first = agent_calls(session)[-1]
    assert first["usage"]["input_tokens"] == first["estimated_input_tokens"] * 13 // 10 - first["estimated_input_tokens"]
    assert session["context_info"]["last_request"]["input_tokens"] == first["estimated_input_tokens"] * 13 // 10
    await manager.run(session, "Again")
    assert session["context_info"]["estimate_anchor"]["reported"] == first["estimated_input_tokens"] * 13 // 10
