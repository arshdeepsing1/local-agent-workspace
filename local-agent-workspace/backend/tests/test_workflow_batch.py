"""One approval for a whole batch of delegate_task calls (a workflow): the batch asks once in Manual and Plan
mode, allow all / decline all / review one by one answer it, and Stop, the approval wait, rule and mode changes
treat it as one waiting thing. The page-only fields (delegation_batch.approval and .origin) never reach the model."""
import asyncio
import hashlib
import json
import re

import pytest

from local_agent.agents import AgentManager

from test_parallel_subagents import (Gateway, calls_reply, card, delegate, eventually, make_runtime, reply, run_turn,
                                     tool_messages)

TASKS = ("First", "Second", "Third")
WAIT_FOR = asyncio.wait_for  # The real one; a test of the wait replaces it in the app module.


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    async for value in make_runtime(tmp_path, monkeypatch):
        yield value


def done(text):
    async def handler(count, body):
        return reply(text)
    return handler


def scripted(*tasks, first=None):
    """A gateway whose parent delegates the tasks in one response (call-1, call-2, ...) and then answers."""
    # Delegate, answer; and again for a second turn of the same chat.
    turn = [lambda: calls_reply(*[delegate(task, f"call-{index}") for index, task in enumerate(tasks, 1)]),
            lambda: reply("Done.")]
    return [*turn, *turn, *turn[:1]], {task: done(f"{task} report.") for task in tasks}


def gateway_for(monkeypatch, parent, tasks=TASKS):
    replies, children = scripted(*tasks)
    return Gateway(monkeypatch, replies, children)


def waiting(live):
    return [event for event in live["events"] if event.get("name") == "delegate_task" and event.get("state") == "pending"]


async def waiting_batch(manager, parent, size=3):
    """Wait until `size` delegate_task cards wait; returns the live chat."""
    await eventually(lambda: manager.live.get(parent["id"]) and len(waiting(manager.live[parent["id"]])) == size)
    return manager.live[parent["id"]]


def manual(manager, parent, mode="manual"):
    parent["permission_mode"] = mode
    manager.store.save(parent)


def started(gateway):
    return [task for task, flag in gateway.started.items() if flag.is_set()]


def uuid_free(text):
    return re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", text)


# What the model receives for a two-delegation batch that needs no approval (Bypass), captured from the code as
# it was before the batch approval existed: the tool list's hash and the two tool results with IDs removed.
WIRE_TOOLS_SHA256 = "742e5047d3f683d11d8533ce5aa9c098577c5d67c9ac8ce29cb346b26804ba1d"


def wire_result(report):
    return json.dumps({"child_session_id": "<id>", "agent_type": "general", "tool_profile": "inherit",
                       "status": "completed", "terminal_reason": "completed", "steps_used": 1, "tool_calls": 0,
                       "changed_files": [], "transcript": "conversations/<id>.jsonl", "output": report}, indent=2)


async def test_the_wire_of_a_batch_that_needs_no_approval_is_unchanged(runtime, monkeypatch):
    manager, parent, _ = runtime
    gateway = Gateway(monkeypatch, *scripted("One", "Two"))
    await run_turn(manager, parent)
    first, second = gateway.requests["parent"]
    tools = json.dumps(first["tools"], sort_keys=True, ensure_ascii=False).encode()
    assert json.dumps(second["tools"], sort_keys=True, ensure_ascii=False).encode() == tools
    results = [uuid_free(message["content"]) for message in second["messages"] if message["role"] == "tool"]
    assert hashlib.sha256(tools).hexdigest() == WIRE_TOOLS_SHA256
    assert results == [wire_result("One report."), wire_result("Two report.")]
    for word in ("approval", "origin", "delegation_batch", "batch"):
        assert word not in json.dumps(second["messages"][-4:]).replace("batch_", "")


# One waiting batch

async def test_three_calls_in_manual_mode_wait_as_one_batch_and_nothing_starts(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    cards = waiting(live)
    shared = cards[0]["batch_approval"]
    assert [item["call_id"] for item in cards] == ["call-1", "call-2", "call-3"]
    assert all(item["batch_approval"] == shared for item in cards)
    assert shared["size"] == 3 and shared["event_ids"] == [item["id"] for item in cards]
    assert shared["id"] == cards[0]["delegation_batch"]["id"] and re.fullmatch(r"[0-9a-f]{12}", shared["id"])
    # One wait for the whole batch, not three.
    assert [key for key in manager.pending if key[0] == parent["id"]] == [(parent["id"], shared["id"])]
    assert manager.statuses[parent["id"]] == "awaiting_approval" and started(gateway) == []
    assert all(f"Task: {task}" in item["preview"] for task, item in zip(TASKS, cards))
    assert all(item["rememberable"] == ["chat", "project", "user"] for item in cards)
    manager.decide(parent["id"], shared["id"], False)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)


async def test_allow_all_starts_the_three_in_order_and_records_the_batch_approval(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    manager.decide(parent["id"], waiting(live)[0]["batch_approval"]["id"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    cards = [card(saved, f"call-{index}") for index in (1, 2, 3)]
    assert [item["state"] for item in cards] == ["completed"] * 3 and [item["approval"] for item in cards] == ["approved"] * 3
    assert [item["delegation_batch"]["approval"] for item in cards] == ["batch"] * 3
    assert [item["delegation_batch"]["origin"] for item in cards] == ["agent"] * 3
    assert not any("batch_approval" in item or "rememberable" in item for item in saved["events"])
    results = tool_messages(saved["wire"])
    assert [call_id for call_id, _ in results] == ["call-1", "call-2", "call-3"]
    assert [json.loads(output)["output"] for _, output in results] == [f"{task} report." for task in TASKS]
    assert not manager.pending and manager.delegates.active == {}
    # The model's wire is what a batch that needed no card sends: same tools, same result shape.
    sent = gateway.requests["parent"]
    assert sent[0]["tools"] == sent[1]["tools"]
    wire = [uuid_free(output) for _, output in results]
    assert wire == [wire_result(f"{task} report.") for task in TASKS]
    assert "approval" not in json.dumps(sent[1]["messages"][-3:]) and "batch" not in json.dumps(sent[1]["messages"][-3:])


async def test_decline_all_with_a_note_starts_none_and_the_model_gets_three_declines(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    manager.decide(parent["id"], waiting(live)[0]["batch_approval"]["id"], False, note="Do it yourself")
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    cards = [card(saved, f"call-{index}") for index in (1, 2, 3)]
    assert [(item["state"], item["approval"], item["decline_note"]) for item in cards] == [
        ("rejected", "declined", "Do it yourself")] * 3
    assert [item["delegation_batch"]["approval"] for item in cards] == ["batch"] * 3
    assert started(gateway) == [] and "child_session_id" not in json.dumps(saved["events"])
    results = tool_messages(saved["wire"])
    assert [call_id for call_id, _ in results] == ["call-1", "call-2", "call-3"]
    assert all(output.startswith("User declined this action.") and "Do it yourself" in output for _, output in results)
    assert len(gateway.requests["parent"]) == 2  # The turn continued with the declines.
    assert manager.delegates.active == {} and not saved.get("subagents")


async def test_a_note_can_only_go_with_decline_all(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    batch = waiting(live)[0]["batch_approval"]["id"]
    with pytest.raises(ValueError, match="note can only be sent with Decline"):
        manager.decide(parent["id"], batch, True, note="x")
    manager.decide(parent["id"], batch, False)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)


async def test_allow_for_this_chat_from_the_batch_stores_one_rule_for_all(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    manager.decide(parent["id"], waiting(live)[0]["batch_approval"]["id"], True, scope="chat")
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    assert saved["allow_rules"] == [{"behavior": "allow", "tool": "delegate_task"}]
    assert sorted(started(gateway)) == sorted(TASKS)
    # The rule now skips the card of the next batch.
    saved = await run_turn(manager, parent, "Again")
    second = [item for item in saved["events"] if item.get("delegation_batch")][3:]
    assert [item["delegation_batch"]["approval"] for item in second] == ["none"] * 3
    assert all(item["allowed_by_rule"] for item in second)


# Review one by one

async def test_review_one_by_one_asks_today_s_cards_in_turn_and_each_is_answered_alone(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    batch = waiting(live)[0]["batch_approval"]["id"]
    manager.review_batch(parent["id"], batch)
    for call_id, allowed in (("call-1", True), ("call-2", False), ("call-3", True)):
        await eventually(lambda: card(live, call_id)["state"] == "pending" and "batch_approval" not in card(live, call_id))
        # The card now asks alone, with its own wait; the later ones still wait their turn.
        assert [key[1] for key in manager.pending if key[0] == parent["id"]] == [card(live, call_id)["id"]]
        assert started(gateway) == []
        later = [item for item in waiting(live) if item["call_id"] > call_id]
        assert all(item["batch_approval"]["review"] is True for item in later)
        manager.decide(parent["id"], card(live, call_id)["id"], allowed)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    assert [card(saved, f"call-{index}")["delegation_batch"]["approval"] for index in (1, 2, 3)] == ["single"] * 3
    assert [card(saved, f"call-{index}")["state"] for index in (1, 2, 3)] == ["completed", "rejected", "completed"]
    messages = tool_messages(saved["wire"])
    assert json.loads(messages[0][1])["output"] == "First report." and messages[1][1].startswith("User declined")
    assert "Second" not in gateway.requests


async def test_answering_one_card_by_its_event_id_falls_back_to_cards_and_answers_that_card(runtime, monkeypatch):
    """A headless client answers each pending card by its own ID: every answer works, in any order."""
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    ids = {item["call_id"]: item["id"] for item in waiting(live)}
    manager.decide(parent["id"], ids["call-3"], False, note="Skip the third")
    await eventually(lambda: card(live, "call-1")["state"] == "pending" and "batch_approval" not in card(live, "call-1"))
    manager.decide(parent["id"], ids["call-2"], True)  # Its turn has not come: the answer waits for it.
    manager.decide(parent["id"], ids["call-1"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    assert [card(saved, f"call-{index}")["state"] for index in (1, 2, 3)] == ["completed", "completed", "rejected"]
    assert card(saved, "call-3")["decline_note"] == "Skip the third"
    assert [card(saved, f"call-{index}")["delegation_batch"]["approval"] for index in (1, 2, 3)] == ["single"] * 3
    assert sorted(started(gateway)) == ["First", "Second"]


async def test_a_lone_delegate_task_asks_its_own_card_with_no_batch(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    Gateway(monkeypatch, *scripted("Alone"))
    manager.start(parent["id"], "Delegate one")
    live = await waiting_batch(manager, parent, 1)
    only = waiting(live)[0]
    assert "batch_approval" not in only and "delegation_batch" not in only
    assert [key[1] for key in manager.pending if key[0] == parent["id"]] == [only["id"]]
    manager.decide(parent["id"], only["id"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    assert "delegation_batch" not in card(manager.store.get(parent["id"]), "call-1")


# Permission modes and rules

@pytest.mark.parametrize("mode", ["acceptEdits", "auto", "bypassPermissions"])
async def test_modes_that_allow_subagents_show_no_card_and_record_approval_none(runtime, monkeypatch, mode):
    manager, parent, _ = runtime
    manual(manager, parent, mode)
    gateway_for(monkeypatch, parent)
    saved = await run_turn(manager, parent)
    cards = [card(saved, f"call-{index}") for index in (1, 2, 3)]
    assert [item["delegation_batch"]["approval"] for item in cards] == ["none"] * 3
    assert [item["state"] for item in cards] == ["completed"] * 3
    assert not any("batch_approval" in item or item.get("approval") for item in saved["events"])


async def test_plan_mode_asks_for_subagents_as_it_does_per_call_so_the_batch_asks_once(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent, "plan")
    gateway = gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    assert [key[1] for key in manager.pending if key[0] == parent["id"]] == [waiting(live)[0]["batch_approval"]["id"]]
    assert started(gateway) == []
    manager.decide(parent["id"], waiting(live)[0]["batch_approval"]["id"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    assert sorted(started(gateway)) == sorted(TASKS)


async def test_dont_ask_refuses_each_call_unless_a_rule_allows(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent, "dontAsk")
    gateway = gateway_for(monkeypatch, parent)
    saved = await run_turn(manager, parent)
    assert [card(saved, f"call-{index}")["state"] for index in (1, 2, 3)] == ["rejected"] * 3
    assert all(output.startswith("Not run: Don't ask mode") for _, output in tool_messages(saved["wire"]))
    assert started(gateway) == [] and not manager.pending
    parent["allow_rules"] = [{"behavior": "allow", "tool": "delegate_task"}]
    manager.store.save(parent)
    saved = await run_turn(manager, parent)
    assert sorted(started(gateway)) == sorted(TASKS)


async def test_an_allow_rule_for_subagents_skips_the_batch_card(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    parent["allow_rules"] = [{"behavior": "allow", "tool": "delegate_task"}]
    manager.store.save(parent)
    gateway_for(monkeypatch, parent)
    saved = await run_turn(manager, parent)
    cards = [card(saved, f"call-{index}") for index in (1, 2, 3)]
    assert [item["delegation_batch"]["approval"] for item in cards] == ["none"] * 3
    assert all(item["allowed_by_rule"] for item in cards)


async def test_a_call_a_rule_denies_is_refused_and_the_other_two_are_asked_as_a_batch(runtime, monkeypatch):
    from local_agent.permissions import Verdict
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    original = manager.permission

    def permission(session, tools, name, arguments):
        if name == "delegate_task" and arguments.get("task") == "Second":
            return Verdict("deny", "rule", "Not run: a rule denies this subagent.")
        return original(session, tools, name, arguments)
    monkeypatch.setattr(manager, "permission", permission)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent, 2)
    cards = waiting(live)
    assert [item["call_id"] for item in cards] == ["call-1", "call-3"]
    shared = cards[0]["batch_approval"]
    assert shared["size"] == 2 and shared["event_ids"] == [item["id"] for item in cards] and cards[1]["batch_approval"] == shared
    assert card(live, "call-2")["state"] == "rejected" and "batch_approval" not in card(live, "call-2")
    manager.decide(parent["id"], shared["id"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    assert [card(saved, f"call-{index}")["delegation_batch"]["approval"] for index in (1, 2, 3)] == ["batch", "none", "batch"]
    assert sorted(started(gateway)) == ["First", "Third"]
    assert tool_messages(saved["wire"])[1][1] == "Not run: a rule denies this subagent."


# Stop, the wait, rules and modes while the batch waits

async def test_stop_while_the_batch_waits_cancels_all_cards_and_leaves_nothing_started(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    await waiting_batch(manager, parent)
    await asyncio.wait_for(manager.stop(parent["id"]), 5)
    saved = manager.store.get(parent["id"])
    assert [card(saved, f"call-{index}")["state"] for index in (1, 2, 3)] == ["cancelled"] * 3
    assert not any("batch_approval" in item for item in saved["events"])
    assert not manager.pending and not manager.pending_checks
    assert started(gateway) == [] and manager.delegates.active == {} and manager.delegates.running() == 0
    assert not saved.get("subagents") and len(gateway.requests["parent"]) == 1
    assert [call_id for call_id, _ in tool_messages(saved["wire"])] == ["call-1", "call-2", "call-3"]
    assert manager.statuses[parent["id"]] == "idle"


async def test_the_approval_wait_expires_the_whole_batch_once(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    original = asyncio.wait_for
    timeouts = []

    async def wait_for(awaitable, timeout=None):
        timeouts.append(timeout)
        if timeout == 3600:
            raise TimeoutError
        return await original(awaitable, timeout)
    monkeypatch.setattr("local_agent.agents.asyncio.wait_for", wait_for)
    gateway = gateway_for(monkeypatch, parent)
    saved = await run_turn(manager, parent)
    assert timeouts.count(3600) == 1
    cards = [card(saved, f"call-{index}") for index in (1, 2, 3)]
    assert [(item["state"], item["approval"]) for item in cards] == [("rejected", "expired")] * 3
    assert [item["delegation_batch"]["approval"] for item in cards] == ["batch"] * 3
    assert all("did not answer this approval request in time" in output for _, output in tool_messages(saved["wire"]))
    assert started(gateway) == [] and not manager.pending


async def test_the_batch_shows_when_it_expires(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    expires = {item["approval_expires"] for item in waiting(live)}
    assert len(expires) == 1  # One timer for the batch.
    manager.decide(parent["id"], waiting(live)[0]["batch_approval"]["id"], False)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)


async def test_a_deny_rule_added_while_the_batch_waits_refuses_the_cards(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    live.setdefault("allow_rules", []).append({"behavior": "deny", "tool": "delegate_task"})
    await manager.rules_changed("chat", live)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    cards = [card(saved, f"call-{index}") for index in (1, 2, 3)]
    assert [(item["state"], item["approval"]) for item in cards] == [("rejected", "blocked")] * 3
    assert started(gateway) == [] and not manager.pending
    assert len({output for _, output in tool_messages(saved["wire"])}) == 1
    assert all(output == card(saved, "call-1")["blocked_reason"] for _, output in tool_messages(saved["wire"]))
    assert "deny rule" in card(saved, "call-1")["blocked_reason"]


async def test_a_mode_that_allows_subagents_releases_the_batch(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    await manager.change_mode(live, "auto")
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    cards = [card(saved, f"call-{index}") for index in (1, 2, 3)]
    assert [item["state"] for item in cards] == ["completed"] * 3
    assert [item["approved_by_mode"] for item in cards] == ["auto"] * 3
    assert [item["delegation_batch"]["approval"] for item in cards] == ["none"] * 3
    assert sorted(started(gateway)) == sorted(TASKS)


async def test_a_mode_that_still_asks_leaves_the_batch_waiting(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    await manager.change_mode(live, "plan")
    assert len(waiting(live)) == 3 and all("batch_approval" in item for item in waiting(live))
    manager.decide(parent["id"], waiting(live)[0]["batch_approval"]["id"], False)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)


# Hooks and later checks

async def test_before_tool_hooks_run_per_call_in_order_after_the_batch_is_allowed(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    seen = []

    async def hooks(session, name, arguments, definition, call_id, fixed=False):
        if name != "delegate_task":
            return {"arguments": arguments, "forced": None, "contexts": [], "refusal": None, "changed_by": []}
        seen.append((call_id, card(session, call_id)["state"], bool(manager.pending)))
        if arguments["task"] == "Second":
            return {"arguments": arguments, "forced": None, "contexts": [], "changed_by": [],
                    "refusal": "Blocked by the before-tool hook h: no.", "blocked": {"hook_id": "h", "reason": "no."}}
        return {"arguments": arguments, "forced": None, "contexts": [], "refusal": None, "changed_by": []}
    monkeypatch.setattr(manager, "before_tool_hooks", hooks)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    assert seen == []  # No hook runs before the decision.
    manager.decide(parent["id"], waiting(live)[0]["batch_approval"]["id"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    # Per call and in order, once the batch is decided: no card waits then.
    assert seen == [("call-1", "running", False), ("call-2", "running", False), ("call-3", "running", False)]
    saved = manager.store.get(parent["id"])
    assert [card(saved, f"call-{index}")["state"] for index in (1, 2, 3)] == ["completed", "rejected", "completed"]
    assert card(saved, "call-2")["hook_blocked"] == {"hook_id": "h", "reason": "no."}
    assert sorted(started(gateway)) == ["First", "Third"]


async def test_a_hook_that_asks_shows_its_own_card_after_the_batch_is_allowed(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)

    async def hooks(session, name, arguments, definition, call_id, fixed=False):
        forced = {"hook_id": "h", "reason": "check"} if arguments.get("task") == "Third" else None
        return {"arguments": arguments, "forced": forced, "contexts": [], "refusal": None, "changed_by": []}
    monkeypatch.setattr(manager, "before_tool_hooks", hooks)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    manager.decide(parent["id"], waiting(live)[0]["batch_approval"]["id"], True)
    await eventually(lambda: card(live, "call-3")["state"] == "pending" and "batch_approval" not in card(live, "call-3"))
    assert [item["call_id"] for item in waiting(live)] == ["call-3"] and started(gateway) == []
    manager.decide(parent["id"], card(live, "call-3")["id"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    assert sorted(started(gateway)) == sorted(TASKS)


# Origin

def write_file(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.mark.parametrize("prompt, origin", [("/workflow check the three parts", "user"),
                                             ("/skill workflow check the three parts", "user"),
                                             ("/parts the three things", "user"),
                                             ("Check the three parts", "agent"),
                                             ("/other the three things", "agent")])
async def test_origin_is_user_for_the_workflow_skill_or_a_command_file_and_agent_otherwise(
        runtime, monkeypatch, prompt, origin):
    manager, parent, project = runtime
    write_file(project / ".agents/skills/workflow/SKILL.md", "---\nname: workflow\ndescription: Run a workflow.\n---\nSplit it.\n")
    write_file(project / ".agents/skills/other/SKILL.md", "---\nname: other\ndescription: Another skill.\n---\nDo it.\n")
    write_file(project / ".agents/commands/parts.md", "---\ndescription: Parts.\n---\nDo this: $ARGUMENTS\n")
    gateway_for(monkeypatch, parent)
    saved = await run_turn(manager, parent, prompt)
    cards = [card(saved, f"call-{index}") for index in (1, 2, 3)]
    assert [item["delegation_batch"]["origin"] for item in cards] == [origin] * 3


async def test_the_origin_follows_the_turns_own_message_not_an_earlier_one(runtime, monkeypatch):
    manager, parent, project = runtime
    write_file(project / ".agents/skills/workflow/SKILL.md", "---\nname: workflow\ndescription: Run a workflow.\n---\nSplit it.\n")
    gateway_for(monkeypatch, parent)
    await run_turn(manager, parent, "/workflow first")
    saved = await run_turn(manager, parent, "Plain follow-up")
    origins = [item["delegation_batch"]["origin"] for item in saved["events"] if item.get("delegation_batch")]
    assert origins == ["user"] * 3 + ["agent"] * 3


def test_the_review_route_is_registered_next_to_the_decision_route(tmp_path, monkeypatch):
    from local_agent.api import create_app
    from local_agent.config import Settings
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    app = create_app(Settings(tmp_path / "state"))
    paths = {route.path for route in app.routes}
    assert "/api/sessions/{session_id}/approvals/{event_id}" in paths
    assert "/api/sessions/{session_id}/approvals/{batch_id}/review" in paths


def test_a_decision_for_an_unknown_batch_is_refused(tmp_path):
    from local_agent.config import Settings
    from local_agent.store import Store
    settings = Settings(tmp_path / "state")
    manager = AgentManager(Store(settings.state_dir / "x.sqlite3"), settings)
    with pytest.raises(ValueError, match="no longer pending"):
        manager.decide("s", "b", True)
    with pytest.raises(ValueError, match="no longer pending"):
        manager.review_batch("s", "b")


# Export, fork and import of a chat that ran a batch

def transfer_client(manager):
    """A client for the export, import and fork routes of this manager (the gateway stub replaces httpx's client)."""
    import httpx
    from fastapi import FastAPI, HTTPException
    from local_agent.portability_api import register_portability
    from test_parallel_subagents import REAL_CLIENT

    def session_or_404(session_id):
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, "Conversation not found.")
        return session
    app = FastAPI()
    register_portability(app, manager, manager.settings, manager.store, session_or_404)
    return REAL_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def batch_cards(session):
    return [item for item in session["events"] if item.get("delegation_batch")]


def only_the_bundle_fields(bundle):
    from local_agent.portability import parse_bundle
    cards = [item for record in bundle["sessions"] for item in record["events"] if item.get("delegation_batch")]
    assert cards and all(set(item["delegation_batch"]) == {"id", "index", "size"} for item in cards)
    assert not any("batch_approval" in item for record in bundle["sessions"] for item in record["events"])
    parse_bundle(json.dumps(bundle).encode())  # The bundle validates as an older version reads it.


async def batch_chat(manager, parent, monkeypatch, mode):
    """A chat that ran two parallel batches: in Bypass (cards carry approval none), or in Manual with Allow all
    (approval batch). Returns the saved chat."""
    manual(manager, parent, mode)
    gateway_for(monkeypatch, parent)
    for prompt in ("Delegate three", "Delegate again"):
        manager.start(parent["id"], prompt)
        if mode == "manual":
            live = await waiting_batch(manager, parent)
            manager.decide(parent["id"], waiting(live)[0]["batch_approval"]["id"], True)
        await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    return manager.store.get(parent["id"])


@pytest.mark.parametrize("mode, approval", [("bypassPermissions", "none"), ("manual", "batch")])
async def test_export_and_fork_work_for_a_chat_that_ran_a_batch_and_keep_only_the_bundle_fields(
        runtime, monkeypatch, tmp_path, mode, approval):
    manager, parent, project = runtime
    saved = await batch_chat(manager, parent, monkeypatch, mode)
    cards = batch_cards(saved)
    assert len(cards) == 6 and {item["delegation_batch"]["approval"] for item in cards} == {approval}
    assert {item["delegation_batch"]["origin"] for item in cards} == {"agent"}
    destination = tmp_path / "destination"
    destination.mkdir()
    async with transfer_client(manager) as client:
        for query in ("", "?include_children=false"):
            response = await client.get(f"/api/sessions/{parent['id']}/export{query}")
            assert response.status_code == 200, response.text
            bundle = response.json()
            only_the_bundle_fields(bundle)
            assert len(bundle["sessions"]) == (1 if query else 7)
            imported = await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)
            assert imported.status_code == 201, imported.text
            copied = manager.store.get(imported.json()["session"]["id"])
            assert [item["delegation_batch"] for item in batch_cards(copied)] == [
                {key: item["delegation_batch"][key] for key in ("id", "index", "size")} for item in cards]
        forked = await client.post(f"/api/sessions/{parent['id']}/fork")
        assert forked.status_code == 201, forked.text
        assert forked.json()["children_copied"] == 6
        copy = manager.store.get(forked.json()["id"])
        assert len(batch_cards(copy)) == 6 and all(set(item["delegation_batch"]) == {"id", "index", "size"}
                                                    for item in batch_cards(copy))
        second = [item for item in saved["events"] if item.get("type") == "user"][1]
        here = await client.post(f"/api/sessions/{parent['id']}/fork", json={"before_event_id": second["id"]})
        assert here.status_code == 201, here.text
        cut = manager.store.get(here.json()["id"])
        assert len(batch_cards(cut)) == 3 and here.json()["children_copied"] == 3
    # The chat itself still shows how its calls were approved.
    assert {item["delegation_batch"]["approval"] for item in batch_cards(manager.store.get(parent["id"]))} == {approval}


async def test_a_chat_that_waits_for_its_batch_is_busy_for_export_and_fork_and_a_saved_waiting_batch_imports_cancelled(
        runtime, monkeypatch, tmp_path):
    manager, parent, project = runtime
    manual(manager, parent)
    gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    destination = tmp_path / "destination"
    destination.mkdir()
    async with transfer_client(manager) as client:
        # A waiting chat is busy, as for any waiting card: export and fork wait until it is answered.
        assert (await client.get(f"/api/sessions/{parent['id']}/export")).status_code == 409
        assert (await client.post(f"/api/sessions/{parent['id']}/fork")).status_code == 409
        # A copy of such a chat that was saved while waiting (a crash, a copy of the file) still exports.
        from local_agent.portability import export_bundle
        snapshot = json.loads(json.dumps(live))
        bundle = export_bundle([snapshot], snapshot["id"])
        only_the_bundle_fields(bundle)
        assert [item["state"] for item in bundle["sessions"][0]["events"] if item.get("delegation_batch")] == ["pending"] * 3
        imported = await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)
        assert imported.status_code == 201, imported.text
        copied = imported.json()["session"]
        assert [item["state"] for item in batch_cards(copied)] == ["cancelled"] * 3
        assert not any("batch_approval" in item for item in copied["events"])
        assert not [key for key in manager.pending if key[0] == copied["id"]]
    manager.decide(parent["id"], waiting(live)[0]["batch_approval"]["id"], False)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)


# A hook that changes a call's arguments after the batch was allowed

def plain(arguments, **extra):
    return {"arguments": arguments, "forced": None, "contexts": [], "refusal": None, "changed_by": [], **extra}


def rewrite_second(manager, monkeypatch, new, seen=None):
    """A before-tool hook that replaces the arguments of the call whose task is Second."""
    async def hooks(session, name, arguments, definition, call_id, fixed=False):
        if name == "delegate_task" and arguments.get("task") == "Second":
            if seen is not None:
                seen.append(call_id)
            return plain(new, changed_by=["rewriter"])
        return plain(arguments)
    monkeypatch.setattr(manager, "before_tool_hooks", hooks)


def run_result(saved, call_id):
    text = dict(tool_messages(saved["wire"]))[call_id]  # A hook's note about changed arguments may come first.
    return json.loads(re.search(r"^\{\n.*\}\Z", text, re.S | re.M).group(0))


async def allow_all_and_wait_for_second(manager, parent):
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    manager.decide(parent["id"], waiting(live)[0]["batch_approval"]["id"], True)
    await eventually(lambda: card(live, "call-2")["state"] == "pending" and "batch_approval" not in card(live, "call-2"))
    return live


async def test_a_hook_that_rewrites_a_call_after_allow_all_asks_on_that_calls_own_card_and_runs_what_it_shows(
        runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    rewrite_second(manager, monkeypatch, {"task": "EVIL", "agent_type": "explore"})
    live = await allow_all_and_wait_for_second(manager, parent)
    second = card(live, "call-2")
    # Its own card shows the arguments that will run, and what the hook changed.
    assert second["input"] == {"task": "EVIL", "agent_type": "explore"}
    assert "explore" in second["preview"] and "Task: EVIL" in second["preview"] and "read-only" in second["preview"]
    assert second["hook_change"]["hooks"] == ["rewriter"] and second["hook_change"]["original"]["task"] == "Second"
    assert "approval" not in second and "approved_by_mode" not in second and second["state"] == "pending"
    assert [key[1] for key in manager.pending if key[0] == parent["id"]] == [second["id"]]
    assert started(gateway) == []
    # The call after it waits, approved, behind it.
    assert card(live, "call-3")["state"] == "pending" and card(live, "call-3")["batch_decided"] == "allowed"
    manager.decide(parent["id"], second["id"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    assert sorted(started(gateway)) == ["EVIL", "First", "Third"]  # Never the task the batch card showed.
    cards = [card(saved, f"call-{index}") for index in (1, 2, 3)]
    assert [item["delegation_batch"]["approval"] for item in cards] == ["batch", "single", "batch"]
    assert [item["approval"] for item in cards] == ["approved"] * 3
    assert run_result(saved, "call-2")["agent_type"] == "explore" and run_result(saved, "call-2")["tool_profile"] == "read_only"
    assert [run_result(saved, f"call-{index}")["agent_type"] for index in (1, 3)] == ["general", "general"]
    child = manager.store.get(cards[1]["child_session_id"])
    assert (child["agent_type"], child["tool_profile"]) == ("explore", "read_only")
    assert cards[1]["input"]["agent_type"] == "explore"
    assert not any("batch_decided" in item or "batch_approval" in item for item in saved["events"])


async def test_declining_the_card_of_a_call_a_hook_rewrote_stops_only_that_call(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    rewrite_second(manager, monkeypatch, {"task": "EVIL"})
    live = await allow_all_and_wait_for_second(manager, parent)
    manager.decide(parent["id"], card(live, "call-2")["id"], False)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    assert [card(saved, f"call-{index}")["state"] for index in (1, 2, 3)] == ["completed", "rejected", "completed"]
    assert sorted(started(gateway)) == ["First", "Third"]
    assert card(saved, "call-2")["approval"] == "declined" and card(saved, "call-2")["delegation_batch"]["approval"] == "single"


async def test_a_hook_that_narrows_the_tool_profile_of_a_call_runs_it_narrowed_after_its_own_approval(
        runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway_for(monkeypatch, parent)
    rewrite_second(manager, monkeypatch, {"task": "Second", "tool_profile": "read_only"})
    live = await allow_all_and_wait_for_second(manager, parent)
    assert "Tool profile: read_only" in card(live, "call-2")["preview"] and "read-only" in card(live, "call-2")["preview"]
    manager.decide(parent["id"], card(live, "call-2")["id"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    assert run_result(saved, "call-2")["tool_profile"] == "read_only"
    assert [run_result(saved, f"call-{index}")["tool_profile"] for index in (1, 3)] == ["inherit", "inherit"]


async def test_a_hook_that_changes_only_the_step_limit_asks_again_with_the_new_limit(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway_for(monkeypatch, parent)
    rewrite_second(manager, monkeypatch, {"task": "Second", "max_steps": 3})
    live = await allow_all_and_wait_for_second(manager, parent)
    assert "at most 3 steps" in card(live, "call-2")["preview"] and card(live, "call-2")["input"]["max_steps"] == 3
    manager.decide(parent["id"], card(live, "call-2")["id"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    assert run_result(saved, "call-2")["status"] == "completed"
    assert card(saved, "call-2")["delegation_batch"]["approval"] == "single"


async def test_a_hook_that_leaves_the_arguments_unchanged_keeps_the_batch_approval_and_shows_no_extra_card(
        runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)

    async def hooks(session, name, arguments, definition, call_id, fixed=False):
        return plain(arguments, contexts=["Checked by a hook."] if name == "delegate_task" else [])
    monkeypatch.setattr(manager, "before_tool_hooks", hooks)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    manager.decide(parent["id"], waiting(live)[0]["batch_approval"]["id"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    assert [card(saved, f"call-{index}")["delegation_batch"]["approval"] for index in (1, 2, 3)] == ["batch"] * 3
    assert sorted(started(gateway)) == sorted(TASKS)
    assert not any("hook_change" in item for item in saved["events"])


async def test_a_hook_that_rewrites_a_call_the_mode_then_allows_runs_it_without_a_card(runtime, monkeypatch):
    """The new arguments are checked as a lone call's are: a rule that allows them needs no card."""
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    rewrite_second(manager, monkeypatch, {"task": "EVIL", "agent_type": "explore"})
    original = manager.hooked_permission

    def hooked_permission(session, tools, name, arguments, forced=None):
        if name == "delegate_task" and arguments.get("task") == "EVIL":
            from local_agent.permissions import Verdict
            return Verdict("allow", "rule", scope="chat")
        return original(session, tools, name, arguments, forced)
    monkeypatch.setattr(manager, "hooked_permission", hooked_permission)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    manager.decide(parent["id"], waiting(live)[0]["batch_approval"]["id"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    second = card(saved, "call-2")
    assert second["delegation_batch"]["approval"] == "none" and second["allowed_by_rule"] is True
    assert "approval" not in second and "hook_change" in second
    assert run_result(saved, "call-2")["agent_type"] == "explore"
    assert sorted(started(gateway)) == ["EVIL", "First", "Third"]


# A decided batch no longer shows an undecided approval

def undecided(item):
    approval = item.get("batch_approval")
    return bool(approval) and approval.get("review") is not True


async def test_calls_that_wait_behind_a_hook_card_after_allow_all_are_decided_and_say_they_wait(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway_for(monkeypatch, parent)

    async def hooks(session, name, arguments, definition, call_id, fixed=False):
        forced = {"hook_id": "h", "reason": "check"} if arguments.get("task") == "First" else None
        return plain(arguments, forced=forced)
    monkeypatch.setattr(manager, "before_tool_hooks", hooks)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    batch = waiting(live)[0]["batch_approval"]["id"]
    manager.decide(parent["id"], batch, True)
    await eventually(lambda: card(live, "call-1")["state"] == "pending" and "batch_approval" not in card(live, "call-1"))
    assert not any(undecided(item) for item in live["events"])
    for call_id in ("call-2", "call-3"):
        queued = card(live, call_id)
        assert queued["state"] == "pending" and queued["batch_decided"] == "allowed"
        assert "approval_expires" not in queued and "rememberable" not in queued and "batch_approval" not in queued
    assert "batch_decided" not in card(live, "call-1")
    with pytest.raises(ValueError, match="no longer pending"):
        manager.decide(parent["id"], batch, True)
    with pytest.raises(ValueError, match="no longer pending"):  # A queued card has no card of its own to answer yet.
        manager.decide(parent["id"], card(live, "call-2")["id"], True)
    manager.decide(parent["id"], card(live, "call-1")["id"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    assert [card(saved, f"call-{index}")["state"] for index in (1, 2, 3)] == ["completed"] * 3
    assert not any("batch_decided" in item for item in saved["events"])


async def test_a_rule_that_settles_one_call_leaves_the_others_to_ask_alone_and_no_card_keeps_the_dead_batch(
        runtime, monkeypatch):
    from local_agent.permissions import Verdict
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    original, rule = manager.hooked_permission, {"on": False}

    def hooked_permission(session, tools, name, arguments, forced=None):
        if rule["on"] and name == "delegate_task" and arguments.get("task") == "Second":
            return Verdict("allow", "rule", scope="chat")
        return original(session, tools, name, arguments, forced)
    monkeypatch.setattr(manager, "hooked_permission", hooked_permission)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    batch = waiting(live)[0]["batch_approval"]["id"]
    rule["on"] = True
    await manager.recheck_pending(live, "rules")
    await eventually(lambda: card(live, "call-1")["state"] == "pending" and "batch_approval" not in card(live, "call-1"))
    assert not any(undecided(item) for item in live["events"])
    assert card(live, "call-2")["batch_decided"] == "allowed" and card(live, "call-3")["batch_decided"] == "ask"
    with pytest.raises(ValueError, match="no longer pending"):
        manager.decide(parent["id"], batch, True)
    manager.decide(parent["id"], card(live, "call-1")["id"], True)
    await eventually(lambda: card(live, "call-3")["state"] == "pending" and "batch_decided" not in card(live, "call-3"))
    manager.decide(parent["id"], card(live, "call-3")["id"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    assert [card(saved, f"call-{index}")["delegation_batch"]["approval"] for index in (1, 2, 3)] == ["single", "none", "single"]
    assert sorted(started(gateway)) == sorted(TASKS)


@pytest.mark.parametrize("decision", ["allow", "decline", "review", "expire"])
async def test_no_broadcast_after_a_batch_decision_carries_an_undecided_batch_approval(runtime, monkeypatch, decision):
    manager, parent, _ = runtime
    manual(manager, parent)
    if decision == "expire":
        original = asyncio.wait_for

        async def wait_for(awaitable, timeout=None):
            if timeout == 3600:
                raise TimeoutError
            return await original(awaitable, timeout)
        monkeypatch.setattr("local_agent.agents.asyncio.wait_for", wait_for)
    gateway_for(monkeypatch, parent)
    sent = []
    broadcast = manager.broadcast

    async def recording(session_id, message):
        if message.get("type") == "event" and message["event"].get("name") == "delegate_task":
            sent.append(json.loads(json.dumps(message["event"])))
        return await broadcast(session_id, message)
    monkeypatch.setattr(manager, "broadcast", recording)
    manager.start(parent["id"], "Delegate three")
    if decision == "expire":
        await asyncio.wait_for(manager.tasks[parent["id"]], 10)
        decided = next(index for index, item in enumerate(sent) if item.get("approval") == "expired")
        sent = sent[max(0, decided - 2):]  # The decision's own broadcasts.
        after = [item for item in sent if item.get("approval") != "expired"]
    else:
        live = await waiting_batch(manager, parent)
        batch = waiting(live)[0]["batch_approval"]["id"]
        mark = len(sent)
        if decision == "review":
            manager.review_batch(parent["id"], batch)
            await eventually(lambda: card(live, "call-1")["state"] == "pending" and "batch_approval" not in card(live, "call-1"))
            manager.decide(parent["id"], card(live, "call-1")["id"], False)
            manager.decide(parent["id"], card(live, "call-2")["id"], False)
            manager.decide(parent["id"], card(live, "call-3")["id"], False)
        else:
            manager.decide(parent["id"], batch, decision == "allow")
        await asyncio.wait_for(manager.tasks[parent["id"]], 10)
        after = sent[mark:]
    assert after and not any(undecided(item) for item in after)


# Per-card answers sent in one tick, and what a rule records

async def test_three_card_answers_sent_in_the_same_tick_are_all_applied_each_to_its_own_card(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    ids = {item["call_id"]: item["id"] for item in waiting(live)}
    # No await between the three calls, as a client that answers all its cards at once.
    manager.decide(parent["id"], ids["call-3"], True)
    manager.decide(parent["id"], ids["call-2"], False, note="Not this one")
    manager.decide(parent["id"], ids["call-1"], True)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    assert [card(saved, f"call-{index}")["state"] for index in (1, 2, 3)] == ["completed", "rejected", "completed"]
    assert card(saved, "call-2")["decline_note"] == "Not this one" and "decline_note" not in card(saved, "call-3")
    assert sorted(started(gateway)) == ["First", "Third"]
    assert [card(saved, f"call-{index}")["delegation_batch"]["approval"] for index in (1, 2, 3)] == ["single"] * 3


async def test_a_card_cannot_be_answered_twice_before_its_turn(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    third = card(live, "call-3")["id"]
    manager.decide(parent["id"], third, False)
    with pytest.raises(ValueError, match="no longer pending"):
        manager.decide(parent["id"], third, True)
    manager.decide(parent["id"], card(live, "call-1")["id"], False)
    manager.decide(parent["id"], card(live, "call-2")["id"], False)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)


async def test_stop_clears_the_answers_stored_for_cards_that_had_not_had_their_turn(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    ids = {item["call_id"]: item["id"] for item in waiting(live)}
    approval = next(iter(manager.batches.values()))
    manager.decide(parent["id"], ids["call-3"], True)
    manager.decide(parent["id"], ids["call-2"], True)
    assert set(approval.answers) == {ids["call-2"], ids["call-3"]}
    await asyncio.wait_for(manager.stop(parent["id"]), 5)
    assert approval.answers == {} and not manager.batches
    with pytest.raises(ValueError, match="no longer pending"):
        manager.decide(parent["id"], ids["call-3"], True)
    assert started(gateway) == [] and not manager.pending


async def test_a_call_a_rule_allows_during_review_records_no_card_and_a_shown_card_records_single(runtime, monkeypatch):
    manager, parent, _ = runtime
    manual(manager, parent)
    gateway = gateway_for(monkeypatch, parent)
    manager.start(parent["id"], "Delegate three")
    live = await waiting_batch(manager, parent)
    manager.review_batch(parent["id"], waiting(live)[0]["batch_approval"]["id"])
    await eventually(lambda: card(live, "call-1")["state"] == "pending" and "batch_approval" not in card(live, "call-1"))
    live.setdefault("allow_rules", []).append({"behavior": "allow", "tool": "delegate_task"})
    await manager.rules_changed("chat", live)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    # Call 1 showed its card (a rule then answered it); calls 2 and 3 never showed one.
    assert [card(saved, f"call-{index}")["delegation_batch"]["approval"] for index in (1, 2, 3)] == ["single", "none", "none"]
    assert sorted(started(gateway)) == sorted(TASKS)
