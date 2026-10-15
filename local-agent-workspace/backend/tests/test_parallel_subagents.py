"""Autonomous delegation: agent types, parallel delegate_task calls in one response, the
subagents index in the parent's header, and the bounded report the parent receives."""
import asyncio
import json

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.feature_tools import FEATURE_TOOLS
from local_agent.planning import DEFAULT_STEPS, MAX_RUNNING, MAX_STEPS, REPORT, bounded_result
from local_agent.portability import export_bundle, fresh_copies
from local_agent.portability_api import backup_copy
from local_agent.store import Store
from local_agent.titles import needs_title

REAL_CLIENT = httpx.AsyncClient


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("base\n")
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="", model="test-model")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-token")
    store = Store(settings.state_dir / "parallel.sqlite3")
    manager = AgentManager(store, settings)
    parent = store.create(settings.values)
    parent.update(title="Parent", title_generated=True, permission_mode="bypassPermissions")
    store.save(parent)
    yield manager, parent, project
    for session_id in list(manager.tasks):
        await manager.stop(session_id)
    await manager.jobs.shutdown()
    store.db.close()


def frame(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


def reply(text):
    return httpx.Response(200, text=frame({"content": text}, "stop") + "data: [DONE]\n\n")


def calls_reply(*calls):
    """One model response with several tool calls: (name, arguments, call_id) each."""
    parts = [{"index": index, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
             for index, (name, arguments, call_id) in enumerate(calls)]
    return httpx.Response(200, text=frame({"content": "", "tool_calls": parts}, "tool_calls") + "data: [DONE]\n\n")


def delegate(task, call_id, **extra):
    return ("delegate_task", {"task": task, **extra}, call_id)


class Gateway:
    """Streamed chat completions: the parent's scripted responses in order, and each child's by the
    task it was delegated (its first message starts with "Delegated task:\\n<task>")."""

    def __init__(self, monkeypatch, parent, children):
        self.parent, self.children = parent, children
        self.requests = {"parent": []}
        self.started = {}  # task -> asyncio.Event set when its first request arrives
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(self)))

    def child_started(self, task):
        return self.started.setdefault(task, asyncio.Event())

    async def __call__(self, request):
        body = json.loads(request.content)
        if body.get("stream") is not True:  # A title request.
            return httpx.Response(200, json={"choices": [{"message": {"content": "A title"}, "finish_reason": "stop"}]})
        first = next((m["content"] for m in body["messages"] if m["role"] == "user"), "")
        if isinstance(first, str) and first.startswith("Delegated task:\n"):
            task = first.removeprefix("Delegated task:\n").split("\n\n", 1)[0]
            seen = self.requests.setdefault(task, [])
            seen.append(body)
            self.child_started(task).set()
            return await self.children[task](len(seen), body)
        self.requests["parent"].append(body)
        return self.parent[min(len(self.requests["parent"]), len(self.parent)) - 1]()


async def eventually(condition, timeout=5):
    for _ in range(int(timeout * 100)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def run_turn(manager, parent, prompt="Delegate the work"):
    manager.start(parent["id"], prompt)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    return manager.store.get(parent["id"])


def tool_messages(wire):
    return [(message["tool_call_id"], message["content"]) for message in wire if message["role"] == "tool"]


def card(session, call_id):
    return next(event for event in session["events"] if event.get("call_id") == call_id)


# The prompt and the tool definition teach the model to delegate on its own.

def test_the_tool_definition_offers_agent_types_and_up_to_24_steps():
    definition = next(item["function"] for item in FEATURE_TOOLS if item["function"]["name"] == "delegate_task")
    properties = definition["parameters"]["properties"]
    # A string, not an enum: custom agent names differ per project and the definitions stay the same.
    assert properties["agent_type"]["type"] == "string" and "enum" not in properties["agent_type"]
    assert "or a custom agent the system message lists" in definition["description"]
    assert properties["max_steps"]["maximum"] == MAX_STEPS == 24 and DEFAULT_STEPS == 10
    for phrase in ("broad exploration", "sees nothing of this chat", "run in parallel", "not for a single small lookup"):
        assert phrase in definition["description"]


def test_the_system_prompt_says_when_to_delegate():
    from local_agent.agents import SYSTEM_PROMPT
    text = " ".join(SYSTEM_PROMPT.split())
    for phrase in ("Use delegate_task to keep this context small", "A subagent sees nothing of this chat",
                   "several delegate_task calls in one response", "never split edits that could conflict"):
        assert phrase in text


# Agent types map to tool profiles under the parent's ceiling.

@pytest.mark.parametrize("arguments,ceiling,profile,kind", [
    ({"agent_type": "explore"}, "inherit", "read_only", "explore"),
    ({"agent_type": "editor"}, "inherit", "file_editor", "editor"),
    ({"agent_type": "general"}, "inherit", "inherit", "general"),
    ({}, "inherit", "inherit", "general"),
    ({"agent_type": "general"}, "file_editor", "file_editor", "general"),
    ({"agent_type": "editor"}, "read_only", "read_only", "editor"),
    ({"tool_profile": "read_only"}, "inherit", "read_only", "explore"),
    # Both given: the narrower applies, in either direction.
    ({"agent_type": "general", "tool_profile": "file_editor"}, "inherit", "file_editor", "general"),
    ({"agent_type": "explore", "tool_profile": "inherit"}, "inherit", "read_only", "explore"),
])
async def test_agent_types_map_to_profiles_within_the_ceiling(runtime, arguments, ceiling, profile, kind):
    manager, parent, _ = runtime
    parent["subagent_tool_profile"] = ceiling
    manager.store.save(parent)
    seen = {}

    async def worker(child, prompt):
        seen.update(prompt=prompt, profile=child["tool_profile"], kind=child["agent_type"])
        await manager.event(child, "assistant", text="Done.")
    manager.run_databricks = worker
    result = await manager.delegates.delegate(parent, "Look around", **arguments)
    assert (seen["profile"], seen["kind"]) == (profile, kind)
    assert (result["tool_profile"], result["agent_type"]) == (profile, kind)
    assert REPORT in seen["prompt"] and seen["prompt"].startswith("Delegated task:\nLook around\n\n")
    assert ("explore subagent" in seen["prompt"]) == (profile == "read_only")


async def test_an_unknown_agent_type_or_too_many_steps_is_refused_before_a_child_exists(runtime):
    manager, parent, _ = runtime
    with pytest.raises(ValueError, match="agent_type"):
        await manager.delegates.delegate(parent, "Look", agent_type="planner")
    with pytest.raises(ValueError, match="1 to 24"):
        await manager.delegates.delegate(parent, "Look", max_steps=25)
    assert len(manager.store.list()) == 1


async def test_an_explore_subagent_is_offered_only_read_tools_and_runs_at_fast_on_the_chats_model(runtime, monkeypatch):
    manager, parent, _ = runtime
    parent["reasoning_effort"] = "high"
    manager.store.save(parent)
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Map the project", "a", agent_type="explore")),
                                    lambda: reply("Parent done.")],
                      {"Map the project": lambda count, body: asyncio.sleep(0, reply("Found it at a.txt:1."))})
    saved = await run_turn(manager, parent)
    body = gateway.requests["Map the project"][0]
    offered = {tool["function"]["name"] for tool in body["tools"]}
    assert "read_file" in offered and not offered & {"run_command", "write_file", "edit_file", "delegate_task"}
    child = manager.store.get(card(saved, "a")["child_session_id"])
    # Settings → Subagents: explore defaults to Fast (low); the model is the chat's.
    assert child["reasoning_effort"] == "low" and child["agent_effort"] == "low" and child["model"] == parent["model"]
    assert "agent_model" not in child
    assert child["max_steps"] == DEFAULT_STEPS
    # Named by its task until a title is generated (needs_title accepts that fallback).
    assert child["title"] == "A title" and child["title_generated"] is True
    first = child["events"][0]["text"]
    assert needs_title({"title": "Delegated task: Map the project", "events": [{"type": "user", "text": first}]})


# Parallel runs

async def test_parallel_delegations_run_together_and_answer_in_call_order(runtime, monkeypatch):
    manager, parent, project = runtime
    release = asyncio.Event()

    async def slow(count, body):
        await release.wait()
        return reply("Summary: the slow part is fine (a.txt:1).")

    async def quick(count, body):
        if count == 1:
            return calls_reply(("edit_file", {"path": "a.txt", "old_text": "base", "new_text": "quick"}, "edit"))
        return reply("Summary: changed a.txt.")
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Slow", "call-slow", agent_type="explore"),
                                                        delegate("Quick", "call-quick", agent_type="editor")),
                                    lambda: reply("Both done.")],
                      {"Slow": slow, "Quick": quick})
    manager.start(parent["id"], "Investigate and edit")
    await asyncio.wait_for(gateway.child_started("Slow").wait(), 5)
    await asyncio.wait_for(gateway.child_started("Quick").wait(), 5)
    live = manager.live[parent["id"]]
    assert manager.statuses[parent["id"]] == "delegating" and len(manager.delegates.active[parent["id"]]) == 2
    # The quick subagent's card is finished while the slow one still runs.
    await eventually(lambda: card(live, "call-quick")["state"] == "completed")
    assert card(live, "call-slow")["state"] == "running"
    assert json.loads(card(live, "call-quick")["output"])["changed_files"] == ["a.txt"]
    release.set()
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    final = gateway.requests["parent"][-1]["messages"]
    assert [call["id"] for call in final[-3]["tool_calls"]] == ["call-slow", "call-quick"]
    assert [message.get("tool_call_id") for message in final[-2:]] == ["call-slow", "call-quick"]
    results = {call_id: json.loads(output) for call_id, output in tool_messages(saved["wire"])}
    assert list(results) == ["call-slow", "call-quick"]
    slow_result, quick_result = results["call-slow"], results["call-quick"]
    assert (slow_result["status"], slow_result["agent_type"], slow_result["tool_profile"]) == ("completed", "explore", "read_only")
    assert (quick_result["status"], quick_result["agent_type"], quick_result["tool_profile"]) == ("completed", "editor", "file_editor")
    assert slow_result["output"] == "Summary: the slow part is fine (a.txt:1)."
    assert quick_result["steps_used"] == 2 and quick_result["tool_calls"] == 1
    assert slow_result["transcript"] == f"conversations/{slow_result['child_session_id']}.jsonl"
    assert (manager.store.conversations / f"{slow_result['child_session_id']}.jsonl").is_file()
    assert (project / "a.txt").read_text() == "quick\n"
    # The parent's header links each child; each child's header links back to its card.
    index = manager.store.header(parent["id"])["subagents"]
    assert [(item["id"], item["task"], item["agent_type"], item["status"]) for item in index] == [
        (slow_result["child_session_id"], "Slow", "explore", "completed"),
        (quick_result["child_session_id"], "Quick", "editor", "completed")]
    assert all(item["created"] <= item["ended"] and item["transcript"].endswith(".jsonl") for item in index)
    assert index[1]["steps_used"] == 2 and index[1]["tool_calls"] == 1
    for call_id, result in results.items():
        child = manager.store.header(result["child_session_id"])
        assert child["parent_session_id"] == parent["id"] and child["parent_event_id"] == card(saved, call_id)["id"]
        assert card(saved, call_id)["delegation"]["status"] == "completed"
    assert manager.statuses[parent["id"]] == "idle" and manager.delegates.active == {}


async def test_one_failing_subagent_leaves_the_others_and_every_call_gets_one_result(runtime, monkeypatch):
    manager, parent, _ = runtime

    async def failing(count, body):
        return httpx.Response(400, json={"error": {"message": "Bad request"}})
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Fails", "call-1"), delegate("Works", "call-2"),
                                                        ("list_tasks", {}, "call-3")),
                                    lambda: reply("Handled.")],
                      {"Fails": failing, "Works": lambda count, body: asyncio.sleep(0, reply("Worked."))})
    saved = await run_turn(manager, parent)
    messages = tool_messages(saved["wire"])
    assert [call_id for call_id, _ in messages] == ["call-1", "call-2", "call-3"]
    first, second = json.loads(messages[0][1]), json.loads(messages[1][1])
    assert first["status"] == "failed" and first["terminal_reason"] == "inference_error"
    assert second["status"] == "completed" and second["output"] == "Worked."
    assert json.loads(messages[2][1])["tasks"] == []
    assert gateway.requests["parent"][-1]["messages"][-1]["tool_call_id"] == "call-3"
    statuses = [item["status"] for item in manager.store.header(parent["id"])["subagents"]]
    assert statuses == ["failed", "completed"]


async def test_stop_during_a_parallel_run_stops_every_subagent(runtime, monkeypatch):
    manager, parent, _ = runtime

    async def forever(count, body):
        await asyncio.Event().wait()
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("One", "call-1"), delegate("Two", "call-2")),
                                    lambda: reply("Must not run.")],
                      {"One": forever, "Two": forever})
    manager.start(parent["id"], "Delegate")
    await asyncio.wait_for(gateway.child_started("One").wait(), 5)
    await asyncio.wait_for(gateway.child_started("Two").wait(), 5)
    children = [entry["child_session_id"] for entry in manager.delegates.active[parent["id"]]]
    await asyncio.wait_for(manager.stop(parent["id"]), 5)
    assert len(gateway.requests["parent"]) == 1
    assert all(manager.tasks[child].done() and manager.statuses[child] == "idle" for child in children)
    assert manager.statuses[parent["id"]] == "idle" and manager.delegates.active == {}
    saved = manager.store.get(parent["id"])
    # The stopped exchange is closed with exactly one result per call, in call order.
    assert [call_id for call_id, _ in tool_messages(saved["wire"])] == ["call-1", "call-2"]
    assert [card(saved, call_id)["state"] for call_id in ("call-1", "call-2")] == ["cancelled", "cancelled"]
    for child in children:
        assert manager.store.get(child)["delegation"]["terminal_reason"] == "stopped"
    assert [item["status"] for item in saved["subagents"]] == ["cancelled", "cancelled"]


async def test_manual_mode_asks_for_each_delegation_in_order_before_any_starts(runtime, monkeypatch):
    manager, parent, _ = runtime
    parent["permission_mode"] = "manual"
    manager.store.save(parent)
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("First", "call-1"), delegate("Second", "call-2"),
                                                        delegate("Third", "call-3")),
                                    lambda: reply("Done.")],
                      {task: (lambda count, body, task=task: asyncio.sleep(0, reply(f"{task} report.")))
                       for task in ("First", "Second", "Third")})
    manager.start(parent["id"], "Delegate three")
    live = None
    for call_id, allowed in (("call-1", True), ("call-2", False), ("call-3", True)):
        await eventually(lambda: manager.live.get(parent["id"]) and any(
            event.get("call_id") == call_id and event.get("state") == "pending" for event in manager.live[parent["id"]]["events"]))
        live = manager.live[parent["id"]]
        # One card at a time, and nothing starts before every card is answered.
        assert [event["call_id"] for event in live["events"] if event.get("state") == "pending"] == [call_id]
        assert [task for task in gateway.started if gateway.started[task].is_set()] == []
        manager.decide(parent["id"], card(live, call_id)["id"], allowed)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    messages = tool_messages(saved["wire"])
    assert [call_id for call_id, _ in messages] == ["call-1", "call-2", "call-3"]
    assert json.loads(messages[0][1])["output"] == "First report."
    assert messages[1][1].startswith("User declined this action.")
    assert json.loads(messages[2][1])["output"] == "Third report."
    assert "Second" not in gateway.requests


async def test_delegations_beyond_the_limit_wait_for_a_slot_and_then_run(runtime, monkeypatch):
    manager, parent, _ = runtime
    releases = {task: asyncio.Event() for task in ("A", "B", "C", "D")}

    async def held(task):
        async def respond(count, body):
            await releases[task].wait()
            return reply(f"{task} done.")
        return respond
    children = {task: await held(task) for task in releases}
    gateway = Gateway(monkeypatch, [lambda: calls_reply(*(delegate(task, f"call-{task}") for task in releases)),
                                    lambda: reply("All done.")], children)
    manager.start(parent["id"], "Delegate four")
    for task in ("A", "B", "C"):
        await asyncio.wait_for(gateway.child_started(task).wait(), 5)
    live = manager.live[parent["id"]]
    await eventually(lambda: (card(live, "call-D").get("delegation") or {}).get("status") == "queued")
    assert manager.delegates.running() == MAX_RUNNING and not gateway.child_started("D").is_set()
    assert "child_session_id" not in card(live, "call-D")
    releases["B"].set()
    await asyncio.wait_for(gateway.child_started("D").wait(), 5)
    for release in releases.values():
        release.set()
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    saved = manager.store.get(parent["id"])
    messages = tool_messages(saved["wire"])
    assert [call_id for call_id, _ in messages] == ["call-A", "call-B", "call-C", "call-D"]
    assert [json.loads(output)["output"] for _, output in messages] == ["A done.", "B done.", "C done.", "D done."]
    assert len(saved["subagents"]) == 4 and manager.delegates.waiters == []


async def test_a_mode_change_reaches_every_running_subagent(runtime, monkeypatch):
    manager, parent, _ = runtime

    async def forever(count, body):
        await asyncio.Event().wait()
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("One", "call-1"), delegate("Two", "call-2"))],
                      {"One": forever, "Two": forever})
    manager.start(parent["id"], "Delegate")
    await asyncio.wait_for(gateway.child_started("One").wait(), 5)
    await asyncio.wait_for(gateway.child_started("Two").wait(), 5)
    children = manager.active_children(manager.live[parent["id"]])
    assert len(children) == 2
    await manager.change_mode(manager.live[parent["id"]], "acceptEdits")
    assert [child["permission_mode"] for child in children] == ["acceptEdits", "acceptEdits"]
    await manager.set_disabled_tools(manager.live[parent["id"]], ["read_file"])
    assert [child["disabled_tools"] for child in children] == [["read_file"], ["read_file"]]
    await manager.stop(parent["id"])


# Restart recovery and the subagents index

async def test_restart_closes_parallel_delegations_and_their_index_entries(runtime, monkeypatch):
    manager, parent, _ = runtime

    async def forever(count, body):
        await asyncio.Event().wait()
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("One", "call-1"), delegate("Two", "call-2"))],
                      {"One": forever, "Two": forever})
    manager.start(parent["id"], "Delegate")
    await asyncio.wait_for(gateway.child_started("One").wait(), 5)
    await asyncio.wait_for(gateway.child_started("Two").wait(), 5)
    # What the files held at that moment, as a crash would leave them.
    ids = [parent["id"], *(entry["child_session_id"] for entry in manager.delegates.active[parent["id"]])]
    snapshot = [manager.store.get(session_id) for session_id in ids]
    assert [item["status"] for item in snapshot[0]["subagents"]] == ["running", "running"]
    await manager.stop(parent["id"])
    for session in snapshot:
        manager.store.save(session)
    restarted = AgentManager(manager.store, manager.settings)
    try:
        saved = manager.store.get(parent["id"])
        assert [item["status"] for item in saved["subagents"]] == ["interrupted", "interrupted"]
        assert [card(saved, call_id)["delegation"]["terminal_reason"] for call_id in ("call-1", "call-2")] == [
            "interrupted", "interrupted"]
        assert all(manager.store.get(child)["terminal_reason"] == "interrupted" for child in ids[1:])
    finally:
        await restarted.jobs.shutdown()


async def test_restart_closes_a_delegation_that_was_waiting_for_a_slot(runtime):
    manager, parent, _ = runtime
    parent["events"] = [{"id": "u", "type": "user", "text": "Delegate", "in_wire": True},
                        {"id": "d", "type": "tool", "name": "delegate_task", "call_id": "c", "state": "running",
                         "input": {"task": "Waits"}, "delegation": {"status": "queued", "completed_tools": 0, "agent_type": "general"}}]
    parent.update(turn_running=True, turn_ended=None)
    manager.store.save(parent)
    restarted = AgentManager(manager.store, manager.settings)
    try:
        saved = manager.store.get(parent["id"])
        assert card(saved, "c")["state"] == "cancelled"
        assert card(saved, "c")["delegation"] == {"status": "interrupted", "completed_tools": 0, "agent_type": "general",
                                                  "terminal_reason": "interrupted"}
    finally:
        await restarted.jobs.shutdown()


async def test_the_index_survives_header_only_reads_and_a_deleted_child_is_marked(runtime):
    manager, parent, _ = runtime

    async def worker(child, prompt):
        await manager.event(child, "assistant", text="Report.")
    manager.run_databricks = worker
    first = await manager.delegates.delegate(parent, "First task " + "x" * 300)
    second = await manager.delegates.delegate(parent, "Second task")
    header = manager.store.header(parent["id"])
    assert [item["id"] for item in header["subagents"]] == [first["child_session_id"], second["child_session_id"]]
    assert len(header["subagents"][0]["task"]) == 200
    manager.delegates.forget(first["child_session_id"])
    entry = manager.store.header(parent["id"])["subagents"][0]
    assert entry["deleted"] is True and entry["transcript"] is None and entry["status"] == "completed"
    manager.delegates.forget(second["child_session_id"], keep={parent["id"], second["child_session_id"]})
    assert "deleted" not in manager.store.header(parent["id"])["subagents"][1]


async def test_import_rebuilds_the_index_and_rewind_backups_and_forks_have_none(runtime):
    manager, parent, project = runtime

    async def worker(child, prompt):
        await manager.event(child, "assistant", text="Report.")
    manager.run_databricks = worker
    manager.live[parent["id"]] = parent
    spawning = await manager.event(parent, "tool", name="delegate_task", call_id="c", input={"task": "Inspect"},
                                   output="", state="running")
    result = await manager.delegates.delegate(parent, "Inspect", parent_event=spawning, agent_type="explore")
    await manager.update_event(parent, spawning, state="completed", output=json.dumps(result))
    saved = manager.store.get(parent["id"])
    child = manager.store.get(result["child_session_id"])
    bundle = export_bundle([saved, child], parent["id"])
    assert all("subagents" not in session for session in bundle["sessions"])
    copies, root = fresh_copies(bundle, str(project))
    by_id = {session["id"]: session for session in copies}
    copied_child = next(session for session in copies if session["id"] != root)
    assert by_id[root]["subagents"] == [{
        "id": copied_child["id"], "task": "Inspect", "agent_type": "explore", "status": "completed",
        "created": child["delegation"]["started"], "ended": child["delegation"]["ended"],
        "transcript": f"conversations/{copied_child['id']}.jsonl", "model": "test-model", "effort": "low"}]
    assert copied_child["agent_type"] == "explore" and copied_child["max_steps"] == DEFAULT_STEPS
    assert copied_child["agent_effort"] == copied_child["reasoning_effort"] == "low"
    fork, fork_root = fresh_copies(export_bundle([saved], parent["id"]), str(project), kind="fork")
    assert "subagents" not in fork[0]
    assert "subagents" not in backup_copy(saved)
    # Without the spawning card (a notice-started child), the task comes from the child's first message.
    for event in saved["events"]:
        event.pop("child_session_id", None)
    copies, root = fresh_copies(export_bundle([saved, child], parent["id"]), str(project))
    assert next(session for session in copies if session["id"] == root)["subagents"][0]["task"] == "Inspect"


# The report the parent receives

async def test_the_result_lists_changed_files_and_stays_within_6000_bytes(runtime):
    manager, parent, _ = runtime

    async def worker(child, prompt):
        for index in range(30):
            await manager.event(child, "tool", name="write_file", state="completed", input={"path": f"dir/{'p' * 200}{index}.txt"})
        await manager.event(child, "tool", name="move_path", state="completed", input={"path": "old.txt", "destination": "new.txt"})
        await manager.event(child, "tool", name="read_file", state="completed", input={"path": "read.txt"})
        await manager.event(child, "assistant", text="Report " + "y" * 9000)
    manager.run_databricks = worker
    result = await manager.delegates.delegate(parent, "Write many files", agent_type="editor")
    assert len(json.dumps(result, indent=2).encode("utf-8")) <= 6000
    assert result["output"].endswith("[truncated]") and result["changed_files_total"] == 32
    assert 0 < len(result["changed_files"]) <= 20 and "read.txt" not in result["changed_files"]
    assert result["tool_calls"] == 32 and result["steps_used"] == 1


def test_bounded_result_keeps_small_results_unchanged():
    result = {"child_session_id": "c", "status": "completed", "changed_files": ["a"], "output": "Short."}
    assert bounded_result(result) is result


# Review fixes: a queued delegation follows later permission changes; failures free the slot;
# a deleted child stays deleted in the index; imports keep the index order.

@pytest.mark.parametrize("change", ["manual", "deny_rule", "disabled"])
async def test_a_queued_delegation_does_not_start_after_the_chat_stops_allowing_it(runtime, monkeypatch, change):
    manager, parent, _ = runtime
    parent["permission_mode"] = "auto"
    manager.store.save(parent)
    releases = {task: asyncio.Event() for task in ("A", "B", "C", "D")}

    def held(task):
        async def respond(count, body):
            await releases[task].wait()
            return reply(f"{task} done.")
        return respond
    gateway = Gateway(monkeypatch, [lambda: calls_reply(*(delegate(task, f"call-{task}") for task in releases)),
                                    lambda: reply("All done.")], {task: held(task) for task in releases})
    manager.start(parent["id"], "Delegate four")
    for task in ("A", "B", "C"):
        await asyncio.wait_for(gateway.child_started(task).wait(), 5)
    live = manager.live[parent["id"]]
    await eventually(lambda: (card(live, "call-D").get("delegation") or {}).get("status") == "queued")
    if change == "manual":
        await manager.change_mode(live, "manual")
    elif change == "disabled":
        await manager.set_disabled_tools(live, ["delegate_task"])
    else:
        live.setdefault("allow_rules", []).append({"behavior": "deny", "tool": "delegate_task"})
        manager.store.save(live)
        await manager.rules_changed("chat", live)
    releases["A"].set()
    await eventually(lambda: card(live, "call-D").get("state") in ("error", "rejected"))
    assert not gateway.child_started("D").is_set()
    assert "did not start" in card(live, "call-D")["output"] or change == "deny_rule"
    assert card(live, "call-D")["delegation"]["status"] == "cancelled"
    for release in releases.values():
        release.set()
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    assert manager.delegates.running() == 0


async def test_a_failed_child_write_frees_the_slot(runtime, monkeypatch):
    manager, parent, _ = runtime
    original = manager.store.create

    def failing(values):
        if values.get("workspace") and "events" not in values and values is not manager.settings.values:
            raise OSError(28, "No space left on device")
        return original(values)
    monkeypatch.setattr(manager.store, "create", failing)
    with pytest.raises(OSError):
        await manager.delegates.delegate(parent, "Inspect")
    assert manager.delegates.running() == 0 and not manager.delegates.active.get(parent["id"])


async def test_an_import_lists_subagents_in_the_order_they_started(runtime):
    manager, parent, project = runtime

    async def worker(child, prompt):
        await manager.event(child, "assistant", text="Report.")
    manager.run_databricks = worker
    await manager.delegates.delegate(parent, "First")
    await asyncio.sleep(0.01)
    await manager.delegates.delegate(parent, "Second")
    saved = manager.store.get(parent["id"])
    assert [item["task"] for item in saved["subagents"]] == ["First", "Second"]
    # The export endpoint lists children newest updated first.
    children = [manager.store.get(item["id"]) for item in manager.store.list() if item.get("parent_session_id") == parent["id"]]
    copies, root = fresh_copies(export_bundle([saved, *children], parent["id"]), str(project))
    assert [item["task"] for item in next(item for item in copies if item["id"] == root)["subagents"]] == ["First", "Second"]


async def test_a_subagent_chat_deleted_while_it_runs_stays_deleted_in_the_index(runtime, monkeypatch):
    manager, parent, _ = runtime

    class SlowSocket:  # The parent chat is open in a browser, so broadcasts yield.
        async def send_json(self, data):
            await asyncio.sleep(0.01)

    async def forever(count, body):
        await asyncio.Event().wait()
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("One", "call-1"), delegate("Two", "call-2")),
                                    lambda: reply("Parent done.")],
                      {"One": forever, "Two": lambda count, body: asyncio.sleep(0, reply("Two done."))})
    manager.listeners[parent["id"]] = {SlowSocket()}
    manager.start(parent["id"], "Delegate")
    await asyncio.wait_for(gateway.child_started("One").wait(), 5)
    live = manager.live[parent["id"]]
    await eventually(lambda: card(live, "call-1").get("child_session_id"))
    child_id = card(live, "call-1")["child_session_id"]
    # What DELETE /api/sessions/<child> does for a child deleted alone.
    await manager.stop(child_id)
    manager.delegates.forget(child_id, keep=[child_id])
    manager.store.delete(child_id)
    manager.live.pop(child_id, None)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    entry = next(item for item in manager.store.header(parent["id"])["subagents"] if item["id"] == child_id)
    assert entry["deleted"] is True and entry["transcript"] is None
