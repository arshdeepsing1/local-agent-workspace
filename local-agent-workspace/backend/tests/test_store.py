import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from local_agent.store import Store
from local_agent.telemetry import LEDGER_VERSION


def prepared(session_id="example"):
    return {"id": session_id, "title": "Example", "created": 10, "updated": 20,
            "workspace": "/project", "model": "model", "events": [], "wire": []}


def table_names(store):
    return {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_create_initializes_a_complete_empty_inference_ledger(tmp_path):
    store = Store(tmp_path / "sessions.db")
    session = store.create({"workspace": "/project", "model": "model"})

    assert session["inference_ledger_version"] == LEDGER_VERSION
    assert session["inference_calls"] == []
    assert store.get(session["id"])["inference_calls"] == []
    store.db.close()


def test_jsonl_contains_the_complete_conversation_and_sqlite_contains_no_chat_tables(tmp_path):
    store = Store(tmp_path / "sessions.db")
    session = {**prepared(),
               "events": [
                   {"id": "user", "type": "user", "text": "Inspect it"},
                   {"id": "tool", "type": "tool", "name": "run_command",
                    "input": {"command": "pwd"}, "output": "/project"},
                   {"id": "assistant", "type": "assistant", "text": "Done",
                    "request_info": {"usage": {"input_tokens": 5, "output_tokens": 2}}},
               ],
               "wire": [{"role": "user", "content": "Inspect it"},
                        {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
                        {"role": "tool", "tool_call_id": "call-1", "content": "/project"}],
               "context_state": {"summary": "private context", "through": 2},
               "instruction_directories": ["src"],
               "inference_calls": [{"id": "request-1", "purpose": "agent", "status": "completed"}],
               "command_jobs": [{"id": "job-1", "session_id": "example", "command": "pwd",
                                  "state": "completed", "created": 12, "updated": 13,
                                  "exit_code": 0, "output": "/project\n", "truncated": False}],
               "custom_flag": {"preserved": True}}
    store.save(session)

    path = tmp_path / "conversations" / "example.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["record"] == "session"
    assert "context_state" not in records[0]["data"]
    assert "inference_calls" not in records[0]["data"]
    context_record = next(record for record in records if record.get("name") == "context_state")
    assert context_record["data"] == session["context_state"]
    assert [record["record"] for record in records].count("event") == 3
    assert [record["record"] for record in records].count("wire") == 3
    assert [record["record"] for record in records].count("inference_call") == 1
    assert [record["record"] for record in records].count("command_job") == 1
    assert store.get("example") == session
    assert "sessions" not in table_names(store)
    assert "session_summaries" not in table_names(store)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    store.db.close()


def test_list_reads_only_the_small_first_record(tmp_path):
    store = Store(tmp_path / "sessions.db")
    session = {**prepared(), "events": [{"text": "x" * 2_000_000}],
               "wire": [{"content": "y" * 2_000_000}],
               "context_state": {"summary": "private context"},
               "instruction_directories": ["src"],
               "inference_calls": [{"detail": "z" * 2_000_000}],
               "tool_profile": "read_only"}
    store.save(session)
    path = tmp_path / "conversations" / "example.jsonl"
    with path.open() as source:
        assert len(source.readline()) < 1000
    with path.open("a") as output:
        output.write("invalid trailing record\n")

    assert store.list() == [Store.summary(session)]
    assert len(json.dumps(store.list())) < 1000
    with pytest.raises(json.JSONDecodeError):
        store.get("example")
    store.db.close()


def test_repeated_session_saves_reuse_cached_command_history(tmp_path, monkeypatch):
    store = Store(tmp_path / "sessions.db")
    session = prepared()
    store.save(session)
    reads = 0
    read_lines = store._read_lines

    def capture(*args, **kwargs):
        nonlocal reads
        reads += 1
        return read_lines(*args, **kwargs)

    monkeypatch.setattr(store, "_read_lines", capture)
    session["title"] = "First edit"
    store.save(session)
    session["title"] = "Second edit"
    store.save(session)

    assert reads == 0
    store.db.close()


def test_equal_timestamp_terminal_job_replaces_running_snapshot(tmp_path):
    store = Store(tmp_path / "sessions.db")
    running = {"id": "job", "session_id": "example", "command": "true", "state": "running",
               "created": 10, "updated": 10, "exit_code": None, "output": "", "truncated": False}
    session = {**prepared(), "command_jobs": [running]}
    store.save(session)

    completed = {**running, "state": "completed", "exit_code": 0}
    store.save_job(completed)

    assert store.get(session["id"])["command_jobs"] == [completed]
    store.db.close()


def test_old_database_migrates_once_then_drops_chat_tables(tmp_path):
    path = tmp_path / "sessions.db"
    marker = "legacy-private-transcript-marker"
    session = {**prepared(), "events": [{"text": marker}],
               "wire": [{"role": "assistant", "content": "complete"}],
               "context_state": {"through": 1}, "permission_mode": "manual",
               "terminal_reason": "interrupted"}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, updated REAL, data TEXT)")
        db.execute("CREATE TABLE session_summaries (id TEXT PRIMARY KEY, updated REAL, data TEXT)")
        db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                   (session["id"], session["updated"], json.dumps(session)))
        db.execute("INSERT INTO session_summaries VALUES (?, ?, ?)",
                   (session["id"], session["updated"], json.dumps(Store.summary(session))))

    store = Store(path)
    assert store.get(session["id"]) == session
    assert store.list() == [Store.summary(session)]
    assert "sessions" not in table_names(store)
    assert "session_summaries" not in table_names(store)
    assert store.db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    store.db.close()
    for database_file in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if database_file.exists():
            assert marker.encode() not in database_file.read_bytes()

    reopened = Store(path)
    assert reopened.get(session["id"]) == session
    assert reopened.list() == [Store.summary(session)]
    reopened.db.close()


def test_failed_legacy_migration_keeps_source_tables_for_retry(tmp_path, monkeypatch):
    path = tmp_path / "sessions.db"
    sessions = [prepared("first"), prepared("second")]
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, updated REAL, data TEXT)")
        for session in sessions:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                       (session["id"], session["updated"], json.dumps(session)))

    original = Store._write

    def fail_second(self, session, **options):
        if session["id"] == "second":
            raise OSError("disk unavailable")
        return original(self, session, **options)

    monkeypatch.setattr(Store, "_write", fail_second)
    with pytest.raises(OSError, match="disk unavailable"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2

    monkeypatch.setattr(Store, "_write", original)
    store = Store(path)
    assert {item["id"] for item in store.list()} == {"first", "second"}
    assert "sessions" not in table_names(store)
    store.db.close()


def test_failed_save_keeps_previous_file_and_timestamp(tmp_path, monkeypatch):
    store = Store(tmp_path / "sessions.db")
    session = prepared()
    store.save(session)
    previous = store.get(session["id"])

    def unavailable(source, destination):
        raise OSError("publish unavailable")

    monkeypatch.setattr("local_agent.store.os.replace", unavailable)
    session["title"] = "New title"
    with pytest.raises(OSError, match="publish unavailable"):
        store.save(session)
    assert session["updated"] == previous["updated"]
    assert store.get(session["id"]) == previous
    assert list((tmp_path / "conversations").glob("*.tmp")) == []
    store.db.close()


def test_delete_removes_file_and_survives_restart(tmp_path):
    path = tmp_path / "sessions.db"
    store = Store(path)
    session = prepared()
    store.save(session)
    store.delete(session["id"])
    assert store.get(session["id"]) is None and store.list() == []
    store.db.close()

    reopened = Store(path)
    assert reopened.get(session["id"]) is None and reopened.list() == []
    reopened.db.close()


def test_insert_sessions_is_atomic_and_never_overwrites_existing_ids(tmp_path):
    store = Store(tmp_path / "sessions.db")
    original = prepared("existing")
    store.save(original)
    collision = {**prepared("existing"), "title": "Must not replace"}
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_sessions([prepared("new"), collision])
    assert store.get("new") is None
    assert store.get("existing") == original
    assert store.list() == [Store.summary(original)]

    new = [prepared("first"), {**prepared("second"), "updated": 30, "archive_only": True}]
    store.insert_sessions(new)
    assert store.get("first") == new[0]
    assert store.get("second") == new[1]
    assert len(store.list()) == 3
    assert next(item for item in store.list() if item["id"] == "second")["archive_only"] is True
    store.db.close()


def test_insert_sessions_removes_the_group_if_publication_fails(tmp_path, monkeypatch):
    store = Store(tmp_path / "sessions.db")
    original_link = os.link
    calls = 0

    def fail_second(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk unavailable")
        return original_link(source, destination)

    monkeypatch.setattr("local_agent.store.os.link", fail_second)
    with pytest.raises(OSError, match="disk unavailable"):
        store.insert_sessions([prepared("first"), prepared("second")])
    assert store.get("first") is None and store.get("second") is None
    assert store.list() == []
    assert list((tmp_path / "conversations").iterdir()) == []
    store.db.close()


@pytest.mark.parametrize("session_id", ["../escape", "nested/name", ".hidden", "x" * 129])
def test_unsafe_ids_cannot_escape_the_conversation_directory(tmp_path, session_id):
    store = Store(tmp_path / "sessions.db")
    with pytest.raises(ValueError, match="not safe"):
        store.save(prepared(session_id))
    assert store.get(session_id) is None
    assert not (tmp_path / "escape.jsonl").exists()
    store.db.close()


def test_conversation_directory_cannot_redirect_through_a_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "conversations").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        Store(tmp_path / "sessions.db")
    assert list(outside.iterdir()) == []
