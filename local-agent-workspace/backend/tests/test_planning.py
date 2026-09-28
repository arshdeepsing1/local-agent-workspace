import asyncio
import json
import uuid

import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.planning import DelegateManager, TaskManager
from local_agent.store import Store


@pytest.fixture
def planning(tmp_path):
    store = Store(tmp_path / "tasks.sqlite3")
    first = store.create({"workspace": str(tmp_path), "model": "synthetic-model"})
    second = store.create({"workspace": str(tmp_path), "model": "synthetic-model"})
    yield store, TaskManager(store), first, second
    store.db.close()


def test_tasks_persist_without_changing_conversation_data(planning):
    store, tasks, first, _ = planning
    before = store.get(first["id"])
    task = tasks.create(first["id"], "Inspect project", "Find the relevant files")
    uuid.UUID(task["id"])
    assert task["status"] == "pending"
    assert task["depends_on"] == []
    assert TaskManager(store).list(first["id"]) == [task]
    assert store.get(first["id"]) == before
    updated = tasks.update(first["id"], task["id"], {"title": "Inspect source", "status": "in_progress"})
    assert updated["created"] == task["created"]
    assert updated["updated"] >= task["updated"]
    assert tasks.list(first["id"])[0]["title"] == "Inspect source"


def test_tasks_validate_dependencies_and_scope_before_writing(planning):
    _, tasks, first, second = planning
    foreign = tasks.create(second["id"], "Foreign task")
    with pytest.raises(ValueError, match="this conversation"):
        tasks.create(first["id"], "Invalid", depends_on=[foreign["id"]])
    with pytest.raises(ValueError, match="not found"):
        tasks.update(first["id"], foreign["id"], {"status": "completed"})
    with pytest.raises(ValueError, match="Conversation not found"):
        tasks.list("missing")
    assert tasks.list(first["id"]) == []
    assert tasks.list(second["id"]) == [foreign]


def test_tasks_reject_cycles_and_incomplete_dependency_transitions(planning):
    _, tasks, first, _ = planning
    sid = first["id"]
    initial = tasks.create(sid, "First")
    dependent = tasks.create(sid, "Second", depends_on=[initial["id"]])
    last = tasks.create(sid, "Third", depends_on=[dependent["id"]])
    for status in ("in_progress", "completed"):
        with pytest.raises(ValueError, match="Complete all dependencies"):
            tasks.update(sid, dependent["id"], {"status": status})
    with pytest.raises(ValueError, match="cycle"):
        tasks.update(sid, initial["id"], {"depends_on": [last["id"]]})
    with pytest.raises(ValueError, match="cycle"):
        tasks.update(sid, initial["id"], {"depends_on": [initial["id"]]})
    assert tasks.list(sid) == [initial, dependent, last]
    tasks.update(sid, initial["id"], {"status": "completed"})
    assert tasks.update(sid, dependent["id"], {"status": "in_progress"})["status"] == "in_progress"
    assert tasks.update(sid, dependent["id"], {"status": "completed"})["status"] == "completed"
    assert tasks.update(sid, last["id"], {"status": "completed"})["status"] == "completed"


@pytest.mark.parametrize("dependent_status", ["in_progress", "completed"])
@pytest.mark.parametrize("reset_status", ["pending", "in_progress", "cancelled"])
def test_completed_dependencies_cannot_be_reset_under_active_dependents(planning, dependent_status, reset_status):
    _, tasks, first, _ = planning
    sid = first["id"]
    initial = tasks.create(sid, "First")
    dependent = tasks.create(sid, "Second", depends_on=[initial["id"]])
    tasks.update(sid, initial["id"], {"status": "completed"})
    tasks.update(sid, dependent["id"], {"status": dependent_status})
    with pytest.raises(ValueError, match="cannot be reset"):
        tasks.update(sid, initial["id"], {"status": reset_status})
    tasks.update(sid, dependent["id"], {"status": "cancelled"})
    assert tasks.update(sid, initial["id"], {"status": reset_status})["status"] == reset_status


@pytest.mark.parametrize("fields", [
    {"title": ""}, {"title": " "}, {"title": "x" * 201}, {"title": 123},
    {"description": "x" * 4001}, {"description": None}, {"status": "done"},
    {"depends_on": "task"}, {"depends_on": [1]}, {"depends_on": ["missing"]},
    {"id": "different"}, {"session_id": "other"},
])
def test_task_field_validation_is_atomic(planning, fields):
    _, tasks, first, _ = planning
    original = tasks.create(first["id"], "Valid")
    with pytest.raises(ValueError):
        tasks.update(first["id"], original["id"], fields)
    assert tasks.list(first["id"]) == [original]


def test_task_capacity_duplicate_dependencies_and_session_cleanup(planning):
    store, tasks, first, second = planning
    sid = first["id"]
    task = tasks.create(sid, "First", "x" * 4000)
    with pytest.raises(ValueError, match="unique"):
        tasks.create(sid, "Duplicate dependencies", depends_on=[task["id"], task["id"]])
    for index in range(49):
        tasks.create(sid, f"Task {index}")
    with pytest.raises(ValueError, match="at most 50"):
        tasks.create(sid, "Too many")
    other = tasks.create(second["id"], "Other task")
    store.delete(sid)
    tasks.delete_session(sid)
    assert store.db.execute("SELECT COUNT(*) FROM tasks WHERE session_id=?", (sid,)).fetchone()[0] == 0
    assert tasks.list(second["id"]) == [other]


@pytest.fixture
async def delegation(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(tmp_path), model="synthetic-model", env_file="")
    store = Store(settings.state_dir / "delegation.sqlite3")
    manager = AgentManager(store, settings)
    parent = store.create(settings.values)
    helper = DelegateManager(manager)
    yield manager, helper, parent
    for sid in list(manager.tasks):
        await manager.stop(sid)
    await manager.jobs.shutdown()
    store.db.close()


@pytest.mark.parametrize("mode", ["manual", "auto", "acceptEdits", "plan", "bypassPermissions"])
async def test_child_is_persisted_isolated_and_inherits_permissions(delegation, mode):
    manager, helper, parent = delegation
    parent.update(permission_mode=mode, allowed_directories=["/granted"])
    parent["wire"] = [{"role": "user", "content": "private parent history"}]
    manager.store.save(parent)
    observed = {}
    async def worker(session, prompt):
        observed.update(session)
        assert "private parent history" not in prompt
        assert "Delegated task:\nInspect source" in prompt
        assert "Parent-provided context:\nCheck the parser" in prompt
        session["allowed_directories"].append("/child-only")
        await manager.event(session, "assistant", text="Parser inspected.")
    manager.run_databricks = worker
    result = await helper.delegate(parent, "Inspect source", context="Check the parser", max_steps=4)
    assert result["status"] == "completed"
    assert result["output"] == "Parser inspected."
    assert observed["id"] != parent["id"]
    assert observed["permission_mode"] == mode
    assert observed["workspace"] == parent["workspace"] and observed["model"] == parent["model"]
    assert observed["parent_session_id"] == parent["id"] and observed["is_subagent"] is True
    assert observed["max_steps"] == 4 and observed["wire"] == []
    child = manager.store.get(result["child_session_id"])
    assert child["events"][-1]["text"] == "Parser inspected."
    assert manager.store.get(parent["id"])["allowed_directories"] == ["/granted"]
    notice = manager.store.get(parent["id"])["events"][-1]
    assert notice["type"] == "notice" and notice["child_session_id"] == child["id"]
    assert helper.children(parent["id"])[0]["id"] == child["id"]
    assert helper.active == {}


async def test_delegate_uses_authoritative_parent_permissions_and_rejects_recursion(delegation):
    manager, helper, parent = delegation
    manager.run_databricks = lambda session, prompt: asyncio.sleep(0)
    result = await helper.delegate({**parent, "permission_mode": "bypassPermissions"}, "Inspect")
    child = manager.store.get(result["child_session_id"])
    assert child["permission_mode"] == "manual"
    with pytest.raises(ValueError, match="cannot delegate"):
        await helper.delegate(child, "Recursive work")
    manager.store.delete(parent["id"])
    with pytest.raises(ValueError, match="not found"):
        await helper.delegate(parent, "Gone parent")


@pytest.mark.parametrize("steps", [0, 9, True, 1.5, "6"])
async def test_delegate_validates_step_limit_before_creating_child(delegation, steps):
    manager, helper, parent = delegation
    with pytest.raises(ValueError, match="max_steps"):
        await helper.delegate(parent, "Inspect", max_steps=steps)
    assert len(manager.store.list()) == 1


async def test_delegate_enforces_global_and_per_parent_limits(delegation):
    manager, helper, parent = delegation
    parents = [parent] + [manager.store.create(manager.settings.values) for _ in range(3)]
    release, started = asyncio.Event(), asyncio.Event()
    count = 0
    async def worker(session, prompt):
        nonlocal count
        count += 1
        if count == 3:
            started.set()
        await release.wait()
        await manager.event(session, "assistant", text="Finished")
    manager.run_databricks = worker
    pending = [asyncio.create_task(helper.delegate(item, "Inspect")) for item in parents[:3]]
    await asyncio.wait_for(started.wait(), 1)
    with pytest.raises(ValueError, match="already running"):
        await helper.delegate(parent, "Duplicate")
    with pytest.raises(ValueError, match="three subagents"):
        await helper.delegate(parents[3], "Fourth")
    release.set()
    assert all(result["status"] == "completed" for result in await asyncio.gather(*pending))
    assert helper.active == {}
    assert (await helper.delegate(parents[3], "Now allowed"))["status"] == "completed"


async def test_parent_cancellation_awaits_child_cleanup(delegation):
    manager, helper, parent = delegation
    started, cleaned = asyncio.Event(), asyncio.Event()
    async def worker(session, prompt):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()
    manager.run_databricks = worker
    pending = asyncio.create_task(helper.delegate(parent, "Slow work"))
    await asyncio.wait_for(started.wait(), 1)
    child_id = helper.children(parent["id"])[0]["id"]
    assert manager.store.get(parent["id"])["events"][-1]["child_session_id"] == child_id
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert cleaned.is_set()
    assert manager.tasks[child_id].done()
    assert child_id not in manager.live and helper.active == {}
    assert manager.store.get(child_id)["events"][-1]["type"] == "notice"


async def test_cancel_parent_reports_cancelled_child_and_releases_capacity(delegation):
    manager, helper, parent = delegation
    started = asyncio.Event()
    async def worker(session, prompt):
        started.set()
        await asyncio.Event().wait()
    manager.run_databricks = worker
    pending = asyncio.create_task(helper.delegate(parent, "Slow work"))
    await asyncio.wait_for(started.wait(), 1)
    await helper.cancel_parent(parent["id"])
    result = await pending
    assert result["status"] == "cancelled"
    assert manager.tasks[result["child_session_id"]].done()
    assert helper.active == {}
    await helper.cancel_parent("not-running")


@pytest.mark.parametrize("tool_error", [False, True])
async def test_child_errors_are_reported_even_with_a_final_assistant_reply(delegation, tool_error):
    manager, helper, parent = delegation
    async def worker(session, prompt):
        if tool_error:
            await manager.event(session, "tool", state="error", output="Tool failed")
        else:
            await manager.event(session, "error", text="Model request failed")
        await manager.event(session, "assistant", text="Partial findings")
    manager.run_databricks = worker
    result = await helper.delegate(parent, "Inspect")
    assert result["status"] == "failed"
    assert "failed" in result["output"] and "Partial findings" in result["output"]


async def test_delegate_bounds_unicode_serialized_result_and_handles_worker_failure(delegation):
    manager, helper, parent = delegation
    async def worker(session, prompt):
        await manager.event(session, "assistant", text="😀\n" * 4000)
    manager.run_databricks = worker
    result = await helper.delegate(parent, "Inspect")
    assert len(json.dumps(result, indent=2).encode("utf-8")) <= 6000
    assert result["output"].endswith("[truncated]")
    async def failure(session, prompt):
        raise RuntimeError("Synthetic worker failure")
    manager.run_databricks = failure
    result = await helper.delegate(parent, "Inspect again")
    assert result["status"] == "failed" and "Synthetic worker failure" in result["output"]
    assert helper.active == {}


async def test_parent_progress_is_live_correlated_and_contains_no_child_payloads(delegation):
    manager, helper, parent = delegation
    manager.live[parent["id"]] = parent
    spawning = await manager.event(parent, "tool", name="delegate_task", call_id="call-review",
                                   input={"task": "Inspect source"}, output="", state="running")
    ready, release = asyncio.Event(), asyncio.Event()
    async def worker(child, prompt):
        action = await manager.event(child, "tool", name="read_file", input={"path": "private.py"},
                                     state="pending", output="private child contents")
        await manager.status(child, "awaiting_approval")
        ready.set()
        await release.wait()
        await manager.update_event(child, action, state="completed")
        await manager.update_event(child, action, state="completed")
        await manager.status(child, "running")
        await manager.event(child, "assistant", text="Reviewed.")
    manager.run_databricks = worker
    pending = asyncio.create_task(helper.delegate(parent, "Inspect source", parent_event=spawning))
    await asyncio.wait_for(ready.wait(), 1)
    saved = manager.store.get(parent["id"])
    event = saved["events"][0]
    child = manager.store.get(event["child_session_id"])
    assert child["parent_event_id"] == spawning["id"] and child["parent_call_id"] == "call-review"
    assert child["events"][0]["origin"] == {"kind": "delegated", "parent_session_id": parent["id"],
                                            "parent_event_id": spawning["id"], "parent_call_id": "call-review"}
    assert event["delegation"] == {"status": "awaiting_approval", "completed_tools": 0, "last_tool": "read_file"}
    assert "private child contents" not in json.dumps(saved)
    assert saved["wire"] == []
    release.set()
    result = await pending
    assert result["status"] == result["terminal_reason"] == "completed"
    progress = manager.store.get(parent["id"])["events"][0]["delegation"]
    assert progress == {"status": "completed", "completed_tools": 1, "last_tool": "read_file", "terminal_reason": "completed"}
    await helper.progress(manager.store.get(child["id"]), status="running", terminal_reason="stopped")
    assert manager.store.get(parent["id"])["events"][0]["delegation"] == progress


async def test_delegation_provenance_is_only_on_initial_prompt_and_ids_do_not_cross(delegation):
    manager, helper, parent = delegation
    async def worker(child, prompt):
        await manager.event(child, "assistant", text="Done")
    manager.run_databricks = worker
    first = await helper.delegate(parent, "First")
    second = await helper.delegate(parent, "Second")
    children = [manager.store.get(result["child_session_id"]) for result in (first, second)]
    assert children[0]["parent_event_id"] != children[1]["parent_event_id"]
    before = manager.store.get(parent["id"])
    manager.start(children[0]["id"], "A new human follow-up")
    await manager.tasks[children[0]["id"]]
    users = [event for event in manager.store.get(children[0]["id"])["events"] if event["type"] == "user"]
    assert users[0]["origin"]["kind"] == "delegated"
    assert "origin" not in users[1]
    assert manager.store.get(parent["id"]) == before


@pytest.mark.parametrize("reason", ["step_limit", "inference_error", "tool_error", "stopped"])
async def test_parent_retains_structured_child_stop_reason(delegation, reason):
    from local_agent.telemetry import InferenceError
    manager, helper, parent = delegation
    async def worker(child, prompt):
        if reason == "step_limit":
            await manager.event(child, "error", text="Step limit reached", terminal_reason="step_limit")
        elif reason == "inference_error":
            raise InferenceError("Response truncated", "output_limit")
        elif reason == "tool_error":
            await manager.event(child, "tool", name="read_file", state="error", output="Missing file")
            await manager.event(child, "assistant", text="Partial findings")
        else:
            raise asyncio.CancelledError
    manager.run_databricks = worker
    result = await helper.delegate(parent, "Review")
    assert result["terminal_reason"] == reason
    child = manager.store.get(result["child_session_id"])
    assert child["terminal_reason"] == reason
    progress = manager.store.get(parent["id"])["events"][-1]["delegation"]
    assert progress["terminal_reason"] == reason
    assert progress["status"] == ("cancelled" if reason == "stopped" else "failed")


async def test_child_cancelled_before_first_schedule_has_terminal_progress(delegation, monkeypatch):
    manager, helper, parent = delegation
    original = manager.start
    def start(session_id, prompt):
        original(session_id, prompt)
        manager.tasks[session_id].cancel()
    monkeypatch.setattr(manager, "start", start)
    result = await helper.delegate(parent, "Never started")
    assert result["terminal_reason"] == "stopped" and result["status"] == "cancelled"
    assert result["child_session_id"] not in manager.live
    assert manager.statuses[result["child_session_id"]] == "idle"
    assert helper.active == {}
    progress = manager.store.get(parent["id"])["events"][-1]["delegation"]
    assert progress == {"status": "cancelled", "completed_tools": 0, "terminal_reason": "stopped"}
    monkeypatch.setattr(manager, "start", original)
    manager.run_databricks = lambda session, prompt: asyncio.sleep(0)
    manager.start(result["child_session_id"], "Human follow-up after cancellation")
    await manager.tasks[result["child_session_id"]]
    assert "origin" not in manager.store.get(result["child_session_id"])["events"][0]


async def test_delegation_reserves_capacity_before_publishing_and_cleans_up_cancelled_creation(delegation, monkeypatch):
    manager, helper, parent = delegation
    publishing = asyncio.Event()
    first = True
    async def broadcast(session_id, data):
        nonlocal first
        if first:
            first = False
            publishing.set()
            await asyncio.Event().wait()
    monkeypatch.setattr(manager, "broadcast", broadcast)
    pending = asyncio.create_task(helper.delegate(parent, "Slow notification"))
    await asyncio.wait_for(publishing.wait(), 1)
    with pytest.raises(ValueError, match="already running"):
        await helper.delegate(parent, "Duplicate")
    assert len(helper.children(parent["id"])) == 1
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert helper.active == {}
    assert manager.store.get(parent["id"])["events"][-1]["delegation"]["terminal_reason"] == "stopped"


async def test_parent_cancellation_during_unscheduled_child_cleanup_propagates(delegation, monkeypatch):
    manager, helper, parent = delegation
    original_start, original_stop = manager.start, manager.stop
    cleanup_started = asyncio.Event()
    first = True
    def start(session_id, prompt):
        original_start(session_id, prompt)
        manager.tasks[session_id].cancel()
    async def stop(session_id):
        nonlocal first
        if first:
            first = False
            cleanup_started.set()
            await asyncio.Event().wait()
        await original_stop(session_id)
    monkeypatch.setattr(manager, "start", start)
    monkeypatch.setattr(manager, "stop", stop)
    pending = asyncio.create_task(helper.delegate(parent, "Never started"))
    await asyncio.wait_for(cleanup_started.wait(), 1)
    child_id = helper.active[parent["id"]]["child_session_id"]
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert child_id not in manager.live and manager.statuses[child_id] == "idle"
    assert helper.active == {}
    assert manager.store.get(parent["id"])["events"][-1]["delegation"]["terminal_reason"] == "stopped"


async def test_restart_marks_active_delegation_interrupted_and_preserves_terminal_result(delegation):
    manager, helper, parent = delegation
    async def worker(child, prompt):
        await manager.event(child, "assistant", text="Finished")
    manager.run_databricks = worker
    completed = await helper.delegate(parent, "Completed work")
    saved_parent = manager.store.get(parent["id"])
    finished_progress = saved_parent["events"][-1]["delegation"]
    child = manager.store.create(manager.settings.values)
    spawning = await manager.event(saved_parent, "tool", name="delegate_task", state="running", child_session_id=child["id"],
                                   delegation={"status": "awaiting_approval", "completed_tools": 1, "last_tool": "run_command"})
    child.update(parent_session_id=parent["id"], parent_event_id=spawning["id"], is_subagent=True,
                 delegation=dict(spawning["delegation"]))
    manager.store.save(child)
    restarted = AgentManager(manager.store, manager.settings)
    try:
        progress = manager.store.get(parent["id"])["events"][-1]["delegation"]
        assert progress == {"status": "interrupted", "completed_tools": 1, "last_tool": "run_command", "terminal_reason": "interrupted"}
        assert manager.store.get(child["id"])["terminal_reason"] == "interrupted"
        assert manager.store.get(completed["child_session_id"])["delegation"] == finished_progress
        restarted.delegates.recover()
        assert manager.store.get(parent["id"])["events"][-1]["delegation"] == progress
    finally:
        await restarted.jobs.shutdown()
