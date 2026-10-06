"""file_views and allow_rules live in <id>.state.json, so earlier versions can still read the chat."""
import importlib
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from local_agent.store import Store

PRE_M1 = "88eaad4"
VIEWS = {"/project/README.md": [12, 34]}
RULES = [{"tool": "run_command", "command": "make test"}]


def prepared(session_id="example"):
    return {"id": session_id, "title": "Example", "created": 10, "updated": 20,
            "workspace": "/project", "model": "model", "events": [], "wire": []}


def pre_m1_store(tmp_path):
    """The Store class of the version before M1, loaded from Git into its own package."""
    root = Path(__file__).resolve().parents[3]
    package = tmp_path / "pre_m1_pkg" / "old_local_agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    for name in ("store.py", "telemetry.py"):
        shown = subprocess.run(["git", "-C", str(root), "show",
                                f"{PRE_M1}:local-agent-workspace/backend/local_agent/{name}"],
                               capture_output=True, text=True)
        if shown.returncode:
            pytest.skip(f"commit {PRE_M1} is not available in this checkout")
        (package / name).write_text(shown.stdout)
    sys.path.insert(0, str(package.parent))
    try:
        return importlib.import_module("old_local_agent.store").Store
    finally:
        sys.path.remove(str(package.parent))


def test_the_version_before_m1_still_reads_a_conversation_with_file_views_and_rules(tmp_path):
    state = tmp_path / "state"
    store = Store(state / "sessions.db")
    session = {**prepared(), "file_views": VIEWS, "allow_rules": RULES,
               "context_state": {"summary": "s", "through": 0}, "turn_running": False}
    store.save(session)
    store.db.close()

    OldStore = pre_m1_store(tmp_path)
    old = OldStore(state / "sessions.db")
    assert [item["id"] for item in old.list()] == ["example"]
    loaded = old.get("example")  # Raised "invalid record" before the sidecar.
    assert loaded["context_state"] == {"summary": "s", "through": 0}
    old.save(loaded)  # The old version writes the conversation back without the sidecar fields.
    old.db.close()

    reopened = Store(state / "sessions.db")
    again = reopened.get("example")
    assert again["file_views"] == VIEWS and again["allow_rules"] == RULES
    reopened.db.close()


def test_sidecar_round_trip_is_private_and_only_written_on_change(tmp_path, monkeypatch):
    store = Store(tmp_path / "sessions.db")
    session = {**prepared(), "file_views": VIEWS, "allow_rules": RULES}
    store.save(session)
    sidecar = tmp_path / "conversations" / "example.state.json"
    assert json.loads(sidecar.read_text()) == {"file_views": VIEWS, "allow_rules": RULES}
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    lines = (tmp_path / "conversations" / "example.jsonl").read_text()
    assert "file_views" not in lines and "allow_rules" not in lines and "make test" not in lines
    assert "file_views" not in store.list()[0]

    writes = []
    original = Store._write_sidecar
    monkeypatch.setattr(Store, "_write_sidecar", lambda self, *args: (writes.append(args), original(self, *args)))
    store.save(session)
    assert writes == []  # Unchanged fields are not rewritten.
    session["allow_rules"] = []
    store.save(session)
    assert len(writes) == 1
    monkeypatch.undo()

    reopened = Store(tmp_path / "sessions.db")
    loaded = reopened.get("example")
    assert loaded["file_views"] == VIEWS and loaded["allow_rules"] == []
    assert list((tmp_path / "conversations").glob("*.tmp")) == []
    reopened.db.close()
    store.db.close()


def test_legacy_field_records_are_still_read_and_moved_on_save(tmp_path):
    store = Store(tmp_path / "sessions.db")
    store.save(prepared())
    path = tmp_path / "conversations" / "example.jsonl"
    with path.open("a") as output:  # As this version wrote them before the sidecar.
        output.write(json.dumps({"record": "field", "name": "file_views", "data": VIEWS}) + "\n")
        output.write(json.dumps({"record": "field", "name": "allow_rules", "data": RULES}) + "\n")
    session = store.get("example")
    assert session["file_views"] == VIEWS and session["allow_rules"] == RULES
    store.save(session)
    assert "allow_rules" not in path.read_text()
    assert json.loads((tmp_path / "conversations" / "example.state.json").read_text())["allow_rules"] == RULES
    store.db.close()


def test_delete_removes_the_sidecar(tmp_path):
    store = Store(tmp_path / "sessions.db")
    store.save({**prepared(), "allow_rules": RULES})
    assert (tmp_path / "conversations" / "example.state.json").exists()
    store.delete("example")
    assert list((tmp_path / "conversations").iterdir()) == []
    store.db.close()


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", "\udcff"])
def test_a_missing_or_corrupt_sidecar_is_treated_as_empty(tmp_path, content):
    store = Store(tmp_path / "sessions.db")
    store.save({**prepared(), "allow_rules": RULES})
    sidecar = tmp_path / "conversations" / "example.state.json"
    sidecar.write_bytes(content.encode("utf-8", errors="surrogateescape"))
    reopened = Store(tmp_path / "sessions.db")
    session = reopened.get("example")
    assert "allow_rules" not in session and session["title"] == "Example"
    assert reopened.list()[0]["id"] == "example" and reopened.problems() == []
    session["allow_rules"] = RULES
    reopened.save(session)  # The next save repairs it.
    assert json.loads(sidecar.read_text()) == {"allow_rules": RULES}
    sidecar.unlink()
    third = Store(tmp_path / "sessions.db")
    assert "allow_rules" not in third.get("example")
    third.db.close()
    reopened.db.close()
    store.db.close()
