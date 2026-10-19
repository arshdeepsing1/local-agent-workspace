"""A turn's result record (turns.turn_result) and its token and time budget (turns.py): the record on the last
reply, in the header and in the idle status; the wrap-up request at the limit, its refused tool calls and the
budget_limit end; subagents counting toward their parent's turn; the Settings default and the chat's own."""
import asyncio
import json
import time

import httpx
import pytest

from local_agent import planning, turns
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import MAX_TURN_TOKENS, Settings
from local_agent.prompt_cache import unmarked
from local_agent.store import Store
from local_agent.telemetry import reported_tokens
from local_agent.transcript import turn_summary_text

from test_parallel_subagents import eventually

OPUS = "databricks-claude-opus-5-5"
REAL_CLIENT = httpx.AsyncClient
# 10,000 input and 1,000 output tokens per response: 11,000 tokens and 0.857144 DBU on Opus 5.5.
USAGE = {"prompt_tokens": 10_000, "completion_tokens": 1_000}
PER_CALL = 0.857144


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(tmp_path / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Budget", title_generated=True, model=OPUS, permission_mode="bypassPermissions")
    store.save(session)
    yield AgentManager(store, settings), session
    store.db.close()


def sse(delta, finish, usage=USAGE):
    chunks = [{"choices": [{"delta": delta, "finish_reason": finish}]}, {"choices": [], "usage": usage}]
    return "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def calls_reply(*calls, usage=USAGE):
    return sse({"tool_calls": [{"index": index, "id": call_id, "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)}}
                               for index, (call_id, name, arguments) in enumerate(calls)]}, "tool_calls", usage)


def text_reply(text, usage=USAGE):
    return sse({"content": text}, "stop", usage)


def is_child(body):
    first = next((m["content"] for m in unmarked(body)["messages"] if m["role"] == "user"), "")
    return isinstance(first, str) and first.startswith("Delegated task:\n")


def system_text(body):
    """The system message and the last message: a request's notices end one of them (agents.STEP_NOTE)."""
    messages = unmarked(body)["messages"]
    last = messages[-1]["content"] if len(messages) > 1 else ""
    return messages[0]["content"] + "\n" + (last if isinstance(last, str) else json.dumps(last))


def scripted(monkeypatch, replies, child=None):
    """replies(requests) -> SSE text for the chat's agent requests; child(child requests) for subagents'."""
    requests, children = [], []

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}]})
        if is_child(body):
            children.append(body)
            return httpx.Response(200, text=child(children))
        requests.append(body)
        return httpx.Response(200, text=replies(requests))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    return requests, children


class Listener:
    """A page's socket: records what the chat broadcasts."""

    def __init__(self):
        self.messages = []

    async def send_json(self, data):
        self.messages.append(json.loads(json.dumps(data)))

    async def close(self, code=1000):
        pass


def listen(manager, session):
    listener = Listener()
    manager.listeners.setdefault(session["id"], set()).add(listener)
    return listener


async def started_turn(manager, session, text):
    """A turn started as the message route starts it (the chat is live, so its subagents find it); the saved chat."""
    manager.start(session["id"], text)
    await manager.tasks[session["id"]]
    return manager.store.get(session["id"])


def notices(session):
    return [event for event in session["events"] if event["type"] in ("notice", "error")]


# The result record

async def test_a_completed_turn_records_its_result_on_the_reply_the_header_and_the_idle_status(runtime, monkeypatch):
    manager, session = runtime
    replies = [calls_reply(("c1", "write_file", {"path": "notes.txt", "content": "one\n"}), ("c2", "list_tasks", {})),
               text_reply("Done.")]
    scripted(monkeypatch, lambda requests: replies[len(requests) - 1])
    listener = listen(manager, session)
    await manager.run(session, "Write a note")
    result = session["last_turn_result"]
    assert result["terminal_reason"] == "completed" and result["is_error"] is False
    assert (result["steps"], result["tool_calls"]) == (2, 2)
    assert result["usage"] == {"calls": 2, "input_tokens": 20_000, "output_tokens": 2_000, "cache_read_input_tokens": None,
                               "cache_creation_input_tokens": None, "reasoning_tokens": None}  # Not reported: None.
    assert result["tokens"] == 22_000 and result["estimated_dbu"] == pytest.approx(2 * PER_CALL)
    assert result["unpriced_calls"] == 0 and result["subagents"] == {"count": 0, "tokens": 0, "estimated_dbu": 0.0}
    assert result["changed_files"] == ["notes.txt"] and result["duration_s"] >= 0
    assert result["ended"] >= result["started"] and "changed_files_total" not in result
    reply = [event for event in session["events"] if event["type"] == "assistant"][-1]
    assert reply["text"] == "Done." and reply["turn_result"] == result
    assert manager.store.header(session["id"])["last_turn_result"] == result
    idle = [message for message in listener.messages if message.get("type") == "status" and message["status"] == "idle"]
    assert idle[-1]["turn_result"] == result and idle[-1]["terminal_reason"] == "completed"
    # The footer's record reached the page with the reply, before the idle status.
    pushed = [message["event"] for message in listener.messages
              if message.get("type") == "event" and message["event"]["id"] == reply["id"]]
    assert pushed[-1]["turn_result"] == result
    assert manager.turns == {} and manager.budget_wrapups == set()
    # Only the latest turn: the next one counts its own requests.
    replies.append(text_reply("Again."))
    await manager.run(session, "Once more")
    assert (session["last_turn_result"]["steps"], session["last_turn_result"]["tokens"]) == (1, 11_000)


async def test_results_of_step_limit_spending_limit_error_and_stopped_turns(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["max_agent_steps"] = 2
    endless = lambda requests: calls_reply((f"c{len(requests)}", "list_tasks", {}))  # noqa: E731
    requests, _ = scripted(monkeypatch, endless)
    await manager.run(session, "Go")
    assert session["last_turn_result"]["terminal_reason"] == "step_limit"
    assert (session["last_turn_result"]["steps"], session["last_turn_result"]["tool_calls"]) == (2, 2)
    session["spending_limit_dbu"] = 0.001
    await manager.run(session, "Go on")
    result = session["last_turn_result"]
    assert result["terminal_reason"] == "spending_limit" and result["steps"] == 0 and result["estimated_dbu"] is None
    session.pop("spending_limit_dbu")

    async def broken(request):
        return httpx.Response(401, json={"message": "no"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(broken)))
    await manager.run(session, "Fail")
    assert session["last_turn_result"]["terminal_reason"] == "inference_error" and session["last_turn_result"]["is_error"]

    started = asyncio.Event()

    async def hang(request):
        started.set()
        await asyncio.sleep(3600)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(hang)))
    listener = listen(manager, session)
    manager.start(session["id"], "Stop me")
    await asyncio.wait_for(started.wait(), 5)
    await manager.stop(session["id"])
    assert manager.store.get(session["id"])["last_turn_result"]["terminal_reason"] == "stopped"
    assert [m for m in listener.messages if m.get("type") == "status" and m["status"] == "idle"][-1]["turn_result"][
        "terminal_reason"] == "stopped"


async def test_an_endpoint_without_prices_has_no_dbu_and_counts_unpriced_calls(runtime, monkeypatch):
    manager, session = runtime
    session["model"] = "test-endpoint"
    scripted(monkeypatch, lambda requests: text_reply("Hi.", {"prompt_tokens": 50, "completion_tokens": 5,
                                                             "prompt_tokens_details": {"cached_tokens": 20}}))
    await manager.run(session, "Hello")
    result = session["last_turn_result"]
    assert result["estimated_dbu"] is None and result["unpriced_calls"] == 1 and result["tokens"] == 55
    assert result["usage"]["input_tokens"] == 30 and result["usage"]["cache_read_input_tokens"] == 20


def delegation(call_id="d1", task="Map it"):
    return (call_id, "delegate_task", {"task": task, "agent_type": "explore"})


async def test_subagents_count_in_the_parents_result(runtime, monkeypatch):
    manager, session = runtime
    parent_replies = [calls_reply(delegation()), text_reply("Reported.")]
    child_replies = [calls_reply(("x1", "list_tasks", {})), text_reply("## Report\nNothing.")]
    scripted(monkeypatch, lambda requests: parent_replies[len(requests) - 1],
             lambda children: child_replies[len(children) - 1])
    session = await started_turn(manager, session, "Delegate")
    result = session["last_turn_result"]
    assert result["subagents"] == {"count": 1, "tokens": 22_000, "estimated_dbu": pytest.approx(2 * PER_CALL)}
    assert result["tokens"] == 44_000 and result["estimated_dbu"] == pytest.approx(4 * PER_CALL)
    child = manager.store.get(session["subagents"][0]["id"])
    assert child["last_turn_result"]["tokens"] == 22_000 and child["last_turn_result"]["subagents"]["count"] == 0
    assert session["subagents"][0]["tokens"] == 22_000  # The index keeps it for later turns' checks.


async def test_a_count_no_call_reported_is_none_not_zero(runtime, monkeypatch):
    manager, session = runtime
    parent_replies = [calls_reply(delegation(), usage={}), text_reply("Reported.", usage={})]
    child_replies = [text_reply("## Report\nNothing.", usage={})]
    scripted(monkeypatch, lambda requests: parent_replies[len(requests) - 1],
             lambda children: child_replies[len(children) - 1])
    session = await started_turn(manager, session, "Delegate")
    result = session["last_turn_result"]
    assert result["usage"]["input_tokens"] is None and result["tokens"] is None
    assert result["subagents"]["count"] == 1 and result["subagents"]["tokens"] is None
    assert session["subagents"][0]["tokens"] is None  # The subagents index tells it apart from 0 too.
    assert "— in / — out" in turn_summary_text(result)


async def test_a_restart_counts_the_cut_short_turns_own_requests(runtime, monkeypatch, tmp_path):
    """The ledger as a real turn writes it: a turn cut short after a finished tool step keeps that step's tokens and
    DBU in the record a restart gives it (what scripts and a scheduled run's history read), ended before the
    restart."""
    manager, session = runtime
    (tmp_path / "a.txt").write_text("hi")
    never, requests = asyncio.Event(), []

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}]})
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, text=calls_reply(("c1", "read_file", {"path": "a.txt"})))
        await never.wait()  # The second request is still running when the app stops.
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    manager.start(session["id"], "Read a.txt")
    for _ in range(500):
        await asyncio.sleep(0.01)
        if len(requests) == 2 and manager.store.get(session["id"])["inference_calls"][-1]["status"] == "running":
            break
    else:
        raise AssertionError("The second request did not start.")
    before = time.time()
    restarted = AgentManager(Store(tmp_path / "tests.sqlite3"), manager.settings)
    try:
        result = restarted.store.get(session["id"])["last_turn_result"]
        assert (result["terminal_reason"], result["steps"], result["tool_calls"]) == ("interrupted", 2, 1)
        assert (result["usage"]["calls"], result["tokens"]) == (2, 11_000)
        assert result["estimated_dbu"] == pytest.approx(PER_CALL) and result["ended"] <= round(before, 3)
    finally:
        manager.tasks[session["id"]].cancel()
        await asyncio.gather(manager.tasks[session["id"]], return_exceptions=True)
        restarted.store.db.close()


def test_reported_tokens_tells_an_unreported_count_from_zero():
    assert reported_tokens([]) is None and reported_tokens([{"usage": {}}, "damaged", {"usage": None}]) is None
    assert reported_tokens([{"usage": {"prompt_tokens": 0, "completion_tokens": 0}}]) == 0
    assert reported_tokens([{"usage": {}}, {"usage": {"prompt_tokens": 5, "completion_tokens": 2}}]) == 7


# The budget

async def test_the_token_limit_gives_one_wrap_up_request_then_ends_the_turn(runtime, monkeypatch):
    manager, session = runtime
    session["turn_token_limit"] = 20_000  # Reached after two responses (22,000 tokens).
    endless = lambda requests: calls_reply((f"c{len(requests)}", "list_tasks", {}))  # noqa: E731
    requests, _ = scripted(monkeypatch, endless)
    await manager.run(session, "Work")
    # Two requests under the limit, then the wrap-up request; its tool call does not run.
    assert len(requests) == 3
    assert "This turn has used about 22,000 of its 20,000-token limit per turn." in system_text(requests[2])
    assert "Do not call any tools" in system_text(requests[2])
    assert not any("20,000-token limit" in system_text(body) for body in requests[:2])
    tools = [event for event in session["events"] if event["type"] == "tool"]
    assert [event["state"] for event in tools] == ["completed", "completed", "rejected"]
    assert tools[-1]["output"] == turns.WRAP_UP_REFUSAL
    stop = notices(session)[-1]
    assert stop["type"] == "notice" and stop["terminal_reason"] == "budget_limit"
    assert stop["text"].startswith("This turn has used about 33,000 of its 20,000-token limit per turn. The agent "
                                   "wrapped up instead of continuing.")
    assert session["terminal_reason"] == "budget_limit" == session["last_turn_result"]["terminal_reason"]
    assert manager.budget_wrapups == set()
    # Continue starts a new budget: the next turn sends requests again.
    await manager.run(session, "Continue from where you stopped.")
    assert len(requests) == 6 and session["terminal_reason"] == "budget_limit"


async def test_a_wrap_up_reply_ends_the_turn_without_a_verify_round(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["turn_token_limit"] = 10_000  # The Settings default; the chat has none of its own.
    replies = [calls_reply(("c1", "list_tasks", {})), text_reply("Summary: one step done, the rest remains.")]
    requests, _ = scripted(monkeypatch, lambda requests: replies[len(requests) - 1])
    await manager.run(session, "Work")
    assert len(requests) == 2 and session["terminal_reason"] == "budget_limit"
    assert session["events"][-1]["terminal_reason"] == "budget_limit"
    reply = [event for event in session["events"] if event["type"] == "assistant"][-1]
    assert reply["text"].startswith("Summary") and reply["turn_result"]["terminal_reason"] == "budget_limit"
    assert turns.status(manager, session) == {"turn_token_limit": 10_000, "token_source": "settings",
                                              "turn_time_limit_minutes": None, "time_source": "settings",
                                              "follows_parent": False}


async def test_an_unpriced_endpoint_is_capped_by_tokens(runtime, monkeypatch):
    manager, session = runtime
    session.update(model="test-endpoint", turn_token_limit=1)
    requests, _ = scripted(monkeypatch, lambda requests: calls_reply((f"c{len(requests)}", "list_tasks", {})))
    await manager.run(session, "Work")
    assert len(requests) == 2 and session["terminal_reason"] == "budget_limit"


async def test_the_time_limit_uses_the_turns_wall_clock(runtime, monkeypatch):
    manager, session = runtime
    session["turn_time_limit_minutes"] = 2
    now = [1_000_000.0]
    monkeypatch.setattr(turns, "clock", lambda: now[0])

    def replies(requests):
        now[0] += 50  # Each step takes 50 seconds.
        return calls_reply((f"c{len(requests)}", "list_tasks", {}))
    requests, _ = scripted(monkeypatch, replies)
    await manager.run(session, "Work")
    # 0, 50, 100 s: under 2 minutes; at 150 s the wrap-up request, then the end.
    assert len(requests) == 4
    assert "2 min 30 s of its 2-minute limit per turn" in system_text(requests[3])
    assert session["terminal_reason"] == "budget_limit" and "of its 2-minute limit" in notices(session)[-1]["text"]


async def test_a_subagent_counts_toward_its_parents_turn_and_reports_its_stop(runtime, monkeypatch):
    manager, session = runtime
    session["turn_token_limit"] = 30_000
    parent_replies = [calls_reply(delegation()), text_reply("The subagent stopped at the budget.")]
    child = lambda children: calls_reply((f"x{len(children)}", "list_tasks", {}))  # noqa: E731
    requests, children = scripted(monkeypatch, lambda requests: parent_replies[min(len(requests), 2) - 1], child)
    manager.store.save(session)
    session = await started_turn(manager, session, "Delegate")
    # Parent 11,000 + child 11,000 + 11,000 = 33,000: the child's third request is its wrap-up.
    assert len(children) == 3 and "per turn" in system_text(children[2])
    child_session = manager.store.get(session["subagents"][0]["id"])
    error = [event for event in child_session["events"] if event["type"] == "error"][-1]
    assert error["terminal_reason"] == "budget_limit" and error["text"].startswith("Its parent conversation's turn has used")
    delegated = next(event for event in session["events"] if event.get("name") == "delegate_task")
    report = json.loads(delegated["output"])
    assert report["terminal_reason"] == "budget_limit" and report["status"] == "failed"
    # Then the parent itself is over: its next request is its own wrap-up.
    assert len(requests) == 2 and "per turn" in system_text(requests[1])
    assert session["terminal_reason"] == "budget_limit"
    assert session["last_turn_result"]["subagents"]["tokens"] == 33_000


# A background subagent across its parent's turns: its requests count in the turn during which they were made.

def gated_gateway(monkeypatch, parent, child):
    """parent(n) and child(n), async: the SSE text of the chat's nth agent request and of its subagent's nth."""
    requests, children = [], []

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}]})
        if is_child(body):
            children.append(body)
            return httpx.Response(200, text=await child(len(children)))
        requests.append(body)
        return httpx.Response(200, text=await parent(len(requests)))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    return requests, children


def completed_calls(manager, child_id):
    child = manager.get(child_id) or {}
    return sum(call.get("status") == "completed" for call in child.get("inference_calls") or [])


async def background_turn(manager, session, monkeypatch, turn_two, gates):
    """Turn 1 starts a background subagent that makes one request in it and one more while the chat is idle;
    its request n then waits for gates[n]. turn_two(n, child_id): the parent's nth request (from 3 on)."""
    state, idle = {}, asyncio.Event()
    gates = {2: idle, **gates}

    async def child(count):
        if count in gates:
            await gates[count].wait()
        return calls_reply((f"x{count}", "list_tasks", {})) if count < 4 else text_reply("## Report\nMapped.")

    async def parent(count):
        if count == 1:
            return calls_reply(("d1", "delegate_task", {"task": "Map it", "agent_type": "explore", "background": True}))
        if count == 2:  # The subagent's first request ends within turn 1.
            state["child"] = manager.delegates.background[session["id"]][0]["child_session_id"]
            await eventually(lambda: completed_calls(manager, state["child"]) == 1)
            return text_reply("Started it.")
        return await turn_two(count, state["child"])
    requests, children = gated_gateway(monkeypatch, parent, child)
    manager.store.save(session)
    first = await started_turn(manager, session, "Map it in the background")
    idle.set()
    await eventually(lambda: completed_calls(manager, state["child"]) == 2)  # Its second, while the chat is idle.
    return first, state["child"], requests


async def test_a_background_subagent_counts_only_in_the_turn_during_which_it_made_its_requests(runtime, monkeypatch):
    manager, session = runtime
    gates = {3: asyncio.Event(), 4: asyncio.Event()}
    seen = {}

    async def turn_two(count, child_id):
        if count == 3:
            seen["used"] = turns.used(manager, manager.get(session["id"]))[2]
            root, _, tokens, _ = turns.used(manager, manager.get(child_id))
            seen["child_scope"] = (root["id"], tokens)
            gates[3].set()  # The subagent's third request is made during turn 2.
            await eventually(lambda: completed_calls(manager, child_id) == 3)
            return calls_reply(("p3", "list_tasks", {}))
        return text_reply("Done with turn two.")
    first, child_id, requests = await background_turn(manager, session, monkeypatch, turn_two, gates)
    one = first["last_turn_result"]
    assert one["tokens"] == 33_000 and one["subagents"] == {"count": 1, "tokens": 11_000,
                                                             "estimated_dbu": pytest.approx(PER_CALL)}
    # Turn 2 has its own limit: its requests (22,000) and the subagent's one request in it (11,000) stay under it,
    # though the subagent spent 22,000 more before the turn began.
    manager.get(session["id"])["turn_token_limit"] = 40_000
    second = await started_turn(manager, session, "Something else")
    two = second["last_turn_result"]
    assert seen["used"] == 0 and len(requests) == 4 and "per turn" not in system_text(requests[3])
    assert two["terminal_reason"] == "completed" and two["tokens"] == 33_000
    assert two["subagents"] == {"count": 1, "tokens": 11_000, "estimated_dbu": pytest.approx(PER_CALL)}
    assert two["estimated_dbu"] == pytest.approx(3 * PER_CALL)
    # The subagent started before turn 2 keeps its own budget: its whole run, not the parent's new turn.
    assert seen["child_scope"] == (child_id, 22_000)
    assert first["last_turn_result"] == one  # Turn 1's record is unchanged.
    # A turn during which it makes no request does not count it.
    third = await started_turn(manager, session, "And a third thing")
    assert third["last_turn_result"]["subagents"] == {"count": 0, "tokens": 0, "estimated_dbu": 0.0}
    gates[4].set()
    await eventually(lambda: manager.delegates.background_running(session["id"]) == 0)


async def test_a_background_subagent_that_ends_during_a_later_turn_counts_its_requests_in_it(runtime, monkeypatch):
    manager, session = runtime
    gates = {3: asyncio.Event(), 4: asyncio.Event()}

    async def turn_two(count, child_id):
        if count == 3:
            gates[3].set()
            gates[4].set()  # It makes its last two requests and ends while the parent waits for it.
            return calls_reply(("g1", "get_subagent_result", {"subagent_id": child_id, "wait_seconds": 10}))
        return text_reply("It is done.")
    _, child_id, _ = await background_turn(manager, session, monkeypatch, turn_two, gates)
    second = await started_turn(manager, session, "Wait for it")
    assert manager.delegates.background_running(session["id"]) == 0
    two = second["last_turn_result"]
    assert two["subagents"] == {"count": 1, "tokens": 22_000, "estimated_dbu": pytest.approx(2 * PER_CALL)}
    assert two["tokens"] == 44_000


async def test_a_subagents_wrap_up_runs_only_its_report_which_reaches_the_parent(runtime, monkeypatch):
    manager, session = runtime
    session["turn_token_limit"] = 30_000
    summary = "Mapped the parser: three modules; the tokenizer remains."
    parent_replies = [calls_reply(delegation()), text_reply("The subagent reported at the budget.")]

    def child(children):
        if len(children) < 3:
            return calls_reply((f"x{len(children)}", "list_tasks", {}))
        # The wrap-up request: another call (refused) and the report (the one call that runs).
        return calls_reply(("x3", "list_tasks", {}), ("r1", "submit_report", {"summary": summary}))
    requests, children = scripted(monkeypatch, lambda requests: parent_replies[min(len(requests), 2) - 1], child)
    manager.store.save(session)
    session = await started_turn(manager, session, "Delegate")
    # Its wrap-up request still offers submit_report and says to send the report with it.
    assert len(children) == 3 and "submit_report" in {tool["function"]["name"] for tool in children[2]["tools"]}
    assert "Call no tool except submit_report" in system_text(children[2])
    assert "Do not call any tools" not in system_text(children[2])
    child_session = manager.store.get(session["subagents"][0]["id"])
    tools = [event for event in child_session["events"] if event["type"] == "tool"]
    assert [(event["name"], event["state"]) for event in tools][-2:] == [("list_tasks", "rejected"),
                                                                        ("submit_report", "completed")]
    assert tools[-2]["output"] == turns.WRAP_UP_REFUSAL
    # The report ends the child's turn at the budget: no request follows it, and the parent gets both.
    assert child_session["terminal_reason"] == "budget_limit"
    delegated = next(event for event in session["events"] if event.get("name") == "delegate_task")
    result = json.loads(delegated["output"])
    assert result["report"] == {"summary": summary}
    assert (result["status"], result["terminal_reason"]) == ("failed", "budget_limit")
    assert result["output"].startswith("Errors:\nIts parent conversation's turn has used")
    assert "No final assistant reply" not in result["output"]
    assert manager.budget_wrapups == set()


async def test_a_wrap_up_on_the_last_agent_step_ends_the_turn_at_the_budget(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["max_agent_steps"] = 3
    session["turn_token_limit"] = 20_000  # 22,000 after two responses: the third and last request is the wrap-up.
    requests, _ = scripted(monkeypatch, lambda requests: calls_reply((f"c{len(requests)}", "list_tasks", {})))
    await manager.run(session, "Work")
    assert len(requests) == 3 and "Do not call any tools" in system_text(requests[2])
    assert [event["state"] for event in session["events"] if event["type"] == "tool"][-1] == "rejected"
    stop = notices(session)[-1]
    assert stop["terminal_reason"] == "budget_limit" == session["last_turn_result"]["terminal_reason"]
    assert stop["text"].startswith("This turn has used about 33,000 of its 20,000-token limit per turn. The agent "
                                   "wrapped up instead of continuing.")
    assert not any("Increase Agent steps" in event["text"] for event in notices(session))


def narrated(call_id, text):
    """A response with narration text and one list_tasks call."""
    return sse({"content": text, "tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                                  "function": {"name": "list_tasks", "arguments": "{}"}}]}, "tool_calls")


def broken_call(call_id, name, arguments):
    """A response whose one tool call has arguments that are not valid JSON."""
    return sse({"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                "function": {"name": name, "arguments": arguments}}]}, "tool_calls")


async def test_a_subagents_wrap_up_report_the_schema_refuses_still_reaches_the_parent_as_text(runtime, monkeypatch):
    """Outside the wrap-up a report the schema refuses is an error the model fixes and sends again; the wrap-up is
    the turn's last request, so no request follows, and the parent gets the refused report as written (redacted)
    with the error, not an earlier step's narration."""
    manager, session = runtime
    session["turn_token_limit"] = 30_000
    summary = "Mapped the parser: three modules; the tokenizer remains."
    # A realistic slip: a finding with a key the schema does not take (additionalProperties: false).
    refused = {"summary": summary, "findings": [{"path": "parser.py", "line": 3, "text": "Entry point.",
                                                 "severity": "info"}],
               "next_steps": ["Map the tokenizer with the key private-token."]}
    parent_replies = [calls_reply(delegation()), text_reply("The subagent reported at the budget.")]

    def child(children):
        if len(children) < 3:
            return narrated(f"x{len(children)}", "Looking at the parser next.")
        if len(children) == 3:  # The wrap-up request: the report, with one bad field.
            return calls_reply(("r1", "submit_report", refused))
        return calls_reply(("r2", "submit_report", {"summary": summary}))  # Never asked for.
    requests, children = scripted(monkeypatch, lambda requests: parent_replies[min(len(requests), 2) - 1], child)
    manager.store.save(session)
    session = await started_turn(manager, session, "Delegate")
    assert len(children) == 3 and "Call no tool except submit_report" in system_text(children[2])
    child_session = manager.store.get(session["subagents"][0]["id"])
    assert next(event for event in child_session["events"] if event.get("call_id") == "r1")["state"] == "error"
    assert child_session["terminal_reason"] == "budget_limit"
    delegated = next(event for event in session["events"] if event.get("name") == "delegate_task")
    result = json.loads(delegated["output"])
    assert (result["status"], result["terminal_reason"]) == ("failed", "budget_limit") and "report" not in result
    output = result["output"]
    assert output.startswith("Errors:\nInvalid tool arguments at findings.0")
    assert "Looking at the parser" not in output and "No final assistant reply" not in output, output
    written = json.dumps(refused).replace("private-token", "[REDACTED]")
    assert output.endswith(planning.REFUSED_REPORT + written) and '"severity": "info"' in output, output


REMAINING = "The tokenizer remains: map tokenizer.py next."
# Wrap-up reports the schema refuses, each with what remains in the part it refused: the wrap-up asks for what is
# done, what remains and how to continue, which invites these shapes.
REFUSED_SHAPES = {
    "summary_object": {"summary": {"done": "Mapped the parser: three modules.", "remaining": REMAINING}},
    "extra_top_level_key": {"summary": "Mapped the parser: three modules.", "remaining": REMAINING},
    "string_findings": {"summary": "Mapped the parser: three modules.", "findings": [f"parser.py:3: {REMAINING}"]},
}


@pytest.mark.parametrize("shape", list(REFUSED_SHAPES))
async def test_a_refused_wrap_up_report_reaches_the_parent_with_the_part_the_schema_refused(runtime, monkeypatch,
                                                                                            shape):
    """A summary that is not text, a key the schema does not take or findings that are not objects: the parent gets
    the refused report as the model wrote it, never an earlier step's narration nor the report without that part."""
    manager, session = runtime
    session["turn_token_limit"] = 30_000
    parent_replies = [calls_reply(delegation()), text_reply("The subagent reported at the budget.")]

    def child(children):
        if len(children) < 3:
            return narrated(f"x{len(children)}", "Looking at the parser next.")
        return calls_reply(("r1", "submit_report", REFUSED_SHAPES[shape]))  # The wrap-up request's report.
    requests, children = scripted(monkeypatch, lambda requests: parent_replies[min(len(requests), 2) - 1], child)
    manager.store.save(session)
    session = await started_turn(manager, session, "Delegate")
    assert len(children) == 3 and "Call no tool except submit_report" in system_text(children[2])
    child_session = manager.store.get(session["subagents"][0]["id"])
    assert next(event for event in child_session["events"] if event.get("call_id") == "r1")["state"] == "error"
    delegated = next(event for event in session["events"] if event.get("name") == "delegate_task")
    result = json.loads(delegated["output"])
    assert result["terminal_reason"] == "budget_limit" and "report" not in result
    output = result["output"]
    assert output.startswith("Errors:\nInvalid tool arguments") and "Looking at the parser" not in output, output
    assert output.endswith(planning.REFUSED_REPORT + json.dumps(REFUSED_SHAPES[shape])), output


async def test_a_subagents_wrap_up_report_with_broken_json_is_promised_no_retry_and_reaches_the_parent_as_written(
        runtime, monkeypatch):
    """Outside the wrap-up a hidden request asks the model to send broken tool arguments again; the wrap-up is the
    turn's last request, so the chat promises no retry, and the parent gets the report's arguments as written
    (redacted)."""
    manager, session = runtime
    session["turn_token_limit"] = 30_000
    summary = "Mapped the parser with the key private-token: three modules; the tokenizer remains."
    arguments = '{"summary": "' + summary + '", "findings": [{"path": "parser.py", "text": "Entry point."},]}'
    parent_replies = [calls_reply(delegation()), text_reply("The subagent reported at the budget.")]

    def child(children):
        if len(children) < 3:
            return calls_reply((f"x{len(children)}", "list_tasks", {}))
        if len(children) == 3:  # The wrap-up request: a trailing comma breaks the report's JSON.
            return broken_call("r1", "submit_report", arguments)
        return calls_reply(("r2", "submit_report", {"summary": summary}))  # Never asked for.
    requests, children = scripted(monkeypatch, lambda requests: parent_replies[min(len(requests), 2) - 1], child)
    manager.store.save(session)
    session = await started_turn(manager, session, "Delegate")
    assert len(children) == 3 and "Call no tool except submit_report" in system_text(children[2])
    child_session = manager.store.get(session["subagents"][0]["id"])
    texts = [event["text"] for event in notices(child_session)]
    assert not any("automatically" in text.lower() for text in texts), texts  # No hidden retry is promised.
    assert texts[-1].startswith("Its parent conversation's turn has used about 44,000 of its 30,000-token limit")
    assert child_session["terminal_reason"] == "budget_limit" and manager.budget_wrapups == set()
    delegated = next(event for event in session["events"] if event.get("name") == "delegate_task")
    result = json.loads(delegated["output"])
    assert (result["status"], result["terminal_reason"]) == ("failed", "budget_limit") and "report" not in result
    assert result["output"].startswith("Errors:\nIts parent conversation's turn has used")
    assert result["output"].endswith(planning.UNSENT_REPORT + arguments.replace("private-token", "[REDACTED]"))
    assert "private-token" not in json.dumps(child_session["events"])  # Kept on its reply, redacted.


@pytest.mark.parametrize("failure", ["output_limit", "incomplete_response"])
async def test_a_wrap_up_summary_that_ends_early_is_promised_no_continuation(runtime, monkeypatch, failure):
    """A plain chat's wrap-up summary cut at the output limit (or ended without a finish reason): an earlier step
    would continue in a hidden request, but the wrap-up is the turn's last request, so the budget ends the turn
    with the partial summary kept and no continuation promised."""
    manager, session = runtime
    session["turn_token_limit"] = 20_000  # 22,000 after two responses: the third request wraps up.

    def replies(requests):
        if len(requests) < 3:
            return calls_reply((f"c{len(requests)}", "list_tasks", {}))
        if len(requests) == 3:
            return sse({"content": "Summary: two steps done; "}, "length" if failure == "output_limit" else None)
        return text_reply("Summary: two steps done, the rest remains.")  # Never asked for.
    requests, _ = scripted(monkeypatch, replies)
    await manager.run(session, "Work")
    assert len(requests) == 3 and "Do not call any tools" in system_text(requests[2])
    texts = [event["text"] for event in notices(session)]
    assert not any("automatically" in text.lower() for text in texts), texts  # No hidden retry is promised.
    stop = notices(session)[-1]
    assert stop["terminal_reason"] == "budget_limit" == session["last_turn_result"]["terminal_reason"]
    assert stop["text"].startswith("This turn has used about 33,000 of its 20,000-token limit per turn. The agent "
                                   "wrapped up instead of continuing.")
    reply = [event for event in session["events"] if event["type"] == "assistant"][-1]
    assert reply["text"] == "Summary: two steps done; "
    assert session["wire"][-1]["role"] == "assistant" and manager.budget_wrapups == set()


@pytest.mark.parametrize("failure", ["output_limit", "invalid_tool_arguments"])
async def test_a_wrap_up_reply_on_the_last_agent_step_that_cannot_be_used_still_ends_at_the_budget(runtime, monkeypatch,
                                                                                                  failure):
    """The wrap-up is the third and last request, and its reply is cut at the output limit or has broken tool JSON:
    the turn ends at the budget, as the same reply on an earlier step does, not with an inference error that
    advises more Agent steps."""
    manager, session = runtime
    manager.settings.values["max_agent_steps"] = 3
    session["turn_token_limit"] = 20_000  # 22,000 after two responses: the third and last request is the wrap-up.

    def replies(requests):
        if len(requests) < 3:
            return calls_reply((f"c{len(requests)}", "list_tasks", {}))
        if failure == "output_limit":
            return sse({"content": "Done so far: the parser. Remaining: the tok"}, "length")
        return broken_call("c3", "list_tasks", '{"oops')
    requests, _ = scripted(monkeypatch, replies)
    await manager.run(session, "Work")
    assert len(requests) == 3 and "Do not call any tools" in system_text(requests[2])
    stop = notices(session)[-1]
    assert (session["terminal_reason"], stop.get("terminal_reason")) == ("budget_limit", "budget_limit"), stop["text"]
    assert session["last_turn_result"]["terminal_reason"] == "budget_limit"
    assert stop["text"].startswith("This turn has used about 33,000 of its 20,000-token limit per turn. The agent "
                                   "wrapped up instead of continuing.")
    assert not any(event["type"] == "error" for event in session["events"]) and manager.budget_wrapups == set()


async def test_a_side_question_during_a_turn_counts_toward_neither_its_limit_nor_its_result(runtime, monkeypatch):
    manager, session = runtime
    session["turn_token_limit"] = 30_000  # The turn's own two requests use 22,000; the side question 25,100.
    manager.store.save(session)
    hold, agent = asyncio.Event(), []

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:  # The side question.
            return httpx.Response(200, json={"choices": [{"message": {"content": "Side answer."}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 25_000, "completion_tokens": 100}})
        agent.append(body)
        if len(agent) == 1:
            await hold.wait()
            return httpx.Response(200, text=calls_reply(("c1", "list_tasks", {})))
        return httpx.Response(200, text=text_reply("Done."))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    manager.start(session["id"], "Work")
    await eventually(lambda: agent)  # The side question is asked while the turn's first request runs.
    assert (await manager.side_question(session["id"], "What is this about?"))["answer"] == "Side answer."
    hold.set()
    await manager.tasks[session["id"]]
    saved = manager.store.get(session["id"])
    assert [call["purpose"] for call in saved["inference_calls"]] == ["agent", "side_question", "agent"]
    assert len(agent) == 2 and "Do not call any tools" not in system_text(agent[1])  # No wrap-up request.
    result = saved["last_turn_result"]
    assert (result["terminal_reason"], result["tokens"], result["usage"]["calls"]) == ("completed", 22_000, 2)
    assert result["estimated_dbu"] == pytest.approx(2 * PER_CALL)


async def test_a_change_during_the_turn_applies_from_the_next_request(runtime, monkeypatch):
    manager, session = runtime

    def replies(requests):
        if len(requests) == 2:
            session["turn_token_limit"] = 1  # As the route sets it while the turn runs.
        return calls_reply((f"c{len(requests)}", "list_tasks", {}))
    manager.settings.values["max_agent_steps"] = 6
    requests, _ = scripted(monkeypatch, replies)
    await manager.run(session, "Work")
    assert len(requests) == 3 and session["terminal_reason"] == "budget_limit"


async def test_a_limit_raised_during_the_wrap_up_request_lets_the_turn_go_on(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["max_agent_steps"] = 6
    session["turn_token_limit"] = 20_000

    def replies(requests):
        if len(requests) == 3:  # The wrap-up request: the limit is raised now, as the route does.
            session["turn_token_limit"] = 10_000_000
        return calls_reply((f"c{len(requests)}", "list_tasks", {}))
    requests, _ = scripted(monkeypatch, replies)
    await manager.run(session, "Work")
    # Only the wrap-up request's own tool call is refused; from the next request on the turn goes on as usual.
    assert ["Do not call any tools" in system_text(body) for body in requests] == [False, False, True, False, False, False]
    states = [event["state"] for event in session["events"] if event["type"] == "tool"]
    assert states == ["completed", "completed", "rejected", "completed", "completed", "completed"]
    assert session["terminal_reason"] == "step_limit" and manager.budget_wrapups == set()


async def test_a_wrap_up_reply_after_the_limit_was_removed_ends_the_turn_as_usual(runtime, monkeypatch):
    manager, session = runtime
    session["turn_token_limit"] = 10_000

    def replies(requests):
        if len(requests) == 2:  # The wrap-up request: the limit is removed now.
            session["turn_token_limit"] = 0
            return text_reply("Summary: one step done.")
        return calls_reply(("c1", "list_tasks", {}))
    requests, _ = scripted(monkeypatch, replies)
    await manager.run(session, "Work")
    assert len(requests) == 2 and session["terminal_reason"] == "completed"
    assert not any(event.get("terminal_reason") == "budget_limit" for event in session["events"])
    assert manager.budget_wrapups == set()


# Settings and the route

@pytest.fixture
async def api(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.env = {}
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            yield client, app.state.manager, settings


def settings_body(settings, **values):
    return {key: settings.values[key] for key in ("workspace", "model", "env_file")} | values


async def test_settings_keep_default_turn_limits(api):
    client, _, settings = api
    saved = await client.put("/api/settings", json=settings_body(settings, turn_token_limit=500_000,
                                                                 turn_time_limit_minutes=30))
    assert saved.status_code == 200 and (saved.json()["turn_token_limit"], saved.json()["turn_time_limit_minutes"]) == (
        500_000, 30)
    assert json.loads(settings.path.read_text())["turn_token_limit"] == 500_000
    for key, value in (("turn_token_limit", -1), ("turn_token_limit", 100_000_001), ("turn_token_limit", 1.5),
                       ("turn_time_limit_minutes", 1441), ("turn_time_limit_minutes", True)):
        refused = await client.put("/api/settings", json=settings_body(settings, **{key: value}))
        assert refused.status_code in (400, 422), (key, value)
    settings.values["turn_time_limit_minutes"] = "5"  # Hand-edited: the next save refuses it.
    refused = await client.put("/api/settings", json=settings_body(settings))
    assert refused.status_code == 400 and "Time limit per turn" in refused.json()["detail"]
    settings.path.write_text(json.dumps({**json.loads(settings.path.read_text()), "turn_token_limit": "many"}))
    assert Settings(settings.state_dir).values["turn_token_limit"] == 0  # Read as none.


async def test_the_route_sets_clears_and_reports_a_chats_turn_limits(api):
    client, manager, settings = api
    settings.values["turn_time_limit_minutes"] = 10
    session = (await client.post("/api/sessions", json={})).json()
    url = f"/api/sessions/{session['id']}/turn-limits"
    metrics = (await client.get(f"/api/sessions/{session['id']}/metrics")).json()
    assert metrics["turn_limits"] == {"turn_token_limit": None, "token_source": "settings", "turn_time_limit_minutes": 10,
                                      "time_source": "settings", "follows_parent": False}
    listener = listen(manager, manager.get(session["id"]))
    response = await client.put(url, json={"turn_token_limit": 250_000})
    assert response.status_code == 200 and response.json()["turn_token_limit"] == 250_000
    assert "turn_time_limit_minutes" not in response.json()  # Left out: still the Settings default.
    assert listener.messages[-1] == {"type": "turn_limits", "turn_token_limit": 250_000, "turn_time_limit_minutes": None}
    response = await client.put(url, json={"turn_time_limit_minutes": 0, "turn_token_limit": None})
    assert response.status_code == 200 and "turn_token_limit" not in response.json()
    assert manager.store.header(session["id"])["turn_time_limit_minutes"] == 0  # Its own: none, whatever Settings say.
    status = (await client.get(f"/api/sessions/{session['id']}/metrics")).json()["turn_limits"]
    assert (status["turn_time_limit_minutes"], status["time_source"]) == (None, "chat")
    for body in ({}, {"turn_token_limit": -1}, {"turn_token_limit": 2.5}, {"other": 1}):
        assert (await client.put(url, json=body)).status_code in (400, 422), body
    # A subagent follows its parent's limits.
    child = manager.store.create(settings.values)
    child.update(is_subagent=True, parent_session_id=session["id"])
    manager.store.save(child)
    refused = await client.put(f"/api/sessions/{child['id']}/turn-limits", json={"turn_token_limit": 5})
    assert refused.status_code == 400 and "parent conversation's turn limits" in refused.json()["detail"]
    child_status = (await client.get(f"/api/sessions/{child['id']}/metrics")).json()["turn_limits"]
    assert child_status["follows_parent"] is True and child_status["turn_time_limit_minutes"] is None


async def test_forks_exports_and_imports_keep_a_chats_own_turn_limits_and_steps(api):
    client, manager, settings = api
    settings.values.update(turn_token_limit=50_000, turn_time_limit_minutes=5)  # Its own 0 must stay none.
    source, done = manager.store.create(settings.values), {"model": OPUS, "status": "completed"}
    source.update(title="Capped", title_generated=True, terminal_reason="completed", spending_limit_dbu=2.5,
                  turn_token_limit=200_000, turn_time_limit_minutes=0, max_agent_steps=5,
                  events=[{"id": "u1", "type": "user", "text": "Hi", "in_wire": True},
                          {"id": "a1", "type": "assistant", "text": "Hello", "request_info": done},
                          {"id": "u2", "type": "user", "text": "Again", "in_wire": True},
                          {"id": "a2", "type": "assistant", "text": "Hello again", "request_info": done}],
                  wire=[{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"},
                        {"role": "user", "content": "Again"}, {"role": "assistant", "content": "Hello again"}])
    manager.store.save(source)
    own = ("spending_limit_dbu", "turn_token_limit", "turn_time_limit_minutes", "max_agent_steps")
    whole = await client.post(f"/api/sessions/{source['id']}/fork")
    here = await client.post(f"/api/sessions/{source['id']}/fork", json={"before_event_id": "u2"})
    bundle = (await client.get(f"/api/sessions/{source['id']}/export")).json()
    imported = await client.post("/api/sessions/import", params={"workspace": settings.values["workspace"]}, json=bundle)
    assert (whole.status_code, here.status_code, imported.status_code) == (201, 201, 201), imported.text
    for copy_id in (whole.json()["id"], here.json()["id"], imported.json()["session"]["id"]):
        copied = manager.store.get(copy_id)
        assert {key: copied.get(key) for key in own} == {key: source[key] for key in own}
        status = (await client.get(f"/api/sessions/{copy_id}/metrics")).json()["turn_limits"]
        assert (status["turn_token_limit"], status["token_source"], status["turn_time_limit_minutes"],
                status["time_source"]) == (200_000, "chat", None, "chat")
    assert len(manager.store.get(here.json()["id"])["wire"]) == 2
    for key, value in (("turn_token_limit", -1), ("turn_token_limit", MAX_TURN_TOKENS + 1),
                       ("turn_time_limit_minutes", True), ("max_agent_steps", 0), ("max_agent_steps", 65)):
        edited = json.loads(json.dumps(bundle))
        edited["sessions"][0][key] = value
        refused = await client.post("/api/sessions/import", params={"workspace": settings.values["workspace"]},
                                    json=edited)
        assert refused.status_code == 400 and key in refused.json()["detail"], (key, value)
