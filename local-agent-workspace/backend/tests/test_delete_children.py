"""Deleting a conversation with its subagent chats, or keeping them as separate chats."""
import asyncio
import json

import httpx
import pytest

from local_agent.api import create_app
from local_agent.config import Settings


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), model="test-model", env_file="")
    settings.env = {}
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            yield client, app.state.manager


def family(manager):
    """A parent, its subagent with a nested child (from damaged or older data), and an unrelated chat."""
    store = manager.store
    parent = store.create(manager.settings.values)
    child = store.create(manager.settings.values)
    grandchild = store.create(manager.settings.values)
    other = store.create(manager.settings.values)
    parent["events"] = [{"id": "spawn", "type": "tool", "name": "delegate_task", "state": "completed",
                         "child_session_id": child["id"], "delegation": {"status": "running", "completed_tools": 0}}]
    child.update(parent_session_id=parent["id"], parent_event_id="spawn", parent_call_id="call-1", is_subagent=True,
                 delegation={"status": "running", "completed_tools": 0}, allow_rules=[{"tool": "run_command", "command": "ls"}])
    grandchild.update(parent_session_id=child["id"], parent_event_id="x", is_subagent=True,
                      delegation={"status": "completed", "completed_tools": 1, "terminal_reason": "completed"})
    for session in (parent, child, grandchild, other):
        store.save(session)
    return parent, child, grandchild, other


def add_rows(manager, session_id):
    db = manager.store.db
    db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?)", (f"job-{session_id}", session_id, "/srv/x", json.dumps({"id": "j"})))
    db.execute("INSERT INTO checkpoints VALUES (?, ?, ?, ?)", (f"cp-{session_id}", "/srv/x", session_id, json.dumps({})))
    db.execute("INSERT INTO agent_changes VALUES (?, ?, ?, ?)", (session_id, f"/srv/x/{session_id}", "/srv/x", json.dumps({})))
    db.commit()
    manager.task_board.create(session_id, "A task")


def rows(manager, session_id):
    db = manager.store.db
    return {table: db.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (session_id,)).fetchone()[0]
            for table in ("jobs", "checkpoints", "agent_changes", "tasks")}


def files(manager, session_id):
    folder = manager.store.conversations
    return [path.name for path in (folder / f"{session_id}.jsonl", folder / f"{session_id}.state.json") if path.exists()]


def running(manager, session):
    """Make a chat look busy with a turn that waits until it is cancelled."""
    cancelled = asyncio.Event()

    async def turn():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    manager.live[session["id"]] = session
    manager.statuses[session["id"]] = "running"
    manager.tasks[session["id"]] = asyncio.create_task(turn())
    return cancelled


async def test_the_unreviewed_change_count_is_listed_per_chat(runtime):
    client, manager = runtime
    parent, child, _, other = family(manager)
    add_rows(manager, parent["id"])
    manager.store.db.execute("INSERT INTO agent_changes VALUES (?, ?, ?, ?)",
                             (parent["id"], "/srv/x/rolled-up", "/srv/x", json.dumps({"via_session_id": child["id"]})))
    listed = {item["id"]: item["unreviewed_changes"] for item in (await client.get("/api/sessions")).json()}
    assert listed[parent["id"]] == 2 and listed[other["id"]] == 0 and listed[child["id"]] == 0


async def test_deleting_with_children_removes_every_trace_of_the_family(runtime):
    client, manager = runtime
    parent, child, grandchild, other = family(manager)
    for session in (parent, child, grandchild, other):
        add_rows(manager, session["id"])
    assert manager.descendants(parent["id"]) == [grandchild["id"], child["id"]]
    cancelled = running(manager, manager.store.get(child["id"]))
    response = await client.delete(f"/api/sessions/{parent['id']}", params={"children": "delete"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": [grandchild["id"], child["id"], parent["id"]]}
    assert cancelled.is_set()
    for session in (parent, child, grandchild):
        assert files(manager, session["id"]) == []
        assert rows(manager, session["id"]) == {"jobs": 0, "checkpoints": 0, "agent_changes": 0, "tasks": 0}
        assert session["id"] not in manager.live and session["id"] not in manager.statuses
    assert files(manager, other["id"]) == [f"{other['id']}.jsonl"]
    assert rows(manager, other["id"]) == {"jobs": 1, "checkpoints": 1, "agent_changes": 1, "tasks": 1}
    assert [item["id"] for item in (await client.get("/api/sessions")).json()] == [other["id"]]


async def test_deleting_and_keeping_children_detaches_them(runtime):
    client, manager = runtime
    parent, child, grandchild, _ = family(manager)
    before = manager.store.get(child["id"])["updated"]
    response = await client.delete(f"/api/sessions/{parent['id']}")
    assert response.json() == {"ok": True, "deleted": [parent["id"]]}
    kept = manager.store.get(child["id"])
    assert not {"parent_session_id", "parent_event_id", "parent_call_id"} & kept.keys()
    assert kept["parent_deleted"] is True and kept["updated"] == before
    assert kept["delegation"] == {"status": "cancelled", "completed_tools": 0, "terminal_reason": "stopped"}
    assert kept["allow_rules"] == [{"tool": "run_command", "command": "ls"}]  # The sidecar is kept.
    assert manager.store.get(grandchild["id"])["parent_session_id"] == child["id"]  # Only direct children.
    await manager.status(kept, "running")  # Progress reports go nowhere now, without an error.
    await manager.status(kept, "idle")
    assert manager.store.get(child["id"])["delegation"]["terminal_reason"] == "stopped"


async def test_progress_of_a_detached_child_goes_nowhere(runtime):
    client, manager = runtime
    _, child, _, _ = family(manager)
    detached = manager.store.get(child["id"])
    for key in ("parent_session_id", "parent_event_id", "parent_call_id"):
        detached.pop(key)
    await manager.delegates.progress(detached, status="awaiting_approval")  # Its delegation is still open here.
    assert detached["delegation"] == {"status": "running", "completed_tools": 0}


async def test_keeping_children_stops_the_active_subagent_first(runtime):
    client, manager = runtime
    parent, child, _, _ = family(manager)
    live_child = manager.store.get(child["id"])
    cancelled = running(manager, live_child)
    manager.delegates.active[parent["id"]] = [{"child_session_id": child["id"], "cancelled": False}]
    response = await client.delete(f"/api/sessions/{parent['id']}", params={"children": "keep"})
    assert response.status_code == 200
    assert cancelled.is_set()
    kept = manager.store.get(child["id"])
    assert kept["parent_deleted"] is True and kept["terminal_reason"] == "stopped"
    assert kept["delegation"]["terminal_reason"] == "stopped"


async def test_delete_refuses_an_unknown_children_option(runtime):
    client, manager = runtime
    parent, *_ = family(manager)
    response = await client.delete(f"/api/sessions/{parent['id']}", params={"children": "orphan"})
    assert response.status_code == 422
    assert manager.store.get(parent["id"]) is not None


def test_descendants_survive_a_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    app = create_app(settings)
    manager = app.state.manager
    first = manager.store.create(settings.values)
    second = manager.store.create(settings.values)
    first["parent_session_id"], second["parent_session_id"] = second["id"], first["id"]
    manager.store.save(first)
    manager.store.save(second)
    assert manager.descendants(first["id"]) == [second["id"]]
    manager.store.db.close()


async def test_deleting_a_subagent_marks_it_in_its_parents_index_and_the_list_leaves_the_index_out(runtime):
    client, manager = runtime
    parent, child, grandchild, other = family(manager)
    entry = {"id": child["id"], "task": "Review", "agent_type": "explore", "status": "completed", "created": 1.0, "ended": 2.0,
             "transcript": f"conversations/{child['id']}.jsonl"}
    parent["subagents"] = [entry]
    manager.store.save(parent)
    listed = (await client.get(f"/api/sessions/{parent['id']}/children")).json()
    assert [(item["id"], item["transcript"], item["subagent"]) for item in listed] == [
        (child["id"], f"conversations/{child['id']}.jsonl", entry)]
    summary = next(item for item in (await client.get("/api/sessions")).json() if item["id"] == parent["id"])
    assert "subagents" not in summary
    assert (await client.get(f"/api/sessions/{parent['id']}/info")).json()["subagents"] == [entry]
    response = await client.delete(f"/api/sessions/{child['id']}", params={"children": "delete"})
    assert response.status_code == 200
    assert manager.store.header(parent["id"])["subagents"] == [{**entry, "deleted": True, "transcript": None}]
    assert manager.store.header(parent["id"])["updated"] == parent["updated"]  # Not reordered in the sidebar.
