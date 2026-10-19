"""A turn's result record (turns.turn_result) and its token and time budget (turns.py): the record on the last
reply, in the header and in the idle status; the wrap-up request at the limit, its refused tool calls and the
budget_limit end; subagents counting toward their parent's turn; the Settings default and the chat's own."""
import asyncio
import json
import time

import httpx
import pytest

from local_agent import turns
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.composer_api import SIDE_QUESTION_PREFIX
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
    assert session["subagents"][0]["turn_spend"]["tokens"] == 22_000  # The index keeps it for later turns' checks.


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
    assert session["subagents"][0]["turn_spend"]["tokens"] is None  # The subagents index tells it apart from 0 too.
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


def test_an_index_entry_from_before_the_turn_spend_counts_its_whole_spend(runtime):
    manager, session = runtime
    # Its tokens and DBU as the index kept them then (/btw side questions included), so a turn still counts it.
    session["subagents"] = [{"id": "older", "created": time.time(), "tokens": 11_000, "estimated_dbu": PER_CALL,
                             "unpriced_calls": 1}]
    found = turns.children(manager, session, {"started": time.time() - 60})
    assert found["older"][:3] == (11_000, PER_CALL, 1)


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

# A /btw side question's usage: 25,100 tokens and 1.457146 DBU on Opus 5.5 (more than two agent responses).
SIDE_USAGE = {"prompt_tokens": 25_000, "completion_tokens": 100}
SIDE_DBU = 1.457146


def gated_gateway(monkeypatch, parent, child):
    """parent(n) and child(n), async: the SSE text of the chat's nth agent request and of its subagent's nth (or
    the subagent's HTTP response). A /btw side question is answered with SIDE_USAGE."""
    requests, children = [], []

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            if SIDE_QUESTION_PREFIX.split("\n")[0] in json.dumps(body):
                return httpx.Response(200, json={"choices": [{"message": {"content": "Side answer."},
                                                              "finish_reason": "stop"}], "usage": SIDE_USAGE})
            return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}]})
        if is_child(body):
            children.append(body)
            reply = await child(len(children))
            return reply if isinstance(reply, httpx.Response) else httpx.Response(200, text=reply)
        requests.append(body)
        return httpx.Response(200, text=await parent(len(requests)))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    return requests, children


def completed_calls(manager, child_id):
    child = manager.get(child_id) or {}
    return sum(call.get("status") == "completed" for call in child.get("inference_calls") or [])


async def background_turn(manager, session, monkeypatch, turn_two, gates, replies=None):
    """Turn 1 starts a background subagent that makes one request in it and one more while the chat is idle;
    its request n then waits for gates[n] and gets replies[n] when given. turn_two(n, child_id): the parent's nth
    request (from 3 on)."""
    state, idle = {}, asyncio.Event()
    gates = {2: idle, **gates}

    async def child(count):
        if count in gates:
            await gates[count].wait()
        if count in (replies or {}):
            return replies[count]
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


async def test_a_carried_subagent_whose_request_in_the_turn_reported_no_count_still_counts_in_it(runtime, monkeypatch):
    manager, session = runtime
    gates = {3: asyncio.Event()}

    async def turn_two(count, child_id):
        if count == 3:
            gates[3].set()  # The subagent's third request is made during turn 2.
            await eventually(lambda: len((manager.get(child_id) or {}).get("inference_calls") or []) == 3)
            return calls_reply(("p3", "list_tasks", {}))
        return text_reply("Done with turn two.")
    # Databricks answers it with an error and no usage, which ends it.
    failed = httpx.Response(500, json={"error_code": "INTERNAL_ERROR", "message": "Server problem."})
    _, child_id, _ = await background_turn(manager, session, monkeypatch, turn_two, gates, {3: failed})
    second = await started_turn(manager, session, "Something else")
    await eventually(lambda: manager.delegates.background_running(session["id"]) == 0)
    assert [call["status"] for call in manager.store.get(child_id)["inference_calls"]] == ["completed", "completed",
                                                                                          "error"]
    # It worked in turn 2, and no request it made then reported a count: counted, its tokens None (not 0).
    two = second["last_turn_result"]
    assert two["subagents"] == {"count": 1, "tokens": None, "estimated_dbu": 0.0}
    assert two["tokens"] == 22_000


async def test_a_side_question_in_a_background_subagents_chat_does_not_count_in_the_turn_it_ends_in(runtime, monkeypatch):
    manager, session = runtime
    gates = {3: asyncio.Event(), 4: asyncio.Event()}

    async def turn_two(count, child_id):
        if count == 3:
            gates[3].set()
            gates[4].set()  # It makes its last two requests and ends while the parent waits for it.
            return calls_reply(("g1", "get_subagent_result", {"subagent_id": child_id, "wait_seconds": 10}))
        return text_reply("It is done.")
    _, child_id, requests = await background_turn(manager, session, monkeypatch, turn_two, gates)
    # While the chat is idle, the user asks the running subagent a side question in its chat (25,100 tokens).
    assert (await manager.side_question(child_id, "Which file are you on?"))["answer"] == "Side answer."
    # Turn 2's limit: its two requests (22,000) and the subagent's two in it (22,000) stay under it.
    manager.get(session["id"])["turn_token_limit"] = 50_000
    second = await started_turn(manager, session, "Wait for it")
    assert manager.delegates.background_running(session["id"]) == 0
    # Read from the subagents index once it ended, the subagent's requests in turn 2 count in its result and its
    # limit, the side question asked before it does not.
    two = second["last_turn_result"]
    assert two["subagents"] == {"count": 1, "tokens": 22_000, "estimated_dbu": pytest.approx(2 * PER_CALL)}
    assert (two["terminal_reason"], two["tokens"]) == ("completed", 44_000)
    assert len(requests) == 4 and "per turn" not in system_text(requests[3])
    # Its spend for the spending limit keeps the side question: it is billed.
    assert second["subagents"][0]["estimated_dbu"] == pytest.approx(4 * PER_CALL + SIDE_DBU)


async def test_a_subagent_chat_the_user_continues_while_its_parents_turn_runs_keeps_its_own_budget(runtime, monkeypatch):
    manager, session = runtime
    hold = asyncio.Event()

    async def parent(count):
        if count == 1:
            return calls_reply(("d1", "delegate_task", {"task": "Map it", "agent_type": "explore", "max_steps": 8}))
        if count == 3:  # Turn 2's first request: it runs while the user continues the subagent chat.
            await hold.wait()
        return text_reply("Done.")

    async def child(count):
        # Its delegated turn reports at once; the turn the user continues keeps calling tools.
        return text_reply("## Report\nMapped.") if count == 1 else calls_reply((f"x{count}", "list_tasks", {}))
    requests, children = gated_gateway(monkeypatch, parent, child)
    manager.store.save(session)
    first = await started_turn(manager, session, "Delegate")
    child_id = first["subagents"][0]["id"]
    first["turn_token_limit"] = 30_000  # The subagent chat follows its parent's limits.
    manager.store.save(first)
    manager.start(session["id"], "Something else")
    await eventually(lambda: len(requests) == 3)  # The parent's turn 2 runs.
    manager.start(child_id, "Go on mapping the rest")  # The user continues the subagent chat on its own.
    await asyncio.wait_for(manager.tasks[child_id], 10)
    hold.set()
    await asyncio.wait_for(manager.tasks[session["id"]], 10)
    # Its own turn: three requests (33,000 tokens) reach the 30,000-token limit, so the fourth is its wrap-up.
    continued = children[1:]
    child_session = manager.store.get(child_id)
    assert len(continued) == 4 and "per turn" in system_text(continued[3]), child_session["terminal_reason"]
    assert child_session["terminal_reason"] == "budget_limit"
    assert notices(child_session)[-1]["text"].startswith("This turn has used about 44,000 of its 30,000-token limit")
    # Nor is it part of the parent's turn 2.
    two = manager.store.get(session["id"])["last_turn_result"]
    assert two["subagents"]["count"] == 0 and two["tokens"] == 11_000


async def test_a_subagent_chat_continued_during_the_parent_turn_that_delegated_it_counts_there_only_its_delegated_run(
        runtime, monkeypatch):
    manager, session = runtime
    # The parent's 33,000 and the subagent's delegated 11,000 stay under 50,000; with the 22,000 of the turn the
    # user continued the subagent chat in, the parent's third request would be its wrap-up.
    session["turn_token_limit"] = 50_000
    hold, seen = asyncio.Event(), {}

    async def parent(count):
        if count == 1:
            return calls_reply(("d1", "delegate_task", {"task": "Map it", "agent_type": "explore", "max_steps": 8}))
        if count == 2:  # The subagent reported; while this request runs the user continues its chat.
            await hold.wait()
            return calls_reply(("p2", "list_tasks", {}))
        return text_reply("Done.")

    async def child(count):
        if count == 3:  # The continued turn's second request: its first is in the live subagent's ledger.
            seen["used"] = turns.used(manager, manager.get(session["id"]))[2]
        if count == 1:
            return text_reply("## Report\nMapped.")
        return calls_reply(("x2", "list_tasks", {})) if count == 2 else text_reply("More mapped.")
    requests, _ = gated_gateway(monkeypatch, parent, child)
    manager.store.save(session)
    manager.start(session["id"], "Delegate")
    await eventually(lambda: len(requests) == 2)
    child_id = manager.get(session["id"])["subagents"][0]["id"]
    manager.start(child_id, "Go on mapping the rest")  # The user continues the subagent chat on its own.
    await asyncio.wait_for(manager.tasks[child_id], 10)
    assert manager.store.get(child_id)["last_turn_result"]["tokens"] == 22_000  # Its own turn.
    hold.set()
    await asyncio.wait_for(manager.tasks[session["id"]], 10)
    saved = manager.store.get(session["id"])
    result = saved["last_turn_result"]
    # The parent's turn: its three requests and the subagent's delegated one, not the turn the user had with it,
    # neither while that ran nor after.
    assert seen["used"] == 22_000
    assert len(requests) == 3 and "per turn" not in system_text(requests[2])
    assert result["terminal_reason"] == "completed"
    assert (result["subagents"]["tokens"], result["tokens"]) == (11_000, 44_000)
    # The index keeps the delegated run for its parent's turns; its spend for the spending limit has every request.
    entry = saved["subagents"][0]
    assert entry["turn_spend"]["tokens"] == 11_000 and entry["estimated_dbu"] == pytest.approx(3 * PER_CALL)


async def test_a_finished_subagent_the_user_continues_does_not_stop_its_running_sibling_at_the_parents_budget(
        runtime, monkeypatch):
    manager, session = runtime
    # The parent's 22,000, Quick's 11,000 and Slow's 22,000 stay under 60,000; the user's own turn with Quick
    # (33,000) is not the parent's.
    session["turn_token_limit"] = 60_000
    hold = asyncio.Event()
    slow, continued = [], []

    async def gateway(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}]})
        text = json.dumps(unmarked(body)["messages"])
        if not is_child(body):
            if "d1" not in text:
                return httpx.Response(200, text=calls_reply(delegation("d1", "Quick"), delegation("d2", "Slow")))
            return httpx.Response(200, text=text_reply("Done."))
        if "Go on" in text:  # The user's own turn in Quick's chat: two tool rounds, then a reply.
            continued.append(body)
            return httpx.Response(200, text=calls_reply((f"q{len(continued)}", "list_tasks", {}))
                                  if len(continued) < 3 else text_reply("More."))
        if "Delegated task:\\nQuick" in text:
            return httpx.Response(200, text=text_reply("## Report\nQuick done."))
        slow.append(body)
        if len(slow) == 1:
            await hold.wait()  # Slow's first request runs while the user continues Quick.
            return httpx.Response(200, text=calls_reply(("s1", "list_tasks", {})))
        return httpx.Response(200, text=text_reply("## Report\nSlow done."))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    manager.store.save(session)
    manager.start(session["id"], "Delegate both")
    await eventually(lambda: len(manager.get(session["id"]).get("subagents") or []) == 2 and slow)
    quick = next(entry["id"] for entry in manager.get(session["id"])["subagents"] if entry["task"] == "Quick")
    await eventually(lambda: not any(entry.get("child_session_id") == quick
                                     for entry in manager.delegates.active.get(session["id"], ())))
    manager.start(quick, "Go on")  # The user continues the finished subagent's chat on its own.
    await asyncio.wait_for(manager.tasks[quick], 10)
    assert manager.store.get(quick)["last_turn_result"]["tokens"] == 33_000
    hold.set()
    await asyncio.wait_for(manager.tasks[session["id"]], 10)
    saved = manager.store.get(session["id"])
    slow_id = next(entry["id"] for entry in saved["subagents"] if entry["task"] == "Slow")
    # Slow's second request is an ordinary one: the parent's turn is at 33,000 then (its request, Quick's and Slow's
    # first), not 66,000.
    assert len(slow) == 2 and "per turn" not in system_text(slow[1])
    assert manager.store.get(slow_id)["terminal_reason"] == "completed"
    result = saved["last_turn_result"]
    assert (result["terminal_reason"], result["subagents"]["tokens"], result["tokens"]) == ("completed", 33_000, 55_000)


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


async def test_a_side_question_in_a_subagents_chat_counts_in_its_parents_turn_neither_while_it_runs_nor_after(
        runtime, monkeypatch):
    manager, session = runtime
    # Parent 22,000 + subagent 22,000 stay under 50,000; with the side question's 25,100 the parent's second
    # request would be its wrap-up.
    session["turn_token_limit"] = 50_000
    hold = asyncio.Event()

    async def parent(count):
        return calls_reply(delegation()) if count == 1 else text_reply("Done.")

    async def child(count):
        if count == 1:
            await hold.wait()  # The user asks the subagent a side question while its first request runs.
            return calls_reply(("x1", "list_tasks", {}))
        return text_reply("## Report\nMapped.")
    requests, children = gated_gateway(monkeypatch, parent, child)
    manager.store.save(session)
    manager.start(session["id"], "Delegate")
    await eventually(lambda: children)
    child_id = manager.delegates.active[session["id"]][0]["child_session_id"]
    assert (await manager.side_question(child_id, "What are you doing?"))["answer"] == "Side answer."
    assert turns.used(manager, manager.get(session["id"]))[2] == 11_000  # Read live while it runs.
    hold.set()
    await manager.tasks[session["id"]]
    assert "side_question" in [call["purpose"] for call in manager.store.get(child_id)["inference_calls"]]
    # Read from the subagents index once it ended: its two agent requests, not the side question.
    assert len(requests) == 2 and "per turn" not in system_text(requests[1])
    result = manager.store.get(session["id"])["last_turn_result"]
    assert result["terminal_reason"] == "completed"
    assert result["subagents"] == {"count": 1, "tokens": 22_000, "estimated_dbu": pytest.approx(2 * PER_CALL)}
    assert result["tokens"] == 44_000 and result["estimated_dbu"] == pytest.approx(4 * PER_CALL)


async def test_the_step_limit_notice_names_the_turns_dbu_without_a_side_question_asked_during_it(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["max_agent_steps"] = 2
    hold = asyncio.Event()

    async def parent(count):
        if count == 1:
            await hold.wait()  # The side question is asked while the turn's first request runs.
        return calls_reply((f"c{count}", "list_tasks", {}))
    requests, _ = gated_gateway(monkeypatch, parent, None)
    manager.store.save(session)
    manager.start(session["id"], "Work")
    await eventually(lambda: requests)
    assert (await manager.side_question(session["id"], "What is this about?"))["answer"] == "Side answer."
    hold.set()
    await manager.tasks[session["id"]]
    saved = manager.store.get(session["id"])
    result = saved["last_turn_result"]
    assert result["terminal_reason"] == "step_limit" and result["estimated_dbu"] == pytest.approx(2 * PER_CALL)
    # The notice names the turn's cost as its record (the footer under the same reply) does: 1.714 DBU, not 3.2.
    assert notices(saved)[-1]["text"].endswith(" This turn used about 1.7 DBU (estimate).")


async def test_a_subagents_step_limit_error_names_its_turns_dbu_without_a_side_question_in_its_chat(
        runtime, monkeypatch):
    manager, session = runtime
    hold = asyncio.Event()

    async def parent(count):
        return calls_reply(("d1", "delegate_task", {"task": "Map it", "agent_type": "explore", "max_steps": 2})) \
            if count == 1 else text_reply("Done.")

    async def child(count):
        if count == 1:
            await hold.wait()  # The user asks the subagent a side question while its first request runs.
        return calls_reply((f"x{count}", "list_tasks", {}))
    _, children = gated_gateway(monkeypatch, parent, child)
    manager.store.save(session)
    manager.start(session["id"], "Delegate")
    await eventually(lambda: children)
    child_id = manager.delegates.active[session["id"]][0]["child_session_id"]
    assert (await manager.side_question(child_id, "What are you doing?"))["answer"] == "Side answer."
    hold.set()
    await manager.tasks[session["id"]]
    child_session = manager.store.get(child_id)
    assert child_session["terminal_reason"] == "step_limit"
    assert notices(child_session)[-1]["text"].endswith(" This turn used about 1.7 DBU (estimate).")


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
