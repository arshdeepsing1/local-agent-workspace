"""Steered messages and compaction: the request and the user's steering stay verbatim, the
latest results are never summarized away, and a queued message waits when it does not fit
(u-large-inputs item 6)."""
import json

import httpx
import pytest

from local_agent import mentions
from local_agent.agents import AgentManager
from local_agent.composer_api import STEER_PREFIX as REEXPORTED
from local_agent.config import Settings
from local_agent.context import (STEER_PREFIX, in_turn_tokens, is_steer, prepare_context, retained_request,
                                 turn_request, turn_start)
from local_agent.store import Store


def call(identifier, path):
    return {"id": identifier, "type": "function", "function": {"name": "read_file",
                                                                "arguments": json.dumps({"path": path})}}


def steered_turn():
    old = [message for number in range(3) for message in (
        {"role": "user", "content": f"Old request {number} " + "o" * 30_000},
        {"role": "assistant", "content": f"Old answer {number}."})]
    request = {"role": "user", "content": "Refactor the parser"}
    response = {"role": "assistant", "content": None,
                "tool_calls": [call(f"c{index}", f"src/p{index}.py") for index in range(3)]}
    results = [{"role": "tool", "tool_call_id": f"c{index}", "content": f"LATEST-RESULT-{index} " + "r" * 19_500}
               for index in range(3)]
    steer = {"role": "user", "content": STEER_PREFIX + "Keep the public API unchanged"}
    return [*old, request, response, *results, steer]


def test_the_steering_prefix_lives_in_context_and_is_re_exported():
    assert REEXPORTED is STEER_PREFIX
    assert is_steer({"role": "user", "content": STEER_PREFIX + "x"})
    assert not is_steer({"role": "assistant", "content": STEER_PREFIX + "x"}) and not is_steer({"role": "user"})


async def test_compaction_after_a_steered_message_keeps_the_turn_and_its_results():
    wire = steered_turn()
    request_index = 6
    chunks = []

    async def summarize(previous, chunk, limit):
        chunks.append(chunk)
        return "## Goal and current status\nEarlier work summarized."
    system = {"role": "system", "content": "s"}
    # The latest results do not fit under the 80 % threshold with the turn kept, only under the
    # input budget: a cut at the steered message would fit, and would summarize them.
    messages, state, info = await prepare_context(wire, {}, system, [], 32768, summarize, reply_reserve=4096,
                                                  auto_compact_percent=80)
    assert info["compactions"] == 1 and state["through"] == request_index
    contents = [message.get("content") for message in messages]
    assert "Refactor the parser" in contents and STEER_PREFIX + "Keep the public API unchanged" in contents
    for index in range(3):
        assert any(isinstance(text, str) and text.startswith(f"LATEST-RESULT-{index}") for text in contents)
    assert chunks and not any("LATEST-RESULT" in chunk for chunk in chunks)


def test_turn_request_skips_steered_messages_and_retained_request_keeps_them():
    wire = [{"role": "user", "content": "Do it"},
            {"role": "assistant", "content": None, "tool_calls": [call("a", "x")]},
            {"role": "tool", "tool_call_id": "a", "content": "1"},
            {"role": "user", "content": STEER_PREFIX + "Faster"},
            {"role": "assistant", "content": None, "tool_calls": [call("b", "y")]},
            {"role": "tool", "tool_call_id": "b", "content": "2"},
            {"role": "user", "content": STEER_PREFIX + "Done soon?"},
            {"role": "assistant", "content": "Yes."}]
    assert turn_request(wire, 7) == 0 and turn_start(wire, 7) == 6  # M4's turn_start is unchanged.
    assert retained_request(wire, 4) == [wire[0], wire[3]]
    assert retained_request(wire, 7) == [wire[0], wire[3], wire[6]]
    assert retained_request(wire, 2) == [wire[0]] and retained_request(wire, 0) == []


def test_in_turn_tokens_matches_the_in_turn_cut_at_the_latest_response():
    wire = steered_turn()
    system = {"role": "system", "content": "s"}
    small, large = in_turn_tokens(wire, {}, system, [], 20000), in_turn_tokens(wire, {}, system, [], 200000)
    assert large > small  # The summary and record placeholders grow with the budget.
    with_tools = in_turn_tokens(wire, {}, system, [{"type": "function", "function": {"name": "t" * 3000}}], 20000)
    assert with_tools >= small + 1000
    # Earlier turns are not counted: they are what an in-turn cut summarizes.
    assert in_turn_tokens(wire[6:], {}, system, [], 20000) == small


async def test_in_turn_tokens_equals_what_the_in_turn_cut_at_the_latest_response_needs():
    """The allowance's guarantee rests on in_turn_tokens being exactly prepare_context's needed_at
    for the latest response (summary, app record, handoff line, request and steering, tools)."""
    import re
    request = {"role": "user", "content": "Refactor the parser"}
    first = {"role": "assistant", "content": None, "tool_calls": [call("c0", "src/a.py")]}
    steer = {"role": "user", "content": STEER_PREFIX + "Also keep the old API."}
    latest = {"role": "assistant", "content": None, "tool_calls": [call(f"c{index}", f"src/p{index}.py")
                                                                   for index in range(1, 4)]}
    wire = [request, first, {"role": "tool", "tool_call_id": "c0", "content": "small"}, steer, latest,
            *({"role": "tool", "tool_call_id": f"c{index}", "content": "r" * 40_000} for index in range(1, 4))]
    system = {"role": "system", "content": "s" * 2000}
    tools = [{"type": "function", "function": {"name": "read_file", "description": "d" * 3000}}]

    async def summarize(previous, chunk, limit):
        raise AssertionError("no cut fits")
    # The in-turn helper reserves one more handoff line (compaction 999, a 128-character path).
    state = {"summary": "", "through": 0, "compactions": 998}
    with pytest.raises(ValueError) as caught:
        await prepare_context(wire, state, system, tools, 32768, summarize, reply_reserve=4096, scale=1.3,
                              auto_compact_percent=100, record_requests=True, handoff_path="x" * 128)
    needed = int(re.search(r"need about ([\d,]+) estimated", str(caught.value)).group(1).replace(",", ""))
    assert needed == in_turn_tokens(wire, state, system, tools, 32768 - 4096 - 2048, 1.3)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", compaction_handoffs=False)
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Steer", title_generated=True)
    store.save(session)
    yield manager, session, project
    store.db.close()


def queue(session, text):
    session["queued_messages"] = [{"id": "q1", "text": text, "paused": False, "created": 1}]


WAIT_NOTICE = ("Your queued message waits for a later step: together with the latest tool results it would not fit "
               "in the context budget. If the reply completes first, it is sent as the next message; after Stop, an "
               "error or the step limit it stays queued until you click Send queued messages.")


async def test_a_queued_message_that_does_not_fit_waits_unpaused_with_one_notice(runtime):
    manager, session, project = runtime
    (project / "big.txt").write_text("".join(f"line {number} " + "b" * 30 + "\n" for number in range(150)))
    session["events"].append({"id": "u1", "type": "user", "text": "Work", "created": 1})
    wire = [{"role": "user", "content": "Work"},
            {"role": "assistant", "content": None, "tool_calls": [call("a", "x")]},
            {"role": "tool", "tool_call_id": "a", "content": "result"}]
    queue(session, "Also see @big.txt")
    tools = manager.session_tools(session)
    sizes = []

    def refuse(message):
        sizes.append(len(message["content"]))
        return False
    await manager.drain_queue(session, wire, tools, fits=refuse)
    await manager.drain_queue(session, wire, tools, fits=refuse)
    assert len(wire) == 3 and sizes[0] > 5_000  # The attachment was measured.
    assert [item["paused"] for item in session["queued_messages"]] == [False]
    assert [event["text"] for event in session["events"] if event["type"] == "notice"] == [WAIT_NOTICE]
    await manager.drain_queue(session, wire, tools, fits=lambda message: True)
    assert wire[-1]["content"].startswith(STEER_PREFIX + "Also see @big.txt") and session["queued_messages"] == []
    steered = session["events"][-1]
    assert steered["type"] == "user" and steered["steered"] is True


async def test_the_agent_loop_passes_a_budget_check_to_the_queue(runtime, monkeypatch):
    manager, session, project = runtime
    seen = []
    drain = manager.drain_queue

    async def spy(session, wire, tools, fits=None):
        seen.append((fits({"role": "user", "content": "short"}), fits({"role": "user", "content": "x" * 600_000})))
        return await drain(session, wire, tools, fits=fits)
    monkeypatch.setattr(manager, "drain_queue", spy)
    client = httpx.AsyncClient
    responses = iter([
        "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "l1", "function": {
            "name": "list_files", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}) + "\n\ndata: [DONE]\n\n",
        "data: " + json.dumps({"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}]})
        + "\n\ndata: [DONE]\n\n"])

    async def gateway(request):
        return httpx.Response(200, text=next(responses))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(gateway)))
    await manager.run(session, "List")
    assert seen == [(True, False)]


def test_a_shortened_attachment_says_where_the_rest_is(runtime):
    manager, session, project = runtime
    (project / "long.py").write_text("".join(f"x{number} = {number}\n" for number in range(500)))
    tools = manager.session_tools(session)
    content, attachments, _ = mentions.expand_context(manager, session, tools, "See @long.py")
    # The size, which read_file gives only from line 1, lets the model choose between paging and outlining.
    size = f" (the file is {(project / 'long.py').stat().st_size:,} bytes)"
    assert (f", shortened{size}; read the rest with read_file start_line=201 or list its sections with "
            "outline_file>>>") in content
    assert attachments[0]["truncated"] is True and mentions.attached_paths(content) == ["long.py"]
    # A header stored before the size was added is still recognised at compaction.
    assert mentions.attached_paths(content.replace(size, "")) == ["long.py"]
    session["disabled_tools"] = ["outline_file"]
    content, _, _ = mentions.expand_context(manager, session, tools, "See @long.py")
    assert f", shortened{size}; read the rest with read_file start_line=201>>>" in content
    assert mentions.attached_paths(content) == ["long.py"]
