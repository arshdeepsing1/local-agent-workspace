"""file_views, allow_rules, queued_messages, pending_context and instruction_paths are field records of the
conversation file. Versions up to 2026-10-20 kept them in <id>.state.json: such a chat still opens,
and its next save folds the file in."""
import importlib
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from local_agent.store import Store

SIDECAR_VERSION = "8fa5f46"  # main on 2026-10-20, the last version that wrote <id>.state.json.
VIEWS = {"/project/README.md": [12, 34]}
RULES = [{"tool": "run_command", "command": "make test"}]
QUEUE = [{"id": "q1", "text": "Then run the tests"}]


def prepared(session_id="example"):
    return {"id": session_id, "title": "Example", "created": 10, "updated": 20,
            "workspace": "/project", "model": "model", "events": [], "wire": []}


def store_at(tmp_path, commit):
    """The Store class of an earlier version, loaded from Git into its own package."""
    root = Path(__file__).resolve().parents[3]
    package = tmp_path / f"store_{commit}" / f"local_agent_{commit}"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    for name in ("store.py", "telemetry.py"):
        shown = subprocess.run(["git", "-C", str(root), "show", f"{commit}:local-agent-workspace/backend/local_agent/{name}"],
                               capture_output=True, text=True)
        if shown.returncode:
            pytest.skip(f"commit {commit} is not available in this checkout")
        (package / name).write_text(shown.stdout)
    sys.path.insert(0, str(package.parent))
    try:
        return importlib.import_module(f"local_agent_{commit}.store").Store
    finally:
        sys.path.remove(str(package.parent))


def folder(tmp_path):
    return tmp_path / "conversations"


def test_a_chat_saved_with_a_state_file_opens_and_its_next_save_folds_it_in(tmp_path):
    OldStore = store_at(tmp_path, SIDECAR_VERSION)
    old = OldStore(tmp_path / "sessions.db")
    old.save({**prepared(), "file_views": VIEWS, "allow_rules": RULES, "queued_messages": QUEUE,
              "context_state": {"summary": "s", "through": 0}})
    old.db.close()
    sidecar = folder(tmp_path) / "example.state.json"
    assert sidecar.exists() and "make test" not in (folder(tmp_path) / "example.jsonl").read_text()

    store = Store(tmp_path / "sessions.db")
    assert [item["id"] for item in store.list()] == ["example"]
    session = store.get("example")
    assert session["file_views"] == VIEWS and session["allow_rules"] == RULES and session["queued_messages"] == QUEUE
    assert session["context_state"] == {"summary": "s", "through": 0}
    assert sidecar.exists()  # Only a save changes the files.
    session["queued_messages"] = []
    store.save(session)
    assert not sidecar.exists()
    lines = [json.loads(line) for line in (folder(tmp_path) / "example.jsonl").read_text().splitlines()]
    fields = {line["name"]: line["data"] for line in lines if line["record"] == "field"}
    assert fields["file_views"] == VIEWS and fields["allow_rules"] == RULES and fields["queued_messages"] == []
    store.db.close()
    again = Store(tmp_path / "sessions.db").get("example")
    assert again["allow_rules"] == RULES and again["queued_messages"] == [] and again["file_views"] == VIEWS


def test_the_fields_are_kept_in_the_private_conversation_file_and_left_out_of_headers(tmp_path):
    store = Store(tmp_path / "sessions.db")
    store.save({**prepared(), "file_views": VIEWS, "allow_rules": RULES})
    assert sorted(path.name for path in folder(tmp_path).iterdir()) == ["example.jsonl"]
    path = folder(tmp_path) / "example.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and "make test" in path.read_text()
    header = json.loads(path.read_text().splitlines()[0])["data"]
    assert "file_views" not in header and "allow_rules" not in header
    assert "file_views" not in store.list()[0] and "allow_rules" not in store.header("example")
    loaded = Store(tmp_path / "sessions.db").get("example")
    assert loaded["file_views"] == VIEWS and loaded["allow_rules"] == RULES
    assert list(folder(tmp_path).glob("*.tmp")) == []
    store.db.close()


def test_a_session_saved_without_loading_its_state_file_keeps_its_fields(tmp_path):
    # A save of a conversation object that get() did not load (so it lacks the state file's fields) still keeps them.
    store = Store(tmp_path / "sessions.db")
    store.save(prepared())
    (folder(tmp_path) / "example.state.json").write_text(json.dumps({"allow_rules": RULES, "file_views": VIEWS}))
    store.save({**prepared(), "file_views": {}})  # Its own value wins; the missing one comes from the file.
    assert not (folder(tmp_path) / "example.state.json").exists()
    loaded = Store(tmp_path / "sessions.db").get("example")
    assert loaded["allow_rules"] == RULES and loaded["file_views"] == {}
    store.db.close()


@pytest.mark.parametrize("newer", [None, [{"tool": "run_command", "command": "make lint"}]])
def test_a_command_job_folds_in_the_state_file(tmp_path, newer):
    """A job mirrored into a chat (before any save) keeps its rules: a state file is newer than field records."""
    store = Store(tmp_path / "sessions.db")
    store.save({**prepared(), "file_views": VIEWS, "allow_rules": RULES})
    store.db.close()
    sidecar = folder(tmp_path) / "example.state.json"
    if newer:
        sidecar.write_text(json.dumps({"allow_rules": newer}))
    store = Store(tmp_path / "sessions.db")
    store.save_job({"id": "job", "session_id": "example", "created": 30, "updated": 30, "state": "interrupted"})
    assert not sidecar.exists()
    reopened = Store(tmp_path / "sessions.db")
    session = reopened.get("example")
    reopened.db.close()
    assert session["command_jobs"][0]["state"] == "interrupted"
    assert session["file_views"] == VIEWS and session["allow_rules"] == (newer or RULES)
    store.db.close()


def test_delete_removes_an_older_state_file_too(tmp_path):
    store = Store(tmp_path / "sessions.db")
    store.save({**prepared(), "allow_rules": RULES})
    (folder(tmp_path) / "example.state.json").write_text(json.dumps({"allow_rules": RULES}))
    store.delete("example")
    assert list(folder(tmp_path).iterdir()) == []
    store.db.close()


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", "\udcff"])
def test_a_corrupt_state_file_is_ignored_and_removed_by_the_next_save(tmp_path, content):
    store = Store(tmp_path / "sessions.db")
    store.save({**prepared(), "allow_rules": RULES})
    sidecar = folder(tmp_path) / "example.state.json"
    sidecar.write_bytes(content.encode("utf-8", errors="surrogateescape"))
    reopened = Store(tmp_path / "sessions.db")
    session = reopened.get("example")
    assert session["allow_rules"] == RULES and session["title"] == "Example"  # The conversation file's own.
    assert reopened.list()[0]["id"] == "example" and reopened.problems() == []
    reopened.save(session)
    assert not sidecar.exists()
    assert Store(tmp_path / "sessions.db").get("example")["allow_rules"] == RULES
    reopened.db.close()
    store.db.close()
