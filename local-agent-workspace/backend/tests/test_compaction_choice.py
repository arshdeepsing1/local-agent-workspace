"""The choice before an automatic compaction (Story 4, FR-028, decision 4b).

context_info.warn says "none", "near" (85% of the compaction threshold) or "at_threshold" (the next turn would
compact). With Settings > compaction_ask on and a person at a page, a new user message at the threshold waits for one
card: Compact now (the default) or Continue without compacting. A compaction in the middle of a turn, a scheduled run,
a script, a subagent chat, a wake turn and a queued message never wait. The fake endpoint holds its replies on events,
so no test waits by sleeping.
"""
import asyncio
import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent import interactions
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.context import context_reading, estimate_tokens
from local_agent.prompt_cache import unmarked
from local_agent.store import Store
from test_context_trim import WINDOW, History, long_history, page

REAL_CLIENT = httpx.AsyncClient  # Taken before the fake endpoint replaces it.
WARM_TURNS = 5  # About 60% of the input budget with the app's own system message.
CARD = "compaction_choice"
SUMMARY = "Summary of the earlier work."
STEP_TIMEOUT = 20  # Seconds a test waits for its own turn before it fails instead of hanging.


# --- A fake endpoint whose replies a test can hold -------------------------------------------------------------------

class Endpoint:
    """Answers agent requests from a script (one entry each: text, tool call, hold, entered) and everything else (the
    summary and title requests, not streamed) with a fixed summary. Records what it received."""

    def __init__(self):
        self.agent, self.raw, self.script, self.summaries = [], [], [], 0

    async def handle(self, request):
        body = json.loads(request.content)
        self.raw.append(request.content.decode())
        if body.get("stream") is not True:
            self.summaries += 1
            return httpx.Response(200, json={"choices": [{"message": {"content": SUMMARY}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        sent = unmarked(body)
        self.agent.append(sent["messages"])
        step = self.script.pop(0) if self.script else {}
        if step.get("entered") is not None:
            step["entered"].set()
        if step.get("hold") is not None:
            await step["hold"].wait()
        if step.get("call"):
            name, arguments = step["call"]
            delta = {"tool_calls": [{"index": 0, "id": f"call_{len(self.agent)}", "type": "function",
                                     "function": {"name": name, "arguments": json.dumps(arguments)}}]}
            finish = "tool_calls"
        else:
            delta, finish = {"content": step.get("text", "Done.")}, "stop"
        usage = {"prompt_tokens": estimate_tokens(sent["messages"], sent["tools"]), "completion_tokens": 5}
        chunk = {"choices": [{"delta": delta, "finish_reason": finish}], "usage": usage}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


class FakePage:
    """A page's socket: what the presence and chat websockets of api.py add to AgentManager.pages and .listeners."""

    async def send_json(self, data):
        pass


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """(manager, session, endpoint, project): a chat of WARM_TURNS turns that has not run yet, a person at a page,
    the trim off (it is tested in test_context_trim.py) and the threshold out of reach until a test sets it."""
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", compaction_handoffs=False, context_window=WINDOW,
                           max_output_tokens=4_096, context_trim=False, auto_compact_percent=100, compaction_ask=True)
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    page = FakePage()
    manager.pages.add(page)  # A page holds its presence socket open: a person is there,
    manager.listeners[session["id"]] = {page}  # and has this chat open (its chat socket is a listener of the chat).
    session.update(title="Choice", title_generated=True, permission_mode="bypassPermissions")
    history = long_history(WARM_TURNS)
    session["wire"], session["events"] = copy.deepcopy(history.wire), copy.deepcopy(history.events)
    store.save(session)
    endpoint = Endpoint()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(
        **kwargs, transport=httpx.MockTransport(endpoint.handle)))
    yield manager, session, endpoint, project
    store.db.close()


# --- Helpers ---------------------------------------------------------------------------------------------------------

async def bounded(awaitable):
    return await asyncio.wait_for(awaitable, STEP_TIMEOUT)


def saved(manager, session):
    return manager.store.get(session["id"])


def compactions(manager, session):
    return (saved(manager, session).get("context_state") or {}).get("compactions", 0)


def cards(manager, session):
    return [event for event in saved(manager, session)["events"] if CARD in event]


async def turn(manager, session, text="Next step please.", **options):
    """Start a turn and wait for it to end."""
    manager.start(session["id"], text, **options)
    await bounded(manager.tasks[session["id"]])


def watch(manager):
    """The card messages the page gets (broadcast), and an event set when the first one is sent."""
    seen = {"messages": [], "arrived": asyncio.Event()}
    original = manager.broadcast

    async def broadcast(session_id, data):
        await original(session_id, data)
        if isinstance(data, dict) and data.get("type") == CARD:  # After a page's socket took its turn to send.
            seen["messages"].append(data)
            seen["arrived"].set()
    manager.broadcast = broadcast
    return seen


async def warm(manager, session):
    """One turn under the threshold, so the chat has the estimate of its next request (next_estimate)."""
    await turn(manager, session, "Warm-up turn.")
    info = saved(manager, session)["context_info"]
    return info["next_estimate"]["tokens"], info["input_budget"]


async def at_percent(manager, session, percent):
    manager.settings.values["auto_compact_percent"] = percent
    live = saved(manager, session)
    await manager.refresh_meter_limits(live)
    return saved(manager, session)["context_info"]


def percent_for(tokens, budget, ratio):
    """The whole auto-compact percentage whose threshold is about ratio times the tokens."""
    return -(-int(100 * tokens * ratio) // budget)


async def over(manager, session):
    """The percentage that puts the chat's next request over the threshold, set; the info it gives."""
    tokens, budget = await warm(manager, session)
    return await at_percent(manager, session, max(50, 100 * tokens // budget - 2))


def message_of(tokens):
    """A user message that adds about this many estimated tokens."""
    text = "n" * 4 * tokens
    while estimate_tokens([{"role": "user", "content": text}], []) > tokens:
        text = text[:-200]
    return text


# --- warn ------------------------------------------------------------------------------------------------------------

def reading(**info):
    return context_reading({"context_window": 65_536, "estimated_tokens": 0, "auto_compact_threshold": 40_000, **info})


def test_warn_is_in_every_reading_and_none_for_a_small_chat():
    assert reading(estimated_tokens=1_000)["warn"] == "none"
    assert context_reading({}) == {}  # No reading without a window: no warn either.


@pytest.mark.parametrize("tokens, warn", [
    (33_999, "none"), (34_000, "near"), (39_999, "near"), (40_000, "near"), (40_001, "at_threshold"),
    (60_000, "at_threshold")])
def test_warn_changes_at_85_percent_of_the_threshold_and_at_the_threshold(tokens, warn):
    assert reading(estimated_tokens=tokens)["warn"] == warn
    # The next request's estimate is what a chat at rest is judged by.
    assert reading(estimated_tokens=1, next_estimate={"tokens": tokens})["warn"] == warn


def test_warn_follows_the_estimate_after_a_trim_pass():
    info = {"estimated_tokens": 1, "next_estimate": {"tokens": 45_000, "after_trim_tokens": 30_000}}
    assert reading(**info)["warn"] == "none"
    info["next_estimate"]["after_trim_tokens"] = 37_000
    assert reading(**info)["warn"] == "near"


def test_a_deferred_or_held_compaction_never_shows_at_threshold():
    """Summarizing cannot bring a deferred chat under the threshold, and a held one chose to go on: neither is
    'about to compact'."""
    for flag in ("compaction_deferred", "compaction_held"):
        assert reading(estimated_tokens=50_000, **{flag: True})["warn"] == "near"
        assert reading(estimated_tokens=20_000, **{flag: True})["warn"] == "none"


async def test_warn_has_the_three_values_at_the_right_sizes_in_a_live_chat(runtime):
    manager, session, _, _ = runtime
    tokens, budget = await warm(manager, session)
    assert 100 * tokens / budget < 76, "The test chat must leave room under the threshold."
    seen = {}
    for percent in range(50, 101):
        info = await at_percent(manager, session, percent)
        threshold = info["auto_compact_threshold"]
        expected = ("at_threshold" if tokens > threshold else "near" if tokens * 100 >= 85 * threshold else "none")
        assert info["warn"] == expected, (percent, tokens, threshold)
        seen.setdefault(expected, percent)
    assert set(seen) == {"none", "near", "at_threshold"}
    # The reading is saved with the chat's info, so the page that opens it later gets the same value.
    assert saved(manager, session)["context_info"]["warn"] == "none"


@pytest.mark.parametrize("ratio, warn, compacts", [(1.3, "none", False), (1.06, "near", False)], ids=["none", "near"])
async def test_the_next_turn_does_what_warn_said_below_the_threshold(runtime, ratio, warn, compacts):
    manager, session, endpoint, _ = runtime
    tokens, budget = await warm(manager, session)
    info = await at_percent(manager, session, percent_for(tokens, budget, ratio))
    assert info["warn"] == warn
    await turn(manager, session)
    assert compactions(manager, session) == 0 and not cards(manager, session)


async def test_the_next_turn_does_what_at_threshold_said(runtime):
    manager, session, endpoint, _ = runtime
    manager.settings.values["compaction_ask"] = False  # Ask nothing: this test is about what the next turn does.
    info = await over(manager, session)
    assert info["warn"] == "at_threshold"
    await turn(manager, session)
    assert compactions(manager, session) == 1 and not cards(manager, session)
    after = saved(manager, session)["context_info"]
    assert after["warn"] in ("none", "near") and after["percent_used"] < info["percent_used"]


async def test_a_chat_whose_compaction_is_deferred_reads_near_not_at_threshold(runtime):
    """One huge first turn with nothing earlier to summarize: the request goes as it is, and the chat is not shown as
    about to compact for ever."""
    manager, session, endpoint, project = runtime
    manager.settings.values["compaction_ask"] = False
    history = History()
    history.user("Read everything.")
    for number in range(12):
        history.tool("read_file", {"path": f"src/part_{number}.py"}, page(f"src/part_{number}.py", 60))
    history.reply("Done.")
    session["wire"], session["events"] = copy.deepcopy(history.wire), copy.deepcopy(history.events)
    manager.store.save(session)
    tokens, budget = await warm(manager, session)
    await at_percent(manager, session, max(50, 100 * tokens // budget - 2))
    await turn(manager, session)
    after = saved(manager, session)["context_info"]
    if compactions(manager, session) == 0 and after.get("compaction_deferred"):
        assert after["warn"] in ("near", "none") and not cards(manager, session)
    else:  # The chat could be summarized after all: then it compacted and reads under the threshold.
        assert after["warn"] != "at_threshold"


# --- The card --------------------------------------------------------------------------------------------------------

async def waiting(manager, session):
    """Until the chat shows that it waits for the card's answer (a page's socket takes a turn to send the card)."""
    while manager.statuses.get(session["id"]) != "awaiting_approval":
        await asyncio.sleep(0)


async def start_waiting(manager, session, text="Please go on with the next part."):
    """Start a turn in a chat at the threshold and wait until its card is shown."""
    seen = watch(manager)
    manager.start(session["id"], text)
    await bounded(seen["arrived"].wait())
    await waiting(manager, session)
    return seen


async def test_a_message_at_the_threshold_shows_one_card_and_nothing_else_happens(runtime):
    manager, session, endpoint, _ = runtime
    info = await over(manager, session)
    before = len(endpoint.agent)
    summaries = endpoint.summaries
    seen = await start_waiting(manager, session)
    shown = cards(manager, session)
    assert len(shown) == 1 and len(seen["messages"]) == 1
    card = shown[0]
    assert card["type"] == "notice" and card["state"] == "pending"
    assert card[CARD]["options"] == ["compact_now", "continue"] and card[CARD]["answer"] is None
    assert card[CARD]["compact_at_percent"] == info["compact_at_percent"]
    assert card[CARD]["percent_used"] >= card[CARD]["compact_at_percent"]
    assert seen["messages"][0] == {"type": CARD, "event_id": card["id"], "percent_used": card[CARD]["percent_used"],
                                   "compact_at_percent": card[CARD]["compact_at_percent"],
                                   "options": ["compact_now", "continue"]}
    assert manager.statuses[session["id"]] == "awaiting_approval"
    # No model request and no compaction until the answer; the message is already in the chat.
    assert len(endpoint.agent) == before and endpoint.summaries == summaries
    assert compactions(manager, session) == 0
    user = [event for event in saved(manager, session)["events"] if event["type"] == "user"][-1]
    assert user["text"] == "Please go on with the next part."
    manager.answer_compaction_choice(session["id"], "compact_now")
    await bounded(manager.tasks[session["id"]])


async def test_compact_now_compacts_then_runs_the_turn_with_the_same_message(runtime):
    manager, session, endpoint, project = runtime
    (project / "notes.txt").write_text("The notes.\n")
    await over(manager, session)
    text = "Please read @notes.txt and go on."
    await start_waiting(manager, session, text)
    waiting = copy.deepcopy([event for event in saved(manager, session)["events"] if event["type"] == "user"][-1])
    manager.answer_compaction_choice(session["id"], "compact_now")
    await bounded(manager.tasks[session["id"]])
    assert compactions(manager, session) == 1 and endpoint.summaries >= 1
    final = saved(manager, session)
    user = [event for event in final["events"] if event["type"] == "user"][-1]
    for key in ("text", "attachments", "verbatim", "skills", "in_wire"):
        assert user.get(key) == waiting.get(key), key
    assert user["text"] == text and user.get("attachments")
    request = endpoint.agent[-1]
    assert "notes.txt" in json.dumps(request[-1]) and "The notes." in json.dumps(request[-1])
    [card] = cards(manager, session)
    assert card["state"] == "completed" and card[CARD]["answer"] == "compact_now"
    assert manager.statuses[session["id"]] == "idle"
    assert final["terminal_reason"] == "completed"


async def test_a_plain_text_message_stays_plain_text_after_the_choice(runtime):
    manager, session, endpoint, _ = runtime
    await over(manager, session)
    seen = watch(manager)
    manager.start(session["id"], "/not-a-skill literal text", verbatim=True)
    await bounded(seen["arrived"].wait())
    manager.answer_compaction_choice(session["id"], "compact_now")
    await bounded(manager.tasks[session["id"]])
    user = [event for event in saved(manager, session)["events"] if event["type"] == "user"][-1]
    assert user["verbatim"] is True and user["text"] == "/not-a-skill literal text" and not user.get("skills")
    assert endpoint.agent[-1][-1]["content"].startswith("/not-a-skill literal text")


async def continue_chat(manager, session, endpoint):
    """A chat at the threshold, its message answered with Continue; the turn has ended."""
    await over(manager, session)
    seen = await start_waiting(manager, session)
    manager.answer_compaction_choice(session["id"], "continue")
    await bounded(manager.tasks[session["id"]])
    return seen


async def test_continue_runs_the_turn_without_compacting_and_later_turns_do_not_ask(runtime):
    manager, session, endpoint, _ = runtime
    await continue_chat(manager, session, endpoint)
    assert compactions(manager, session) == 0 and endpoint.summaries == 0
    assert endpoint.agent[-1][-1]["content"].startswith("Please go on with the next part.")
    [card] = cards(manager, session)
    assert card["state"] == "completed" and card[CARD]["answer"] == "continue"
    info = saved(manager, session)["context_info"]
    assert info["warn"] == "near"  # It chose to go on: not "about to compact".
    assert saved(manager, session)["compaction_choice"]["choice"] == "continue"
    for number in range(2):
        await turn(manager, session, f"Another step {number}.")
    assert len(cards(manager, session)) == 1 and compactions(manager, session) == 0


async def test_when_the_request_no_longer_fits_the_app_compacts_and_names_the_deferral(runtime):
    manager, session, endpoint, _ = runtime
    await continue_chat(manager, session, endpoint)
    info = saved(manager, session)["context_info"]
    room = info["input_budget"] - info["next_estimate"]["tokens"]
    await turn(manager, session, message_of(room + 3_000))
    assert compactions(manager, session) == 1 and len(cards(manager, session)) == 1
    notices = [event["text"] for event in saved(manager, session)["events"]
               if event["type"] == "notice" and event.get("compaction")]
    assert len(notices) == 1 and notices[0].startswith("Context compacted automatically")
    assert "You chose to continue without compacting. That deferral ended here because the request no longer fits." in notices[0]
    assert "compaction_choice" not in saved(manager, session)  # The crossing is over.


async def over_again(manager, session):
    """The message that takes a chat after a compaction over its threshold (as low as 50 percent allows)."""
    info = saved(manager, session)["context_info"]
    tokens, budget = (info.get("next_estimate") or {}).get("tokens", info["estimated_tokens"]), info["input_budget"]
    manager.settings.values["auto_compact_percent"] = max(50, 100 * tokens // budget - 1)
    threshold = budget * manager.settings.values["auto_compact_percent"] // 100
    return message_of(max(500, threshold - tokens + 1_500))


async def test_after_that_compaction_the_next_crossing_asks_again(runtime):
    manager, session, endpoint, _ = runtime
    await continue_chat(manager, session, endpoint)
    info = saved(manager, session)["context_info"]
    await turn(manager, session, message_of(info["input_budget"] - info["next_estimate"]["tokens"] + 3_000))
    assert compactions(manager, session) == 1 and len(cards(manager, session)) == 1
    await start_waiting(manager, session, await over_again(manager, session))
    assert len(cards(manager, session)) == 2 and cards(manager, session)[-1]["state"] == "pending"
    manager.answer_compaction_choice(session["id"], "compact_now")
    await bounded(manager.tasks[session["id"]])
    assert compactions(manager, session) == 2


async def test_a_manual_compact_resets_the_choice(runtime):
    manager, session, endpoint, _ = runtime
    await continue_chat(manager, session, endpoint)
    assert "compaction_choice" in saved(manager, session)
    manager.start_compaction(session["id"])
    await bounded(manager.tasks[session["id"]])
    live = saved(manager, session)
    assert live["context_state"]["compactions"] == 1 and "compaction_choice" not in live
    assert "You chose to continue" not in json.dumps(live["events"])  # A manual one is not the deferral's end.
    manager.settings.values["auto_compact_percent"] = 100
    for _ in range(2):  # Earlier turns again, so that a compaction has something to summarize.
        await turn(manager, session, message_of(8_000))
    await start_waiting(manager, session, await over_again(manager, session))
    assert len(cards(manager, session)) == 2
    manager.answer_compaction_choice(session["id"], "continue")
    await bounded(manager.tasks[session["id"]])


# --- Stop, timeout, restart and reload -------------------------------------------------------------------------------

async def test_stop_while_the_card_waits_ends_the_turn_like_a_stop_before_the_first_request(runtime):
    manager, session, endpoint, _ = runtime
    await over(manager, session)
    requests = len(endpoint.agent)
    await start_waiting(manager, session, "Do the next part.")
    await bounded(manager.stop(session["id"]))
    assert manager.statuses.get(session["id"], "idle") == "idle" and not manager.busy_task(session["id"])
    final = saved(manager, session)
    assert compactions(manager, session) == 0 and len(endpoint.agent) == requests and endpoint.summaries == 0
    # The message stays in the chat as sent, closed with the stop marker, as after any Stop before a reply.
    user = [event for event in final["events"] if event["type"] == "user"][-1]
    assert user["text"] == "Do the next part." and user["in_wire"] is True
    assert final["wire"][-2]["content"] == "Do the next part." and final["wire"][-1]["role"] == "assistant"
    [card] = cards(manager, session)
    assert card["state"] == "cancelled" and card[CARD]["answer"] is None
    assert final["terminal_reason"] == "stopped" and final["events"][-1]["type"] == "notice"
    assert "compaction_choice" not in final and "ended" not in manager.interactions.waiting
    assert not manager.interactions.waiting
    with pytest.raises(ValueError, match="No compaction choice is waiting"):
        manager.answer_compaction_choice(session["id"], "compact_now")
    # The next message asks again: nothing was chosen.
    await start_waiting(manager, session, "Try again.")
    assert len(cards(manager, session)) == 2
    manager.answer_compaction_choice(session["id"], "compact_now")
    await bounded(manager.tasks[session["id"]])


async def test_an_unanswered_card_applies_compact_now_after_the_approval_wait(runtime, monkeypatch):
    manager, session, endpoint, _ = runtime
    manager.settings.values["approval_timeout_minutes"] = 7
    await over(manager, session)
    original, timeouts = asyncio.wait_for, []

    async def wait_for(awaitable, timeout=None):
        timeouts.append(timeout)
        if timeout == 7 * interactions.MINUTE:
            awaitable.cancel() if hasattr(awaitable, "cancel") else None
            raise TimeoutError
        return await original(awaitable, timeout)
    monkeypatch.setattr("local_agent.interactions.asyncio.wait_for", wait_for)
    await turn(manager, session, "Please go on, nobody answers.")
    assert 7 * interactions.MINUTE in timeouts
    assert compactions(manager, session) == 1
    [card] = cards(manager, session)
    assert card["state"] == "completed" and card[CARD]["answer"] == "expired"
    user = [event for event in saved(manager, session)["events"] if event["type"] == "user"][-1]
    assert user["text"] == "Please go on, nobody answers."
    assert endpoint.agent[-1][-1]["content"].startswith("Please go on, nobody answers.")
    assert saved(manager, session)["terminal_reason"] == "completed"


@pytest.mark.parametrize("minutes", [0, 30])
async def test_a_card_has_an_expiry_time_when_the_wait_has_a_limit_and_none_without(runtime, minutes):
    manager, session, _, _ = runtime
    manager.settings.values["approval_timeout_minutes"] = minutes
    await over(manager, session)
    await start_waiting(manager, session)
    [card] = cards(manager, session)
    assert ("approval_expires" in card) is bool(minutes)
    manager.answer_compaction_choice(session["id"], "continue")
    await bounded(manager.tasks[session["id"]])


async def test_a_chat_saved_while_the_card_waited_reloads_as_an_interrupted_turn(runtime, monkeypatch):
    manager, session, endpoint, _ = runtime
    await over(manager, session)
    await start_waiting(manager, session, "A message the restart must not lose.")
    assert saved(manager, session).get("turn_running") is True
    restarted = AgentManager(Store(manager.settings.state_dir / "tests.sqlite3"), manager.settings)  # What a restart does with the saved chat.
    try:
        reloaded = restarted.get(session["id"])
        user = [event for event in reloaded["events"] if event["type"] == "user"][-1]
        assert user["text"] == "A message the restart must not lose." and user["in_wire"] is True
        assert reloaded["terminal_reason"] == "interrupted" and "turn_running" not in reloaded
        [card] = [event for event in reloaded["events"] if CARD in event]
        assert card["state"] == "cancelled" and card[CARD]["answer"] is None
        assert reloaded["wire"][-1]["role"] == "assistant"  # The message is closed in the model history once.
        assert reloaded["wire"][-2]["content"] == "A message the restart must not lose."
        assert compactions(restarted, session) == 0 and not restarted.interactions.waiting
        with pytest.raises(ValueError, match="No compaction choice is waiting"):
            restarted.answer_compaction_choice(session["id"], "compact_now")
    finally:
        restarted.store.db.close()
        await manager.stop(session["id"])


async def test_the_choice_survives_a_store_reload_and_is_honoured_by_the_next_turn(runtime):
    manager, session, endpoint, _ = runtime
    await continue_chat(manager, session, endpoint)
    reopened = AgentManager(Store(manager.settings.state_dir / "tests.sqlite3"), manager.settings)
    reopened.pages.add(object())
    try:
        assert reopened.get(session["id"])["compaction_choice"]["choice"] == "continue"
        before = len(endpoint.agent)
        await turn(reopened, session, "After the restart.")
        assert len(endpoint.agent) == before + 1 and compactions(reopened, session) == 0
        assert len([event for event in reopened.get(session["id"])["events"] if CARD in event]) == 1
    finally:
        reopened.store.db.close()


# --- Errors ----------------------------------------------------------------------------------------------------------

async def test_a_choice_that_is_not_one_of_the_two_or_has_nothing_to_answer_is_refused(runtime):
    manager, session, _, _ = runtime
    with pytest.raises(ValueError, match="No compaction choice is waiting"):
        manager.answer_compaction_choice(session["id"], "compact_now")
    await over(manager, session)
    await start_waiting(manager, session)
    for choice in ("later", "", None, "Compact now"):
        with pytest.raises(ValueError, match="Choose compact_now or continue"):
            manager.answer_compaction_choice(session["id"], choice)
    assert manager.interactions.waiting and manager.statuses[session["id"]] == "awaiting_approval"
    manager.answer_compaction_choice(session["id"], "compact_now")
    with pytest.raises(ValueError, match="No compaction choice is waiting"):
        manager.answer_compaction_choice(session["id"], "continue")  # Answered once: a second answer finds nothing.
    await bounded(manager.tasks[session["id"]])


def test_the_route_validates_the_choice_and_says_when_nothing_waits(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        manager = client.app.state.manager
        session = manager.store.create(settings.values)
        url = f"/api/sessions/{session['id']}/compaction-choice"
        assert client.post(url, headers=headers, json={"choice": "later"}).status_code == 422
        assert client.post(url, headers=headers, json={}).status_code == 422
        assert client.post(url, headers=headers, json={"choice": "continue", "extra": 1}).status_code == 422
        empty = client.post(url, headers=headers, json={"choice": "continue"})
        assert empty.status_code == 400 and "No compaction choice is waiting" in empty.text
        assert client.post("/api/sessions/nope/compaction-choice", headers=headers,
                           json={"choice": "continue"}).status_code == 404
        assert client.post(url, json={"choice": "continue"}).status_code in (401, 403)  # The local token is checked.


# --- Never asked -----------------------------------------------------------------------------------------------------

async def compacts_silently(manager, session, endpoint, text="Go on.", **options):
    """A turn at the threshold that starts at once and compacts, with no card."""
    seen = watch(manager)
    before = len(endpoint.agent)
    manager.start(session["id"], text, **options)
    await bounded(manager.tasks[session["id"]])
    assert not seen["messages"] and not cards(manager, session) and not manager.interactions.waiting
    assert compactions(manager, session) == 1 and len(endpoint.agent) == before + 1
    final = saved(manager, session)
    assert any(event["type"] == "notice" and event.get("compaction") for event in final["events"])
    assert final["terminal_reason"] == "completed"
    return final


async def test_a_setting_that_is_off_never_asks(runtime):
    manager, session, endpoint, _ = runtime
    manager.settings.values["compaction_ask"] = False
    await over(manager, session)
    await compacts_silently(manager, session, endpoint)


async def test_with_nobody_at_a_page_it_compacts_silently(runtime):
    manager, session, endpoint, _ = runtime
    manager.pages.clear()  # A script or a headless run: no presence socket is open.
    await over(manager, session)
    await compacts_silently(manager, session, endpoint)


async def test_a_message_a_script_sent_with_a_token_never_asks_even_with_a_page_open(runtime):
    manager, session, endpoint, _ = runtime
    await over(manager, session)
    final = await compacts_silently(manager, session, endpoint, client_ref="script-1")
    assert [event for event in final["events"] if event["type"] == "user"][-1]["client_refs"] == ["script-1"]


async def test_a_scheduled_run_never_asks(runtime):
    manager, session, endpoint, _ = runtime
    await over(manager, session)
    manager.scheduled_turns[session["id"]] = {"schedule_id": "s1", "approval_timeout_minutes": 5}
    await compacts_silently(manager, session, endpoint)


async def test_a_subagent_chat_never_asks(runtime):
    manager, session, endpoint, _ = runtime
    await over(manager, session)
    live = saved(manager, session)
    live["is_subagent"] = True
    manager.store.save(live)
    await compacts_silently(manager, session, endpoint)


async def test_a_wake_turn_after_a_background_subagent_never_asks(runtime):
    manager, session, endpoint, _ = runtime
    await over(manager, session)
    final = await compacts_silently(manager, session, endpoint, "A background subagent finished.",
                                    extra={"background_wake": True})
    assert [event for event in final["events"] if event["type"] == "user"][-1]["background_wake"] is True


async def test_a_queued_message_compacts_with_the_default_instead_of_holding_the_queue(runtime):
    manager, session, endpoint, _ = runtime
    tokens, budget = await warm(manager, session)
    entered, hold = asyncio.Event(), asyncio.Event()
    endpoint.script.append({"entered": entered, "hold": hold, "text": "First done."})
    seen = watch(manager)
    manager.start(session["id"], "A first message that runs while the next one is queued.")
    await bounded(entered.wait())
    # The threshold drops below the chat while it runs, so the queued message starts over it.
    manager.settings.values["auto_compact_percent"] = max(50, 100 * tokens // budget - 2)
    queued = await bounded(manager.submit_message(session["id"], "A queued message."))
    assert queued.get("queued") is True
    hold.set()
    first = manager.tasks[session["id"]]
    await bounded(first)
    for _ in range(10_000):  # The queue starts its turn when the first ends: yield until that task is there.
        task = manager.tasks.get(session["id"])
        if task is not first:
            break
        await asyncio.sleep(0)
    await bounded(manager.tasks[session["id"]])
    assert not seen["messages"] and not cards(manager, session) and not manager.interactions.waiting
    final = saved(manager, session)
    assert compactions(manager, session) >= 1
    users = [event["text"] for event in final["events"] if event["type"] == "user"]
    assert users[-1] == "A queued message." and not final.get("queued_messages")
    assert any(event["type"] == "notice" and event.get("compaction") for event in final["events"])


async def test_a_compaction_in_the_middle_of_a_turn_never_asks(runtime):
    manager, session, endpoint, project = runtime
    (project / "big.txt").write_text("".join(f"{number}: " + "word " * 12 + "\n" for number in range(1, 700)))
    tokens, budget = await warm(manager, session)
    await at_percent(manager, session, percent_for(tokens, budget, 1.12))  # Room for the next message, not for more.
    assert saved(manager, session)["context_info"]["warn"] == "near"
    endpoint.script.append({"call": ("read_file", {"path": "big.txt"})})
    seen, before = watch(manager), len(endpoint.agent)
    await turn(manager, session, "Read big.txt and tell me what it says.")
    assert not seen["messages"] and not cards(manager, session) and not manager.interactions.waiting
    final = saved(manager, session)
    assert compactions(manager, session) == 1 and len(endpoint.agent) == before + 2
    notice = next(event for event in final["events"] if event["type"] == "notice" and event.get("compaction"))
    assert notice["compaction"]["trigger"] in ("auto", "in_turn")
    assert final["terminal_reason"] == "completed"


# --- Nothing about the choice reaches the model ----------------------------------------------------------------------

async def test_no_request_mentions_the_choice(runtime):
    manager, session, endpoint, _ = runtime
    await continue_chat(manager, session, endpoint)
    info = saved(manager, session)["context_info"]
    await turn(manager, session, message_of(info["input_budget"] - info["next_estimate"]["tokens"] + 3_000))
    assert compactions(manager, session) == 1 and endpoint.summaries >= 1
    for body in endpoint.raw:
        for text in (CARD, "Continue without compacting", "Compact now", "compaction_choice", "compact_now"):
            assert text not in body, text


# --- Settings --------------------------------------------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(tmp_path), env_file="")
    return settings


def test_compaction_ask_is_on_by_default_and_must_be_a_boolean(settings):
    assert settings.values["compaction_ask"] is True
    for bad in ("yes", 1, None):
        with pytest.raises(ValueError, match="Ask before compacting must be on or off"):
            settings.update({"compaction_ask": bad})
    assert settings.values["compaction_ask"] is True
    settings.update({"compaction_ask": False})
    assert Settings(settings.state_dir).values["compaction_ask"] is False


def test_a_hand_edited_value_that_is_not_a_boolean_means_on(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "settings.json").write_text(json.dumps({"compaction_ask": "no"}))
    assert Settings(tmp_path / "state").values["compaction_ask"] is True


def test_the_settings_route_takes_compaction_ask(settings, tmp_path):
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        base = {"workspace": str(tmp_path), "model": "test-model", "env_file": ""}
        ok = client.put("/api/settings", headers=headers, json={**base, "compaction_ask": False})
        assert ok.status_code == 200 and ok.json()["compaction_ask"] is False
        assert client.put("/api/settings", headers=headers, json={**base, "compaction_ask": "no"}).status_code == 422


async def test_warn_counts_the_trim_the_next_request_runs_first(runtime):
    """Over the threshold at rest, but the pass would bring the request under it: no warning, no card, no compaction."""
    manager, session, endpoint, _ = runtime
    manager.settings.values["context_trim"] = True
    tokens, budget = await warm(manager, session)
    percent = max(50, 100 * tokens // budget - 4)
    info = await at_percent(manager, session, percent)
    assert tokens > info["auto_compact_threshold"], "The chat must be over the threshold before the trim."
    assert info["next_estimate"]["after_trim_tokens"] < tokens
    assert info["warn"] != "at_threshold"
    seen = watch(manager)
    await turn(manager, session)
    assert not seen["messages"] and not cards(manager, session) and compactions(manager, session) == 0
    assert any("to save context" in event.get("text", "") for event in saved(manager, session)["events"])


# --- M1: only a message from a page that has THIS chat open asks -------------------------------------------------------

async def test_a_scripted_message_to_a_chat_no_page_has_open_never_asks_while_a_page_shows_another_chat(runtime):
    manager, session, endpoint, _ = runtime
    manager.listeners.clear()
    manager.listeners["another-chat"] = {FakePage()}  # The page is open, on chat A; the script writes to chat B.
    assert manager.pages
    await over(manager, session)
    await compacts_silently(manager, session, endpoint)


async def test_a_page_with_this_chat_open_gets_the_card(runtime):
    manager, session, endpoint, _ = runtime
    manager.listeners.clear()
    manager.listeners["another-chat"] = {FakePage()}
    manager.listeners[session["id"]] = {FakePage()}  # Now the page has chat B open.
    await over(manager, session)
    seen = watch(manager)
    manager.start(session["id"], "Go on with the work.")
    await bounded(seen["arrived"].wait())
    assert len(seen["messages"]) == 1
    manager.answer_compaction_choice(session["id"], "compact_now")
    await bounded(manager.tasks[session["id"]])
    assert compactions(manager, session) == 1


async def test_a_script_reading_the_chats_stream_is_not_a_page(runtime):
    from local_agent.stream_api import StreamListener
    manager, session, endpoint, _ = runtime
    manager.listeners.clear()
    manager.listeners[session["id"]] = {StreamListener()}  # A script's NDJSON reader of this very chat.
    await over(manager, session)
    await compacts_silently(manager, session, endpoint)


# --- L10: Continue without compacting does not outlive a cut of the conversation ----------------------------------------------

async def test_continue_does_not_survive_a_rewind_or_a_fork_or_an_import(runtime, tmp_path):
    from local_agent.history import cut_session
    from local_agent.portability import export_bundle, fresh_copies
    manager, session, endpoint, _ = runtime
    await continue_chat(manager, session, endpoint)
    live = saved(manager, session)
    assert live["compaction_choice"]["choice"] == "continue"
    last_user = [event for event in live["events"] if event["type"] == "user"][-1]["id"]
    cut, _ = cut_session(live, last_user, keep_local=True)  # A rewind of the last message.
    assert "compaction_choice" not in cut
    for kind in ("fork", "import"):
        copies, _ = fresh_copies(export_bundle([live], live["id"]), str(tmp_path), kind=kind)
        assert "compaction_choice" not in copies[0]
    # After the rewind the chat asks again at its next crossing.
    live.pop("compaction_choice", None)
    manager.store.save(live)
    assert manager.compaction_held(live, live.get("context_state")) is False
