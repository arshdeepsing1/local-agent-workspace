"""Header-only startup after a clean shutdown, and the full scan after anything else."""
import asyncio
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.store import Store


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(workspace), env_file="")
    return settings


@pytest.fixture
def store(settings):
    store = Store(settings.state_dir / "conversations.sqlite3")
    yield store
    store.db.close()


@pytest.fixture
def full_reads(monkeypatch):
    """Names of the conversation files read completely (not only their header)."""
    names = []
    original = Store._read_lines

    def counting(path, *, metadata_only=False):
        if not metadata_only:
            names.append(path.name)
        return original(path, metadata_only=metadata_only)
    monkeypatch.setattr(Store, "_read_lines", staticmethod(counting))
    return names


def marker(store):
    return store.conversations / ".clean-shutdown"


def clean_shutdown(store):
    """What the lifespan leaves after every task and job stopped."""
    marker(store).touch(mode=0o600)


def chat(store, settings, **fields):
    session = store.create(settings.values)
    session.update(fields)
    store.save(session)
    return session


def test_a_fresh_state_folder_loads_every_conversation(store, settings, full_reads):
    ids = [chat(store, settings, terminal_reason="completed")["id"] for _ in range(3)]
    full_reads.clear()
    AgentManager(store, settings)
    assert sorted(set(full_reads)) == sorted(f"{session_id}.jsonl" for session_id in ids)


def test_after_a_clean_shutdown_only_headers_are_read_and_the_marker_is_removed(store, settings, full_reads):
    sessions = [chat(store, settings, terminal_reason="completed", turn_ended=1.0,
                     events=[{"id": "u", "type": "user", "text": "Hi", "created": 1.0}],
                     file_views={"/srv/x": [1, 2]}, allow_rules=[{"tool": "run_command", "command": "ls"}])
                for _ in range(50)]
    files = {path.name: path.read_bytes() for path in store.conversations.iterdir()}
    clean_shutdown(store)
    full_reads.clear()
    manager = AgentManager(store, settings)
    assert full_reads == []
    assert not marker(store).exists()
    # Nothing was rewritten: the files and their <id>.state.json sidecars are unchanged.
    assert {path.name: path.read_bytes() for path in store.conversations.iterdir()} == files
    assert manager.get(sessions[0]["id"])["allow_rules"] == [{"tool": "run_command", "command": "ls"}]


def test_chats_that_own_finished_jobs_are_not_read_after_a_clean_shutdown(store, settings, full_reads):
    """Finished jobs were mirrored into their chats when they ended; only a crash mirrors them again."""
    sessions = [chat(store, settings, terminal_reason="completed", turn_ended=1.0,
                     events=[{"id": "u", "type": "user", "text": "Hi", "created": 1.0}]) for _ in range(4)]
    manager = AgentManager(store, settings)

    async def commands():
        for session in sessions[:3]:
            job = await manager.jobs.start("true", settings.values["workspace"], session_id=session["id"])
            await manager.jobs.wait(job["id"])
        await manager.jobs.shutdown()
    asyncio.run(commands())
    assert all(manager.store.get(session["id"]).get("command_jobs") for session in sessions[:3])
    for crashed in (False, True):
        if not crashed:
            clean_shutdown(store)
        cold = Store(settings.state_dir / "conversations.sqlite3")  # A restart: nothing cached.
        full_reads.clear()
        AgentManager(cold, settings)
        expected = [f"{session['id']}.jsonl" for session in sessions] if crashed else []
        assert sorted(set(full_reads)) == sorted(expected)
        cold.db.close()


def test_a_crash_during_this_run_gives_a_full_scan_next_time(store, settings, full_reads):
    session = chat(store, settings, terminal_reason="completed")
    clean_shutdown(store)
    AgentManager(store, settings)
    full_reads.clear()
    AgentManager(store, settings)  # No lifespan ran in between: a crash.
    assert full_reads == [f"{session['id']}.jsonl"]


def test_the_lifespan_writes_the_marker_after_everything_stopped(settings):
    with TestClient(create_app(settings)) as client:
        client.get("/api/bootstrap")
        conversations = client.app.state.manager.store.conversations
        assert not (conversations / ".clean-shutdown").exists()
        assert client.app.state.manager.closing is False
    assert client.app.state.manager.closing is True
    assert (conversations / ".clean-shutdown").stat().st_mode & 0o777 == 0o600


def test_a_stop_that_raises_leaves_no_marker(settings):
    app = create_app(settings)
    manager = app.state.manager

    async def failing_stop(session_id):
        raise RuntimeError("stop failed")
    with pytest.raises(RuntimeError, match="stop failed"):
        with TestClient(app):
            manager.tasks["running"] = None
            manager.stop = failing_stop
    assert not (manager.store.conversations / ".clean-shutdown").exists()
    manager.store.db.close()


def test_a_turn_the_stop_did_not_finish_is_still_repaired_after_a_clean_shutdown(store, settings, full_reads):
    running = chat(store, settings, terminal_reason="completed", turn_running=True, turn_ended=None, events=[
        {"id": "u", "type": "user", "text": "Work", "created": 1.0},
        {"id": "t", "type": "tool", "name": "run_command", "state": "running", "created": 2.0},
        {"id": "q", "type": "tool", "name": "ask_user", "state": "pending", "created": 3.0}])
    naming = chat(store, settings, terminal_reason="completed", turn_ended=None, events=[
        {"id": "u", "type": "user", "text": "Work", "created": 1.0},
        {"id": "a", "type": "assistant", "text": "Done", "created": 2.0}])
    idle = chat(store, settings, terminal_reason="completed", turn_ended=5.0)
    # turn_running alone also counts (each condition is checked on its own).
    marked = chat(store, settings, terminal_reason="completed", turn_running=True, events=[
        {"id": "u", "type": "user", "text": "Work", "created": 1.0}])
    clean_shutdown(store)
    full_reads.clear()
    AgentManager(store, settings)
    assert f"{idle['id']}.jsonl" not in full_reads
    saved = store.get(running["id"])
    assert [event.get("state") for event in saved["events"]] == [None, "cancelled", "cancelled"]
    assert saved["terminal_reason"] == "interrupted" and "turn_running" not in saved
    assert isinstance(saved["turn_ended"], float)
    assert store.get(marked["id"])["terminal_reason"] == "interrupted"
    named = store.get(naming["id"])
    assert named["terminal_reason"] == "completed" and isinstance(named["turn_ended"], float)


def test_without_the_marker_an_older_crash_without_turn_running_is_repaired(store, settings):
    session = chat(store, settings, terminal_reason="completed", events=[
        {"id": "u", "type": "user", "text": "Work", "created": 1.0},
        {"id": "a", "type": "assistant", "text": "Part", "created": 2.0,
         "request_info": {"model": "m", "status": "running"}}])
    AgentManager(store, settings)
    saved = store.get(session["id"])
    assert saved["terminal_reason"] == "interrupted"
    assert saved["events"][-1]["request_info"]["status"] == "interrupted"


def test_a_running_user_command_is_cancelled_but_not_a_cut_short_turn(store, settings):
    session = chat(store, settings, terminal_reason="completed", events=[
        {"id": "u", "type": "user", "text": "Work", "created": 1.0},
        {"id": "a", "type": "assistant", "text": "Done", "created": 2.0},
        {"id": "c", "type": "tool", "name": "user_command", "input": {"command": "make"}, "state": "running",
         "output": "", "created": 3.0}])
    AgentManager(store, settings)
    saved = store.get(session["id"])
    assert saved["events"][-1]["state"] == "cancelled"
    assert saved["terminal_reason"] == "completed"


def delegated(store, settings, parent, event_id, progress):
    child = chat(store, settings, parent_session_id=parent["id"], parent_event_id=event_id, is_subagent=True,
                 delegation=progress, terminal_reason=progress.get("terminal_reason"))
    return child


def test_after_a_clean_shutdown_only_open_delegations_are_repaired(store, settings, full_reads):
    parent = chat(store, settings, terminal_reason="completed", turn_ended=1.0)
    open_child = delegated(store, settings, parent, "spawn-open", {"status": "running", "completed_tools": 1})
    done_child = delegated(store, settings, parent, "spawn-done",
                           {"status": "completed", "completed_tools": 2, "terminal_reason": "completed"})
    parent["events"] = [
        {"id": "spawn-open", "type": "tool", "name": "delegate_task", "state": "completed", "created": 1.0,
         "child_session_id": open_child["id"], "delegation": {"status": "running", "completed_tools": 1}},
        {"id": "spawn-done", "type": "tool", "name": "delegate_task", "state": "completed", "created": 2.0,
         "child_session_id": done_child["id"],
         "delegation": {"status": "completed", "completed_tools": 2, "terminal_reason": "completed"}}]
    store.save(parent)
    clean_shutdown(store)
    full_reads.clear()
    AgentManager(store, settings)
    assert f"{done_child['id']}.jsonl" not in full_reads
    child = store.get(open_child["id"])
    assert child["delegation"]["terminal_reason"] == "interrupted" and child["terminal_reason"] == "interrupted"
    event = store.get(parent["id"])["events"][0]
    assert event["delegation"] == {"status": "interrupted", "completed_tools": 1, "terminal_reason": "interrupted"}


def test_a_recovered_parent_also_repairs_its_terminal_children(store, settings, full_reads):
    parent = chat(store, settings, terminal_reason="completed", turn_running=True, turn_ended=None)
    child = delegated(store, settings, parent, "spawn",
                      {"status": "completed", "completed_tools": 2, "terminal_reason": "completed"})
    parent["events"] = [{"id": "spawn", "type": "tool", "name": "delegate_task", "state": "running", "created": 1.0,
                         "child_session_id": child["id"], "delegation": {"status": "running", "completed_tools": 1}}]
    store.save(parent)
    clean_shutdown(store)
    full_reads.clear()
    AgentManager(store, settings)
    assert f"{child['id']}.jsonl" in full_reads
    event = store.get(parent["id"])["events"][0]
    assert event["state"] == "cancelled"
    assert event["delegation"]["terminal_reason"] == "completed"


def write_raw(folder, session_id, events):
    header = {"id": session_id, "title": f"Chat {session_id}", "created": 1.0, "updated": 2.0,
              "workspace": "/srv/project", "model": "test-model", "terminal_reason": "completed", "turn_ended": 2.0}
    lines = [json.dumps({"record": "session", "version": 1, "data": header}, separators=(",", ":"))]
    lines += [json.dumps({"record": "event", "data": event}, separators=(",", ":")) for event in events]
    path = folder / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def test_five_hundred_large_chats_start_and_list_quickly_after_a_clean_shutdown(store, settings, full_reads):
    events = [{"id": f"e{index}", "type": "user" if index % 2 == 0 else "assistant", "created": float(index),
               "text": "Some words about the project " * 8} for index in range(200)]
    for number in range(500):
        write_raw(store.conversations, f"chat-{number:03d}", events)
    clean_shutdown(store)
    store.db.close()
    full_reads.clear()
    began = time.monotonic()
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/bootstrap").json()["token"]
        listed = client.get("/api/sessions", headers={"X-Local-Token": token}).json()
        elapsed = time.monotonic() - began
        assert len(listed) == 500 and listed[0]["unreviewed_changes"] == 0
    assert full_reads == []
    assert elapsed < 2, elapsed
