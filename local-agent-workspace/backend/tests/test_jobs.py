import asyncio
import json
import os
import shlex
import sys
import time

import pytest

from local_agent.jobs import CommandTimeout, JobManager, run_process, shell_argv, validate_command_options
from local_agent.store import Store


def python_command(source):
    return shlex.quote(sys.executable) + " -c " + shlex.quote(source)


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "jobs.sqlite3")
    yield instance
    instance.db.close()


@pytest.mark.parametrize("options", [("", 60, 1024), (" " * 3, 60, 1024), ("x" * 50001, 60, 1024),
                                     ("x", True, 1024), ("x", 0, 1024), ("x", 3601, 1024),
                                     ("x", 1.5, 1024), ("x", 60, True), ("x", 60, 1023),
                                     ("x", 60, 1000001)])
def test_command_validation(options):
    with pytest.raises(ValueError):
        validate_command_options(*options)


@pytest.mark.parametrize("available,expected", [
    ({"/bin/zsh", "/bin/bash"}, ["/bin/zsh", "-f", "-c", "printf ok"]),
    ({"/bin/bash"}, ["/bin/bash", "--noprofile", "--norc", "-c", "printf ok"]),
    ({"/bin/sh"}, ["/bin/sh", "-c", "printf ok"]),
])
def test_shell_selection_disables_startup_files(monkeypatch, available, expected):
    monkeypatch.setattr("local_agent.jobs.Path.is_file", lambda path: str(path) in available)
    assert shell_argv("printf ok") == expected


async def test_output_arrives_before_exit(tmp_path):
    seen = asyncio.Event()
    snapshots = []

    async def output(text, truncated):
        snapshots.append((text, truncated))
        seen.set()

    task = asyncio.create_task(run_process("printf first; sleep 0.3; printf second", tmp_path, on_output=output))
    await asyncio.wait_for(seen.wait(), 1)
    assert not task.done()
    assert snapshots[0] == ("first", False)
    assert await task == {"exit_code": 0, "output": "firstsecond", "truncated": False}
    assert snapshots[-1] == ("firstsecond", False)


async def test_partial_output_not_delayed_until_next_read(tmp_path):
    seen = []

    async def output(text, truncated):
        seen.append(text)

    task = asyncio.create_task(run_process("printf one; sleep 0.02; printf two; sleep 0.4", tmp_path, on_output=output))
    await asyncio.sleep(0.25)
    assert not task.done()
    assert seen[-1] == "onetwo"
    await task


@pytest.mark.parametrize("size,truncated", [(1024, False), (1025, True), (200000, True)])
async def test_byte_limit_and_excess_output_is_drained(tmp_path, size, truncated):
    result = await run_process(python_command(f"import sys; sys.stdout.write('a'*{size})"), tmp_path, max_output_bytes=1024)
    assert result == {"exit_code": 0, "output": "a" * 1024, "truncated": truncated}


async def test_split_utf8_is_not_replaced_in_stream(tmp_path):
    snapshots = []

    async def output(text, truncated):
        snapshots.append(text)

    command = python_command("import os,time; os.write(1,b'hi\\xf0'); time.sleep(.15); os.write(1,b'\\x9f\\x8c\\x8d!')")
    result = await run_process(command, tmp_path, on_output=output)
    assert result["output"] == "hi🌍!"
    assert snapshots[0] == "hi"
    assert all("�" not in text for text in snapshots)


async def test_truncated_utf8_drops_incomplete_character(tmp_path):
    result = await run_process(python_command("import os; os.write(1,b'a'*1023+'🌍'.encode())"), tmp_path, max_output_bytes=1024)
    assert result["output"] == "a" * 1023
    assert result["truncated"] is True


async def test_timeout_retains_partial_output(tmp_path):
    with pytest.raises(CommandTimeout) as caught:
        await run_process("printf partial; sleep 5", tmp_path, timeout_seconds=1)
    assert caught.value.result["output"] == "partial"
    assert caught.value.result["exit_code"] != 0


@pytest.mark.parametrize("cancel", [False, True])
async def test_kill_group_even_after_shell_exits(tmp_path, cancel):
    marker = tmp_path / "orphan-completed"
    seen = asyncio.Event()

    async def output(text, truncated):
        seen.set()

    command = f"(sleep 1.4; touch {shlex.quote(str(marker))}) & printf started; exit 0"
    task = asyncio.create_task(run_process(command, tmp_path, timeout_seconds=1, on_output=output))
    await asyncio.wait_for(seen.wait(), 1)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(CommandTimeout):
            await task
    await asyncio.sleep(1.5 if cancel else 0.5)
    assert not marker.exists()


async def test_normal_exit_stops_child_with_redirected_stdio(tmp_path):
    marker = tmp_path / "unmanaged-child"
    command = f"(sleep .3; touch {shlex.quote(str(marker))}) >/dev/null 2>&1 & printf completed"
    result = await run_process(command, tmp_path)
    assert result["exit_code"] == 0
    assert result["output"] == "completed"
    await asyncio.sleep(.4)
    assert not marker.exists()


async def test_only_whitelisted_environment_is_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("DBRICKS_TOKEN", "should-never-be-present")
    monkeypatch.setenv("DATABRICKS_TOKEN", "nor-this")
    monkeypatch.setenv("UNRELATED_SECRET", "nor-this-either")
    result = await run_process(python_command("import os,json; print(json.dumps(dict(os.environ)))"), tmp_path)
    environment = json.loads(result["output"])
    assert "DBRICKS_TOKEN" not in environment
    assert "DATABRICKS_TOKEN" not in environment
    assert "UNRELATED_SECRET" not in environment
    assert environment["PATH"] == os.environ["PATH"]


async def test_noninteractive_command_has_no_inherited_stdin(tmp_path):
    result = await run_process("read value || printf no-input", tmp_path)
    assert result["output"] == "no-input"
    assert result["exit_code"] == 0


async def test_observer_error_does_not_stop_command(tmp_path):
    async def broken(*args):
        raise RuntimeError("UI observer disconnected")

    result = await run_process("printf one; sleep .15; printf two", tmp_path, on_output=broken)
    assert result["output"] == "onetwo"
    assert result["exit_code"] == 0


async def test_cancellation_while_process_creation_finishes_still_reaps_child(tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    created = asyncio.Event()
    release = asyncio.Event()
    processes = []

    async def paused_creation(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        created.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", paused_creation)
    task = asyncio.create_task(run_process("sleep 5", tmp_path))
    await asyncio.wait_for(created.wait(), 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert processes[0].returncode is not None


async def test_job_stop_before_task_starts(store, tmp_path):
    manager = JobManager(store, lambda text: text, None)
    job = await manager.start("touch must-not-exist", tmp_path)
    stopped = await manager.stop(job["id"])
    assert stopped["state"] == "cancelled"
    assert not (tmp_path / "must-not-exist").exists()
    assert (await manager.wait(job["id"]))["state"] == "cancelled"


async def test_stop_during_initial_publication_prevents_execution(store, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def notify(job):
        if job["state"] == "running":
            entered.set()
            await release.wait()

    manager = JobManager(store, lambda text: text, notify)
    starting = asyncio.create_task(manager.start("touch must-not-exist", tmp_path))
    await entered.wait()
    job_id = manager.list()[0]["id"]
    await manager.stop(job_id)
    release.set()
    assert (await starting)["state"] == "cancelled"
    assert not (tmp_path / "must-not-exist").exists()


async def test_cancelling_start_during_publication_leaves_terminal_job(store, tmp_path):
    entered = asyncio.Event()

    async def notify(job):
        if job["state"] == "running":
            entered.set()
            await asyncio.Event().wait()

    manager = JobManager(store, lambda text: text, notify)
    starting = asyncio.create_task(manager.start("touch must-not-exist", tmp_path))
    await entered.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert manager.list()[0]["state"] == "cancelled"
    assert not (tmp_path / "must-not-exist").exists()


async def test_repeated_stop_retains_output_and_cleans_up(store, tmp_path):
    manager = JobManager(store, lambda text: text, None)
    job = await manager.start("printf before; sleep 5; touch must-not-exist", tmp_path)
    while manager.get(job["id"])["output"] != "before":
        await asyncio.sleep(.01)
    stopped = await asyncio.gather(manager.stop(job["id"]), manager.stop(job["id"]))
    assert all(item["state"] == "cancelled" and item["output"] == "before" for item in stopped)
    assert not (tmp_path / "must-not-exist").exists()


async def test_cancelling_wait_leaves_job_for_explicit_stop(store, tmp_path):
    manager = JobManager(store, lambda text: text, None)
    job = await manager.start("sleep 5", tmp_path)
    waiter = asyncio.create_task(manager.wait(job["id"]))
    await asyncio.sleep(.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert manager.get(job["id"])["state"] == "running"
    assert (await manager.stop(job["id"]))["state"] == "cancelled"


async def test_job_state_persistence_failures_and_defensive_copies(store, tmp_path):
    async def broken(job):
        job["output"] = "observer cannot mutate the original"
        raise RuntimeError("observer failed")

    manager = JobManager(store, lambda text: text, broken)
    job = await manager.start("printf failed; exit 3", tmp_path, session_id="session", background=True)
    job["command"] = "cannot mutate"
    result = await manager.wait(job["id"])
    assert result["state"] == "failed"
    assert result["output"] == "failed"
    assert result["exit_code"] == 3
    assert result["background"] is True
    result["output"] = "cannot mutate"
    reloaded = JobManager(store, lambda text: text, None)
    assert reloaded.get(job["id"])["output"] == "failed"
    assert reloaded.list(session_id="session", workspace=tmp_path)[0]["command"] == "printf failed; exit 3"
    assert reloaded.list(session_id="other") == []
    assert manager.get("missing") is None


async def test_background_job_snapshots_are_kept_in_the_conversation_jsonl(store, tmp_path, monkeypatch):
    session = {"id": "chat", "title": "Command chat", "created": 1, "updated": 1,
               "workspace": str(tmp_path), "model": "model", "events": [], "wire": []}
    store.save(session)
    manager = JobManager(store, lambda text: text, None)
    mirrored = []
    save_job = store.save_job

    def capture(job):
        mirrored.append((job["state"], job["output"]))
        save_job(job)

    monkeypatch.setattr(store, "save_job", capture)

    command = "printf first; sleep .2; printf last"
    started = await manager.start(command, tmp_path, session_id=session["id"], background=True)
    running = store.get(session["id"])["command_jobs"]
    assert len(running) == 1
    assert running[0]["id"] == started["id"]
    assert running[0]["command"] == command
    assert running[0]["state"] == "running"

    # A later save from an in-memory session created before the job must not
    # erase the canonical command ledger.
    store.save(session)
    assert store.get(session["id"])["command_jobs"][0]["id"] == started["id"]

    completed = await manager.wait(started["id"])
    saved = store.get(session["id"])["command_jobs"]
    assert saved == [completed]
    assert saved[0]["state"] == "completed"
    assert saved[0]["exit_code"] == 0
    assert saved[0]["output"] == "firstlast"
    assert saved[0]["updated"] >= saved[0]["created"]
    assert mirrored == [("running", ""), ("completed", "firstlast")]

    records = [json.loads(line) for line in (store.conversations / "chat.jsonl").read_text().splitlines()]
    assert [record for record in records if record["record"] == "command_job"] == [
        {"record": "command_job", "data": completed}]
    operational = json.loads(store.db.execute("SELECT data FROM jobs WHERE id=?", (started["id"],)).fetchone()[0])
    assert saved[0] == operational


async def test_stalled_observer_is_bounded_and_does_not_hold_shutdown(store, tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.jobs.UPDATE_TIMEOUT_SECONDS", .05)

    async def stalled(job):
        await asyncio.Event().wait()

    manager = JobManager(store, lambda text: text, stalled)
    job = await asyncio.wait_for(manager.start("printf ready; sleep 5", tmp_path), 1)
    await asyncio.wait_for(manager.shutdown(), 1)
    assert manager.get(job["id"])["state"] == "cancelled"


async def test_job_timeout_persists_partial_result(store, tmp_path):
    manager = JobManager(store, lambda text: text, None)
    job = await manager.start("printf partial; sleep 5", tmp_path, timeout_seconds=1)
    result = await manager.wait(job["id"])
    assert result["state"] == "timed_out"
    assert result["output"] == "partial"


async def test_split_secret_and_command_are_never_published_or_stored(store, tmp_path):
    token = "abcabc-very-secret-token"
    snapshots = []
    current = [token]

    async def notify(job):
        snapshots.append(job)

    manager = JobManager(store, lambda text: text.replace(current[0], "[REDACTED]"), notify,
                         redaction_tokens=lambda: tuple(current))
    source = "import os,time; os.write(1,b'prefix abcabc-very'); time.sleep(.15); os.write(1,b'-secret-token suffix')"
    job = await manager.start(python_command(source) + " # " + token, tmp_path)
    current[0] = "changed-credential"
    result = await manager.wait(job["id"])
    assert result["output"] == "prefix [REDACTED] suffix"
    assert token not in result["command"]
    assert any(item["output"] == "prefix " for item in snapshots)
    assert all("abcabc" not in item["output"] for item in snapshots)
    persisted = store.db.execute("SELECT data FROM jobs").fetchone()[0]
    assert token not in persisted
    assert "abcabc-very" not in result["output"]


async def test_truncated_secret_prefix_is_withheld_even_in_final_result(store, tmp_path):
    token = "complete-secret-token"
    manager = JobManager(store, lambda text: text, None, redaction_tokens=lambda: [token])
    command = python_command("import os; os.write(1,b'x'*1016+b'complete-secret-token')")
    job = await manager.start(command, tmp_path, max_output_bytes=1024)
    result = await manager.wait(job["id"])
    assert result["truncated"] is True
    assert result["output"] == "x" * 1016


async def test_overlapping_token_suffix_does_not_publish_secret_prefix(store, tmp_path):
    snapshots = []

    async def notify(job):
        snapshots.append(job["output"])

    manager = JobManager(store, lambda text: text, notify, redaction_tokens=lambda: ["abcabc"])
    job = await manager.start("printf abcabc; sleep .15", tmp_path)
    result = await manager.wait(job["id"])
    assert result["output"] == "[REDACTED]"
    assert all("abc" not in output for output in snapshots)


async def test_active_limit_and_shutdown(store, tmp_path):
    manager = JobManager(store, lambda text: text, None)
    jobs = [await manager.start("sleep 5", tmp_path) for _ in range(4)]
    with pytest.raises(ValueError, match="four"):
        await manager.start("printf fifth", tmp_path)
    await manager.shutdown()
    assert all(manager.get(job["id"])["state"] == "cancelled" for job in jobs)
    with pytest.raises(ValueError, match="shutting down"):
        await manager.start("printf closed", tmp_path)


async def test_remove_session_stops_and_deletes_only_its_jobs(store, tmp_path):
    manager = JobManager(store, lambda text: text, None)
    deleted = await manager.start("sleep 5; touch must-not-exist", tmp_path, session_id="removed")
    retained = await manager.start("printf kept", tmp_path, session_id="kept")
    await manager.remove_session("removed")
    assert manager.get(deleted["id"]) is None
    assert (await manager.wait(retained["id"]))["output"] == "kept"
    assert not store.db.execute("SELECT id FROM jobs WHERE session_id='removed'").fetchall()


async def test_remove_session_rejects_concurrent_start_before_stopping_jobs(store, tmp_path, monkeypatch):
    manager = JobManager(store, lambda text: text, None)
    job = await manager.start("sleep 5", tmp_path, session_id="removed")
    entered = asyncio.Event()
    release = asyncio.Event()
    original_stop = manager.stop

    async def blocked_stop(job_id):
        entered.set()
        await release.wait()
        return await original_stop(job_id)

    monkeypatch.setattr(manager, "stop", blocked_stop)
    removal = asyncio.create_task(manager.remove_session("removed"))
    await entered.wait()
    with pytest.raises(ValueError, match="being deleted"):
        await manager.start("touch must-not-exist", tmp_path, session_id="removed")
    release.set()
    await removal
    assert manager.get(job["id"]) is None
    assert not manager.list(session_id="removed")
    assert not manager._tasks
    assert "removed" not in manager._removing_sessions
    assert not store.db.execute("SELECT id FROM jobs WHERE session_id='removed'").fetchall()
    assert not (tmp_path / "must-not-exist").exists()


def test_restart_marks_old_running_jobs_interrupted_without_launching(store, tmp_path):
    manager = JobManager(store, lambda text: text, None)
    now = time.time()
    job = {"id": "old-job", "session_id": None, "workspace": str(tmp_path), "command": "touch must-not-exist",
           "state": "running", "created": now, "updated": now, "exit_code": None, "output": "old output",
           "truncated": False, "timeout_seconds": 60, "max_output_bytes": 80000, "background": True}
    manager._persist(job)
    restarted = JobManager(store, lambda text: text, None)
    recovered = restarted.get("old-job")
    assert recovered["state"] == "interrupted"
    assert "outcome is unknown" in recovered["output"]
    assert "not restarted" in recovered["output"]
    assert not (tmp_path / "must-not-exist").exists()


async def test_retention_prunes_only_old_completed_jobs(store, tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.jobs.MAX_COMPLETED_JOBS", 2)
    manager = JobManager(store, lambda text: text, None)
    active = await manager.start("sleep 5", tmp_path)
    completed = []
    for _ in range(3):
        job = await manager.start("true", tmp_path)
        completed.append(await manager.wait(job["id"]))
    assert manager.get(completed[0]["id"]) is None
    assert manager.get(active["id"])["state"] == "running"
    assert len(manager.list()) == 3
    await manager.shutdown()
    assert len(manager.list()) == 2


async def test_operational_job_pruning_does_not_erase_conversation_history(store, tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.jobs.MAX_COMPLETED_JOBS", 2)
    session = {"id": "history", "title": "History", "created": 1, "updated": 1,
               "workspace": str(tmp_path), "model": "model", "events": [], "wire": []}
    store.save(session)
    manager = JobManager(store, lambda text: text, None)

    completed = []
    for index in range(3):
        job = await manager.start(f"printf {index}", tmp_path, session_id=session["id"], background=True)
        completed.append(await manager.wait(job["id"]))

    assert manager.get(completed[0]["id"]) is None
    history = store.get(session["id"])["command_jobs"]
    assert [job["id"] for job in history] == [job["id"] for job in completed]
    assert [job["output"] for job in history] == ["0", "1", "2"]


async def test_restart_does_not_rewrite_already_mirrored_terminal_jobs(tmp_path, monkeypatch):
    path = tmp_path / "restart.sqlite3"
    store = Store(path)
    session = {"id": "restart-chat", "title": "Restart", "created": 1, "updated": 1,
               "workspace": str(tmp_path), "model": "model", "events": [], "wire": []}
    store.save(session)
    manager = JobManager(store, lambda text: text, None)
    job = await manager.start("printf done", tmp_path, session_id=session["id"], background=True)
    await manager.wait(job["id"])
    store.db.close()

    reopened = Store(path)
    writes = []
    write = reopened._write

    def capture(*args, **kwargs):
        writes.append(args[0]["id"])
        return write(*args, **kwargs)

    monkeypatch.setattr(reopened, "_write", capture)
    JobManager(reopened, lambda text: text, None)

    assert writes == []
    assert reopened.get(session["id"])["command_jobs"][0]["state"] == "completed"
    reopened.db.close()
