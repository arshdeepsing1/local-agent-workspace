"""Background subagents (delegate_task background): the call returns at once and the parent goes on; the
subagent's end is a notice in the parent chat and in its next step, its result is read with
get_subagent_result or on its card; Stop and a restart close it; rewind and fork wait for it; the optional
wake-up continues an idle chat once; subagent_stop hooks run in the idle parent without losing its events."""
import asyncio
import json
import time

import httpx
import pytest

from local_agent import planning, spending
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.planning import BACKGROUND_QUEUED, BACKGROUND_STARTED, WAKE_TEXT

from test_parallel_subagents import (Gateway, calls_reply, card, delegate, eventually, make_runtime, reply, run_turn,
                                     tool_messages)

OPUS = "databricks-claude-opus-5-5"


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    async for value in make_runtime(tmp_path, monkeypatch):
        yield value

def gated(gate, text="Found it at a.txt:1."):
    async def handler(count, body):
        await gate.wait()
        return reply(text)
    return handler


async def forever(count, body):
    await asyncio.Event().wait()


def background_child(manager, parent):
    record, = manager.delegates.background[parent["id"]]
    return record["child_session_id"]


async def test_a_background_subagent_returns_at_once_and_reports_when_it_ends(runtime, monkeypatch):
    manager, parent, _ = runtime
    gate = asyncio.Event()
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Long survey", "a", background=True)),
                                    lambda: reply("Started; I will read its report later.")],
                      {"Long survey": gated(gate)})
    saved = await run_turn(manager, parent)
    # The tool result came back while the child works; the parent's turn ended.
    result = json.loads(card(saved, "a")["output"])
    child_id = result["child_session_id"]
    assert result == {"child_session_id": child_id, "agent_type": "general", "status": "running", "background": True,
                      "transcript": f"conversations/{child_id}.jsonl", "message": BACKGROUND_STARTED}
    assert saved["terminal_reason"] == "completed" and len(gateway.requests["parent"]) == 2
    assert manager.delegates.background_running(parent["id"]) == 1 and not manager.tasks[child_id].done()
    # The idle chat stays in memory while its subagent reports to it, and its card shows live progress.
    assert parent["id"] not in manager.live and manager.get(parent["id"]) is manager.delegates.held_session(parent["id"])
    progress = card(manager.get(parent["id"]), "a")["delegation"]
    assert progress["background"] is True and progress["status"] == "running" and "terminal_reason" not in progress
    gate.set()
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    saved = manager.store.get(parent["id"])
    final = card(saved, "a")["delegation_result"]
    assert (final["status"], final["output"], final["child_session_id"]) == ("completed", "Found it at a.txt:1.", child_id)
    notice = saved["events"][-1]
    assert {key: notice[key] for key in ("type", "text", "subagent_id", "subagent_status")} == {
        "type": "notice", "text": "Background subagent general finished: Long survey", "subagent_id": child_id,
        "subagent_status": "completed"}
    assert manager.delegates.held_session(parent["id"]) is None
    entry, = saved["subagents"]
    assert (entry["background"], entry["status"]) == (True, "completed")

    # The next step names it, and get_subagent_result (offered from now on) reads its result.
    gateway.parent = [lambda: calls_reply(("get_subagent_result", {"subagent_id": child_id[:8]}, "r")),
                      lambda: reply("It found a.txt:1.")]
    gateway.requests["parent"] = []
    saved = await run_turn(manager, parent, "What did it find?")
    first = gateway.requests["parent"][0]
    assert "get_subagent_result" in {tool["function"]["name"] for tool in first["tools"]}
    assert (f"Background subagents ended since your last step: {child_id[:8]} general finished: Long survey. "
            "Read their reports with get_subagent_result.") in json.dumps(first["messages"])
    fetched = json.loads(dict(tool_messages(saved["wire"]))["r"])
    assert fetched["status"] == "completed" and fetched["result"]["output"] == "Found it at a.txt:1."
    # Its notice is not repeated in later steps.
    assert "Background subagents ended" not in json.dumps(gateway.requests["parent"][1]["messages"])


async def test_get_subagent_result_lists_waits_and_times_out(runtime, monkeypatch):
    manager, parent, _ = runtime
    gate = asyncio.Event()
    Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), lambda: reply("Going on.")],
            {"Survey": gated(gate)})
    await run_turn(manager, parent)
    child_id = background_child(manager, parent)
    held = manager.get(parent["id"])
    began = time.monotonic()
    still = await manager.delegates.subagent_result(held, child_id[:8], wait_seconds=1)
    assert time.monotonic() - began >= 0.9
    assert (still["subagent_id"], still["status"], still["agent_type"]) == (child_id, "running", "general")
    assert still["message"].startswith("Still working.")
    listing = await manager.delegates.subagent_result(held)
    assert listing == {"running": 1, "subagents": [{"subagent_id": child_id, "agent_type": "general", "status": "running",
                                                    "task": "Survey", "has_result": False}]}
    for wrong, message in (("ffffffff", "No background subagent of this chat has the ID ffffffff"),
                           ("abc", "first 8 characters")):
        with pytest.raises(ValueError, match=message):
            await manager.delegates.subagent_result(held, wrong)
    with pytest.raises(ValueError, match="wait_seconds"):
        await manager.delegates.subagent_result(held, child_id, wait_seconds=301)
    gate.set()
    done = await manager.delegates.subagent_result(held, child_id, wait_seconds=10)
    assert done["status"] == "completed" and done["result"]["output"] == "Found it at a.txt:1."
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    # Read once: the next step's notice leaves it out.
    assert manager.delegates.background_notice(manager.store.get(parent["id"])) == ""


async def test_stop_on_the_parent_stops_its_background_subagents_and_a_card_stops_one(runtime, monkeypatch):
    manager, parent, _ = runtime
    gate = asyncio.Event()
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Forever", "a", background=True),
                                                        delegate("Short", "b", background=True)),
                                    lambda: reply("Going on.")],
                      {"Forever": forever, "Short": gated(gate)})
    await run_turn(manager, parent)
    await asyncio.wait_for(gateway.child_started("Short").wait(), 5)
    ids = {record["task"]: record["child_session_id"] for record in manager.delegates.background[parent["id"]]}
    # Stop subagent on one card: only that one ends, as stopped.
    await manager.stop(ids["Short"])
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 1)
    saved = manager.store.get(parent["id"])
    assert card(saved, "b")["delegation_result"]["status"] == "cancelled"
    assert saved["events"][-1]["text"] == "Background subagent general was stopped: Short"
    assert not card(saved, "a").get("delegation_result")
    # Stop on the parent (also while it is idle) stops the rest and waits for their reports.
    await manager.stop(parent["id"])
    assert manager.delegates.background_running(parent["id"]) == 0
    saved = manager.store.get(parent["id"])
    assert card(saved, "a")["delegation_result"]["status"] == "cancelled"
    assert saved["events"][-1]["subagent_status"] == "cancelled"
    assert manager.store.get(ids["Forever"])["terminal_reason"] == "stopped"


async def test_a_restart_marks_a_background_subagent_interrupted_and_tells_the_parent(runtime, monkeypatch):
    manager, parent, _ = runtime
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Forever", "a", background=True)),
                                    lambda: reply("Going on.")], {"Forever": forever})
    await run_turn(manager, parent)
    await asyncio.wait_for(gateway.child_started("Forever").wait(), 5)
    child_id = background_child(manager, parent)
    snapshot = [manager.store.get(session_id) for session_id in (parent["id"], child_id)]  # As a crash leaves them.
    await manager.stop(parent["id"])
    for session in snapshot:
        manager.store.save(session)
    restarted = AgentManager(manager.store, manager.settings)
    try:
        saved = manager.store.get(parent["id"])
        assert card(saved, "a")["delegation"]["terminal_reason"] == "interrupted"
        notices = [event for event in saved["events"] if event.get("subagent_id") == child_id]
        assert [(event["text"], event["subagent_status"]) for event in notices] == [
            ("Background subagent general was interrupted: Forever", "interrupted")]
        # A second start adds no second notice.
        AgentManager(manager.store, manager.settings)
        assert len([event for event in manager.store.get(parent["id"])["events"]
                    if event.get("subagent_id") == child_id]) == 1
    finally:
        await restarted.jobs.shutdown()


async def test_a_fourth_background_subagent_waits_for_a_slot_and_the_spend_counts_running_ones(runtime, monkeypatch):
    manager, parent, _ = runtime
    gate = asyncio.Event()
    Gateway(monkeypatch, [lambda: calls_reply(*(delegate(f"Task {number}", f"c{number}", background=True)
                                                for number in range(4))),
                          lambda: reply("Waiting for them.")],
            {f"Task {number}": gated(gate, f"Done {number}.") for number in range(4)})
    saved = await run_turn(manager, parent)
    results = [json.loads(card(saved, f"c{number}")["output"]) for number in range(4)]
    assert [item["status"] for item in results] == ["running", "running", "running", "queued"]
    assert results[3]["child_session_id"] is None and results[3]["message"] == BACKGROUND_QUEUED
    assert card(saved, "c3")["delegation"]["status"] == "queued"
    # The parent's spending limit counts its running background subagents.
    child = manager.live[results[0]["child_session_id"]]
    child["inference_calls"].append({"id": "x", "purpose": "agent", "model": OPUS, "status": "completed",
                                     "usage": {"input_tokens": 10_000, "output_tokens": 1_000}})
    assert spending.chat_spend(manager, manager.get(parent["id"]))["subagents_dbu"] == pytest.approx(0.857144)
    gate.set()
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    saved = manager.store.get(parent["id"])
    assert [card(saved, f"c{number}")["delegation_result"]["output"] for number in range(4)] == [
        f"Done {number}." for number in range(4)]
    assert len([event for event in saved["events"] if event.get("subagent_status") == "completed"]) == 4


def full_slots(monkeypatch):
    """Every subagent slot taken until the returned dict's value is False (then the real count applies)."""
    full = {"value": True}
    original = planning.DelegateManager.running
    monkeypatch.setattr(planning.DelegateManager, "running",
                        lambda self: planning.MAX_RUNNING if full["value"] else original(self))
    return full


@pytest.mark.parametrize("change", ["tool_off", "auto_to_manual"])
async def test_a_queued_background_subagent_refused_when_its_slot_frees_tells_the_chat_why(runtime, monkeypatch, change):
    manager, parent, _ = runtime
    parent["permission_mode"] = "auto"  # delegate_task runs without a card in Auto.
    manager.store.save(parent)
    manager.settings.values["background_subagent_wake"] = True
    full = full_slots(monkeypatch)
    Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), lambda: reply("Going on.")],
            {"Survey": lambda count, body: asyncio.sleep(0, reply("Done."))})
    saved = await run_turn(manager, parent)
    assert json.loads(card(saved, "a")["output"])["status"] == "queued"
    held = manager.get(parent["id"])
    if change == "tool_off":
        held["disabled_tools"] = ["delegate_task"]
    else:
        held["permission_mode"] = "manual"
    full["value"] = False
    manager.delegates._wake()
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    saved = manager.store.get(parent["id"])
    reason = ("delegate_task was turned off in this chat while the subagent waited" if change == "tool_off"
              else "starting it now needs approval")
    result = card(saved, "a")["delegation_result"]
    assert (result["status"], result["child_session_id"]) == ("failed", None) and reason in result["output"]
    assert card(saved, "a")["delegation"]["status"] == "failed"
    notice, = [event for event in saved["events"] if event.get("subagent_status")]
    assert notice["subagent_status"] == "failed" and notice.get("subagent_id") is None
    assert notice["text"].startswith("Background subagent general failed: Survey (It did not start: ")
    assert reason in manager.delegates.background_notice(saved)
    listing = await manager.delegates.subagent_result(saved)
    assert listing["subagents"] == [{"subagent_id": None, "agent_type": "general", "status": "failed", "task": "Survey",
                                     "has_result": True}]
    assert not manager.busy_task(parent["id"])  # Nothing to read: the wake option does not start a turn for it.


async def test_a_queued_background_subagent_starts_within_a_ceiling_narrowed_while_it_waited(runtime, monkeypatch):
    manager, parent, _ = runtime
    full = full_slots(monkeypatch)
    gate = asyncio.Event()
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)),
                                    lambda: reply("Going on.")], {"Survey": gated(gate)})
    saved = await run_turn(manager, parent)
    assert json.loads(card(saved, "a")["output"])["status"] == "queued"
    assert not manager.busy_task(parent["id"])  # Idle: Agent tools → Subagents may change the ceiling now.
    manager.get(parent["id"])["subagent_tool_profile"] = "read_only"
    full["value"] = False
    manager.delegates._wake()
    await asyncio.wait_for(gateway.child_started("Survey").wait(), 5)
    child = manager.get(background_child(manager, parent))
    offered = {tool["function"]["name"] for tool in gateway.requests["Survey"][0]["tools"]}
    assert child["tool_profile"] == "read_only" and "run_command" not in offered and "read_file" in offered
    gate.set()
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    assert card(manager.store.get(parent["id"]), "a")["delegation_result"]["tool_profile"] == "read_only"


async def test_subagent_stop_hooks_run_in_an_idle_parent_and_keep_its_newer_events(runtime, monkeypatch):
    manager, parent, _ = runtime
    manager.extensions.update_config({"hooks": [{
        "id": "review", "event": "subagent_stop", "enabled": True, "timeout_seconds": 10,
        "command": "printf '%s' '{\"report\": \"Checked by the hook.\"}'"}]})
    gate = asyncio.Event()
    Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), lambda: reply("Going on."),
                          lambda: reply("Second answer.")], {"Survey": gated(gate)})
    await run_turn(manager, parent)
    # A turn of the idle parent while its subagent works, then the subagent ends and its hook runs there.
    await run_turn(manager, parent, "Another question")
    gate.set()
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    saved = manager.store.get(parent["id"])
    texts = [event.get("text") for event in saved["events"]]
    assert "Another question" in texts and "Second answer." in texts
    assert [event["state"] for event in saved["events"] if event.get("name") == "hook_subagent_stop"] == ["completed"]
    assert card(saved, "a")["delegation_result"]["output"].endswith("Checked by the hook.")
    assert manager.statuses.get(parent["id"], "idle") == "idle"


async def test_answering_a_hook_card_of_an_idle_parent_keeps_a_turn_started_meanwhile_running(runtime, monkeypatch):
    manager, parent, _ = runtime
    parent["permission_mode"] = "acceptEdits"  # delegate_task runs without a card; the hook asks for one.
    manager.store.save(parent)
    manager.extensions.update_config({"hooks": [{"id": "review", "event": "subagent_stop", "enabled": True,
                                                 "timeout_seconds": 10, "command": "true"}]})
    gate, hold = asyncio.Event(), asyncio.Event()
    Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), lambda: reply("Going on.")],
            {"Survey": gated(gate)})
    await run_turn(manager, parent)
    gate.set()
    await eventually(lambda: any(key[0] == parent["id"] for key in manager.pending))  # The hook card waits.
    key = next(key for key in manager.pending if key[0] == parent["id"])
    assert manager.statuses.get(parent["id"]) == "awaiting_approval"
    original = Gateway.__call__

    async def call(self, request):  # The new turn's model request waits for hold.
        body = json.loads(request.content)
        if body.get("stream") is True and any(item.get("content") == "Another question" for item in body["messages"]):
            await hold.wait()
            return reply("Second answer.")
        return await original(self, request)
    monkeypatch.setattr(Gateway, "__call__", call)
    manager.start(parent["id"], "Another question")  # The user writes in the chat while the card waits.
    await eventually(lambda: manager.statuses.get(parent["id"]) == "running")
    manager.decide(parent["id"], key[1], True)
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    assert manager.busy_task(parent["id"]) and manager.statuses.get(parent["id"]) == "running"
    hold.set()
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    assert manager.statuses.get(parent["id"], "idle") == "idle"


class HeldGateway(Gateway):
    """A Gateway whose parent responses may be coroutines (a response that waits)."""

    async def __call__(self, request):
        response = await super().__call__(request)
        return await response if asyncio.iscoroutine(response) else response


@pytest.mark.parametrize("asked", ["while_idle", "during_the_turn"])
async def test_a_turn_that_ends_while_a_hook_card_waits_leaves_that_card_waiting(runtime, monkeypatch, asked):
    """The hook card of a background subagent waits in the parent while a turn of the parent ends (one the user
    started while the card waited, or the turn that was running when it asked): the card stays answerable and
    the chat says it waits, and its answer makes the chat idle."""
    manager, parent, _ = runtime
    parent["permission_mode"] = "acceptEdits"  # delegate_task runs without a card; the hook asks for one.
    manager.store.save(parent)
    manager.extensions.update_config({"hooks": [{"id": "review", "event": "subagent_stop", "enabled": True,
                                                 "timeout_seconds": 10, "command": "true"}]})
    gate, hold = asyncio.Event(), asyncio.Event()

    async def held():  # The parent's second request waits until the card asks.
        await hold.wait()
        return reply("Going on.")
    if asked == "while_idle":
        HeldGateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), lambda: reply("Going on."),
                                  lambda: reply("Second answer.")], {"Survey": gated(gate)})
        await run_turn(manager, parent)
        gate.set()
    else:
        HeldGateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), held],
                    {"Survey": gated(gate)})
        manager.start(parent["id"], "Delegate the work")
        gate.set()
    await eventually(lambda: any(key[0] == parent["id"] for key in manager.pending))  # The hook card waits.
    key = next(key for key in manager.pending if key[0] == parent["id"])
    if asked == "while_idle":
        await manager.submit_message(parent["id"], "Another question")  # As the message route starts a turn.
    hold.set()
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    hook_card = next(event for event in manager.get(parent["id"])["events"] if event["id"] == key[1])
    assert hook_card["state"] == "pending" and not manager.pending[key].done()
    assert manager.statuses.get(parent["id"]) == "awaiting_approval"
    manager.decide(parent["id"], key[1], True)
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    await eventually(lambda: manager.statuses.get(parent["id"], "idle") == "idle")
    saved = manager.store.get(parent["id"])
    hook_card = next(event for event in saved["events"] if event["id"] == key[1])
    assert (hook_card["state"], hook_card["approval"]) == ("completed", "approved")
    assert card(saved, "a")["delegation_result"]["status"] == "completed"


REVIEW_HOOK = {"id": "review", "event": "subagent_stop", "enabled": True, "timeout_seconds": 10, "command": "true"}


def waiting_cards(manager, session_id):
    """The keys of the chat's cards that still wait for their answer."""
    return [key for key, future in manager.pending.items() if key[0] == session_id and not future.done()]


def hold_compaction(manager, monkeypatch, parent, hold):
    """Give the idle chat an earlier turn to compact, and make its summary request wait for hold."""
    held = manager.get(parent["id"])
    held["wire"] = [{"role": "user", "content": "First"}, {"role": "assistant", "content": "Done"}, *held["wire"]]
    manager.store.save(held)
    summarize = manager.summarize_context

    async def held_summary(*args, **kwargs):
        await hold.wait()
        return await summarize(*args, **kwargs)
    monkeypatch.setattr(manager, "summarize_context", held_summary)


@pytest.mark.parametrize("task", ["compaction", "command"])
async def test_a_compaction_or_a_command_that_ends_while_a_hook_card_waits_leaves_the_chat_awaiting_approval(
        runtime, monkeypatch, task):
    """A background subagent's subagent_stop card asks in the chat while a manual compaction or a ! command runs
    there. The task's end leaves the card waiting, so the chat still shows it waits for approval, as at a turn's
    end, until the card is answered."""
    manager, parent, project = runtime
    parent["permission_mode"] = "acceptEdits"  # delegate_task runs without a card; the hook asks for one.
    manager.store.save(parent)
    manager.extensions.update_config({"hooks": [REVIEW_HOOK]})
    gate, hold = asyncio.Event(), asyncio.Event()
    Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), lambda: reply("Going on.")],
            {"Survey": gated(gate)})
    await run_turn(manager, parent)
    if task == "command":  # It runs until the file go exists.
        manager.start_shell(parent["id"], "while [ ! -f go ]; do sleep 0.05; done", timeout_seconds=30)
    else:
        hold_compaction(manager, monkeypatch, parent, hold)
        manager.start_compaction(parent["id"])
    gate.set()  # The subagent ends; its hook asks in the chat while the task runs.
    await eventually(lambda: bool(waiting_cards(manager, parent["id"])))
    key, = waiting_cards(manager, parent["id"])
    hold.set()  # The compaction's summary request goes on ...
    (project / "go").write_text("")  # ... or the command ends.
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)  # The task ended; the card still waits.
    hook_card = next(event for event in manager.get(parent["id"])["events"] if event["id"] == key[1])
    assert hook_card["state"] == "pending" and not manager.pending[key].done()
    assert manager.statuses.get(parent["id"]) == "awaiting_approval"
    manager.decide(parent["id"], key[1], True)
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    await eventually(lambda: manager.statuses.get(parent["id"], "idle") == "idle")


@pytest.mark.parametrize("asked", ["in_an_earlier_turn", "in_the_same_turn"])
async def test_answering_a_hook_card_while_the_turns_own_card_waits_keeps_the_chat_awaiting_approval(
        runtime, monkeypatch, asked):
    """A background subagent's hook card asked while a turn of the chat ran (the turn that ends before the next one
    starts, or the same turn). The running turn then waits for its own run_command card. Answering the hook card
    first leaves the chat awaiting approval, not running, since the turn's card still waits."""
    manager, parent, _ = runtime
    parent["permission_mode"] = "acceptEdits"  # delegate_task runs without a card; run_command asks.
    manager.store.save(parent)
    manager.extensions.update_config({"hooks": [REVIEW_HOOK]})
    gate, hold = asyncio.Event(), asyncio.Event()

    def check():  # The response whose run_command asks for the turn's own card.
        return calls_reply(("run_command", {"command": "echo checking"}, "r"))

    async def held():  # The turn's second request waits until the hook card asks.
        await hold.wait()
        return reply("Going on.") if asked == "in_an_earlier_turn" else check()
    later = [check, lambda: reply("Ran it.")] if asked == "in_an_earlier_turn" else [lambda: reply("Ran it.")]
    HeldGateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), held, *later],
                {"Survey": gated(gate)})
    manager.start(parent["id"], "Delegate the work")
    gate.set()
    await eventually(lambda: bool(waiting_cards(manager, parent["id"])))  # The hook card waits while the turn runs.
    hook_key, = waiting_cards(manager, parent["id"])
    hold.set()
    if asked == "in_an_earlier_turn":
        await asyncio.wait_for(manager.tasks[parent["id"]], 10)  # That turn ends; the card stays waiting.
        await manager.submit_message(parent["id"], "Run a check")
    await eventually(lambda: len(waiting_cards(manager, parent["id"])) == 2)  # The turn's own card waits too.
    own_key, = (key for key in waiting_cards(manager, parent["id"]) if key != hook_key)
    manager.decide(parent["id"], hook_key[1], True)  # The user answers the hook card first.
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    assert not manager.pending[own_key].done()
    assert manager.statuses.get(parent["id"]) == "awaiting_approval"
    manager.decide(parent["id"], own_key[1], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    assert manager.statuses.get(parent["id"], "idle") == "idle"


async def test_a_hook_card_answered_while_the_turn_ends_leaves_the_idle_chat_idle(runtime, monkeypatch):
    """The chat is open in a page that is slow to take the turn's idle status, and the user answers the background
    subagent's hook card right as the turn ends: once everything settled, no task runs and the chat is idle, not
    stuck at running."""
    manager, parent, _ = runtime
    parent["permission_mode"] = "acceptEdits"
    manager.store.save(parent)
    manager.extensions.update_config({"hooks": [REVIEW_HOOK]})
    gate, hold = asyncio.Event(), asyncio.Event()

    class SlowPage:  # Slow to take the idle status, which widens the turn's end.
        async def send_json(self, data):
            await asyncio.sleep(0.5 if data.get("type") == "status" and data.get("status") == "idle" else 0.005)

    async def held():
        await hold.wait()
        return reply("Going on.")
    HeldGateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), held],
                {"Survey": gated(gate)})
    manager.listeners[parent["id"]] = {SlowPage()}
    manager.start(parent["id"], "Delegate the work")
    gate.set()
    await eventually(lambda: bool(waiting_cards(manager, parent["id"])))
    key, = waiting_cards(manager, parent["id"])
    hold.set()
    await eventually(lambda: manager.statuses.get(parent["id"]) == "idle")  # The turn ends ...
    manager.decide(parent["id"], key[1], True)  # ... and the user clicks Allow at that moment.
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    await asyncio.sleep(0.6)  # Every broadcast is over.
    assert not manager.busy_task(parent["id"]) and not waiting_cards(manager, parent["id"])
    assert manager.statuses.get(parent["id"], "idle") == "idle"


@pytest.mark.parametrize("send_seconds", [0, 0.02])
async def test_allow_on_a_hook_card_as_the_reply_footer_arrives_leaves_the_idle_chat_idle(
        runtime, monkeypatch, send_seconds):
    """The same with a page that takes every message equally fast: the turn wrote a file (its end also sends the
    changes list), and the user allows the hook card as the reply's footer (its turn result) reaches the page."""
    manager, parent, _ = runtime
    parent["permission_mode"] = "acceptEdits"  # delegate_task and write_file run without a card; the hook asks.
    manager.store.save(parent)
    manager.extensions.update_config({"hooks": [REVIEW_HOOK]})
    gate, hold = asyncio.Event(), asyncio.Event()
    clicked = {}

    class Page:
        async def send_json(self, data):
            event = data.get("event") or {}
            if data.get("type") == "event" and event.get("turn_result") and "key" in clicked and not clicked.get("done"):
                clicked["done"] = True
                manager.decide(parent["id"], clicked["key"][1], True)
            await asyncio.sleep(send_seconds)

    async def held():
        await hold.wait()
        return calls_reply(("write_file", {"path": "new.txt", "content": "changed\n"}, "w"))
    HeldGateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), held,
                              lambda: reply("Wrote new.txt.")], {"Survey": gated(gate)})
    manager.listeners[parent["id"]] = {Page()}
    manager.start(parent["id"], "Delegate the work")
    gate.set()
    await eventually(lambda: bool(waiting_cards(manager, parent["id"])))
    clicked["key"], = waiting_cards(manager, parent["id"])
    hold.set()
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    assert clicked.get("done")
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    await asyncio.sleep(0.3)  # Every broadcast is over.
    assert not manager.busy_task(parent["id"]) and not waiting_cards(manager, parent["id"])
    assert manager.statuses.get(parent["id"], "idle") == "idle"


@pytest.mark.parametrize("task", ["turn", "compaction"])
async def test_a_turns_or_a_compactions_end_leaves_a_running_background_hook_card_running(runtime, monkeypatch, task):
    """In Bypass a background subagent's subagent_stop hook runs without a card's question, so its card in the chat
    is running while the hook runs. A turn or a manual compaction of the chat that ends meanwhile does not show it
    cancelled: the hook still runs, and its card completes when it ends."""
    manager, parent, project = runtime  # Bypass permissions.
    manager.extensions.update_config({"hooks": [{**REVIEW_HOOK, "command": "while [ ! -f done ]; do sleep 0.05; done"}]})
    gate, hold = asyncio.Event(), asyncio.Event()
    shown = []

    class Page:  # The chat open in a browser: what its hook card shows.
        async def send_json(self, data):
            if data.get("type") == "event" and data["event"].get("name") == "hook_subagent_stop":
                shown.append(data["event"]["state"])

    async def held():
        await hold.wait()
        return reply("Going on.")
    HeldGateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)),
                              held if task == "turn" else lambda: reply("Going on.")], {"Survey": gated(gate)})
    if task == "compaction":
        await run_turn(manager, parent)
        hold_compaction(manager, monkeypatch, parent, hold)
        manager.start_compaction(parent["id"])
    else:
        manager.start(parent["id"], "Delegate the work")
    manager.listeners[parent["id"]] = {Page()}
    gate.set()  # The subagent ends; its hook runs while the turn or the compaction goes on.
    await eventually(lambda: shown == ["running"])
    hold.set()
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)  # It ended; the hook still runs.
    hook_card = next(event for event in manager.get(parent["id"])["events"] if event.get("name") == "hook_subagent_stop")
    saved = next(event for event in manager.store.get(parent["id"])["events"] if event["id"] == hook_card["id"])
    assert manager.delegates.background_running(parent["id"]) == 1
    assert (hook_card["state"], saved["state"], shown) == ("running", "running", ["running"])
    (project / "done").write_text("")
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    assert shown == ["running", "completed"]


async def test_a_restart_after_a_turn_ended_while_a_hook_card_waited_keeps_that_turn_completed(runtime, monkeypatch):
    """The turn completed while a background subagent's hook card waited in the chat, and the app was then killed.
    The restart closes the card and says the subagent was interrupted, but the finished turn stays completed (no
    Continue for an answer that completed)."""
    manager, parent, _ = runtime
    parent["permission_mode"] = "acceptEdits"
    manager.store.save(parent)
    manager.extensions.update_config({"hooks": [REVIEW_HOOK]})
    gate, hold = asyncio.Event(), asyncio.Event()

    async def held():
        await hold.wait()
        return reply("Going on.")
    HeldGateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), held],
                {"Survey": gated(gate)})
    manager.start(parent["id"], "Delegate the work")
    gate.set()
    await eventually(lambda: bool(waiting_cards(manager, parent["id"])))
    key, = waiting_cards(manager, parent["id"])
    hold.set()
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    child_id = background_child(manager, parent)
    snapshot = [manager.store.get(session_id) for session_id in (parent["id"], child_id)]  # As a crash leaves them.
    assert snapshot[0]["terminal_reason"] == "completed"
    assert next(event for event in snapshot[0]["events"] if event["id"] == key[1])["state"] == "pending"
    await manager.stop(parent["id"])
    for session in snapshot:
        manager.store.save(session)
    restarted = AgentManager(manager.store, manager.settings)
    try:
        saved = manager.store.get(parent["id"])
        assert saved["terminal_reason"] == "completed"
        assert saved["last_turn_result"]["terminal_reason"] == "completed"
        assert next(event for event in saved["events"] if event["id"] == key[1])["state"] == "cancelled"
        assert [event["text"] for event in saved["events"] if event.get("subagent_id") == child_id] == [
            "Background subagent general was interrupted: Survey"]
    finally:
        await restarted.jobs.shutdown()


async def test_a_subagent_that_finishes_while_a_hook_card_waits_continues_the_chat_once_the_card_is_answered(
        runtime, monkeypatch):
    """With the wake option on, Survey works in the background while the idle chat waits on the subagent_start card
    of a queued background delegation (the chat awaits approval, so Survey's end cannot continue it then). Once the
    card is answered the chat is idle again, and the app's message continues it for Survey."""
    manager, parent, _ = runtime
    parent["permission_mode"] = "acceptEdits"  # delegate_task runs without a card; the hook asks for one.
    manager.store.save(parent)
    manager.settings.values["background_subagent_wake"] = True
    gate = asyncio.Event()
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)),
                                    lambda: reply("Going on."),
                                    lambda: calls_reply(delegate("Review", "b", background=True)),
                                    lambda: reply("Queued it."),
                                    lambda: reply("Read it; all done.")],
                      {"Survey": gated(gate), "Review": forever})
    await run_turn(manager, parent)  # Survey runs in the background.
    manager.extensions.update_config({"hooks": [{"id": "brief", "event": "subagent_start", "enabled": True,
                                                 "timeout_seconds": 10, "command": "true"}]})
    full = full_slots(monkeypatch)
    saved = await run_turn(manager, parent, "Review it too")  # Review waits for a slot; the turn ends.
    assert json.loads(card(saved, "b")["output"])["status"] == "queued"
    full["value"] = False
    manager.delegates._wake()  # A slot frees: Review's start hook asks in the idle chat.
    await eventually(lambda: bool(waiting_cards(manager, parent["id"])))
    key, = waiting_cards(manager, parent["id"])
    assert manager.statuses.get(parent["id"]) == "awaiting_approval"
    gate.set()  # Survey finishes while the card waits.
    await eventually(lambda: isinstance(card(manager.get(parent["id"]), "a").get("delegation_result"), dict))
    await asyncio.sleep(0.3)
    assert len(gateway.requests["parent"]) == 4  # Not while the card waits.
    manager.decide(parent["id"], key[1], True)  # Review starts; the chat is idle again.
    for _ in range(300):  # The app's message, once its turn ended.
        if len(gateway.requests["parent"]) == 5 and not manager.busy_task(parent["id"]):
            break
        await asyncio.sleep(0.01)
    users = [event for event in manager.get(parent["id"])["events"] if event.get("type") == "user"]
    assert [event.get("background_wake") for event in users] == [None, None, True]


@pytest.mark.parametrize("wake", [True, False])
async def test_the_wake_option_continues_an_idle_chat_exactly_once(runtime, monkeypatch, wake):
    manager, parent, _ = runtime
    manager.settings.values["background_subagent_wake"] = wake
    gate = asyncio.Event()
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)),
                                    lambda: reply("Going on."), lambda: reply("Read it; all done.")],
                      {"Survey": gated(gate)})
    await run_turn(manager, parent)
    child_id = background_child(manager, parent)
    gate.set()
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    if wake:
        await eventually(lambda: len(gateway.requests["parent"]) == 3)
    await asyncio.sleep(0.3)
    await eventually(lambda: not manager.busy_task(parent["id"]))
    saved = manager.store.get(parent["id"])
    users = [event for event in saved["events"] if event["type"] == "user"]
    assert len(gateway.requests["parent"]) == (3 if wake else 2) and len(users) == (2 if wake else 1)
    if wake:
        assert users[1]["background_wake"] is True and users[1]["text"] == WAKE_TEXT.format(
            id=child_id[:8], kind="general", ending="finished")
        # The woken turn's request names the subagent once; the chat then stays idle.
        assert "Background subagents ended since your last step" in json.dumps(gateway.requests["parent"][2]["messages"])
        assert saved["terminal_reason"] == "completed"


@pytest.mark.parametrize("stop", ["step_limit", "inference_error"])
async def test_the_wake_option_never_continues_a_chat_stopped_at_a_limit_or_an_error(runtime, monkeypatch, stop):
    manager, parent, _ = runtime
    manager.settings.values["background_subagent_wake"] = True
    if stop == "step_limit":
        manager.settings.values["max_agent_steps"] = 2
    gate = asyncio.Event()
    second = (lambda: httpx.Response(400, json={"message": "Bad request."})) if stop == "inference_error" else (
        lambda: calls_reply(("list_tasks", {}, "t1")))
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), second,
                                    lambda: reply("Read it; all done.")], {"Survey": gated(gate)})
    saved = await run_turn(manager, parent)
    assert saved["terminal_reason"] == stop
    gate.set()  # The subagent finishes after the turn stopped.
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    await asyncio.sleep(0.3)
    saved = manager.store.get(parent["id"])
    assert len(gateway.requests["parent"]) == 2 and not manager.busy_task(parent["id"])
    assert [event["type"] for event in saved["events"]].count("user") == 1
    # Continue is yours: its first request names the subagent.
    assert manager.delegates.wake_due(manager.get(parent["id"]))
    await run_turn(manager, parent, "Continue")
    assert "Background subagents ended since your last step" in json.dumps(gateway.requests["parent"][2]["messages"])


@pytest.mark.parametrize("wake", [True, False])
@pytest.mark.parametrize("task", ["command", "compaction"])
async def test_a_wake_that_falls_during_a_command_or_a_compaction_comes_after_it(runtime, monkeypatch, task, wake):
    manager, parent, _ = runtime
    manager.settings.values["background_subagent_wake"] = wake
    gate, hold = asyncio.Event(), asyncio.Event()
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)),
                                    lambda: reply("Going on."), lambda: reply("Read it; all done.")],
                      {"Survey": gated(gate)})
    await run_turn(manager, parent)
    if task == "command":
        manager.start_shell(parent["id"], "sleep 1")
    else:
        held = manager.get(parent["id"])  # An earlier turn to compact.
        held["wire"] = [{"role": "user", "content": "First"}, {"role": "assistant", "content": "Done"}, *held["wire"]]
        manager.store.save(held)
        summarize = manager.summarize_context

        async def held_summary(*args, **kwargs):
            await hold.wait()
            return await summarize(*args, **kwargs)
        monkeypatch.setattr(manager, "summarize_context", held_summary)
        manager.start_compaction(parent["id"])
    gate.set()  # The subagent finishes while the command or the compaction runs.
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    assert manager.busy_task(parent["id"]) and len(gateway.requests["parent"]) == 2
    hold.set()
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    if wake:
        await eventually(lambda: len(gateway.requests["parent"]) == 3)
    await asyncio.sleep(0.3)
    await eventually(lambda: not manager.busy_task(parent["id"]))
    saved = manager.store.get(parent["id"])
    users = [event for event in saved["events"] if event["type"] == "user"]
    assert len(gateway.requests["parent"]) == (3 if wake else 2) and len(users) == (2 if wake else 1)
    if wake:
        assert users[1]["background_wake"] is True


async def test_a_running_turn_learns_of_a_background_subagent_at_its_next_step(runtime, monkeypatch):
    manager, parent, _ = runtime
    manager.settings.values["background_subagent_wake"] = True
    gate, ended = asyncio.Event(), asyncio.Event()

    async def child(count, body):
        await gate.wait()
        return reply("Child result.")
    responses = iter([calls_reply(delegate("Survey", "a", background=True)),
                      calls_reply(("list_tasks", {}, "t1")), reply("All done.")])

    def parent_step():
        return next(responses)
    gateway = Gateway(monkeypatch, [parent_step] * 3, {"Survey": child})
    real_execute = manager.execute_tool

    async def execute(session, tools, name, arguments, call_id, **extra):
        if name == "list_tasks":  # The subagent ends while this step's tool runs.
            gate.set()
            await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
            ended.set()
        return await real_execute(session, tools, name, arguments, call_id, **extra)
    manager.execute_tool = execute
    await run_turn(manager, parent)
    assert ended.is_set()
    assert "Background subagents ended since your last step" in json.dumps(gateway.requests["parent"][2]["messages"])
    # The running turn was told, so nothing wakes the chat afterwards.
    await asyncio.sleep(0.3)
    assert len(gateway.requests["parent"]) == 3


@pytest.fixture
async def api(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", model="test-model")
    settings.env = {}
    settings.credentials = lambda: ("https://gateway.example", "synthetic-token")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            yield client, app.state.manager


async def test_rewind_and_fork_wait_for_background_subagents(api, monkeypatch):
    client, manager = api
    parent = manager.store.create(manager.settings.values)
    parent.update(title="Parent", title_generated=True, permission_mode="bypassPermissions")
    manager.store.save(parent)
    gate = asyncio.Event()
    Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), lambda: reply("Going on.")],
            {"Survey": gated(gate)})
    await run_turn(manager, parent)
    first = next(event for event in manager.get(parent["id"])["events"] if event["type"] == "user")
    refusals = [await client.post(f"/api/sessions/{parent['id']}/rewind",
                                  json={"before_event_id": first["id"], "conversation": True}),
                await client.post(f"/api/sessions/{parent['id']}/fork", json={"before_event_id": first["id"]}),
                await client.post(f"/api/sessions/{parent['id']}/fork")]
    for response, action in zip(refusals, ("rewinding", "forking", "forking")):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["message"] == (
            f"“Parent” has a background subagent running. Wait until it ends, or stop it with Stop subagent on its "
            f"card, before {action} this conversation.")
    gate.set()
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    response = await client.post(f"/api/sessions/{parent['id']}/rewind",
                                 json={"before_event_id": first["id"], "conversation": True})
    assert response.status_code == 200, response.text


async def test_exports_keep_a_background_subagents_card_result_and_notice(runtime, monkeypatch):
    from local_agent.portability import export_bundle, fresh_copies
    manager, parent, project = runtime
    gate = asyncio.Event()
    Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), lambda: reply("Going on.")],
            {"Survey": gated(gate)})
    await run_turn(manager, parent)
    child_id = background_child(manager, parent)
    gate.set()
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    saved, child = manager.store.get(parent["id"]), manager.store.get(child_id)
    copies, root = fresh_copies(export_bundle([saved, child], parent["id"]), str(project))
    copied_parent = next(session for session in copies if session["id"] == root)
    copied_child = next(session for session in copies if session["id"] != root)
    spawning = next(event for event in copied_parent["events"] if event.get("name") == "delegate_task")
    assert spawning["delegation"]["background"] is True and spawning["delegation_result"]["status"] == "completed"
    notice = copied_parent["events"][-1]
    assert notice["subagent_id"] == copied_child["id"] and notice["subagent_status"] == "completed"
    # Without the child, the notice names no subagent.
    alone, _ = fresh_copies(export_bundle([saved], parent["id"]), str(project))
    assert "subagent_id" not in alone[0]["events"][-1]


async def test_stop_ends_a_background_subagent_still_waiting_for_its_start_hook_card(runtime, monkeypatch):
    manager, parent, _ = runtime
    parent["permission_mode"] = "acceptEdits"  # delegate_task runs without a card; the hook asks for one.
    manager.store.save(parent)
    manager.extensions.update_config({"hooks": [{"id": "brief", "event": "subagent_start", "enabled": True,
                                                 "timeout_seconds": 10, "command": "true"}]})
    Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), lambda: reply("Going on.")],
            {"Survey": forever})
    manager.start(parent["id"], "Delegate")
    await eventually(lambda: any(key[0] == parent["id"] for key in manager.pending))
    await asyncio.wait_for(manager.stop(parent["id"]), 5)  # Nothing else would end the card's wait.
    assert manager.delegates.background_running(parent["id"]) == 0 and not manager.pending
    saved = manager.store.get(parent["id"])
    assert card(saved, "a")["delegation"]["terminal_reason"] == "stopped"
    assert [event["state"] for event in saved["events"] if event.get("name") == "hook_subagent_start"] == ["cancelled"]
    assert manager.delegates.held_session(parent["id"]) is None


async def test_stop_in_an_idle_parent_cancels_the_start_hook_card_of_its_queued_subagent(runtime, monkeypatch):
    manager, parent, _ = runtime
    parent["permission_mode"] = "acceptEdits"  # delegate_task runs without a card; the hook asks for one.
    manager.store.save(parent)
    manager.extensions.update_config({"hooks": [{"id": "brief", "event": "subagent_start", "enabled": True,
                                                 "timeout_seconds": 10, "command": "true"}]})
    full = full_slots(monkeypatch)
    Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)), lambda: reply("Going on.")],
            {"Survey": forever})
    await run_turn(manager, parent)  # Queued: the turn ends while it waits for a slot.
    full["value"] = False
    manager.delegates._wake()
    await eventually(lambda: any(key[0] == parent["id"] for key in manager.pending))  # Its start hook asks.
    assert manager.statuses.get(parent["id"]) == "awaiting_approval"
    await asyncio.wait_for(manager.stop(parent["id"]), 5)
    assert manager.delegates.background_running(parent["id"]) == 0 and not manager.pending
    saved = manager.store.get(parent["id"])
    assert [event["state"] for event in saved["events"] if event.get("name") == "hook_subagent_start"] == ["cancelled"]
    assert manager.statuses.get(parent["id"], "idle") == "idle"


async def test_stop_subagent_ends_a_queued_subagent_waiting_for_its_start_hook_card_in_the_idle_chat(
        runtime, monkeypatch):
    """Stop subagent on the card (POST /api/sessions/<child>/stop) while a queued background subagent, whose slot
    freed after the turn, waits for its subagent_start card in the idle chat: the card shows cancelled, the subagent
    never starts and its card says it was stopped, as Stop on the idle chat does."""
    manager, parent, _ = runtime
    parent["permission_mode"] = "acceptEdits"  # delegate_task runs without a card; the hook asks for one.
    manager.store.save(parent)
    manager.extensions.update_config({"hooks": [{"id": "brief", "event": "subagent_start", "enabled": True,
                                                 "timeout_seconds": 10, "command": "true"}]})
    full = full_slots(monkeypatch)
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=True)),
                                    lambda: reply("Going on.")], {"Survey": forever})
    await run_turn(manager, parent)  # Queued: the turn ends while it waits for a slot.
    full["value"] = False
    manager.delegates._wake()
    await eventually(lambda: bool(waiting_cards(manager, parent["id"])))  # Its start hook asks in the idle chat.
    spawning = card(manager.get(parent["id"]), "a")
    child_id = spawning["child_session_id"]  # The card shows Stop subagent for this child.
    assert child_id and not spawning["delegation"].get("terminal_reason")
    await asyncio.wait_for(manager.stop(child_id), 5)
    assert not waiting_cards(manager, parent["id"])
    await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
    saved = manager.store.get(parent["id"])
    assert [event["state"] for event in saved["events"] if event.get("name") == "hook_subagent_start"] == ["cancelled"]
    assert card(saved, "a")["delegation_result"]["status"] == "cancelled"
    assert saved["events"][-1]["text"] == "Background subagent general was stopped: Survey"
    assert "Survey" not in gateway.requests and manager.statuses.get(parent["id"], "idle") == "idle"


@pytest.mark.parametrize("background", [False, True])
async def test_stop_subagent_ends_a_subagent_waiting_for_its_start_hook_card_in_a_running_chat(
        runtime, monkeypatch, background):
    """Stop subagent while a subagent (one the turn waits for, or a background one still starting) waits for its
    subagent_start card in the running chat: the card shows cancelled, the subagent never sends a request, and its
    call returns as cancelled while the chat's turn goes on."""
    manager, parent, _ = runtime
    parent["permission_mode"] = "acceptEdits"
    manager.store.save(parent)
    manager.extensions.update_config({"hooks": [{"id": "brief", "event": "subagent_start", "enabled": True,
                                                 "timeout_seconds": 10, "command": "true"}]})
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", background=background)),
                                    lambda: reply("Going on.")], {"Survey": forever})
    manager.start(parent["id"], "Delegate the work")
    await eventually(lambda: bool(waiting_cards(manager, parent["id"])))  # The start hook card waits.
    child_id = card(manager.get(parent["id"]), "a")["child_session_id"]
    await asyncio.wait_for(manager.stop(child_id), 5)
    assert not waiting_cards(manager, parent["id"])
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    assert [event["state"] for event in saved["events"] if event.get("name") == "hook_subagent_start"] == ["cancelled"]
    assert json.loads(card(saved, "a")["output"])["status"] == "cancelled"
    assert card(saved, "a")["delegation"]["terminal_reason"] == "stopped"
    assert saved["terminal_reason"] == "completed" and len(gateway.requests["parent"]) == 2
    assert "Survey" not in gateway.requests and manager.delegates.background_running(parent["id"]) == 0
