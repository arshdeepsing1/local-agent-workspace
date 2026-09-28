import json
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.recovery import CheckpointManager
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


@pytest.fixture
def recovery(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "state.sqlite3")
    yield CheckpointManager(store), WorkspaceTools(str(workspace)), store
    store.db.close()


def first(manager, session_id=None):
    return manager.list(session_id=session_id)[0]["id"]


def restore(manager, tools, checkpoint_id, session_id=None):
    preview = manager.preview(checkpoint_id, tools, session_id=session_id)
    assert preview["can_restore"]
    return manager.restore(checkpoint_id, tools, preview["expected_current_hash"], session_id=session_id)


def test_edit_restore_exact_bytes_mode_and_reverse(recovery):
    manager, tools, _ = recovery
    target = tools.root / "script.py"
    target.write_bytes(b"first\r\nsecond\r\n")
    target.chmod(0o751)
    diff = manager.apply_edit(tools, "edit_file", {"path": "script.py", "old_text": "second", "new_text": "changed"})
    assert "changed" in diff and target.read_bytes() == b"first\nchanged\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o751
    checkpoint_id = first(manager)
    metadata = manager.list()[0]
    assert "content" not in json.dumps(metadata) and "before" not in metadata and "after" not in metadata
    result = restore(manager, tools, checkpoint_id)
    assert target.read_bytes() == b"first\r\nsecond\r\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o751
    assert not manager.preview(checkpoint_id, tools)["can_restore"]
    restore(manager, tools, result["reverse_checkpoint_id"])
    assert target.read_bytes() == b"first\nchanged\n"


@pytest.mark.parametrize("content", ["hello", ""])
def test_creation_restore_deletes_file_and_reverse_recreates(recovery, content):
    manager, tools, _ = recovery
    manager.apply_edit(tools, "write_file", {"path": "nested/new.txt", "content": content})
    target = tools.root / "nested" / "new.txt"
    assert target.read_text() == content
    result = restore(manager, tools, first(manager))
    assert not target.exists()
    restore(manager, tools, result["reverse_checkpoint_id"])
    assert target.read_text() == content


def test_noop_failed_validation_and_failed_write_have_no_checkpoints(recovery, monkeypatch):
    manager, tools, _ = recovery
    target = tools.root / "file"
    target.write_text("original")
    assert manager.apply_edit(tools, "write_file", {"path": "file", "content": "original"}) == "No changes."
    for arguments in ({"path": "file", "old_text": "missing", "new_text": "replacement"},
                      {"path": "file", "old_text": "", "new_text": "replacement"}):
        with pytest.raises(ValueError):
            manager.apply_edit(tools, "edit_file", arguments)
    with pytest.raises(ValueError):
        manager.apply_edit(tools, "write_file", {"path": "file", "content": "x" * 80001})
    with pytest.raises(ValueError):
        manager.apply_edit(tools, "write_file", {"path": "file", "content": "binary\x00"})

    def fail(*args):
        raise PermissionError("synthetic write denial")

    monkeypatch.setattr(manager, "_write", fail)
    with pytest.raises(PermissionError):
        manager.apply_edit(tools, "write_file", {"path": "file", "content": "different"})
    assert manager.list() == [] and target.read_text() == "original"


@pytest.mark.parametrize("change", ["content", "mode", "delete", "replace"])
def test_restore_refuses_postedit_drift(recovery, change):
    manager, tools, _ = recovery
    target = tools.root / "file"
    target.write_text("original")
    manager.apply_edit(tools, "write_file", {"path": "file", "content": "edited"})
    checkpoint_id = first(manager)
    preview = manager.preview(checkpoint_id, tools)
    if change == "content":
        target.write_text("outside edit")
    elif change == "mode":
        target.chmod(0o700)
    elif change == "delete":
        target.unlink()
    else:
        replacement = tools.root / "replacement"
        replacement.write_text("edited")
        replacement.chmod(stat.S_IMODE(target.stat().st_mode))
        os.replace(replacement, target)
    assert not manager.preview(checkpoint_id, tools)["can_restore"]
    with pytest.raises(ValueError, match="changed"):
        manager.restore(checkpoint_id, tools, preview["expected_current_hash"])


def test_restore_rechecks_after_preview_and_before_atomic_write(recovery, monkeypatch):
    manager, tools, _ = recovery
    target = tools.root / "file"
    target.write_text("original")
    manager.apply_edit(tools, "write_file", {"path": "file", "content": "edited"})
    checkpoint_id = first(manager)
    preview = manager.preview(checkpoint_id, tools)
    original_write = manager._write

    def concurrent_change(*arguments):
        target.write_text("concurrent external edit")
        return original_write(*arguments)

    monkeypatch.setattr(manager, "_write", concurrent_change)
    with pytest.raises(ValueError, match="changed"):
        manager.restore(checkpoint_id, tools, preview["expected_current_hash"])
    assert target.read_text() == "concurrent external edit"
    assert len(manager.list()) == 1


def test_checkpoint_is_persisted_before_write_and_survives_restart(recovery, monkeypatch):
    manager, tools, store = recovery
    original_write = manager._write

    def observe(*args):
        record = json.loads(store.db.execute("SELECT data FROM checkpoints").fetchone()[0])
        assert record["status"] == "pending" and record["before"]["exists"] is False
        return original_write(*args)

    monkeypatch.setattr(manager, "_write", observe)
    manager.apply_edit(tools, "write_file", {"path": "file", "content": "checkpointed"}, session_id="session-a")
    checkpoint_id = first(manager)
    restarted = CheckpointManager(store)
    restore(restarted, tools, checkpoint_id, session_id="session-a")
    assert not (tools.root / "file").exists()


def test_scope_and_revoked_external_grant_are_enforced(recovery, tmp_path):
    manager, tools, _ = recovery
    external = tmp_path / "external"
    external.mkdir()
    target = external / "file"
    target.write_text("original")
    granted = WorkspaceTools(str(tools.root), allowed_directories=[str(external)])
    manager.apply_edit(granted, "write_file", {"path": str(target), "content": "edited"}, session_id="session-a")
    checkpoint_id = first(manager)
    with pytest.raises(ValueError, match="not found"):
        manager.preview(checkpoint_id, granted, session_id="session-b")
    with pytest.raises(ValueError, match="not found"):
        manager.preview(checkpoint_id, WorkspaceTools(str(external)), session_id="session-a")
    with pytest.raises(ValueError, match="approval"):
        manager.preview(checkpoint_id, tools, session_id="session-a")
    restore(manager, granted, checkpoint_id, session_id="session-a")
    assert target.read_text() == "original"


def test_secret_aliases_and_retargeted_symlinks_are_never_restored(recovery):
    manager, tools, _ = recovery
    credential = tools.root / "gateway.conf"
    credential.write_text("synthetic credential")
    (tools.root / "alias.txt").hardlink_to(credential)
    guarded = WorkspaceTools(str(tools.root), credential_file=str(credential), unrestricted=True)
    for path in ("gateway.conf", "alias.txt", ".env"):
        with pytest.raises(ValueError, match="excluded"):
            manager.apply_edit(guarded, "write_file", {"path": path, "content": "changed"})
    target = tools.root / "safe"
    target.write_text("original")
    manager.apply_edit(guarded, "write_file", {"path": "safe", "content": "edited"})
    checkpoint_id = first(manager)
    target.unlink()
    target.symlink_to(credential)
    with pytest.raises(ValueError, match="excluded"):
        manager.preview(checkpoint_id, guarded)
    assert credential.read_text() == "synthetic credential"


def test_parent_symlink_swap_cannot_write_external_file(recovery, tmp_path, monkeypatch):
    manager, tools, _ = recovery
    folder = tools.root / "nested"
    folder.mkdir()
    (folder / "file").write_text("inside")
    external = tmp_path / "external"
    external.mkdir()
    (external / "file").write_text("outside")
    original_write = manager._write

    def swap(*args):
        folder.rename(tools.root / "saved")
        folder.symlink_to(external, target_is_directory=True)
        return original_write(*args)

    monkeypatch.setattr(manager, "_write", swap)
    with pytest.raises((ValueError, OSError)):
        manager.apply_edit(tools, "write_file", {"path": "nested/file", "content": "edited"})
    assert (external / "file").read_text() == "outside"
    assert (tools.root / "saved" / "file").read_text() == "inside"
    assert manager.list() == []


@pytest.mark.parametrize("raw", [b"x" * 80001, b"binary\x00", b"\xff"])
def test_unsupported_original_files_have_no_checkpoint(recovery, raw):
    manager, tools, _ = recovery
    (tools.root / "file").write_bytes(raw)
    with pytest.raises((ValueError, UnicodeError)):
        manager.apply_edit(tools, "write_file", {"path": "file", "content": "replacement"})
    assert manager.list() == []


def test_delete_session_removes_only_its_recovery_copies(recovery):
    manager, tools, store = recovery
    for session_id in ("first", "second", None):
        manager.apply_edit(tools, "write_file", {"path": str(session_id), "content": "private original"}, session_id=session_id)
    manager.delete_session("first")
    assert {item["session_id"] for item in manager.list()} == {"second", None}
    assert store.db.execute("SELECT COUNT(*) FROM checkpoints WHERE session_id='first'").fetchone()[0] == 0
    assert (tools.root / "first").read_text() == "private original"


def test_delete_session_api_removes_checkpoint_rows(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://fake.example", "synthetic-test-token")
    app = create_app(settings)
    manager = app.state.manager
    (tmp_path / "file").write_text("original")
    with TestClient(app) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        session_id = client.post("/api/sessions", headers=headers).json()["id"]
        saved = client.put("/api/file", headers=headers, json={
            "path": "file", "original": "original", "content": "edited", "session_id": session_id})
        assert saved.status_code == 200
        assert len(manager.checkpoints.list(session_id)) == 1
        assert client.delete(f"/api/sessions/{session_id}", headers=headers).status_code == 200
        assert manager.store.db.execute("SELECT COUNT(*) FROM checkpoints WHERE session_id=?", (session_id,)).fetchone()[0] == 0
        assert (tmp_path / "file").read_text() == "edited"


def test_turn_preview_groups_edits_but_preserves_individual_drift_checks(recovery):
    manager, tools, store = recovery
    for path in ("first.py", "second.py"):
        (tools.root / path).write_text("before")
        manager.apply_edit(tools, "write_file", {"path": path, "content": "after"}, "session", turn_id="turn-1")
    manager.apply_edit(tools, "write_file", {"path": "other.py", "content": "other turn"}, "session", turn_id="turn-2")
    manager.apply_edit(tools, "write_file", {"path": "manual.py", "content": "manual edit"}, "session")
    (tools.root / "second.py").write_text("external change")
    restarted = CheckpointManager(store)
    group = restarted.preview_turn("turn-1", tools, "session")
    previews = {item["path"]: item for item in group["previews"]}
    assert set(previews) == {"first.py", "second.py"}
    assert group["next_offset"] is None
    assert previews["first.py"]["can_restore"] is True
    assert previews["second.py"]["can_restore"] is False
    assert "external change" in previews["second.py"]["diff"]
    assert (tools.root / "first.py").read_text() == "after", "Preview never writes files."
    with pytest.raises(ValueError, match="No checkpoints"):
        restarted.preview_turn("turn-1", tools, "unrelated-session")
    with pytest.raises(ValueError, match="No checkpoints"):
        restarted.preview_turn("turn-1", WorkspaceTools(str(tools.root.parent)), "session")
    legacy = next(item for item in restarted.list() if item["path"] == "manual.py")
    assert legacy["turn_id"] is None
    restored = restarted.restore(previews["first.py"]["id"], tools, previews["first.py"]["expected_current_hash"], "session")
    assert restarted._metadata(restarted._get(restored["reverse_checkpoint_id"], tools, "session")[0])["turn_id"] is None


def test_turn_preview_is_bounded_and_revoked_access_does_not_reveal_diff(recovery, tmp_path):
    manager, tools, _ = recovery
    for index in range(11):
        manager.apply_edit(tools, "write_file", {"path": f"file{index}", "content": "new"}, "session", turn_id="turn")
    page = manager.preview_turn("turn", tools, "session")
    assert len(page["previews"]) == 10 and page["next_offset"] == 10
    last = manager.preview_turn("turn", tools, "session", page["next_offset"])
    assert len(last["previews"]) == 1 and last["next_offset"] is None
    assert len({item["id"] for item in page["previews"] + last["previews"]}) == 11
    for offset in (-1, 11, True):
        with pytest.raises(ValueError, match="offset"):
            manager.preview_turn("turn", tools, "session", offset)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "file"
    target.write_text("original private content")
    granted = WorkspaceTools(str(tools.root), allowed_directories=[str(outside)])
    manager.apply_edit(granted, "write_file", {"path": str(target), "content": "updated private content"}, "session", turn_id="external")
    preview = manager.preview_turn("external", tools, "session")["previews"][0]
    assert preview["can_restore"] is False and preview["diff"] == ""
    assert "private content" not in json.dumps(preview)


def test_turn_preview_api_requires_scoped_session_and_does_not_mutate(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(tmp_path), env_file="")
    app = create_app(settings)
    manager = app.state.manager
    with TestClient(app) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        session_id = client.post("/api/sessions", headers=headers).json()["id"]
        other_id = client.post("/api/sessions", headers=headers).json()["id"]
        manager.checkpoints.apply_edit(WorkspaceTools(str(tmp_path)), "write_file", {"path": "file", "content": "new"}, session_id, turn_id="turn")
        response = client.get(f"/api/checkpoint-turns/turn/preview?session_id={session_id}", headers=headers)
        assert response.status_code == 200
        assert response.json()["previews"][0]["can_restore"] is True
        assert client.get(f"/api/checkpoint-turns/turn/preview?session_id={other_id}", headers=headers).status_code == 400
        assert client.get("/api/checkpoint-turns/turn/preview", headers=headers).status_code == 422
        assert (tmp_path / "file").read_text() == "new"
